"""Turn one raw CSV row into one transaction, per a Mapping.

This module is pure: no database, no filesystem. That is deliberate -
the sign conventions and date handling are the easiest thing in the
project to get quietly wrong, so they live where unit tests can reach
them (see etl/tests/).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .mapping import Mapping

CURRENCY_JUNK = re.compile(r"[^\d\-.,()]")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class RowError(ValueError):
    """A row that cannot be interpreted; recorded and skipped, not fatal."""


@dataclass
class NormalizedTxn:
    posted_on: date
    amount: Decimal
    external_id: str
    transacted_on: date | None = None
    raw_description: str | None = None
    statement_balance: Decimal | None = None
    status: str = "posted"
    #: One of Withdraw / Deposit / Transfer / Receive, when the source says.
    direction: str | None = None
    #: Set when the file carries its own account column.
    account_name: str | None = None
    #: A category name or path from the row, resolved by the importer.
    category_path: str | None = None
    merchant_name: str | None = None
    merchant_ref: str | None = None
    location_ref: str | None = None
    tax: Decimal = Decimal("0")
    discount: Decimal = Decimal("0")
    #: Where it happened, when the export says. Resolved to a location row
    #: by the importer; None when the CSV carries no place at all.
    location: "object | None" = None

    def as_params(self) -> dict:
        return {
            "posted_on": self.posted_on,
            "transacted_on": self.transacted_on,
            "amount": self.amount,
            "raw_description": self.raw_description,
            "statement_balance": self.statement_balance,
            "status": self.status,
            "external_id": self.external_id,
            "direction": self.direction,
            "tax": self.tax,
            "discount": self.discount,
            "source_merchant_ref": self.merchant_ref,
            "source_location_ref": self.location_ref,
        }


def parse_money(value: str | None) -> Decimal | None:
    """'$1,234.56' -> 1234.56 ; '(45.00)' -> -45.00 ; '' -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    original = text
    text = CURRENCY_JUNK.sub("", text)
    text = text.replace("(", "").replace(")", "")

    # Non-empty but digit-free means the column held something that isn't
    # money at all. Treating that as "blank" would silently drop a row, so
    # it is an error the import records against the line.
    if not any(ch.isdigit() for ch in text):
        raise RowError(f"cannot parse amount {original!r}")

    # Thousands separators only; this project assumes '.' is the decimal point.
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif text.count(",") == 1 and len(text.split(",")[-1]) in (1, 2):
        text = text.replace(",", ".")      # European style, e.g. 12,50
    else:
        text = text.replace(",", "")

    if not text or text in {"-", ".", "-."}:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise RowError(f"cannot parse amount {value!r}") from exc

    if negative:
        amount = -abs(amount)
    return amount.quantize(Decimal("0.01"))


def parse_date(value: str | None, formats: list[str]) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if ISO_DATE.match(text):                # last resort: leading ISO date
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    raise RowError(f"cannot parse date {value!r} with formats {formats}")


def _signed_amount(row: dict[str, str], m: Mapping) -> tuple[Decimal, str | None]:
    """The signed amount, and the canonical direction when the file gives one."""
    spec = m.amount
    direction = None

    if spec.mode == "signed":
        amount = parse_money(row.get(m.column("amount") or ""))
        if amount is None:
            raise RowError("empty amount")

    elif spec.mode == "debit_credit":
        debit = parse_money(row.get(spec.debit_column or "")) or Decimal("0")
        credit = parse_money(row.get(spec.credit_column or "")) or Decimal("0")
        if debit and credit:
            raise RowError("row has both a debit and a credit")
        if not debit and not credit:
            raise RowError("row has neither a debit nor a credit")
        # A debit is money leaving you, so it is negative in this schema.
        amount = -abs(debit) if debit else abs(credit)

    elif spec.mode == "direction":
        magnitude = parse_money(row.get(m.column("amount") or ""))
        if magnitude is None:
            raise RowError("empty amount")
        raw = str(row.get(m.column("direction") or "", "") or "").strip()
        direction = spec.resolve_direction(raw)
        if direction is None:
            raise RowError(
                f"unrecognised direction {raw!r}; add it to amount.direction_map"
            )
        # Withdraw and Transfer take money out; Deposit and Receive bring it
        # in. The magnitude's own sign is ignored, since the column is
        # unsigned by definition in this mode - but a parenthesised or
        # negative value alongside an outward direction is a contradiction
        # worth refusing rather than quietly resolving.
        if magnitude < 0 and direction in ("Deposit", "Receive"):
            raise RowError(
                f"direction {direction!r} says money came in, but the amount is "
                f"negative ({magnitude}) - check the mapping"
            )
        amount = -abs(magnitude) if direction in ("Withdraw", "Transfer") else abs(magnitude)

    else:  # magnitude_with_type
        magnitude = parse_money(row.get(m.column("amount") or ""))
        if magnitude is None:
            raise RowError("empty amount")
        kind = str(row.get(spec.type_column or "", "")).strip().lower()
        if kind in spec.debit_values:
            amount = -abs(magnitude)
        elif not spec.credit_values or kind in spec.credit_values:
            amount = abs(magnitude)
        else:
            raise RowError(f"unrecognised transaction type {kind!r}")

    if spec.flip_sign:
        amount = -amount
        if direction:        # keep the pair consistent for the CHECK constraint
            direction = {
                "Withdraw": "Deposit", "Deposit": "Withdraw",
                "Transfer": "Receive", "Receive": "Transfer",
            }[direction]
    return amount.quantize(Decimal("0.01")), direction


def _description(row: dict[str, str], m: Mapping) -> str | None:
    parts = []
    for logical in ("description", "memo"):
        col = m.column(logical)
        if col:
            text = str(row.get(col, "") or "").strip()
            if text and text not in parts:
                parts.append(text)
    return " | ".join(parts) or None


def fallback_external_id(
    account: str, posted: date, amount: Decimal, description: str | None, occurrence: int
) -> str:
    """Deterministic id for banks that don't supply one.

    Includes an occurrence counter so two genuinely identical charges on
    the same day both load, while re-importing the same file still
    produces the same ids and therefore inserts nothing new.
    """
    payload = "|".join(
        [account, posted.isoformat(), f"{amount:.2f}", (description or ""), str(occurrence)]
    )
    return "h:" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def _popularity(row: dict[str, str], m: Mapping) -> Decimal | None:
    """A score from a location API, describing the place not the payment."""
    column = m.column("popularity")
    if not column:
        return None
    raw = str(row.get(column, "") or "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RowError(f"cannot parse popularity {raw!r}") from exc
    if value < 0:
        raise RowError(f"popularity {value} is negative")
    return value.quantize(Decimal("0.000000001"))


def _location(row: dict[str, str], m: Mapping):
    """Pull coordinates and address parts out of a row, per the mapping."""
    from .locations import (
        LocationFields,
        normalize_country,
        parse_coordinate,
        parse_coordinate_pair,
        validate,
    )

    spec = m.location
    lat = lon = None

    combined = m.column("coordinates")
    if combined:
        pair = parse_coordinate_pair(row.get(combined), spec.precision)
        if pair:
            lat, lon = pair
    elif m.column("latitude"):
        lat = parse_coordinate(row.get(m.column("latitude") or ""), spec.precision)
        lon = parse_coordinate(row.get(m.column("longitude") or ""), spec.precision)

    if spec.swap_lat_lon:
        lat, lon = lon, lat

    # Most exports write 0,0 when the device had no fix.
    if spec.drop_zero_zero and lat == 0 and lon == 0:
        lat = lon = None

    validate(lat, lon)

    def text(logical: str) -> str | None:
        col = m.column(logical)
        if not col:
            return None
        return str(row.get(col, "") or "").strip() or None

    loc = LocationFields(
        match_radius_m=spec.match_radius_m,
        latitude=lat,
        longitude=lon,
        label=text("place"),
        address=text("address"),
        city=text("city"),
        state=text("state"),
        zip=text("zip"),
        country=normalize_country(text("country")),
        popularity=_popularity(row, m),
    )
    return None if loc.is_empty() else loc


def normalize_row(
    row: dict[str, str], m: Mapping, seen: dict[tuple, int] | None = None
) -> NormalizedTxn:
    """Normalize one row. `seen` tracks duplicates within a single file."""
    posted = parse_date(row.get(m.column("posted_on") or ""), m.date_formats)
    if posted is None:
        raise RowError("missing posted date")

    transacted = None
    if m.column("transacted_on"):
        transacted = parse_date(row.get(m.column("transacted_on") or ""), m.date_formats)

    amount, direction = _signed_amount(row, m)
    description = _description(row, m)

    def text(logical: str) -> str | None:
        col = m.column(logical)
        if not col:
            return None
        return str(row.get(col, "") or "").strip() or None

    def money(logical: str) -> Decimal:
        col = m.column(logical)
        if not col:
            return Decimal("0")
        return parse_money(row.get(col)) or Decimal("0")

    balance = None
    if m.column("balance"):
        balance = parse_money(row.get(m.column("balance") or ""))

    status = "posted"
    if m.column("status") and m.pending_values:
        if str(row.get(m.column("status") or "", "")).strip().lower() in m.pending_values:
            status = "pending"

    external_id = None
    if m.column("external_id"):
        external_id = str(row.get(m.column("external_id") or "", "") or "").strip() or None

    if external_id is None:
        key = (posted, amount, description)
        counter = seen if seen is not None else {}
        occurrence = counter.get(key, 0)
        counter[key] = occurrence + 1
        external_id = fallback_external_id(
            m.account.name, posted, amount, description, occurrence
        )

    location = _location(row, m) if m.has_location else None

    return NormalizedTxn(
        posted_on=posted,
        transacted_on=transacted,
        amount=amount,
        raw_description=description,
        statement_balance=balance,
        status=status,
        external_id=external_id,
        location=location,
        direction=direction,
        account_name=text("account"),
        category_path=text("category"),
        merchant_name=text("merchant"),
        merchant_ref=text("merchant_ref"),
        location_ref=text("location_ref"),
        tax=money("tax"),
        discount=money("discount"),
    )


def location_from_row(row: dict[str, str], m: Mapping):
    """Public form of the location extractor, used by the backfill."""
    return _location(row, m) if m.has_location else None
