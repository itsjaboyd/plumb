"""The direction amount mode, and the ledger-shaped CSV it exists for."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from plumb.importer import EXTRA_FIELDS, check_row_shape
from plumb.mapping import DEFAULT_DIRECTION_MAP, from_dict
from plumb.normalize import RowError, normalize_row


def mk(**overrides):
    """A mapping shaped like the real AFCU export."""
    base = {
        "account": {"name": "AFCU Checking", "type": "Checking"},
        "account_types": {
            "AFCU Checking": "Checking",
            "AFCU Visa Classic": "Credit",
        },
        "columns": {
            "posted_on": "Posted",
            "description": "Description",
            "category": "Subdivision",
            "direction": "Direction",
            "amount": "Amount",
            "balance": "Balance",
            "account": "Account",
            "merchant_ref": "Merchant ID",
            "location_ref": "Location ID",
        },
        "date_format": ["%m/%d/%y", "%Y-%m-%d"],
        "amount": {"mode": "direction"},
    }
    base.update(overrides)
    return from_dict(base, "afcu_test")


def row(**overrides):
    base = {
        "Posted": "1/18/20",
        "Description": "VISA-01/16 AMZN MKTP US",
        "Subdivision": "Amazon Shopping",
        "Direction": "Withdraw",
        "Amount": " $11.71 ",
        "Balance": " $348.68 ",
        "Account": "AFCU Checking",
        "Merchant ID": "25",
        "Location ID": "12",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------- signs
@pytest.mark.parametrize(
    "direction,expected",
    [
        ("Withdraw", Decimal("-11.71")),
        ("Transfer", Decimal("-11.71")),
        ("Deposit", Decimal("11.71")),
        ("Receive", Decimal("11.71")),
    ],
)
def test_direction_sets_the_sign(direction, expected):
    txn = normalize_row(row(Direction=direction), mk())
    assert txn.amount == expected
    assert txn.direction == direction


def test_direction_is_case_insensitive():
    assert normalize_row(row(Direction="withdraw"), mk()).direction == "Withdraw"
    assert normalize_row(row(Direction=" DEPOSIT "), mk()).direction == "Deposit"


def test_common_synonyms_are_understood():
    for word, canonical in [("debit", "Withdraw"), ("credit", "Deposit"),
                            ("transfer in", "Receive"), ("xfer out", "Transfer")]:
        assert DEFAULT_DIRECTION_MAP[word] == canonical
        assert normalize_row(row(Direction=word), mk()).direction == canonical


def test_unknown_direction_names_the_fix():
    with pytest.raises(RowError, match="direction_map"):
        normalize_row(row(Direction="Sideways"), mk())


def test_direction_map_extends_the_vocabulary():
    m = mk(amount={"mode": "direction",
                   "direction_map": {"Sideways": "Withdraw"}})
    assert normalize_row(row(Direction="Sideways"), m).amount == Decimal("-11.71")


def test_direction_map_rejects_a_non_canonical_target():
    with pytest.raises(ValueError, match="direction_map"):
        mk(amount={"mode": "direction", "direction_map": {"x": "Sideways"}})


def test_contradiction_between_sign_and_direction_is_refused():
    """A negative amount on an inward direction means the mapping is wrong."""
    with pytest.raises(RowError, match="came in"):
        normalize_row(row(Direction="Deposit", Amount="-11.71"), mk())


def test_magnitude_sign_is_ignored_for_outward_directions():
    """Some exports write outflows negative even with a direction column."""
    txn = normalize_row(row(Direction="Withdraw", Amount="-11.71"), mk())
    assert txn.amount == Decimal("-11.71")


def test_flip_sign_also_flips_the_direction():
    """Otherwise amount and direction would violate the CHECK constraint."""
    m = mk(amount={"mode": "direction", "flip_sign": True})
    txn = normalize_row(row(Direction="Withdraw"), m)
    assert txn.amount == Decimal("11.71")
    assert txn.direction == "Deposit"


# ------------------------------------------------------- the rest of the row
def test_padded_headers_and_dollar_amounts():
    txn = normalize_row(row(), mk())
    assert txn.amount == Decimal("-11.71")
    assert txn.statement_balance == Decimal("348.68")


def test_two_digit_year_is_parsed():
    assert normalize_row(row(), mk()).posted_on == date(2020, 1, 18)


def test_per_row_account_and_refs_are_captured():
    txn = normalize_row(row(Account="AFCU Visa Classic"), mk())
    assert txn.account_name == "AFCU Visa Classic"
    assert txn.merchant_ref == "25"
    assert txn.location_ref == "12"
    assert txn.category_path == "Amazon Shopping"


def test_blank_refs_become_none():
    txn = normalize_row(row(**{"Merchant ID": "", "Location ID": ""}), mk())
    assert txn.merchant_ref is None
    assert txn.location_ref is None


def test_quoted_thousands_separator_parses():
    txn = normalize_row(row(Direction="Deposit", Amount="$2,450.00"), mk())
    assert txn.amount == Decimal("2450.00")


# ---------------------------------------------------- account type guessing
def test_explicit_account_types_win():
    assert mk().account_type_for("AFCU Visa Classic") == ("Credit", False)


def test_unlisted_account_is_guessed_and_flagged():
    kind, guessed = mk().account_type_for("AFCU Visa Platinum")
    assert kind == "Credit"
    assert guessed is True        # so the importer can say so out loud


def test_unguessable_account_falls_back_to_the_default():
    assert mk().account_type_for("Something Else") == ("Checking", False)


# ------------------------------------------------------------ ragged rows
def test_extra_fields_are_rejected_with_the_likely_cause():
    """An unquoted $2,450.00 shifts every later column."""
    bad = row()
    bad[EXTRA_FIELDS] = ["450.00 ", " $2"]
    with pytest.raises(RowError, match="unquoted comma"):
        check_row_shape(bad, mk())


def test_missing_required_field_is_rejected():
    bad = row()
    bad["Amount"] = None
    with pytest.raises(RowError, match="fewer columns"):
        check_row_shape(bad, mk())


def test_well_formed_row_passes_the_shape_check():
    check_row_shape(row(), mk())


# ------------------------------------------------------ mapping validation
def test_direction_mode_requires_a_direction_column():
    with pytest.raises(ValueError, match="columns.direction"):
        from_dict(
            {
                "account": {"name": "X", "type": "Checking"},
                "columns": {"posted_on": "Posted", "amount": "Amount"},
                "date_format": ["%Y-%m-%d"],
                "amount": {"mode": "direction"},
            },
            "no_direction",
        )


def test_direction_mode_requires_an_amount_column():
    with pytest.raises(ValueError, match="columns.amount"):
        from_dict(
            {
                "account": {"name": "X", "type": "Checking"},
                "columns": {"posted_on": "Posted", "direction": "Direction"},
                "date_format": ["%Y-%m-%d"],
                "amount": {"mode": "direction"},
            },
            "no_amount",
        )


def test_bad_account_types_are_rejected():
    with pytest.raises(ValueError, match="account_types"):
        mk(account_types={"AFCU Checking": "Piggybank"})


def test_bad_on_unknown_is_rejected():
    with pytest.raises(ValueError, match="on_unknown"):
        mk(category={"on_unknown": "maybe"})
