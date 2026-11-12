"""Re-run the raw rows kept in staging_rows against today's rules.

This is why every imported row is stored verbatim. When a mapping gains a
column, a merchant alias is added, or a resolution rule changes - like
ambiguous category names now picking the match closest to the root - the
transactions already in the database were derived under the old rules.
Re-importing the file does nothing, because those rows already exist.
Replay re-derives them from the original CSV rows instead.

Two things happen:

  * rows that produced a transaction are UPDATED IN PLACE. The transaction
    keeps its id, so receipt line items, tags and transfer pairings all
    survive. Nothing is deleted.
  * rows that previously failed are retried, and may now insert. A parse
    rule you fixed is exactly the case this covers.

Money is left alone unless you ask for it (`fields="all"`), because a
replay is usually about classification, and silently rewriting amounts is
not something to do by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

from . import categories, locations as locmod, mapping as mapping_mod, merchants
from .importer import check_row_shape, ensure_account
from .normalize import RowError, normalize_row

#: Fields a replay may touch. "classification" never changes a number.
CLASSIFICATION = ("category", "merchant", "location")
AMOUNTS = ("amount", "direction", "tax", "discount", "balance", "dates")
FIELD_SETS = {
    "classification": CLASSIFICATION,
    "amounts": AMOUNTS,
    "all": CLASSIFICATION + AMOUNTS,
}


@dataclass
class ReplayResult:
    batches: int = 0
    rows_examined: int = 0
    unchanged: int = 0
    category_changed: int = 0
    merchant_changed: int = 0
    location_changed: int = 0
    amount_changed: int = 0
    recovered: int = 0
    still_failing: int = 0
    skipped_batches: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (self.category_changed + self.merchant_changed
                + self.location_changed + self.amount_changed)

    def summary(self) -> str:
        return (
            f"{self.rows_examined} row(s) across {self.batches} batch(es): "
            f"{self.category_changed} category, {self.merchant_changed} merchant, "
            f"{self.location_changed} location, {self.amount_changed} amount "
            f"change(s); {self.recovered} previously-failed row(s) recovered, "
            f"{self.still_failing} still failing"
        )


def _batches(conn: psycopg.Connection, batch_id: int | None) -> list[dict]:
    return conn.execute(
        """
        SELECT b.id, b.mapping, b.source_file, count(s.id) AS rows
        FROM import_batches b
        JOIN staging_rows s ON s.batch_id = b.id
        WHERE (%s::bigint IS NULL OR b.id = %s::bigint)
        GROUP BY b.id, b.mapping, b.source_file
        ORDER BY b.id
        """,
        (batch_id, batch_id),
    ).fetchall()


def _resolve_all(
    conn: psycopg.Connection, txn, m, caches: dict
) -> tuple[int | None, int | None, int | None, str | None]:
    """Account, category, merchant, location - exactly as the importer does."""
    account_id = caches["default_account"]
    if txn.account_name:
        if txn.account_name not in caches["account"]:
            caches["account"][txn.account_name] = ensure_account(
                conn, m, txn.account_name
            )
        account_id = caches["account"][txn.account_name]

    category_id, warning = None, None
    if txn.category_path:
        if txn.category_path not in caches["category"]:
            caches["category"][txn.category_path] = categories.resolve_or_create(
                conn, txn.category_path, m.category
            )
        category_id, warning = caches["category"][txn.category_path]

    merchant_id = None
    if txn.merchant_name:
        if txn.merchant_name not in caches["merchant"]:
            caches["merchant"][txn.merchant_name] = merchants.ensure_merchant(
                conn, txn.merchant_name
            )
        merchant_id = caches["merchant"][txn.merchant_name]
    elif txn.merchant_ref:
        found = conn.execute(
            "SELECT id FROM merchants WHERE external_ref = %s", (txn.merchant_ref,)
        ).fetchone()
        merchant_id = found["id"] if found else None

    location_id = None
    if txn.location is not None:
        location_id = locmod.resolve(conn, txn.location, caches["location"])
    if location_id is None and txn.location_ref:
        found = conn.execute(
            "SELECT id FROM locations WHERE external_ref = %s", (txn.location_ref,)
        ).fetchone()
        location_id = found["id"] if found else None

    return account_id, category_id, merchant_id, location_id, warning


def replay(
    conn: psycopg.Connection,
    batch_id: int | None = None,
    mapping_override: str | None = None,
    fields: str = "classification",
    only_missing: bool = False,
    retry_errors: bool = True,
    dry_run: bool = False,
    limit_examples: int = 12,
) -> ReplayResult:
    if fields not in FIELD_SETS:
        raise ValueError(
            f"fields must be one of {sorted(FIELD_SETS)}, not {fields!r}"
        )
    touching = FIELD_SETS[fields]
    result = ReplayResult()

    for batch in _batches(conn, batch_id):
        name = mapping_override or batch["mapping"]
        try:
            m = mapping_mod.load(name)
        except Exception as exc:
            result.skipped_batches.append(f"#{batch['id']} ({name}): {exc}")
            continue

        caches = {
            "account": {}, "category": {}, "merchant": {}, "location": {},
            "default_account": ensure_account(conn, m),
        }
        # The fallback external_id counts repeats within a file, so rows must
        # be walked in their original order for the ids to come out the same.
        rows = conn.execute(
            "SELECT id, line_no, raw, transaction_id, status, warning "
            "FROM staging_rows WHERE batch_id = %s ORDER BY line_no, id",
            (batch["id"],),
        ).fetchall()

        seen: dict = {}
        touched_batch = False

        for staged in rows:
            raw = staged["raw"]
            try:
                check_row_shape(raw, m)
                txn = normalize_row(raw, m, seen)
            except RowError as exc:
                if staged["status"] == "error":
                    result.still_failing += 1
                    if not dry_run:
                        conn.execute(
                            "UPDATE staging_rows SET error = %s WHERE id = %s",
                            (str(exc), staged["id"]),
                        )
                else:
                    # It parsed when imported and does not now: a mapping
                    # change broke it. Leave the transaction alone and say so.
                    result.still_failing += 1
                    result.examples.append(
                        f"line {staged['line_no']}: now fails to parse ({exc}); "
                        f"existing transaction left untouched"
                    )
                continue

            if staged["status"] == "error" or staged["transaction_id"] is None:
                if staged["status"] != "error" or not retry_errors:
                    continue
                result.rows_examined += 1
                if dry_run:
                    result.recovered += 1
                    if len(result.examples) < limit_examples:
                        result.examples.append(
                            f"line {staged['line_no']}: would now import"
                        )
                    continue
                account_id, category_id, merchant_id, location_id, warning = (
                    _resolve_all(conn, txn, m, caches)
                )
                params = txn.as_params()
                inserted = conn.execute(
                    """
                    INSERT INTO transactions
                        (account_id, posted_on, transacted_on, amount,
                         raw_description, statement_balance, status, source,
                         external_id, import_batch_id, location_id, category_id,
                         merchant_id, direction, tax, discount,
                         source_merchant_ref, source_location_ref)
                    VALUES
                        (%(account_id)s, %(posted_on)s, %(transacted_on)s,
                         %(amount)s, %(raw_description)s, %(statement_balance)s,
                         %(status)s, %(source)s, %(external_id)s, %(batch_id)s,
                         %(location_id)s, %(category_id)s, %(merchant_id)s,
                         %(direction)s, %(tax)s, %(discount)s,
                         %(source_merchant_ref)s, %(source_location_ref)s)
                    ON CONFLICT (account_id, external_id) DO NOTHING
                    RETURNING id
                    """,
                    {**params, "account_id": account_id,
                     "source": f"csv:{m.name}", "batch_id": batch["id"],
                     "location_id": location_id, "category_id": category_id,
                     "merchant_id": merchant_id},
                ).fetchone()
                if inserted:
                    conn.execute(
                        "UPDATE staging_rows SET status = 'inserted', "
                        "transaction_id = %s, error = NULL, warning = %s "
                        "WHERE id = %s",
                        (inserted["id"], warning, staged["id"]),
                    )
                    result.recovered += 1
                    touched_batch = True
                else:
                    conn.execute(
                        "UPDATE staging_rows SET status = 'duplicate', "
                        "error = NULL WHERE id = %s",
                        (staged["id"],),
                    )
                continue

            # ---- an existing transaction: update in place -----------------
            result.rows_examined += 1
            current = conn.execute(
                "SELECT account_id, category_id, merchant_id, location_id, "
                "amount, direction, tax, discount, statement_balance, "
                "transacted_on, posted_on, raw_description "
                "FROM transactions WHERE id = %s",
                (staged["transaction_id"],),
            ).fetchone()
            if current is None:          # transaction deleted by hand
                continue

            account_id, category_id, merchant_id, location_id, warning = (
                _resolve_all(conn, txn, m, caches)
            )

            updates: dict[str, object] = {}
            if "category" in touching and category_id is not None:
                if category_id != current["category_id"] and not (
                    only_missing and current["category_id"] is not None
                ):
                    updates["category_id"] = category_id
            if "merchant" in touching and merchant_id is not None:
                if merchant_id != current["merchant_id"] and not (
                    only_missing and current["merchant_id"] is not None
                ):
                    updates["merchant_id"] = merchant_id
            if "location" in touching and location_id is not None:
                if location_id != current["location_id"] and not (
                    only_missing and current["location_id"] is not None
                ):
                    updates["location_id"] = location_id
            if "amount" in touching:
                if txn.amount != current["amount"]:
                    updates["amount"] = txn.amount
                if txn.direction != current["direction"]:
                    updates["direction"] = txn.direction
                if txn.tax != current["tax"]:
                    updates["tax"] = txn.tax
                if txn.discount != current["discount"]:
                    updates["discount"] = txn.discount
                if txn.statement_balance != current["statement_balance"]:
                    updates["statement_balance"] = txn.statement_balance
                if txn.transacted_on != current["transacted_on"]:
                    updates["transacted_on"] = txn.transacted_on

            if "category_id" in updates:
                result.category_changed += 1
            if "merchant_id" in updates:
                result.merchant_changed += 1
            if "location_id" in updates:
                result.location_changed += 1
            if "amount" in updates:
                result.amount_changed += 1

            if not updates and warning == staged["warning"]:
                result.unchanged += 1
                continue

            if len(result.examples) < limit_examples and updates:
                shown = ", ".join(f"{k}: {current[k]} -> {v}"
                                  for k, v in updates.items())
                result.examples.append(f"txn {staged['transaction_id']}: {shown}")

            if not dry_run:
                if updates:
                    assignments = ", ".join(f"{k} = %({k})s" for k in updates)
                    conn.execute(
                        f"UPDATE transactions SET {assignments} WHERE id = %(id)s",
                        {**updates, "id": staged["transaction_id"]},
                    )
                if warning != staged["warning"]:
                    conn.execute(
                        "UPDATE staging_rows SET warning = %s WHERE id = %s",
                        (warning, staged["id"]),
                    )
                touched_batch = True

        if touched_batch or dry_run:
            result.batches += 1

        if not dry_run:
            # Keep the batch's counters honest after recoveries.
            conn.execute(
                """
                UPDATE import_batches b
                   SET rows_inserted  = s.inserted,
                       rows_duplicate = s.duplicate,
                       rows_error     = s.errored
                  FROM (
                    SELECT count(*) FILTER (WHERE status = 'inserted')  AS inserted,
                           count(*) FILTER (WHERE status = 'duplicate') AS duplicate,
                           count(*) FILTER (WHERE status = 'error')     AS errored
                    FROM staging_rows WHERE batch_id = %s
                  ) s
                 WHERE b.id = %s
                """,
                (batch["id"], batch["id"]),
            )

    return result
