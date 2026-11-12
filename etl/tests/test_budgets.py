"""What a budget file is allowed to say, and what it turns into.

The spreadsheet is the part of this stack a person edits by hand every
month, so the parsing is deliberately forgiving - "$1,200", "semi-annual",
"Yes" all land. What is NOT forgiving is the collapse: a wide file has to
become the smallest set of plan rows that means the same thing, because
one row per month would turn the intent table back into the grid the
schema exists to avoid.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from plumb.budgets import (BudgetError, LoadResult, PlanRow, detect_shape,
                            parse_bool, parse_month, parse_month_raw,
                            parse_period, read_long, read_wide)


# ------------------------------------------------------------------- months
@pytest.mark.parametrize("text", [
    "2026-01", "2026/01", "2026-01-01", "1/2026", "01-2026",
    "Jan 2026", "January 2026", "Jan-2026", "1/1/2026",
])
def test_every_reasonable_spelling_of_january_2026(text):
    assert parse_month(text) == date(2026, 1, 1)


def test_a_day_is_snapped_to_the_first():
    # A budget applies to whole months, and the schema's CHECK requires
    # the 1st - so a spreadsheet date of the 15th means "that month".
    assert parse_month("2026-03-15") == date(2026, 3, 1)


def test_the_raw_day_survives_so_a_snap_can_be_reported():
    # Snapping silently would hide a date the person may have meant
    # differently; the importer counts these and says so.
    assert parse_month_raw("2026-03-15").day == 15
    assert parse_month_raw("2026-03").day == 1


def test_a_bare_month_name_needs_a_year():
    with pytest.raises(BudgetError):
        parse_month("March")
    assert parse_month("March", default_year=2027) == date(2027, 3, 1)


def test_nonsense_is_an_error_not_a_guess():
    for text in ("", "   ", "not-a-month", "13/2026", "Smarch 2026"):
        with pytest.raises(BudgetError):
            parse_month(text)


# ------------------------------------------------------------------ periods
@pytest.mark.parametrize("text,expected", [
    ("monthly", "monthly"), ("Monthly", "monthly"), ("m", "monthly"),
    ("1", "monthly"), ("", "monthly"),
    ("quarterly", "quarterly"), ("Quarter", "quarterly"), ("3", "quarterly"),
    ("semiannual", "semiannual"), ("semi-annual", "semiannual"),
    ("Semi Annual", "semiannual"), ("half-yearly", "semiannual"),
    ("6", "semiannual"),
    ("annual", "annual"), ("Annually", "annual"), ("yearly", "annual"),
    ("12", "annual"),
])
def test_periods_as_a_person_would_write_them(text, expected):
    assert parse_period(text) == expected


def test_an_unknown_period_names_the_valid_ones():
    with pytest.raises(BudgetError) as exc:
        parse_period("fortnightly")
    assert "monthly" in str(exc.value) and "annual" in str(exc.value)


@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("Y", True), ("TRUE", True), ("1", True), ("x", True),
    ("envelope", True),
    ("no", False), ("N", False), ("false", False), ("0", False), ("", False),
])
def test_yes_and_no_in_the_forms_a_spreadsheet_produces(text, expected):
    assert parse_bool(text) is expected


def test_a_third_answer_is_an_error():
    with pytest.raises(BudgetError):
        parse_bool("maybe")


# -------------------------------------------------------------------- shapes
def test_two_month_columns_means_the_grid():
    assert detect_shape(["Category", "2026-01", "2026-02"]) == "wide"


def test_one_date_column_is_the_list_not_the_grid():
    # "effective_from" is a column OF dates, not a month column.
    assert detect_shape(
        ["category", "effective_from", "amount", "period"]) == "long"


# ---------------------------------------------------------------- long shape
def _long(rows):
    fields = list(rows[0].keys())
    result = LoadResult()
    return read_long(rows, fields, result), result


def test_long_reads_money_the_way_excel_exports_it():
    plans, result = _long([
        {"category": "Food", "effective_from": "2026-01",
         "amount": "$1,200.50", "period": "monthly", "carry_forward": "no"},
    ])
    assert result.errors == []
    assert plans[0].amount == Decimal("1200.50")


def test_a_blank_amount_is_a_stop_not_a_zero():
    # NULL means "stop budgeting from here"; zero would mean "budget
    # nothing and judge me against it", which is a different claim.
    plans, result = _long([
        {"category": "Food", "effective_from": "2026-09", "amount": "",
         "period": "monthly", "carry_forward": "no"},
    ])
    assert result.errors == []
    assert plans[0].amount is None


def test_a_negative_budget_is_refused():
    plans, result = _long([
        {"category": "Food", "effective_from": "2026-01", "amount": "-500",
         "period": "monthly", "carry_forward": "no"},
    ])
    assert plans == []
    assert "negative" in result.errors[0]


def test_a_missing_amount_column_is_reported_against_the_file():
    rows = [{"category": "Food", "effective_from": "2026-01"}]
    result = LoadResult()
    assert read_long(rows, list(rows[0]), result) == []
    assert "amount" in result.errors[0]


def test_blank_category_rows_are_skipped_not_errors():
    # Spreadsheets end with empty rows; they are not mistakes.
    plans, result = _long([
        {"category": "Food", "effective_from": "2026-01", "amount": "800"},
        {"category": "", "effective_from": "", "amount": ""},
    ])
    assert len(plans) == 1 and result.errors == []


# ---------------------------------------------------------------- wide shape
def _wide(rows, year=None):
    fields = list(rows[0].keys())
    result = LoadResult()
    return read_wide(rows, fields, result, year), result


def test_identical_months_collapse_to_one_plan_row():
    plans, result = _wide([{
        "Category": "Food", "2026-01": "800", "2026-02": "800",
        "2026-03": "800",
    }])
    assert [(p.valid_from, p.amount) for p in plans] == [
        (date(2026, 1, 1), Decimal("800"))]
    assert result.collapsed == 2


def test_a_change_starts_a_new_plan_row():
    plans, _ = _wide([{
        "Category": "Food", "2026-01": "800", "2026-02": "800",
        "2026-03": "850",
    }])
    assert [(p.valid_from.strftime("%Y-%m"), p.amount) for p in plans] == [
        ("2026-01", Decimal("800")), ("2026-03", Decimal("850"))]


def test_leading_blanks_are_months_before_the_budget_began():
    # Not a stop: there was nothing to stop. A stop row here would show up
    # on the dashboard as a category that was deliberately un-budgeted.
    plans, _ = _wide([{
        "Category": "Health", "2026-01": "", "2026-02": "", "2026-03": "300",
    }])
    assert len(plans) == 1
    assert plans[0].valid_from == date(2026, 3, 1)


def test_a_blank_after_a_value_stops_the_plan():
    plans, _ = _wide([{
        "Category": "Personal", "2026-01": "100", "2026-02": "100",
        "2026-03": "",
    }])
    assert [(p.valid_from.strftime("%Y-%m"), p.amount) for p in plans] == [
        ("2026-01", Decimal("100")), ("2026-03", None)]


def test_months_are_ordered_by_date_not_by_column_order():
    plans, _ = _wide([{
        "Category": "Food", "2026-03": "850", "2026-01": "800",
    }])
    assert [p.valid_from.strftime("%Y-%m") for p in plans] == [
        "2026-01", "2026-03"]


def test_an_opening_balance_seeds_only_the_first_row():
    # Repeating it would re-baseline the envelope every time the amount
    # changed, wiping out the balance the accruals had built up.
    plans, _ = _wide([{
        "Category": "Health", "2026-01": "100", "2026-02": "120",
        "opening balance": "400",
    }])
    assert [p.opening_balance for p in plans] == [Decimal("400"), None]


def test_per_category_attributes_apply_to_every_row_of_that_category():
    plans, _ = _wide([{
        "Category": "Health", "2026-01": "100", "2026-02": "120",
        "period": "monthly", "carry forward": "yes",
    }])
    assert all(p.carry_forward for p in plans)


def test_a_non_monthly_period_in_a_grid_warns_about_the_blanks():
    # The grid's "blank means stop" rule is a trap for an annual bill
    # entered once, so say so rather than loading something surprising.
    _, result = _wide([{
        "Category": "Health", "2026-01": "1200", "2026-02": "",
        "period": "annual", "carry forward": "yes",
    }])
    assert result.warnings and "annual" in result.warnings[0]


def test_bare_month_headers_need_the_year_flag():
    plans, _ = _wide([{"Category": "Food", "Jan": "800", "Feb": "850"}],
                     year=2026)
    assert [p.valid_from.strftime("%Y-%m") for p in plans] == [
        "2026-01", "2026-02"]


# --------------------------------------------------------------- the accrual
def test_accrual_divides_the_period_into_months():
    assert PlanRow("Health", date(2026, 1, 1), Decimal("1200"),
                   "annual").accrual == Decimal("100.00")
    assert PlanRow("X", date(2026, 1, 1), Decimal("600"),
                   "semiannual").accrual == Decimal("100.00")
    assert PlanRow("X", date(2026, 1, 1), Decimal("800"),
                   "monthly").accrual == Decimal("800.00")


def test_a_stop_row_accrues_nothing():
    assert PlanRow("X", date(2026, 1, 1), None, "monthly").accrual is None


def test_the_reported_total_does_not_double_count_a_mid_file_change():
    # The total exists so the person can tie it to their spreadsheet. If a
    # category's amount changes partway through the file, only the latest
    # plan is in force - summing both would overstate it and hide the very
    # mistake the check is for.
    result = LoadResult(plans=[
        PlanRow("Food", date(2026, 1, 1), Decimal("800"), "monthly"),
        PlanRow("Food", date(2026, 6, 1), Decimal("850"), "monthly"),
        PlanRow("Health", date(2026, 1, 1), Decimal("1200"), "annual"),
    ])
    assert result.monthly_total == Decimal("950.00")
