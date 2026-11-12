"""Unit tests for the parts that are easy to get quietly wrong."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from plumb.mapping import from_dict
from plumb.normalize import (
    RowError,
    fallback_external_id,
    normalize_row,
    parse_date,
    parse_money,
)


def mk(**overrides):
    base = {
        "account": {"name": "Test Card", "type": "Credit"},
        "columns": {
            "posted_on": "Post Date",
            "transacted_on": "Transaction Date",
            "description": "Description",
            "amount": "Amount",
        },
        "date_format": ["%m/%d/%Y"],
        "amount": {"mode": "signed"},
    }
    base.update(overrides)
    return from_dict(base, "test")


# ------------------------------------------------------------------ money
@pytest.mark.parametrize(
    "text,expected",
    [
        ("12.34", Decimal("12.34")),
        ("-12.34", Decimal("-12.34")),
        ("$1,234.56", Decimal("1234.56")),
        ("(45.00)", Decimal("-45.00")),
        ("  -0.99  ", Decimal("-0.99")),
        ("1.234,50".replace(".", ""), Decimal("1234.50")),   # 1234,50
        ("", None),
        (None, None),
    ],
)
def test_parse_money(text, expected):
    assert parse_money(text) == expected


def test_parse_money_rejects_garbage():
    with pytest.raises(RowError):
        parse_money("not a number")


# ------------------------------------------------------------------- dates
def test_parse_date_tries_each_format():
    formats = ["%m/%d/%Y", "%Y-%m-%d"]
    assert parse_date("10/08/2026", formats) == date(2026, 10, 8)
    assert parse_date("2026-10-08", formats) == date(2026, 10, 8)


def test_parse_date_rejects_unknown():
    with pytest.raises(RowError):
        parse_date("08.10.2026", ["%m/%d/%Y"])


# ------------------------------------------------------------------- signs
def test_signed_mode_keeps_bank_sign():
    m = mk()
    row = {"Post Date": "10/08/2026", "Transaction Date": "10/07/2026",
           "Description": "COFFEE", "Amount": "-4.50"}
    txn = normalize_row(row, m)
    assert txn.amount == Decimal("-4.50")
    assert txn.posted_on == date(2026, 10, 8)
    assert txn.transacted_on == date(2026, 10, 7)
    assert txn.raw_description == "COFFEE"


def test_flip_sign_inverts_amex_style_exports():
    m = mk(amount={"mode": "signed", "flip_sign": True})
    row = {"Post Date": "10/08/2026", "Description": "DINNER", "Amount": "82.10"}
    assert normalize_row(row, m).amount == Decimal("-82.10")


def test_debit_credit_mode():
    m = mk(
        columns={"posted_on": "Posted Date", "description": "Description"},
        date_format=["%Y-%m-%d"],
        amount={"mode": "debit_credit", "debit_column": "Debit",
                "credit_column": "Credit"},
    )
    out = normalize_row(
        {"Posted Date": "2026-10-08", "Description": "GAS", "Debit": "40.00",
         "Credit": ""}, m
    )
    assert out.amount == Decimal("-40.00")

    back = normalize_row(
        {"Posted Date": "2026-10-09", "Description": "REFUND", "Debit": "",
         "Credit": "15.00"}, m
    )
    assert back.amount == Decimal("15.00")


def test_debit_credit_rejects_ambiguous_rows():
    m = mk(
        columns={"posted_on": "Posted Date", "description": "Description"},
        date_format=["%Y-%m-%d"],
        amount={"mode": "debit_credit", "debit_column": "Debit",
                "credit_column": "Credit"},
    )
    with pytest.raises(RowError):
        normalize_row({"Posted Date": "2026-10-08", "Debit": "1.00",
                       "Credit": "2.00", "Description": "?"}, m)
    with pytest.raises(RowError):
        normalize_row({"Posted Date": "2026-10-08", "Debit": "",
                       "Credit": "", "Description": "?"}, m)


def test_magnitude_with_type_mode():
    m = mk(
        columns={"posted_on": "Date", "description": "Description", "amount": "Amount"},
        date_format=["%Y-%m-%d"],
        amount={"mode": "magnitude_with_type", "type_column": "Type",
                "debit_values": ["debit", "withdrawal"]},
    )
    assert normalize_row(
        {"Date": "2026-10-08", "Description": "ATM", "Amount": "60.00",
         "Type": "WITHDRAWAL"}, m
    ).amount == Decimal("-60.00")
    assert normalize_row(
        {"Date": "2026-10-08", "Description": "PAY", "Amount": "2000.00",
         "Type": "DEPOSIT"}, m
    ).amount == Decimal("2000.00")


# ------------------------------------------------------- identity / dedupe
def test_hash_is_stable_for_identical_input():
    args = ("Card", date(2026, 10, 8), Decimal("-4.50"), "COFFEE", 0)
    assert fallback_external_id(*args) == fallback_external_id(*args)


def test_hash_differs_per_occurrence():
    a = fallback_external_id("Card", date(2026, 10, 8), Decimal("-4.50"), "COFFEE", 0)
    b = fallback_external_id("Card", date(2026, 10, 8), Decimal("-4.50"), "COFFEE", 1)
    assert a != b


def test_two_identical_charges_same_day_get_different_ids():
    """Two coffees on one day are two transactions, not a duplicate."""
    m = mk()
    row = {"Post Date": "10/08/2026", "Transaction Date": "10/08/2026",
           "Description": "COFFEE", "Amount": "-4.50"}
    seen: dict = {}
    first = normalize_row(dict(row), m, seen)
    second = normalize_row(dict(row), m, seen)
    assert first.external_id != second.external_id

    # ...but re-reading the same file produces the same two ids, so a
    # re-import inserts nothing.
    again: dict = {}
    assert normalize_row(dict(row), m, again).external_id == first.external_id
    assert normalize_row(dict(row), m, again).external_id == second.external_id


def test_bank_supplied_id_wins():
    m = mk(columns={"posted_on": "Post Date", "description": "Description",
                    "amount": "Amount", "external_id": "Ref"})
    txn = normalize_row(
        {"Post Date": "10/08/2026", "Description": "X", "Amount": "-1.00",
         "Ref": "TXN-999"}, m
    )
    assert txn.external_id == "TXN-999"


def test_memo_is_appended_to_description():
    m = mk(columns={"posted_on": "Post Date", "description": "Description",
                    "memo": "Memo", "amount": "Amount"})
    txn = normalize_row(
        {"Post Date": "10/08/2026", "Description": "SQ *SHOP", "Memo": "beans",
         "Amount": "-9.00"}, m
    )
    assert txn.raw_description == "SQ *SHOP | beans"


def test_missing_date_is_a_row_error_not_a_crash():
    m = mk()
    with pytest.raises(RowError):
        normalize_row({"Post Date": "", "Description": "X", "Amount": "-1.00"}, m)


# ----------------------------------------------------------- mapping rules
def test_mapping_validation_catches_bad_mode():
    with pytest.raises(ValueError):
        mk(amount={"mode": "nonsense"})


def test_mapping_validation_requires_debit_and_credit_columns():
    with pytest.raises(ValueError):
        mk(amount={"mode": "debit_credit", "debit_column": "Debit"})


def test_mapping_validation_rejects_unknown_account_type():
    with pytest.raises(ValueError):
        mk(account={"name": "X", "type": "Piggybank"})
