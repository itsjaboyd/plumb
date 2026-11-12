"""The import sign convention, which net worth depends on.

Net worth subtracts what you owe, and it treats the two sources of a
balance differently (see v_liability_types in 002_views.sql):

  * a hand-entered figure on a Credit or Loan account (opening_balance,
    or a recorded account_balances row) means the amount OWED, so it is
    negated;
  * the sum of imported transactions is used as it is, because every
    mapping already normalises money out to negative - cards included.

That second clause is a premise, not a wish. If a mapping ever brought a
card purchase in as positive, a card you owe on would start ADDING to net
worth and nothing would say so. These tests pin the convention to the
shipped samples, so such a change fails here instead of silently.
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from plumb import mapping
from plumb.normalize import RowError, normalize_row

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"

#: Per sample file: how many rows normalise negative, how many positive,
#: and the net. The counts matter as much as the net - a mapping whose
#: sign convention inverted would swap them while a net of the same
#: magnitude could still look plausible.
#:
#: Note what these say about real statements: chase_savings is deposits
#: only and geo_card is purchases only, so a file having money moving
#: both ways is NOT something to assert in general.
SHAPES = {
    "afcu_wide__2020.csv":            (6, 2, Decimal("2277.93")),
    "amex__2026-10.csv":              (3, 1, Decimal("-139.40")),
    "capital_one_credit__2026-10.csv": (4, 1, Decimal("-387.01")),
    "chase_checking__2026-10.csv":    (5, 2, Decimal("3365.45")),
    "chase_credit__2026-10.csv":      (9, 2, Decimal("50.05")),
    "chase_savings__2026-10.csv":     (0, 2, Decimal("762.19")),
    "geo_card__2026-10.csv":          (10, 0, Decimal("-779.81")),
}

#: geo_card ships one deliberately invalid row (a latitude of 999) so the
#: stack's error reporting has something to show.
EXPECTED_BAD_ROWS = {"geo_card__2026-10.csv": 1}


def normalize_sample(name):
    """Every valid row of one sample, normalised, plus the reject count."""
    path = SAMPLES / name
    spec = mapping.load(path.name.split("__")[0])
    rows, bad = [], 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                rows.append(normalize_row(row, spec))
            except RowError:
                bad += 1
    return spec, rows, bad


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_sample_keeps_its_pinned_signs_and_net(name):
    want_neg, want_pos, want_net = SHAPES[name]
    _, rows, _ = normalize_sample(name)
    got_neg = sum(1 for r in rows if r.amount < 0)
    got_pos = sum(1 for r in rows if r.amount > 0)
    assert (got_neg, got_pos) == (want_neg, want_pos)
    assert sum(r.amount for r in rows) == want_net


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_no_row_normalises_to_zero(name):
    _, rows, _ = normalize_sample(name)
    assert all(r.amount != 0 for r in rows)


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_only_the_deliberately_bad_row_is_rejected(name):
    _, _, bad = normalize_sample(name)
    assert bad == EXPECTED_BAD_ROWS.get(name, 0)


def test_credit_card_spending_is_negative():
    """The premise net worth rests on, stated directly.

    On a credit account, purchases must come out negative whatever the
    bank's own convention was - Amex exports charges positive and
    amex.yml sets flip_sign to turn them around. Purchases outnumber
    payments on a real card statement, so an inverted mapping shows up
    as the negative rows becoming the minority.
    """
    checked = 0
    for name in sorted(SHAPES):
        spec, rows, _ = normalize_sample(name)
        if spec.account.type != "Credit":
            continue
        checked += 1
        neg = [r for r in rows if r.amount < 0]
        pos = [r for r in rows if r.amount > 0]
        assert neg, f"{name}: nothing came out negative"
        assert len(neg) > len(pos), (
            f"{name}: {len(pos)} positive vs {len(neg)} negative rows - the "
            f"sign convention looks inverted, which would make net worth "
            f"count card debt as an asset. db/scripts/signs.sql section 3 "
            f"finds these; the fix is the mapping plus `plumb replay "
            f"--fields amounts`, never a SQL update")
    assert checked >= 3, f"expected several credit samples, checked {checked}"


def test_no_shipped_mapping_claims_a_loan_account():
    """Loans have no export, so a Loan mapping would be a contradiction.

    If one is ever added, its transactions would be signed by the
    importer while its opening_balance is negated by net worth, and the
    two halves of the convention would need reconciling.
    """
    loans = [path.stem for path in mapping.available_mappings()
             if mapping.load(str(path)).account.type == "Loan"]
    assert loans == [], (
        f"{loans} map onto Loan accounts; re-read the balance convention "
        f"in README 'Core conventions' #1 before trusting net worth")
