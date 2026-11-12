"""Loading a budget that was designed in a spreadsheet.

`budget_plans` stores intent - one row per CHANGE - while a spreadsheet is
a grid. So this module accepts both shapes and turns either into the
smallest set of plan rows that means the same thing:

  long   one row per change, which is the table's own shape:
             category,effective_from,amount,period,carry_forward,...
             Food,2026-01,800,monthly,no
             Health,2026-01,1200,annual,yes

  wide   one row per category, one column per month, which is how people
         actually build a budget:
             Category,2026-01,2026-02,2026-03
             Food,800,800,850

The wide shape is melted and then **collapsed**: a row is emitted only
where the amount differs from the month before. Twelve identical columns
become one plan row, because storing twelve would turn the intent table
into the month-by-month grid the design deliberately avoids.

A blank month cell means "stop budgeting this category from here", which
is what a NULL amount means in the schema. Leading blanks are not a stop -
they are months before the budget began, and emit nothing.

Nothing is written unless every row resolves. A budget file is small and
hand-made, so a typo that silently dropped one category would be worse
than a failed load; `skip_bad=True` overrides that.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg

from .normalize import parse_money

# Spelt how a person or a spreadsheet would spell them. Keys are compared
# with punctuation and spaces stripped, so "semi-annual" lands on
# "semiannual".
PERIOD_ALIASES = {
    "monthly": "monthly", "month": "monthly", "m": "monthly", "1": "monthly",
    "quarterly": "quarterly", "quarter": "quarterly", "q": "quarterly",
    "3": "quarterly",
    "semiannual": "semiannual", "semiannually": "semiannual",
    "halfyearly": "semiannual", "biannual": "semiannual",
    "twiceayear": "semiannual", "6": "semiannual",
    "annual": "annual", "annually": "annual", "yearly": "annual",
    "year": "annual", "y": "annual", "a": "annual", "12": "annual",
}
PERIOD_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}

TRUE_WORDS = {"y", "yes", "t", "true", "1", "x", "on", "roll", "rolls",
              "rollover", "carry", "envelope"}
FALSE_WORDS = {"", "n", "no", "f", "false", "0", "off", "flow"}

MONTH_FORMATS = [
    "%Y-%m", "%Y/%m",              # 2026-01
    "%Y-%m-%d", "%Y/%m/%d",        # 2026-01-01
    "%m/%Y", "%m-%Y",              # 1/2026
    "%b %Y", "%B %Y", "%b-%Y", "%B-%Y", "%b_%Y",   # Jan 2026
    "%m/%d/%Y", "%m-%d-%Y",        # 1/1/2026 - US order, this is a US stack
]
# Bare month names, for a sheet whose headers are just Jan..Dec.
BARE_MONTH_FORMATS = ["%b", "%B"]

CATEGORY_HEADERS = {"category", "categories", "name", "budget category",
                    "budget_category", "top level category", "top-level category"}
ATTR_HEADERS = {
    "period": "period", "frequency": "period", "cycle": "period",
    "carry_forward": "carry_forward", "carry forward": "carry_forward",
    "carry": "carry_forward", "rolls over": "carry_forward",
    "rollover": "carry_forward", "roll over": "carry_forward",
    "envelope": "carry_forward",
    "opening_balance": "opening_balance", "opening balance": "opening_balance",
    "balance": "opening_balance", "already saved": "opening_balance",
    "note": "note", "notes": "note", "comment": "note",
    "amount": "amount", "budget": "amount", "limit": "amount",
    "effective_from": "effective_from", "effective from": "effective_from",
    "from": "effective_from", "valid_from": "effective_from",
    "starting": "effective_from", "start": "effective_from",
    "month": "effective_from",
}


class BudgetError(ValueError):
    """A problem with one line, reported with its line number."""


@dataclass
class PlanRow:
    category: str
    valid_from: date
    amount: Decimal | None
    period: str = "monthly"
    carry_forward: bool = False
    opening_balance: Decimal | None = None
    note: str | None = None
    line: int = 0
    category_id: int | None = None

    @property
    def accrual(self) -> Decimal | None:
        if self.amount is None:
            return None
        months = PERIOD_MONTHS[self.period]
        return (self.amount / months).quantize(Decimal("0.01"))


@dataclass
class LoadResult:
    rows_read: int = 0
    plans: list[PlanRow] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    stopped: int = 0
    collapsed: int = 0
    snapped: int = 0
    shape: str = "long"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def monthly_total(self) -> Decimal:
        """Accrual of the plan in force at each category's LAST month here.

        Summing every row instead would double-count a category whose
        amount changes partway through the file, which is exactly the
        mistake this total exists to catch.
        """
        latest: dict[str, PlanRow] = {}
        for plan in self.plans:
            key = plan.category.lower()
            if key not in latest or plan.valid_from >= latest[key].valid_from:
                latest[key] = plan
        return sum((p.accrual or Decimal("0") for p in latest.values()),
                   Decimal("0"))


# ------------------------------------------------------------------ parsing
def parse_month_raw(value: str | None,
                    default_year: int | None = None) -> date:
    """Any reasonable spelling of a month, with whatever day it carried.

    Kept separate from parse_month so a caller can notice that the file
    said the 15th and report the snap, rather than silently moving it.
    """
    text = str(value or "").strip()
    if not text:
        raise BudgetError("no month given")
    for fmt in MONTH_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if default_year:
        for fmt in BARE_MONTH_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                return date(default_year, parsed.month, 1)
            except ValueError:
                continue
    raise BudgetError(f"cannot read {value!r} as a month")


def parse_month(value: str | None, default_year: int | None = None) -> date:
    """A month, snapped to the first - which is what the schema requires."""
    return parse_month_raw(value, default_year).replace(day=1)


def month_or_none(value: str | None,
                  default_year: int | None = None) -> date | None:
    try:
        return parse_month(value, default_year)
    except BudgetError:
        return None


def parse_period(value: str | None) -> str:
    text = str(value or "").strip().lower()
    for ch in " -_/.":
        text = text.replace(ch, "")
    if not text:
        return "monthly"
    if text in PERIOD_ALIASES:
        return PERIOD_ALIASES[text]
    raise BudgetError(
        f"{value!r} is not a period. Use monthly, quarterly, semiannual or "
        f"annual (or a number of months: 1, 3, 6, 12)")


def parse_bool(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise BudgetError(f"{value!r} is not a yes or a no")


def _read(path: Path) -> tuple[list[dict], list[str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fields = [(f or "").strip() for f in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): v for k, v in r.items()} for r in reader]
    return rows, fields


def detect_shape(fields: list[str], default_year: int | None = None) -> str:
    """Two or more month-shaped headers means the grid, not the list."""
    months = sum(1 for f in fields if month_or_none(f, default_year))
    return "wide" if months >= 2 else "long"


# ------------------------------------------------------------- the two shapes
def read_long(rows: list[dict], fields: list[str],
              result: LoadResult) -> list[PlanRow]:
    col = {}
    for f in fields:
        key = ATTR_HEADERS.get(f.strip().lower())
        if key and key not in col:
            col[key] = f
        elif f.strip().lower() in CATEGORY_HEADERS and "category" not in col:
            col["category"] = f
    if "category" not in col:
        col["category"] = fields[0] if fields else ""
    for required in ("effective_from", "amount"):
        if required not in col:
            result.errors.append(
                f"the file has no {required} column. It has: {fields}")
            return []

    plans = []
    for i, row in enumerate(rows, start=2):
        name = str(row.get(col["category"], "") or "").strip()
        if not name:
            continue
        try:
            raw = parse_month_raw(row.get(col["effective_from"]))
            month = raw.replace(day=1)
            if raw.day != 1:
                result.snapped += 1
            amount = parse_money(row.get(col["amount"]))
            if amount is not None and amount < 0:
                raise BudgetError(
                    f"amount {amount} is negative; a budget is a limit, not a "
                    f"signed transaction")
            plans.append(PlanRow(
                category=name,
                valid_from=month,
                amount=amount,
                period=parse_period(row.get(col.get("period", ""))),
                carry_forward=parse_bool(row.get(col.get("carry_forward", ""))),
                opening_balance=parse_money(
                    row.get(col.get("opening_balance", ""))),
                note=str(row.get(col.get("note", ""), "") or "").strip() or None,
                line=i,
            ))
        except (BudgetError, ValueError) as exc:
            result.errors.append(f"line {i} ({name}): {exc}")
    return plans


def read_wide(rows: list[dict], fields: list[str], result: LoadResult,
              default_year: int | None = None) -> list[PlanRow]:
    month_cols = [(f, month_or_none(f, default_year)) for f in fields]
    months = sorted(((f, m) for f, m in month_cols if m), key=lambda x: x[1])
    others = [f for f, m in month_cols if not m]

    cat_col = next((f for f in others if f.strip().lower() in CATEGORY_HEADERS),
                   others[0] if others else "")
    attrs = {}
    for f in others:
        key = ATTR_HEADERS.get(f.strip().lower())
        if key in ("period", "carry_forward", "opening_balance", "note"):
            attrs.setdefault(key, f)

    plans = []
    for i, row in enumerate(rows, start=2):
        name = str(row.get(cat_col, "") or "").strip()
        if not name:
            continue
        try:
            period = parse_period(row.get(attrs.get("period", "")))
            carry = parse_bool(row.get(attrs.get("carry_forward", "")))
            opening = parse_money(row.get(attrs.get("opening_balance", "")))
            note = str(row.get(attrs.get("note", ""), "") or "").strip() or None
        except (BudgetError, ValueError) as exc:
            result.errors.append(f"line {i} ({name}): {exc}")
            continue

        if period != "monthly":
            result.warnings.append(
                f"{name}: period is {period} in a month-by-month file. The "
                f"value in each month column is read as the amount per "
                f"{period[:-2] if period.endswith('ly') else period} period, "
                f"and a blank month after it stops the plan - so fill the "
                f"whole row, or put this category in a long-format file.")

        # Walk the months in order and keep only the changes.
        previous: Decimal | None = None
        started = False
        first = True
        for column, month in months:
            try:
                amount = parse_money(row.get(column))
            except ValueError as exc:
                result.errors.append(
                    f"line {i} ({name}, {column}): {exc}")
                continue
            if amount is not None and amount < 0:
                result.errors.append(
                    f"line {i} ({name}, {column}): amount {amount} is "
                    f"negative; a budget is a limit, not a signed transaction")
                continue
            if amount is None and not started:
                continue                      # before the budget began
            if started and amount == previous:
                result.collapsed += 1
                continue                      # unchanged: no new plan row
            plans.append(PlanRow(
                category=name, valid_from=month, amount=amount,
                period=period, carry_forward=carry,
                # A seed belongs on the first row only; repeating it would
                # re-baseline the envelope every time the amount changed.
                opening_balance=opening if first else None,
                note=note, line=i,
            ))
            previous, started, first = amount, True, False
    return plans


# -------------------------------------------------------------------- loading
def _resolve(conn: psycopg.Connection, plans: list[PlanRow],
             result: LoadResult) -> None:
    from .categories import resolve_existing

    cache: dict[str, tuple[int | None, str | None]] = {}
    for plan in plans:
        key = plan.category.lower()
        if key not in cache:
            cache[key] = resolve_existing(conn, plan.category)
        category_id, warning = cache[key]
        if category_id is None:
            result.errors.append(
                f"line {plan.line}: no category called {plan.category!r}. "
                f"Budgets never create categories - check the spelling, or "
                f"add it with a transaction import first.")
            continue
        if warning and warning not in result.warnings:
            result.warnings.append(warning)
        plan.category_id = category_id


def _classify(conn: psycopg.Connection, plans: list[PlanRow],
              result: LoadResult) -> None:
    """Count what the load would do, before doing it."""
    for plan in plans:
        if plan.category_id is None:
            continue
        existing = conn.execute(
            "SELECT amount, period, carry_forward, opening_balance, note "
            "FROM budget_plans WHERE category_id = %s AND valid_from = %s",
            (plan.category_id, plan.valid_from),
        ).fetchone()
        if plan.amount is None:
            result.stopped += 1
        if existing is None:
            result.inserted += 1
        elif (existing["amount"] == plan.amount
                and existing["period"] == plan.period
                and existing["carry_forward"] == plan.carry_forward
                and existing["opening_balance"] == plan.opening_balance):
            result.unchanged += 1
        else:
            result.updated += 1


def load(conn: psycopg.Connection, path: Path, *, shape: str = "auto",
         year: int | None = None, skip_bad: bool = False,
         dry_run: bool = False) -> LoadResult:
    """Read a budget file and upsert it into budget_plans."""
    result = LoadResult()
    rows, fields = _read(path)
    result.rows_read = len(rows)
    if not fields:
        result.errors.append("the file has no header row")
        return result

    result.shape = detect_shape(fields, year) if shape == "auto" else shape
    if result.shape == "wide":
        plans = read_wide(rows, fields, result, year)
    else:
        plans = read_long(rows, fields, result)

    _resolve(conn, plans, result)
    # A later row for the same month wins, which is what a reader of the
    # file would expect; without this the upsert would hit the same key
    # twice in one statement.
    deduped: dict[tuple[int, date], PlanRow] = {}
    for plan in plans:
        if plan.category_id is not None:
            deduped[(plan.category_id, plan.valid_from)] = plan
    result.plans = sorted(deduped.values(),
                          key=lambda p: (p.category.lower(), p.valid_from))

    if result.errors and not skip_bad:
        return result

    _classify(conn, result.plans, result)
    if dry_run:
        return result

    for plan in result.plans:
        conn.execute(
            """
            INSERT INTO budget_plans (category_id, valid_from, amount, period,
                                      carry_forward, opening_balance, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (category_id, valid_from) DO UPDATE
                SET amount          = excluded.amount,
                    period          = excluded.period,
                    carry_forward   = excluded.carry_forward,
                    opening_balance = excluded.opening_balance,
                    note            = coalesce(excluded.note, budget_plans.note)
            """,
            (plan.category_id, plan.valid_from, plan.amount, plan.period,
             plan.carry_forward, plan.opening_balance, plan.note),
        )
    return result


def current(conn: psycopg.Connection) -> list[dict]:
    """The plan in force this month, for `plumb budget`."""
    return conn.execute(
        """
        SELECT category, period, carry_forward, budget, spent, variance,
               pct_used, envelope_balance, months_of_cover
        FROM v_budget_status
        ORDER BY carry_forward, budget DESC NULLS LAST
        """
    ).fetchall()


def history(conn: psycopg.Connection, limit: int = 50) -> list[dict]:
    return conn.execute(
        """
        SELECT c.name AS category,
               to_char(p.valid_from, 'YYYY-MM') AS from_month,
               p.amount, p.period, p.carry_forward, p.opening_balance,
               round(p.amount / budget_period_months(p.period), 2) AS monthly
        FROM budget_plans p
        JOIN categories c ON c.id = p.category_id
        ORDER BY c.name, p.valid_from DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
