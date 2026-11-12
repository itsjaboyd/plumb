"""Coordinate parsing and location resolution.

Parsing is pure and tested; resolution talks to the database and caches
within a run, so a hundred visits to one store cost one lookup.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation

import psycopg

from .normalize import RowError

# "39.7392, -104.9903" / "39.7392;-104.9903" / "39.7392 -104.9903"
PAIR = re.compile(
    r"^\s*(?P<a>[-+]?\d+(?:\.\d+)?\s*[NSns]?)\s*[,;/|]\s*"
    r"(?P<b>[-+]?\d+(?:\.\d+)?\s*[EWew]?)\s*$"
)
HEMISPHERE = re.compile(r"^\s*(?P<num>[-+]?\d+(?:\.\d+)?)\s*(?P<hemi>[NSEWnsew])?\s*$")

#: Spelled-out country names seen in exports, mapped to ISO-3166 alpha-2.
#: address_key includes the country, so "United States" and "US" would
#: otherwise be two different places at the same address.
COUNTRY_ALIASES = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "united states": "US", "united states of america": "US",
    "ca": "CA", "canada": "CA",
    "mx": "MX", "mexico": "MX",
    "gb": "GB", "uk": "GB", "u.k.": "GB", "united kingdom": "GB",
    "great britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "ie": "IE", "ireland": "IE",
    "de": "DE", "germany": "DE", "deutschland": "DE",
    "fr": "FR", "france": "FR",
    "es": "ES", "spain": "ES",
    "it": "IT", "italy": "IT",
    "nl": "NL", "netherlands": "NL",
    "au": "AU", "australia": "AU",
    "nz": "NZ", "new zealand": "NZ",
    "jp": "JP", "japan": "JP",
    "cn": "CN", "china": "CN",
    "in": "IN", "india": "IN",
    "br": "BR", "brazil": "BR",
}


def normalize_country(value: str | None) -> str | None:
    """'United States' -> 'US'. Unknown names are kept as written."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    hit = COUNTRY_ALIASES.get(text.lower())
    if hit:
        return hit
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return text


@dataclass
class LocationFields:
    #: A reading within this many metres of a known place is that place.
    match_radius_m: int = 25
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    label: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None
    #: Score from a location API. A property of the place, so it lives on
    #: the location row rather than on every transaction there.
    popularity: Decimal | None = None

    @property
    def has_coords(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def has_address(self) -> bool:
        return any([self.address, self.city, self.zip])

    def is_empty(self) -> bool:
        return not (self.has_coords or self.has_address)

    def key(self) -> tuple:
        return tuple(
            getattr(self, f.name)
            for f in fields(self)
            if f.name not in ("match_radius_m", "popularity")
        )


def parse_coordinate(value: str | None, precision: int = 5) -> Decimal | None:
    """One coordinate. Accepts '-104.9903' and '104.9903 W'."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "n/a", "N/A", "null", "NULL"}:
        return None

    match = HEMISPHERE.match(text)
    if not match:
        raise RowError(f"cannot parse coordinate {value!r}")
    try:
        number = Decimal(match.group("num"))
    except InvalidOperation as exc:
        raise RowError(f"cannot parse coordinate {value!r}") from exc

    hemi = (match.group("hemi") or "").upper()
    if hemi in ("S", "W"):
        number = -abs(number)
    elif hemi in ("N", "E"):
        number = abs(number)

    return number.quantize(Decimal(1).scaleb(-precision))


def parse_coordinate_pair(
    value: str | None, precision: int = 5
) -> tuple[Decimal, Decimal] | None:
    """A single column holding both, e.g. '39.7392, -104.9903'."""
    if value is None or not str(value).strip():
        return None
    match = PAIR.match(str(value))
    if not match:
        raise RowError(f"cannot parse coordinate pair {value!r}")
    lat = parse_coordinate(match.group("a"), precision)
    lon = parse_coordinate(match.group("b"), precision)
    if lat is None or lon is None:
        raise RowError(f"cannot parse coordinate pair {value!r}")
    return lat, lon


def validate(lat: Decimal | None, lon: Decimal | None) -> None:
    """Range-check, and catch the classic swapped pair."""
    if lat is None and lon is None:
        return
    if (lat is None) != (lon is None):
        raise RowError("latitude and longitude must both be present")
    if not (-90 <= lat <= 90):
        if -90 <= lon <= 90 and abs(lat) <= 180:
            raise RowError(
                f"latitude {lat} is out of range but longitude {lon} is valid - "
                f"the columns look swapped (set location.swap_lat_lon: true)"
            )
        raise RowError(f"latitude {lat} out of range -90..90")
    if not (-180 <= lon <= 180):
        raise RowError(f"longitude {lon} out of range -180..180")


# ----------------------------------------------------------------- geometry
EARTH_RADIUS_M = 6_371_000.0

#: Derived from the same radius the distance function uses. Taking the
#: textbook 111_320 instead makes the box ~0.1% too small, which can drop
#: a candidate sitting exactly on the radius.
METRES_PER_DEGREE_LAT = math.pi * EARTH_RADIUS_M / 180.0

#: The box only pre-filters; the exact distance decides. A little slack
#: costs nothing and keeps rounding from excluding a real match.
BOX_MARGIN = 1.01


def bounding_box(
    lat: float, lon: float, radius_m: float
) -> tuple[float, float, float, float]:
    """A lat/lon box that encloses the circle, for the index to use.

    Longitude degrees shrink with the cosine of the latitude and collapse
    at the poles, hence the clamp.
    """
    dlat = radius_m * BOX_MARGIN / METRES_PER_DEGREE_LAT
    shrink = max(math.cos(math.radians(lat)), 1e-6)
    dlon = radius_m * BOX_MARGIN / (METRES_PER_DEGREE_LAT * shrink)
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


# Candidates inside the bounding box, nearest first. The exact distance is
# computed in Python so the index can do the coarse filtering.
SELECT_NEARBY = """
SELECT id, latitude, longitude, label
FROM locations
WHERE latitude  BETWEEN %s AND %s
  AND longitude BETWEEN %s AND %s
"""

# ------------------------------------------------------------------ database
SELECT_BY_COORD = """
SELECT id FROM locations
WHERE coord_key = round(%s::numeric, 5)::text || ',' || round(%s::numeric, 5)::text
"""

SELECT_BY_ADDRESS = """
SELECT id FROM locations
WHERE address_key = lower(
    coalesce(%s, '') || '|' || coalesce(%s, '') || '|' || coalesce(%s, '') || '|' ||
    coalesce(%s, '') || '|' || %s)
"""

# Mirrors uq_locations_coord_place: the point plus the normalised name.
SELECT_BY_COORD_AND_LABEL = """
SELECT id FROM locations
WHERE coord_key = round(%s::numeric, 5)::text || ',' || round(%s::numeric, 5)::text
  AND lower(btrim(coalesce(label, ''))) = lower(btrim(coalesce(%s, '')))
"""

INSERT_LOCATION = """
INSERT INTO locations (latitude, longitude, label, address, city, state, zip,
                       country, source, popularity)
VALUES (%(latitude)s, %(longitude)s, %(label)s, %(address)s, %(city)s, %(state)s,
        %(zip)s, %(country)s, %(source)s, %(popularity)s)
RETURNING id
"""


def _same_place(a: str | None, b: str | None) -> bool | None:
    """Do two place names refer to the same spot? None when unknowable."""
    if not a or not b:
        return None                      # one side is unnamed; can't say
    return a.strip().lower() == b.strip().lower()


def _find_nearby(conn: psycopg.Connection, loc: LocationFields) -> dict | None:
    """The same storefront, read slightly differently, or None.

    Proximity alone is not identity: shops on one block sit within any
    radius wide enough to absorb GPS jitter. So when both the incoming row
    and the candidate are named, the names have to agree. Unnamed
    candidates fall back to distance, which is the best available.
    """
    lat, lon = float(loc.latitude), float(loc.longitude)

    if loc.match_radius_m <= 0:      # exact-coordinate mode
        return conn.execute(SELECT_BY_COORD, (loc.latitude, loc.longitude)).fetchone()

    lat_lo, lat_hi, lon_lo, lon_hi = bounding_box(lat, lon, loc.match_radius_m)
    candidates = conn.execute(
        SELECT_NEARBY, (lat_lo, lat_hi, lon_lo, lon_hi)
    ).fetchall()

    best, best_distance = None, None
    for row in candidates:
        if _same_place(loc.label, row["label"]) is False:
            continue                     # different business at this spot
        distance = haversine_m(lat, lon, float(row["latitude"]), float(row["longitude"]))
        if distance > loc.match_radius_m:
            continue
        # A name match beats a merely closer unnamed row.
        named = _same_place(loc.label, row["label"]) is True
        rank = (0 if named else 1, distance)
        if best_distance is None or rank < best_distance:
            best, best_distance = row, rank
    return best


def resolve(
    conn: psycopg.Connection,
    loc: LocationFields,
    cache: dict | None = None,
    source: str = "csv",
) -> int | None:
    """Find or create the location. Returns None when there's nothing to store.

    Coordinates win over address: they identify the storefront, while an
    address may be missing, abbreviated differently, or just the city.
    """
    if loc.is_empty():
        return None

    cache_key = loc.key()
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    found = None
    if loc.has_coords:
        found = _find_nearby(conn, loc)
    elif loc.has_address:
        found = conn.execute(
            SELECT_BY_ADDRESS,
            (loc.address, loc.city, loc.state, loc.zip, loc.country or "US"),
        ).fetchone()

    if found:
        location_id = found["id"]
        # Fill in blanks a later, richer row supplies, without overwriting.
        conn.execute(
            """
            UPDATE locations
               SET label   = coalesce(label, %s),
                   address = coalesce(address, %s),
                   city    = coalesce(city, %s),
                   state   = coalesce(state, %s),
                   zip     = coalesce(zip, %s),
                   -- a later row may carry a refreshed score; a blank
                   -- one must not wipe what we already have
                   popularity = coalesce(%s, popularity)
             WHERE id = %s
            """,
            (loc.label, loc.address, loc.city, loc.state, loc.zip,
             loc.popularity, location_id),
        )
    else:
        location_id = _insert(conn, loc, source)

    if cache is not None:
        cache[cache_key] = location_id
    return location_id


def _refetch(conn: psycopg.Connection, loc: LocationFields) -> int | None:
    """Find the row that already holds this identity, after a clash."""
    if loc.has_coords:
        row = conn.execute(
            SELECT_BY_COORD_AND_LABEL, (loc.latitude, loc.longitude, loc.label)
        ).fetchone()
        if row:
            return row["id"]
        row = conn.execute(SELECT_BY_COORD, (loc.latitude, loc.longitude)).fetchone()
        if row:
            return row["id"]
    if loc.has_address:
        row = conn.execute(
            SELECT_BY_ADDRESS,
            (loc.address, loc.city, loc.state, loc.zip, loc.country or "US"),
        ).fetchone()
        if row:
            return row["id"]
    return None


def _insert(conn: psycopg.Connection, loc: LocationFields, source: str) -> int:
    """Insert a location, surviving a unique clash.

    The savepoint matters more than it looks: an import is one
    transaction, so without it a single constraint violation aborts the
    whole file. Any clash means some row already holds this identity, so
    the right answer is to use it rather than to fail.
    """
    try:
        with conn.transaction():
            return conn.execute(
                INSERT_LOCATION,
                {
                    "latitude": loc.latitude,
                    "longitude": loc.longitude,
                    "label": loc.label,
                    "address": loc.address,
                    "city": loc.city,
                    "state": loc.state,
                    "zip": loc.zip,
                    "country": loc.country or "US",
                    "source": source,
                    "popularity": loc.popularity,
                },
            ).fetchone()["id"]
    except psycopg.errors.UniqueViolation:
        existing = _refetch(conn, loc)
        if existing is None:
            raise
        return existing


def coverage(conn: psycopg.Connection) -> dict:
    return conn.execute("SELECT * FROM v_location_coverage").fetchone()


def backfill_from_staging(
    conn: psycopg.Connection, batch_id: int | None = None
) -> dict:
    """Attach locations to transactions that were imported without one.

    Re-reads the original CSV rows kept in staging_rows and runs them
    through the current mapping. This is how coordinates reach rows that
    were imported before the mapping knew about the columns - re-importing
    the file would otherwise be a no-op, since those rows already exist.
    """
    from .mapping import load as load_mapping
    from .normalize import RowError, location_from_row

    batches = conn.execute(
        """
        SELECT DISTINCT b.id, b.mapping
        FROM import_batches b
        JOIN staging_rows s ON s.batch_id = b.id
        JOIN transactions t ON t.id = s.transaction_id
        WHERE t.location_id IS NULL
          AND (%s::bigint IS NULL OR b.id = %s::bigint)
        ORDER BY b.id
        """,
        (batch_id, batch_id),
    ).fetchall()

    stats = {"batches": 0, "updated": 0, "no_location_in_row": 0, "errors": 0,
             "skipped_batches": []}

    for batch in batches:
        try:
            mapping = load_mapping(batch["mapping"])
        except Exception as exc:
            stats["skipped_batches"].append(f"#{batch['id']} ({batch['mapping']}): {exc}")
            continue

        if not mapping.has_location:
            stats["skipped_batches"].append(
                f"#{batch['id']} ({batch['mapping']}): mapping has no location columns"
            )
            continue

        rows = conn.execute(
            """
            SELECT s.id, s.raw, s.transaction_id
            FROM staging_rows s
            JOIN transactions t ON t.id = s.transaction_id
            WHERE s.batch_id = %s AND t.location_id IS NULL
            ORDER BY s.line_no
            """,
            (batch["id"],),
        ).fetchall()

        cache: dict = {}
        touched = 0
        for row in rows:
            try:
                loc = location_from_row(row["raw"], mapping)
            except RowError:
                stats["errors"] += 1
                continue
            if loc is None:
                stats["no_location_in_row"] += 1
                continue
            location_id = resolve(conn, loc, cache)
            if location_id is None:
                stats["no_location_in_row"] += 1
                continue
            conn.execute(
                "UPDATE transactions SET location_id = %s WHERE id = %s",
                (location_id, row["transaction_id"]),
            )
            touched += 1

        if touched:
            stats["batches"] += 1
            stats["updated"] += touched

    return stats
