"""Replay's contract: what it is allowed to touch, and what it reports.

The body of a replay talks to the database, so it is exercised by the
integration checks rather than here. What is pinned here is the part that
decides scope - because the difference between "re-derive my categories"
and "rewrite my amounts" has to be explicit.
"""
from __future__ import annotations

import pytest

from plumb.replay import AMOUNTS, CLASSIFICATION, FIELD_SETS, ReplayResult


def test_the_three_field_sets_exist():
    assert set(FIELD_SETS) == {"classification", "amounts", "all"}


def test_classification_never_includes_money():
    for field in ("amount", "direction", "tax", "discount", "balance"):
        assert field not in FIELD_SETS["classification"]


def test_classification_covers_what_a_rule_change_affects():
    assert set(FIELD_SETS["classification"]) == {"category", "merchant", "location"}


def test_amounts_covers_the_numeric_fields():
    assert {"amount", "direction", "tax", "discount"} <= set(FIELD_SETS["amounts"])


def test_all_is_the_union():
    assert set(FIELD_SETS["all"]) == set(CLASSIFICATION) | set(AMOUNTS)


def test_default_is_classification_only():
    """The CLI default must be the safe one; a replay that quietly rewrote
    amounts would be a nasty surprise."""
    import inspect

    from plumb.replay import replay
    assert inspect.signature(replay).parameters["fields"].default == "classification"


def test_only_missing_and_retry_defaults():
    import inspect

    from plumb.replay import replay
    params = inspect.signature(replay).parameters
    assert params["only_missing"].default is False
    assert params["retry_errors"].default is True
    assert params["dry_run"].default is False


# ----------------------------------------------------------------- reporting
def test_changed_counts_every_kind_of_change():
    result = ReplayResult(category_changed=2, merchant_changed=1,
                          location_changed=3, amount_changed=4)
    assert result.changed == 10


def test_summary_mentions_recoveries_and_failures():
    result = ReplayResult(batches=1, rows_examined=11, recovered=1,
                          still_failing=2)
    text = result.summary()
    assert "11 row(s)" in text
    assert "1 previously-failed row(s) recovered" in text
    assert "2 still failing" in text


def test_fresh_result_is_empty():
    result = ReplayResult()
    assert result.changed == 0
    assert result.examples == []
    assert result.skipped_batches == []
