"""Resolve bank description strings to merchants, and merchants to categories."""
from __future__ import annotations

import psycopg

APPLY_ALIASES = """
WITH best AS (
    SELECT DISTINCT ON (t.id)
           t.id AS txn_id,
           ma.merchant_id,
           coalesce(ma.category_id, mer.default_category_id) AS category_id
    FROM transactions t
    JOIN merchant_aliases ma
      ON (ma.match_type = 'like'  AND t.raw_description ILIKE ma.pattern)
      OR (ma.match_type = 'regex' AND t.raw_description ~* ma.pattern)
    JOIN merchants mer ON mer.id = ma.merchant_id
    WHERE t.merchant_id IS NULL
      AND t.raw_description IS NOT NULL
      AND t.status <> 'void'
    ORDER BY t.id, ma.priority, ma.id      -- lowest priority number wins
)
UPDATE transactions t
   SET merchant_id = best.merchant_id,
       category_id = coalesce(t.category_id, best.category_id)
  FROM best
 WHERE t.id = best.txn_id
"""

BACKFILL_CATEGORIES = """
UPDATE transactions t
   SET category_id = m.default_category_id
  FROM merchants m
 WHERE t.merchant_id = m.id
   AND t.category_id IS NULL
   AND m.default_category_id IS NOT NULL
"""


def apply_aliases(conn: psycopg.Connection) -> int:
    """Attach merchants (and their default categories) to unmatched rows."""
    cur = conn.execute(APPLY_ALIASES)
    matched = cur.rowcount or 0
    conn.execute(BACKFILL_CATEGORIES)
    return matched


def ensure_merchant(
    conn: psycopg.Connection, name: str, category_path: str | None = None
) -> int:
    category_id = resolve_category(conn, category_path) if category_path else None
    row = conn.execute("SELECT id FROM merchants WHERE name = %s", (name,)).fetchone()
    if row:
        if category_id:
            conn.execute(
                "UPDATE merchants SET default_category_id = %s WHERE id = %s",
                (category_id, row["id"]),
            )
        return row["id"]
    row = conn.execute(
        "INSERT INTO merchants (name, default_category_id) VALUES (%s, %s) RETURNING id",
        (name, category_id),
    ).fetchone()
    return row["id"]


def resolve_category(conn: psycopg.Connection, path: str) -> int:
    """Resolve 'Food > Groceries > Produce' (or just 'Groceries') to an id."""
    parts = [p.strip() for p in path.split(">") if p.strip()]
    if not parts:
        raise ValueError("empty category path")

    if len(parts) == 1:
        rows = conn.execute(
            "SELECT id, parent_id FROM categories WHERE name = %s", (parts[0],)
        ).fetchall()
        if not rows:
            raise ValueError(f"no category named {parts[0]!r}")
        if len(rows) > 1:
            raise ValueError(
                f"{parts[0]!r} exists under several parents; give the full path, "
                f"e.g. 'Food > Groceries'"
            )
        return rows[0]["id"]

    parent_id = None
    current = None
    for part in parts:
        row = conn.execute(
            "SELECT id FROM categories WHERE name = %s AND parent_id IS NOT DISTINCT FROM %s",
            (part, parent_id),
        ).fetchone()
        if not row:
            raise ValueError(f"no category {part!r} under {current or 'top level'}")
        parent_id = row["id"]
        current = part
    return parent_id


def add_alias(
    conn: psycopg.Connection,
    merchant: str,
    pattern: str,
    match_type: str = "like",
    priority: int = 100,
    category_path: str | None = None,
) -> tuple[int, int]:
    """Create merchant + alias, then apply it to existing rows."""
    merchant_id = ensure_merchant(conn, merchant, category_path)
    category_id = resolve_category(conn, category_path) if category_path else None
    conn.execute(
        """
        INSERT INTO merchant_aliases (merchant_id, pattern, match_type, priority, category_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (pattern) DO UPDATE
            SET merchant_id = EXCLUDED.merchant_id,
                match_type  = EXCLUDED.match_type,
                priority    = EXCLUDED.priority,
                category_id = EXCLUDED.category_id
        """,
        (merchant_id, pattern, match_type, priority, category_id),
    )
    return merchant_id, apply_aliases(conn)
