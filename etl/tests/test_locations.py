"""Coordinate parsing, geometry, and the place-identity rules."""
from __future__ import annotations

from decimal import Decimal

import pytest

from plumb.locations import (
    LocationFields,
    _same_place,
    bounding_box,
    haversine_m,
    parse_coordinate,
    parse_coordinate_pair,
    validate,
)
from plumb.mapping import from_dict
from plumb.normalize import RowError, normalize_row


def mk(**overrides):
    base = {
        "account": {"name": "Geo Card", "type": "Credit"},
        "columns": {
            "posted_on": "Date",
            "description": "Description",
            "amount": "Amount",
            "latitude": "Latitude",
            "longitude": "Longitude",
            "place": "Place",
            "city": "City",
        },
        "date_format": ["%Y-%m-%d"],
        "amount": {"mode": "signed"},
    }
    base.update(overrides)
    return from_dict(base, "geo_test")


def row(**overrides):
    base = {
        "Date": "2026-10-02",
        "Description": "KING SOOPERS",
        "Amount": "-82.14",
        "Latitude": "39.74001",
        "Longitude": "-104.98420",
        "Place": "King Soopers Capitol Hill",
        "City": "Denver",
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------- single values
@pytest.mark.parametrize(
    "text,expected",
    [
        ("39.74001", Decimal("39.740010")),
        ("-104.9842", Decimal("-104.984200")),
        ("  39.74  ", Decimal("39.740000")),
        ("39.74 N", Decimal("39.740000")),
        ("104.9842 W", Decimal("-104.984200")),
        ("39.74 S", Decimal("-39.740000")),
        ("", None),
        ("n/a", None),
        (None, None),
    ],
)
def test_parse_coordinate(text, expected):
    assert parse_coordinate(text) == expected


def test_parse_coordinate_rejects_garbage():
    with pytest.raises(RowError):
        parse_coordinate("over there")


@pytest.mark.parametrize(
    "text",
    ["39.74001, -104.98420", "39.74001,-104.98420", "39.74001; -104.98420",
     "39.74001 / -104.98420", "39.74001|-104.98420"],
)
def test_parse_coordinate_pair_separators(text):
    lat, lon = parse_coordinate_pair(text)
    assert (lat, lon) == (Decimal("39.740010"), Decimal("-104.984200"))


def test_parse_coordinate_pair_blank_is_none():
    assert parse_coordinate_pair("") is None
    assert parse_coordinate_pair(None) is None


def test_parse_coordinate_pair_rejects_single_value():
    with pytest.raises(RowError):
        parse_coordinate_pair("39.74001")


# -------------------------------------------------------------- validation
def test_validate_accepts_valid_pair():
    validate(Decimal("39.74"), Decimal("-104.98"))


def test_validate_requires_both():
    with pytest.raises(RowError):
        validate(Decimal("39.74"), None)


def test_validate_rejects_out_of_range():
    with pytest.raises(RowError, match="out of range"):
        validate(Decimal("999"), Decimal("-104.98"))


def test_validate_names_the_swapped_case():
    """A valid longitude in the latitude column is the classic mistake."""
    with pytest.raises(RowError, match="swapped"):
        validate(Decimal("-104.98"), Decimal("39.74"))


# ---------------------------------------------------------------- geometry
def test_haversine_one_degree_of_latitude():
    metres = haversine_m(39.0, -105.0, 40.0, -105.0)
    assert 111_000 < metres < 111_400          # ~111.19 km


def test_haversine_knows_the_two_shops_are_22m_apart():
    """The case that forced name-aware matching: neighbours inside any
    radius wide enough to absorb GPS jitter."""
    metres = haversine_m(39.74001, -104.98420, 39.74015, -104.98402)
    assert 15 < metres < 30


def test_haversine_same_point_is_zero():
    assert haversine_m(39.74, -104.98, 39.74, -104.98) == pytest.approx(0, abs=1e-6)


def test_bounding_box_encloses_the_circle():
    lat, lon, radius = 39.74, -104.98, 25
    lat_lo, lat_hi, lon_lo, lon_hi = bounding_box(lat, lon, radius)
    assert lat_lo < lat < lat_hi and lon_lo < lon < lon_hi
    # every edge is at least the radius away
    assert haversine_m(lat, lon, lat_hi, lon) >= radius
    assert haversine_m(lat, lon, lat, lon_hi) >= radius


def test_bounding_box_widens_with_latitude():
    """Longitude degrees shrink toward the poles, so the box must widen."""
    _, _, lo_equator, hi_equator = bounding_box(0.0, 0.0, 100)
    _, _, lo_arctic, hi_arctic = bounding_box(70.0, 0.0, 100)
    assert (hi_arctic - lo_arctic) > (hi_equator - lo_equator)


def test_bounding_box_survives_the_pole():
    bounding_box(90.0, 0.0, 100)        # must not divide by zero


# -------------------------------------------------------- place identity
def test_same_place_is_case_and_space_insensitive():
    assert _same_place("Blue Bird Coffee", " blue bird coffee ") is True


def test_same_place_distinguishes_different_names():
    assert _same_place("Blue Bird Coffee", "King Soopers") is False


def test_same_place_is_unknown_when_unnamed():
    assert _same_place(None, "King Soopers") is None
    assert _same_place("King Soopers", None) is None


def test_location_cache_key_ignores_the_radius():
    a = LocationFields(match_radius_m=25, latitude=Decimal("1"), longitude=Decimal("2"))
    b = LocationFields(match_radius_m=50, latitude=Decimal("1"), longitude=Decimal("2"))
    assert a.key() == b.key()


# ------------------------------------------------- through the normalizer
def test_normalize_row_picks_up_coordinates():
    txn = normalize_row(row(), mk())
    assert txn.location.latitude == Decimal("39.740010")
    assert txn.location.longitude == Decimal("-104.984200")
    assert txn.location.label == "King Soopers Capitol Hill"
    assert txn.location.city == "Denver"


def test_zero_zero_is_treated_as_no_fix():
    txn = normalize_row(row(Latitude="0", Longitude="0", Place="", City=""), mk())
    assert txn.location is None


def test_zero_zero_kept_when_asked():
    m = mk(location={"drop_zero_zero": False})
    txn = normalize_row(row(Latitude="0", Longitude="0", Place="", City=""), m)
    assert txn.location.latitude == Decimal("0.000000")


def test_swap_lat_lon_fixes_a_reversed_export():
    m = mk(location={"swap_lat_lon": True})
    txn = normalize_row(row(Latitude="-104.98420", Longitude="39.74001"), m)
    assert txn.location.latitude == Decimal("39.740010")
    assert txn.location.longitude == Decimal("-104.984200")


def test_bad_coordinates_are_a_row_error_not_a_crash():
    with pytest.raises(RowError):
        normalize_row(row(Latitude="999"), mk())


def test_address_only_row_still_makes_a_location():
    m = mk(columns={
        "posted_on": "Date", "description": "Description", "amount": "Amount",
        "city": "City", "zip": "Postal",
    })
    txn = normalize_row(
        {"Date": "2026-10-02", "Description": "X", "Amount": "-1.00",
         "City": "Denver", "Postal": "80203"}, m
    )
    assert txn.location.has_coords is False
    assert txn.location.has_address is True


def test_rows_without_any_location_columns_stay_none():
    m = mk(columns={"posted_on": "Date", "description": "Description",
                    "amount": "Amount"})
    assert m.has_location is False
    assert normalize_row(row(), m).location is None


# ------------------------------------------------------- mapping validation
def test_latitude_without_longitude_is_rejected():
    with pytest.raises(ValueError, match="together"):
        mk(columns={"posted_on": "Date", "amount": "Amount", "latitude": "Latitude"})


def test_coordinates_and_latitude_together_are_rejected():
    with pytest.raises(ValueError, match="not both"):
        mk(columns={"posted_on": "Date", "amount": "Amount",
                    "coordinates": "Loc", "latitude": "Latitude",
                    "longitude": "Longitude"})


def test_absurd_match_radius_is_rejected():
    with pytest.raises(ValueError, match="match_radius_m"):
        mk(location={"match_radius_m": 99999})


# --------------------------------------------------- country normalisation
@pytest.mark.parametrize(
    "given,expected",
    [
        ("United States", "US"),
        ("united states of america", "US"),
        ("USA", "US"),
        ("us", "US"),
        ("Canada", "CA"),
        ("United Kingdom", "GB"),
        ("de", "DE"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_country(given, expected):
    from plumb.locations import normalize_country
    assert normalize_country(given) == expected


def test_unknown_country_is_kept_as_written():
    """Better a name we don't recognise than a wrong guess."""
    from plumb.locations import normalize_country
    assert normalize_country("Freedonia") == "Freedonia"


def test_country_normalisation_reaches_the_location():
    """address_key includes the country, so 'United States' and 'US' would
    otherwise be two different places at one address."""
    m = mk(columns={"posted_on": "Date", "description": "Description",
                    "amount": "Amount", "city": "City", "country": "Country"})
    txn = normalize_row(
        {"Date": "2026-10-02", "Description": "X", "Amount": "-1.00",
         "City": "Seattle", "Country": "United States"}, m
    )
    assert txn.location.country == "US"


# -------------------------------------------------------------- popularity
def test_popularity_is_read_at_full_precision():
    m = mk(columns={"posted_on": "Date", "description": "Description",
                    "amount": "Amount", "latitude": "Latitude",
                    "longitude": "Longitude", "popularity": "Popularity"})
    txn = normalize_row(
        {"Date": "2026-10-02", "Description": "X", "Amount": "-1.00",
         "Latitude": "47.6229503", "Longitude": "-122.3365149",
         "Popularity": "6.231813814"}, m
    )
    assert txn.location.popularity == Decimal("6.231813814")


def test_blank_popularity_is_none():
    m = mk(columns={"posted_on": "Date", "description": "Description",
                    "amount": "Amount", "latitude": "Latitude",
                    "longitude": "Longitude", "popularity": "Popularity"})
    txn = normalize_row(
        {"Date": "2026-10-02", "Description": "X", "Amount": "-1.00",
         "Latitude": "47.62295", "Longitude": "-122.33651", "Popularity": ""}, m
    )
    assert txn.location.popularity is None


def test_negative_popularity_is_refused():
    m = mk(columns={"posted_on": "Date", "description": "Description",
                    "amount": "Amount", "latitude": "Latitude",
                    "longitude": "Longitude", "popularity": "Popularity"})
    with pytest.raises(RowError, match="negative"):
        normalize_row(
            {"Date": "2026-10-02", "Description": "X", "Amount": "-1.00",
             "Latitude": "47.62295", "Longitude": "-122.33651",
             "Popularity": "-2"}, m
        )


def test_unparseable_popularity_is_a_row_error():
    m = mk(columns={"posted_on": "Date", "description": "Description",
                    "amount": "Amount", "latitude": "Latitude",
                    "longitude": "Longitude", "popularity": "Popularity"})
    with pytest.raises(RowError, match="popularity"):
        normalize_row(
            {"Date": "2026-10-02", "Description": "X", "Amount": "-1.00",
             "Latitude": "47.62295", "Longitude": "-122.33651",
             "Popularity": "very"}, m
        )


def test_popularity_is_not_part_of_the_location_identity():
    """A refreshed score must not create a second row for one place."""
    a = LocationFields(latitude=Decimal("1"), longitude=Decimal("2"),
                       popularity=Decimal("6.1"))
    b = LocationFields(latitude=Decimal("1"), longitude=Decimal("2"),
                       popularity=Decimal("9.9"))
    assert a.key() == b.key()


# ------------------------------------------- identity: point AND name
def test_identity_normalisation_matches_the_unique_index():
    """The index normalises with lower(btrim(...)); _same_place must agree,
    or two spellings of one name become two rows."""
    assert _same_place("Corner Shop", "corner shop ") is True
    assert _same_place("Panda Express", "Sbarro") is False


def test_two_businesses_may_share_one_point():
    """A geocoder returning one point for a food court is not an error, so
    the resolver must treat them as different places rather than merging."""
    mall = {"latitude": Decimal("39.743"), "longitude": Decimal("-104.99")}
    panda = LocationFields(**mall, label="Panda Express")
    sbarro = LocationFields(**mall, label="Sbarro")
    assert panda.key() != sbarro.key()          # distinct cache identities
    assert _same_place(panda.label, sbarro.label) is False


def test_repeat_visit_to_one_place_is_one_identity():
    mall = {"latitude": Decimal("39.743"), "longitude": Decimal("-104.99")}
    first = LocationFields(**mall, label="Panda Express")
    again = LocationFields(**mall, label="Panda Express", popularity=Decimal("4.2"))
    assert first.key() == again.key()
