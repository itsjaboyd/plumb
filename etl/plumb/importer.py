"""Read a CSV, normalize it, and load it into the database.

Every row is written to staging_rows exactly as it arrived, alongside the
transaction it produced (or the reason it produced none). The whole file
is one transaction: a file either imports or it doesn't.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from . import categories
from . import locations as locmod
from . import merchants
from .mapping import Mapping
from .normalize import RowError, normalize_row


@dataclass
class ImportResult:
    batch_id: int
    account: str
    rows_read: int = 0
    inserted: int = 0
    duplicate: int = 0
    errors: int = 0
    matched_merchants: int = 0
    locations: int = 0
    warnings: int = 0

    def summary(self) -> str:
        text = (
            f"{self.account}: {self.rows_read} rows read, {self.inserted} inserted, "
            f"{self.duplicate} already present, {self.errors} errors"
        )
        if self.warnings:
            text += f", {self.warnings} with warnings"
        return text


#: Money or a bare number in the account column means the row shifted.
IMPLAUSIBLE_ACCOUNT = re.compile(r"^[\s$€£]*[\d.,]+\s*$")

#: Key under which csv.DictReader parks surplus values in a ragged row.
EXTRA_FIELDS = "__extra_fields__"


def check_row_shape(row: dict, m: Mapping) -> None:
    """Refuse a row whose field count doesn't match the header."""
    extra = row.get(EXTRA_FIELDS)
    if extra:
        raise RowError(
            f"row has {len(extra)} more field(s) than the header - usually an "
            f"unquoted comma in a number (e.g. $2,450.00). Surplus values: "
            f"{extra!r}"
        )
    required = [c for c in (m.column("posted_on"), m.column("amount")) if c]
    for column in required:
        if row.get(column) is None:
            raise RowError(
                f"row is missing the {column!r} field - it has fewer columns "
                f"than the header"
            )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_account(
    conn: psycopg.Connection, m: Mapping, name: str | None = None
) -> int:
    """Look up an account by name, creating it if needed.

    `name` comes from the row when the file has an account column, which
    is what lets one ledger-style export span several accounts.
    """
    name = name or m.account.name
    if not IMPLAUSIBLE_ACCOUNT.search(name) is None:
        raise ValueError(
            f"{name!r} does not look like an account name, so no account was "
            f"created. This is almost always a shifted column - check for "
            f"unquoted commas in the CSV."
        )
    row = conn.execute(
        "SELECT id FROM accounts WHERE name = %s", (name,)
    ).fetchone()
    if row:
        return row["id"]

    if name == m.account.name:
        account_type, guessed = m.account.type, False
        institution, last4 = m.account.institution, m.account.last4
    else:
        account_type, guessed = m.account_type_for(name)
        institution, last4 = m.account.institution, None

    row = conn.execute(
        """
        INSERT INTO accounts (name, account_type, institution, last4, currency)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (name, account_type, institution, last4, m.account.currency),
    ).fetchone()
    note = " - guessed from the name, change it if wrong" if guessed else ""
    print(f"  created account {name!r} ({account_type}{note})")
    return row["id"]


def read_rows(path: Path, m: Mapping) -> tuple[list[dict], list[str]]:
    text = path.read_text(encoding=m.encoding, errors="replace")
    handle = io.StringIO(text)
    for _ in range(m.skip_rows):
        handle.readline()
    # restkey catches rows with MORE fields than the header, which is what
    # an unquoted thousands separator ("$2,450.00") looks like. Without
    # this the columns silently shift and every later field is wrong.
    reader = csv.DictReader(handle, delimiter=m.delimiter,
                            restkey=EXTRA_FIELDS, restval=None)
    fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    rows = [
        {(k.strip() if k else k): v for k, v in row.items()}
        for row in reader
    ]
    return rows, fieldnames


def check_expected_columns(fieldnames: list[str], m: Mapping) -> None:
    wanted = {
        v for k, v in m.columns.items() if v and isinstance(v, str)
    } | {
        c for c in (m.amount.debit_column, m.amount.credit_column,
                    m.amount.type_column) if c
    }
    missing = sorted(w for w in wanted if w not in fieldnames)
    if missing:
        raise ValueError(
            f"{m.name}: columns {missing} are not in the CSV. "
            f"The file has: {fieldnames}"
        )


def already_imported(conn: psycopg.Connection, digest: str) -> dict | None:
    return conn.execute(
        "SELECT id, source_file, started_at FROM import_batches "
        "WHERE file_sha256 = %s ORDER BY id DESC LIMIT 1",
        (digest,),
    ).fetchone()


def import_file(
    conn: psycopg.Connection,
    path: Path,
    m: Mapping,
    force: bool = False,
    categorize: bool = True,
) -> ImportResult:
    digest = file_sha256(path)
    prior = already_imported(conn, digest)
    if prior and not force:
        raise RuntimeError(
            f"{path.name} is byte-identical to import #{prior['id']} "
            f"({prior['started_at']:%Y-%m-%d %H:%M}). Use --force to run it anyway; "
            f"duplicate rows would be skipped regardless."
        )

    rows, fieldnames = read_rows(path, m)
    check_expected_columns(fieldnames, m)
    default_account_id = ensure_account(conn, m)

    batch = conn.execute(
        """
        INSERT INTO import_batches (source_file, file_sha256, mapping, account_id, rows_read)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (str(path), digest, m.name, default_account_id, len(rows)),
    ).fetchone()
    result = ImportResult(batch_id=batch["id"], account=m.account.name, rows_read=len(rows))

    seen: dict[tuple, int] = {}
    loc_cache: dict[tuple, int] = {}
    account_cache: dict[str, int] = {}
    category_cache: dict[str, tuple[int | None, str | None]] = {}
    seen_warnings: set[str] = set()
    merchant_cache: dict[str, int] = {}
    ref_cache: dict[str, int | None] = {}
    source = f"csv:{m.name}"

    for line_no, row in enumerate(rows, start=2 + m.skip_rows):  # header is line 1
        try:
            check_row_shape(row, m)
            txn = normalize_row(row, m, seen)
        except RowError as exc:
            conn.execute(
                "INSERT INTO staging_rows (batch_id, line_no, raw, status, error) "
                "VALUES (%s, %s, %s, 'error', %s)",
                (result.batch_id, line_no, Jsonb(row), str(exc)),
            )
            result.errors += 1
            continue

        # The account can vary row by row in a ledger-style export.
        try:
            if txn.account_name:
                if txn.account_name not in account_cache:
                    account_cache[txn.account_name] = ensure_account(
                        conn, m, txn.account_name
                    )
                account_id = account_cache[txn.account_name]
            else:
                account_id = default_account_id

            category_id = None
            warning = None
            if txn.category_path:
                if txn.category_path not in category_cache:
                    category_cache[txn.category_path] = categories.resolve_or_create(
                        conn, txn.category_path, m.category
                    )
                category_id, warning = category_cache[txn.category_path]
                if warning and warning not in seen_warnings:
                    seen_warnings.add(warning)
                    print(f"  warning: {warning}")

            merchant_id = None
            if txn.merchant_name:
                if txn.merchant_name not in merchant_cache:
                    merchant_cache[txn.merchant_name] = merchants.ensure_merchant(
                        conn, txn.merchant_name
                    )
                merchant_id = merchant_cache[txn.merchant_name]
            elif txn.merchant_ref:
                # Only matches if the source's merchant table has been
                # imported (plumb import-refs); otherwise the ref is kept
                # on the row and resolved later.
                if txn.merchant_ref not in ref_cache:
                    found = conn.execute(
                        "SELECT id FROM merchants WHERE external_ref = %s",
                        (txn.merchant_ref,),
                    ).fetchone()
                    ref_cache[txn.merchant_ref] = found["id"] if found else None
                merchant_id = ref_cache[txn.merchant_ref]

            location_id = None
            if txn.location is not None:
                location_id = locmod.resolve(conn, txn.location, loc_cache)
            if location_id is None and txn.location_ref:
                found = conn.execute(
                    "SELECT id FROM locations WHERE external_ref = %s",
                    (txn.location_ref,),
                ).fetchone()
                location_id = found["id"] if found else None
            if location_id is not None:
                result.locations += 1
        except ValueError as exc:
            conn.execute(
                "INSERT INTO staging_rows (batch_id, line_no, raw, status, error) "
                "VALUES (%s, %s, %s, 'error', %s)",
                (result.batch_id, line_no, Jsonb(row), str(exc)),
            )
            result.errors += 1
            continue

        params = txn.as_params()
        inserted = conn.execute(
            """
            INSERT INTO transactions
                (account_id, posted_on, transacted_on, amount, raw_description,
                 statement_balance, status, source, external_id, import_batch_id,
                 location_id, category_id, merchant_id, direction, tax, discount,
                 source_merchant_ref, source_location_ref)
            VALUES
                (%(account_id)s, %(posted_on)s, %(transacted_on)s, %(amount)s,
                 %(raw_description)s, %(statement_balance)s, %(status)s, %(source)s,
                 %(external_id)s, %(batch_id)s, %(location_id)s, %(category_id)s,
                 %(merchant_id)s, %(direction)s, %(tax)s, %(discount)s,
                 %(source_merchant_ref)s, %(source_location_ref)s)
            ON CONFLICT (account_id, external_id) DO NOTHING
            RETURNING id
            """,
            {**params, "account_id": account_id, "source": source,
             "batch_id": result.batch_id, "location_id": location_id,
             "category_id": category_id, "merchant_id": merchant_id},
        ).fetchone()

        if inserted:
            result.inserted += 1
            if warning:
                result.warnings += 1
            conn.execute(
                "INSERT INTO staging_rows "
                "(batch_id, line_no, raw, transaction_id, status, warning) "
                "VALUES (%s, %s, %s, %s, 'inserted', %s)",
                (result.batch_id, line_no, Jsonb(row), inserted["id"], warning),
            )
        else:
            result.duplicate += 1
            # The row is already in the database, but this export may be
            # the first one to carry coordinates for it.
            if location_id is not None:
                conn.execute(
                    """
                    UPDATE transactions SET location_id = %s
                     WHERE account_id = %s AND external_id = %s
                       AND location_id IS NULL
                    """,
                    (location_id, account_id, txn.external_id),
                )
            conn.execute(
                "INSERT INTO staging_rows (batch_id, line_no, raw, status) "
                "VALUES (%s, %s, %s, 'duplicate')",
                (result.batch_id, line_no, Jsonb(row)),
            )

    conn.execute(
        """
        UPDATE import_batches
           SET rows_inserted = %s, rows_duplicate = %s, rows_error = %s,
               finished_at = now()
         WHERE id = %s
        """,
        (result.inserted, result.duplicate, result.errors, result.batch_id),
    )

    if categorize:
        result.matched_merchants = merchants.apply_aliases(conn)

    return result
