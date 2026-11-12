"""Resolving the source system's own merchant and location ids.

A ledger export often carries "Merchant ID: 28" rather than a name. The
importer keeps that id on the transaction (source_merchant_ref) instead of
inventing a merchant called "Merchant 28", which would clutter every
panel. Export the source's merchant and location tables, run them through
here, and the ids turn into real names and coordinates - including on rows
already imported.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import psycopg


def _read(path: Path, encoding: str = "utf-8-sig") -> tuple[list[dict], list[str]]:
    text = path.read_text(encoding=encoding, errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = [f.strip() for f in (reader.fieldnames or [])]
    rows = [{(k.strip() if k else k): v for k, v in r.items()} for r in reader]
    return rows, fields


def _require(fields: list[str], wanted: dict[str, str | None]) -> None:
    missing = sorted(v for v in wanted.values() if v and v not in fields)
    if missing:
        raise ValueError(f"columns {missing} are not in the file. It has: {fields}")


def _value(row: dict, column: str | None) -> str | None:
    if not column:
        return None
    return str(row.get(column, "") or "").strip() or None


def import_merchant_refs(
    conn: psycopg.Connection,
    path: Path,
    ref_column: str = "id",
    name_column: str = "name",
    category_column: str | None = None,
) -> dict:
    """Upsert merchants by external_ref, then attach matching transactions."""
    from .categories import resolve_existing

    rows, fields = _read(path)
    _require(fields, {"ref": ref_column, "name": name_column,
                      "category": category_column})

    stats = {"rows": len(rows), "created": 0, "updated": 0, "skipped": 0}
    seen_warnings: set[str] = set()
    for row in rows:
        ref, name = _value(row, ref_column), _value(row, name_column)
        if not ref or not name:
            stats["skipped"] += 1
            continue

        category_id = None
        path_value = _value(row, category_column)
        if path_value:
            category_id, warning = resolve_existing(conn, path_value)
            if warning and warning not in seen_warnings:
                seen_warnings.add(warning)
                print(f"  warning: {warning}")

        existing = conn.execute(
            "SELECT id, name FROM merchants WHERE external_ref = %s", (ref,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE merchants SET name = %s, "
                "default_category_id = coalesce(%s, default_category_id) "
                "WHERE id = %s",
                (name, category_id, existing["id"]),
            )
            stats["updated"] += 1
            continue

        # A merchant with this name may already exist from alias matching;
        # adopt it rather than creating a second row for the same business.
        by_name = conn.execute(
            "SELECT id, external_ref FROM merchants WHERE name = %s", (name,)
        ).fetchone()
        if by_name and by_name["external_ref"] is None:
            conn.execute(
                "UPDATE merchants SET external_ref = %s, "
                "default_category_id = coalesce(%s, default_category_id) "
                "WHERE id = %s",
                (ref, category_id, by_name["id"]),
            )
            stats["updated"] += 1
        elif by_name:
            stats["skipped"] += 1       # same name, a different ref already
        else:
            conn.execute(
                "INSERT INTO merchants (name, external_ref, default_category_id) "
                "VALUES (%s, %s, %s)",
                (name, ref, category_id),
            )
            stats["created"] += 1

    stats["attached"] = attach_merchants(conn)
    return stats


def import_location_refs(
    conn: psycopg.Connection,
    path: Path,
    ref_column: str = "id",
    label_column: str | None = "name",
    latitude_column: str | None = "latitude",
    longitude_column: str | None = "longitude",
    address_column: str | None = None,
    city_column: str | None = None,
    state_column: str | None = None,
    zip_column: str | None = None,
) -> dict:
    """Upsert locations by external_ref, then attach matching transactions."""
    from .locations import LocationFields, parse_coordinate, validate

    rows, fields = _read(path)
    _require(fields, {
        "ref": ref_column, "label": label_column,
        "latitude": latitude_column, "longitude": longitude_column,
        "address": address_column, "city": city_column,
        "state": state_column, "zip": zip_column,
    })

    stats = {"rows": len(rows), "created": 0, "updated": 0, "skipped": 0,
             "bad_coordinates": 0, "no_location_data": 0}
    for row in rows:
        ref = _value(row, ref_column)
        if not ref:
            stats["skipped"] += 1
            continue

        try:
            lat = parse_coordinate(_value(row, latitude_column))
            lon = parse_coordinate(_value(row, longitude_column))
            validate(lat, lon)
        except Exception:
            lat = lon = None
            stats["bad_coordinates"] += 1

        fields_in = LocationFields(
            latitude=lat,
            longitude=lon,
            label=_value(row, label_column),
            address=_value(row, address_column),
            city=_value(row, city_column),
            state=_value(row, state_column),
            zip=_value(row, zip_column),
        )
        if fields_in.is_empty():
            # An online merchant has no place, so there is nothing to
            # store. Its ref stays unresolved, which is accurate.
            stats["no_location_data"] += 1
            continue

        existing = conn.execute(
            "SELECT id FROM locations WHERE external_ref = %s", (ref,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE locations
                   SET label     = coalesce(%s, label),
                       latitude  = coalesce(%s, latitude),
                       longitude = coalesce(%s, longitude),
                       address   = coalesce(%s, address),
                       city      = coalesce(%s, city),
                       state     = coalesce(%s, state),
                       zip       = coalesce(%s, zip)
                 WHERE id = %s
                """,
                (fields_in.label, fields_in.latitude, fields_in.longitude,
                 fields_in.address, fields_in.city, fields_in.state,
                 fields_in.zip, existing["id"]),
            )
            stats["updated"] += 1
            continue

        conn.execute(
            """
            INSERT INTO locations (external_ref, label, latitude, longitude,
                                   address, city, state, zip, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'csv')
            """,
            (ref, fields_in.label, fields_in.latitude, fields_in.longitude,
             fields_in.address, fields_in.city, fields_in.state, fields_in.zip),
        )
        stats["created"] += 1

    stats["attached"] = attach_locations(conn)
    return stats


def attach_merchants(conn: psycopg.Connection) -> int:
    cur = conn.execute(
        """
        UPDATE transactions t
           SET merchant_id = m.id,
               category_id = coalesce(t.category_id, m.default_category_id)
          FROM merchants m
         WHERE m.external_ref = t.source_merchant_ref
           AND t.merchant_id IS NULL
        """
    )
    return cur.rowcount or 0


def attach_locations(conn: psycopg.Connection) -> int:
    cur = conn.execute(
        """
        UPDATE transactions t
           SET location_id = l.id
          FROM locations l
         WHERE l.external_ref = t.source_location_ref
           AND t.location_id IS NULL
        """
    )
    return cur.rowcount or 0


def unresolved(conn: psycopg.Connection, limit: int = 25) -> list[dict]:
    return conn.execute(
        "SELECT kind, ref, transactions, gross_amount FROM v_unresolved_refs "
        "ORDER BY transactions DESC LIMIT %s",
        (limit,),
    ).fetchall()
