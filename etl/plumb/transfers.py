"""Pair up the two legs of account-to-account transfers.

Moving $500 from checking to savings appears twice: -500 in one account
and +500 in the other. Counted as spending it inflates your outgoings and
makes every category chart wrong, so both legs get a shared transfer_group
and the reporting views exclude them.

Matching is greedy and one-to-one: closest in time first, and no
transaction is ever used twice.
"""
from __future__ import annotations

import uuid

import psycopg

from . import config

# Phase 1: the source told us. A Transfer leg (money out) paired with a
# Receive leg (money in) of the same size in another account. This is
# exact rather than inferred, so it runs first and wins.
DIRECTED_CANDIDATES = """
SELECT a.id AS out_id, b.id AS in_id,
       a.account_id AS out_account, b.account_id AS in_account,
       a.posted_on AS out_posted, b.posted_on AS in_posted,
       a.amount AS out_amount,
       abs(b.posted_on - a.posted_on) AS day_gap
FROM transactions a
JOIN transactions b
  ON b.amount = -a.amount
 AND b.account_id <> a.account_id
 AND b.posted_on BETWEEN a.posted_on - %(window)s AND a.posted_on + %(window)s
 AND b.direction = 'Receive'
WHERE a.direction = 'Transfer'
  AND a.transfer_group IS NULL
  AND b.transfer_group IS NULL
  AND a.status <> 'void'
  AND b.status <> 'void'
ORDER BY abs(b.posted_on - a.posted_on), a.posted_on, a.id
"""

# Phase 2: inference, for rows whose source said nothing about direction.
CANDIDATES = """
SELECT a.id AS out_id, b.id AS in_id,
       a.account_id AS out_account, b.account_id AS in_account,
       a.posted_on AS out_posted, b.posted_on AS in_posted,
       a.amount AS out_amount,
       abs(b.posted_on - a.posted_on) AS day_gap
FROM transactions a
JOIN transactions b
  ON b.amount = -a.amount
 AND b.account_id <> a.account_id
 AND b.posted_on BETWEEN a.posted_on - %(window)s AND a.posted_on + %(window)s
WHERE a.amount < 0
  AND a.transfer_group IS NULL
  AND b.transfer_group IS NULL
  AND a.status <> 'void'
  AND b.status <> 'void'
  -- Rows that carry a direction are handled by the exact pass above; an
  -- outward row explicitly marked Withdraw is spending, not a transfer.
  AND a.direction IS NULL
  AND b.direction IS NULL
ORDER BY abs(b.posted_on - a.posted_on), a.posted_on, a.id
"""


def link(conn: psycopg.Connection, window_days: int | None = None,
         dry_run: bool = False) -> list[dict]:
    """Pair transfer legs. Exact direction matches first, then inference.

    Note that pairing is no longer what keeps transfers out of your
    spending: a row whose direction is Transfer or Receive is internal on
    its own (see is_internal). Pairing is what lets you see the two legs
    as one movement, and it still matters for accounts whose exports carry
    no direction column.
    """
    window = window_days if window_days is not None else config.TRANSFER_WINDOW_DAYS

    used: set[int] = set()
    pairs: list[dict] = []

    for method, query in (("direction", DIRECTED_CANDIDATES),
                          ("amount", CANDIDATES)):
        for row in conn.execute(query, {"window": window}).fetchall():
            if row["out_id"] in used or row["in_id"] in used:
                continue
            used.add(row["out_id"])
            used.add(row["in_id"])
            pairs.append({**row, "method": method})

    if dry_run:
        return pairs

    for pair in pairs:
        group = uuid.uuid4()
        conn.execute(
            "UPDATE transactions SET transfer_group = %s WHERE id IN (%s, %s)",
            (group, pair["out_id"], pair["in_id"]),
        )
        pair["transfer_group"] = str(group)
    return pairs


def unpaired_legs(conn: psycopg.Connection) -> list[dict]:
    """Internal rows whose counterpart is missing.

    Usually means the other account has not been imported. Harmless for
    spending totals, but worth knowing about.
    """
    return conn.execute(
        """
        SELECT a.name AS account, t.direction, count(*) AS legs,
               sum(abs(t.amount)) AS gross,
               min(t.posted_on) AS first_seen, max(t.posted_on) AS last_seen
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE t.direction IN ('Transfer', 'Receive')
          AND t.transfer_group IS NULL
          AND t.status <> 'void'
        GROUP BY a.name, t.direction
        ORDER BY count(*) DESC
        """
    ).fetchall()
