"""Per-institution CSV mappings, loaded from mappings/*.yml.

A mapping says which columns mean what, how dates are written, and how
the bank expresses direction of money. Adding a new bank is a YAML file,
not a code change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config

AMOUNT_MODES = {"signed", "debit_credit", "magnitude_with_type", "direction"}

#: The four canonical directions. Withdraw/Transfer take money out of an
#: account, Deposit/Receive bring it in; Transfer/Receive are internal.
DIRECTIONS = {"Withdraw", "Deposit", "Transfer", "Receive"}

#: Words seen in the wild, lowercased, mapped onto the four. Extend per
#: mapping with amount.direction_map rather than editing this.
DEFAULT_DIRECTION_MAP = {
    "withdraw": "Withdraw", "withdrawal": "Withdraw", "debit": "Withdraw",
    "payment": "Withdraw", "purchase": "Withdraw", "fee": "Withdraw",
    "charge": "Withdraw", "out": "Withdraw",
    "deposit": "Deposit", "credit": "Deposit", "income": "Deposit",
    "refund": "Deposit", "interest": "Deposit", "in": "Deposit",
    "transfer": "Transfer", "transfer out": "Transfer", "xfer out": "Transfer",
    "transfer_out": "Transfer",
    "receive": "Receive", "received": "Receive", "transfer in": "Receive",
    "xfer in": "Receive", "transfer_in": "Receive",
}

VALID_ACCOUNT_TYPES = {
    "Checking", "Savings", "Credit", "Investment", "Loan",
    "Cash", "Retirement", "Crypto", "Other",
}


@dataclass
class AccountSpec:
    name: str
    type: str = "Checking"
    institution: str | None = None
    last4: str | None = None
    currency: str = "USD"


@dataclass
class LocationSpec:
    """How (and whether) a CSV carries where the transaction happened."""
    precision: int = 6          # decimal places stored (6 = full fidelity)
    match_radius_m: int = 25    # a reading this close to a known place IS that
                                # place. Rounding coordinates cannot do this
                                # job: two readings metres apart can still land
                                # either side of a rounding boundary.
    swap_lat_lon: bool = False  # for exports with the columns the wrong way round
    drop_zero_zero: bool = True # treat 0,0 as "unknown", which is how most
                                # exports spell a missing fix


@dataclass
class CategorySpec:
    """How a category column is turned into a row in the category tree."""
    separator: str = ">"        # "Food > Groceries" builds/walks the path
    parent: str = "Imported"    # where bare leaf names land when unknown
    on_unknown: str = "create"  # create | error | ignore
    #: What to do when a bare name matches several categories.
    #: topmost = use the one closest to the root and warn (the default,
    #: because a CSV carrying only a leaf name usually means the broader
    #: category); error = refuse the row and say so.
    on_ambiguous: str = "topmost"   # topmost | error


@dataclass
class AmountSpec:
    mode: str = "signed"
    flip_sign: bool = False
    debit_column: str | None = None
    credit_column: str | None = None
    type_column: str | None = None
    debit_values: list[str] = field(default_factory=list)
    credit_values: list[str] = field(default_factory=list)
    #: mode "direction": extra or overriding words for the four directions
    direction_map: dict[str, str] = field(default_factory=dict)

    def resolve_direction(self, raw: str) -> str | None:
        """Map a source word onto one of the four canonical directions."""
        key = (raw or "").strip().lower()
        if not key:
            return None
        merged = {**DEFAULT_DIRECTION_MAP,
                  **{k.strip().lower(): v for k, v in self.direction_map.items()}}
        return merged.get(key)


@dataclass
class Mapping:
    name: str
    account: AccountSpec
    columns: dict[str, str | None]
    date_formats: list[str]
    amount: AmountSpec
    location: LocationSpec = field(default_factory=LocationSpec)
    category: CategorySpec = field(default_factory=CategorySpec)
    #: Account name -> account type, for files whose rows span accounts.
    account_types: dict[str, str] = field(default_factory=dict)
    encoding: str = "utf-8-sig"
    delimiter: str = ","
    skip_rows: int = 0
    pending_values: list[str] = field(default_factory=list)

    def column(self, logical: str) -> str | None:
        value = self.columns.get(logical)
        return value or None

    def validate(self) -> None:
        if self.amount.mode not in AMOUNT_MODES:
            raise ValueError(
                f"{self.name}: amount.mode must be one of {sorted(AMOUNT_MODES)}"
            )
        if self.account.type not in VALID_ACCOUNT_TYPES:
            raise ValueError(
                f"{self.name}: account.type '{self.account.type}' is not one of "
                f"{sorted(VALID_ACCOUNT_TYPES)}"
            )
        if not self.column("posted_on"):
            raise ValueError(f"{self.name}: columns.posted_on is required")
        if self.amount.mode == "signed" and not self.column("amount"):
            raise ValueError(f"{self.name}: columns.amount is required for mode 'signed'")
        if self.amount.mode == "debit_credit" and not (
            self.amount.debit_column and self.amount.credit_column
        ):
            raise ValueError(
                f"{self.name}: amount.debit_column and amount.credit_column are "
                f"required for mode 'debit_credit'"
            )
        if self.amount.mode == "direction":
            if not self.column("direction"):
                raise ValueError(
                    f"{self.name}: columns.direction is required for mode "
                    f"'direction'"
                )
            if not self.column("amount"):
                raise ValueError(
                    f"{self.name}: columns.amount is required for mode 'direction'"
                )
            unknown = sorted(
                v for v in self.amount.direction_map.values() if v not in DIRECTIONS
            )
            if unknown:
                raise ValueError(
                    f"{self.name}: amount.direction_map values {unknown} are not "
                    f"one of {sorted(DIRECTIONS)}"
                )
        if self.amount.mode == "magnitude_with_type" and not (
            self.amount.type_column and self.amount.debit_values
        ):
            raise ValueError(
                f"{self.name}: amount.type_column and amount.debit_values are "
                f"required for mode 'magnitude_with_type'"
            )
        if not self.date_formats:
            raise ValueError(f"{self.name}: date_format is required")

        has_lat = bool(self.column("latitude"))
        has_lon = bool(self.column("longitude"))
        if has_lat != has_lon:
            raise ValueError(
                f"{self.name}: columns.latitude and columns.longitude must be "
                f"given together (or use columns.coordinates for one combined "
                f"column)"
            )
        if self.column("coordinates") and has_lat:
            raise ValueError(
                f"{self.name}: use either columns.coordinates or "
                f"columns.latitude + columns.longitude, not both"
            )
        if not 0 <= self.location.precision <= 6:
            raise ValueError(f"{self.name}: location.precision must be 0-6")
        bad_types = sorted(
            t for t in self.account_types.values() if t not in VALID_ACCOUNT_TYPES
        )
        if bad_types:
            raise ValueError(
                f"{self.name}: account_types values {bad_types} are not one of "
                f"{sorted(VALID_ACCOUNT_TYPES)}"
            )
        if self.category.on_unknown not in {"create", "error", "ignore"}:
            raise ValueError(
                f"{self.name}: category.on_unknown must be create, error or ignore"
            )
        if self.category.on_ambiguous not in {"topmost", "error"}:
            raise ValueError(
                f"{self.name}: category.on_ambiguous must be topmost or error"
            )
        if not 0 <= self.location.match_radius_m <= 5000:
            raise ValueError(
                f"{self.name}: location.match_radius_m must be 0-5000 metres"
            )

    def account_type_for(self, name: str) -> tuple[str, bool]:
        """(type, guessed) for an account name seen in a row.

        An explicit account_types entry wins. Otherwise the name is
        inspected, and the caller reports the guess so a wrong one is
        visible rather than silent.
        """
        if name in self.account_types:
            return self.account_types[name], False

        lowered = name.lower()
        for needle, kind in (
            ("visa", "Credit"), ("mastercard", "Credit"), ("amex", "Credit"),
            ("credit card", "Credit"), ("card", "Credit"), ("credit", "Credit"),
            ("savings", "Savings"), ("save", "Savings"),
            ("checking", "Checking"), ("chequing", "Checking"),
            ("money market", "Savings"), ("cash", "Cash"),
            ("loan", "Loan"), ("mortgage", "Loan"),
            ("brokerage", "Investment"), ("invest", "Investment"),
            ("401k", "Retirement"), ("ira", "Retirement"),
            ("crypto", "Crypto"), ("wallet", "Crypto"),
        ):
            if needle in lowered:
                return kind, kind != self.account.type

        return self.account.type, False

    @property
    def has_location(self) -> bool:
        return bool(
            self.column("coordinates")
            or self.column("latitude")
            or self.column("address")
            or self.column("city")
            or self.column("zip")
        )


def from_dict(data: dict, name: str) -> Mapping:
    acct = data.get("account") or {}
    if not acct.get("name"):
        raise ValueError(f"{name}: account.name is required")

    raw_formats = data.get("date_format") or []
    if isinstance(raw_formats, str):
        raw_formats = [raw_formats]

    amount_data = data.get("amount") or {}
    csv_data = data.get("csv") or {}
    location_data = data.get("location") or {}
    category_data = data.get("category") or {}

    mapping = Mapping(
        name=data.get("name") or name,
        account=AccountSpec(
            name=acct["name"],
            type=acct.get("type", "Checking"),
            institution=acct.get("institution"),
            last4=str(acct["last4"]) if acct.get("last4") else None,
            currency=acct.get("currency", "USD"),
        ),
        columns=data.get("columns") or {},
        date_formats=list(raw_formats),
        amount=AmountSpec(
            mode=amount_data.get("mode", "signed"),
            flip_sign=bool(amount_data.get("flip_sign", False)),
            debit_column=amount_data.get("debit_column"),
            credit_column=amount_data.get("credit_column"),
            type_column=amount_data.get("type_column"),
            debit_values=[str(v).strip().lower()
                          for v in (amount_data.get("debit_values") or [])],
            credit_values=[str(v).strip().lower()
                           for v in (amount_data.get("credit_values") or [])],
            direction_map={str(k): str(v)
                           for k, v in (amount_data.get("direction_map") or {}).items()},
        ),
        location=LocationSpec(
            precision=int(location_data.get("precision", 6)),
            match_radius_m=int(location_data.get("match_radius_m", 25)),
            swap_lat_lon=bool(location_data.get("swap_lat_lon", False)),
            drop_zero_zero=bool(location_data.get("drop_zero_zero", True)),
        ),
        category=CategorySpec(
            separator=str(category_data.get("separator", ">")),
            parent=str(category_data.get("parent", "Imported")),
            on_unknown=str(category_data.get("on_unknown", "create")),
            on_ambiguous=str(category_data.get("on_ambiguous", "topmost")),
        ),
        account_types={str(k): str(v)
                       for k, v in (data.get("account_types") or {}).items()},
        encoding=csv_data.get("encoding", "utf-8-sig"),
        delimiter=csv_data.get("delimiter", ","),
        skip_rows=int(csv_data.get("skip_rows", 0)),
        pending_values=[str(v).strip().lower()
                        for v in (data.get("pending_values") or [])],
    )
    mapping.validate()
    return mapping


def load(name_or_path: str) -> Mapping:
    """Load by mapping name (mappings/<name>.yml) or by explicit path."""
    config.resolve_dirs()
    path = Path(name_or_path)
    if not path.is_file():
        for suffix in (".yml", ".yaml"):
            candidate = config.MAPPINGS_DIR / f"{name_or_path}{suffix}"
            if candidate.is_file():
                path = candidate
                break
        else:
            available = ", ".join(sorted(m.stem for m in available_mappings())) or "none"
            raise FileNotFoundError(
                f"no mapping '{name_or_path}' in {config.MAPPINGS_DIR} "
                f"(available: {available})"
            )
    data = yaml.safe_load(path.read_text()) or {}
    return from_dict(data, path.stem)


def available_mappings() -> list[Path]:
    config.resolve_dirs()
    if not config.MAPPINGS_DIR.is_dir():
        return []
    return sorted(
        p
        for p in config.MAPPINGS_DIR.iterdir()
        if p.suffix in (".yml", ".yaml") and not p.name.startswith("_")
    )
