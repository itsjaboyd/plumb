"""Choosing between categories that share a name.

A CSV carrying only a leaf name ("Insurance") can match several places in
the tree. pick_topmost is the rule for that, and it is pure, so the choice
and the wording of the warning are pinned here.
"""
from __future__ import annotations

import pytest

from plumb.categories import pick_topmost, resolve_or_create
from plumb.mapping import CategorySpec, from_dict


def match(path: str, id: int, depth: int | None = None) -> dict:
    return {"id": id, "full_path": path, "depth": depth or path.count(">") + 1}


def mk_category(**overrides):
    base = {
        "account": {"name": "X", "type": "Checking"},
        "columns": {"posted_on": "Posted", "amount": "Amount",
                    "category": "Category"},
        "date_format": ["%Y-%m-%d"],
        "amount": {"mode": "signed"},
    }
    base.update(overrides)
    return from_dict(base, "cat_test")


# ------------------------------------------------------------ single match
def test_single_match_has_no_warning():
    chosen, warning = pick_topmost("Groceries", [match("Food > Groceries", 7)])
    assert chosen == 7
    assert warning is None


# ------------------------------------------------- shallowest wins
def test_shallower_match_wins():
    """find_by_name orders by depth, so matches[0] is closest to the root."""
    chosen, warning = pick_topmost("Insurance", [
        match("Housing > Insurance", 11),
        match("Personal > Pets > Insurance", 42),
    ])
    assert chosen == 11
    assert "Housing > Insurance" in warning
    assert "closest to the top" in warning


def test_top_level_match_beats_a_child():
    chosen, _ = pick_topmost("Travel", [
        match("Travel", 3),
        match("Entertainment > Travel", 19),
    ])
    assert chosen == 3


def test_warning_names_the_alternatives():
    _, warning = pick_topmost("Insurance", [
        match("Housing > Insurance", 11),
        match("Personal > Pets > Insurance", 42),
    ])
    assert "Personal > Pets > Insurance" in warning


def test_warning_counts_matches():
    _, warning = pick_topmost("Other", [
        match("Food > Other", 1), match("Housing > Other", 2),
        match("Health > Other", 3),
    ])
    assert "matched 3 categories" in warning


def test_warning_truncates_a_long_list_but_says_how_many():
    matches = [match(f"Top{i} > Other", i) for i in range(1, 8)]
    _, warning = pick_topmost("Other", matches)
    assert "+3 more" in warning


# ----------------------------------------------------------------- ties
def test_tie_on_depth_is_resolved_alphabetically_and_said_so():
    """Two categories at the same depth: the choice must be deterministic,
    because it gets written onto thousands of rows."""
    ordered = [match("Food > Other", 5), match("Housing > Other", 6)]
    chosen, warning = pick_topmost("Other", ordered)
    assert chosen == 5
    assert "tie on depth" in warning


def test_no_tie_note_when_depths_differ():
    _, warning = pick_topmost("Insurance", [
        match("Housing > Insurance", 11),
        match("Personal > Pets > Insurance", 42),
    ])
    assert "tie on depth" not in warning


def test_choice_is_stable_across_calls():
    matches = [match("Food > Other", 5), match("Housing > Other", 6)]
    first = pick_topmost("Other", matches)[0]
    assert all(pick_topmost("Other", matches)[0] == first for _ in range(5))


# ------------------------------------------------------ mapping validation
def test_on_ambiguous_defaults_to_topmost():
    assert mk_category().category.on_ambiguous == "topmost"


def test_on_ambiguous_accepts_error():
    m = mk_category(category={"on_ambiguous": "error"})
    assert m.category.on_ambiguous == "error"


def test_on_ambiguous_rejects_nonsense():
    with pytest.raises(ValueError, match="on_ambiguous"):
        mk_category(category={"on_ambiguous": "whatever"})


@pytest.mark.parametrize("value", [" > ", ">", " >> ", "  >  >  ", ">>>"])
def test_a_cell_of_only_separators_is_no_category(value):
    """A category cell holding only separators means no category.

    It used to return a bare None where every other branch returns an
    (id, warning) pair, so the importer died on
    "cannot unpack non-iterable NoneType object" - an unhelpful failure
    for a plausible empty-ish cell in a file that carries a path. No
    database is needed: the case returns before any query runs, which is
    also why passing None for the connection is safe here.
    """
    assert resolve_or_create(None, value, CategorySpec()) == (None, None)
