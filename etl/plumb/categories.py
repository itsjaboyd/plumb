"""Turning a category column into a row in the category tree.

A ledger export usually carries the leaf only - "Amazon Shopping", not
"Personal > Household Goods > Amazon Shopping". So:

  * a value containing the separator is treated as a full path, and any
    missing level along it is created;
  * a bare name that matches exactly one existing category anywhere in the
    tree attaches to it;
  * a bare name that matches nothing is created under the mapping's
    `category.parent` (default "Imported") rather than at the top level,
    because a flat heap of leaves at depth 1 would make the roll-up
    columns meaningless.

Re-parent them afterwards with `plumb move-category`; transactions
reference the category id, so moving one keeps every row attached.
"""
from __future__ import annotations

import psycopg

from .mapping import CategorySpec


def _child(conn: psycopg.Connection, parent_id: int | None, name: str) -> dict | None:
    return conn.execute(
        "SELECT id FROM categories "
        "WHERE name = %s AND parent_id IS NOT DISTINCT FROM %s",
        (name, parent_id),
    ).fetchone()


def _create(conn: psycopg.Connection, parent_id: int | None, name: str) -> int:
    return conn.execute(
        "INSERT INTO categories (parent_id, name) VALUES (%s, %s) RETURNING id",
        (parent_id, name),
    ).fetchone()["id"]


def ensure_path(conn: psycopg.Connection, parts: list[str]) -> int:
    """Walk (and create) a full path like Food > Groceries > Produce."""
    parent_id = None
    for part in parts:
        row = _child(conn, parent_id, part)
        parent_id = row["id"] if row else _create(conn, parent_id, part)
    return parent_id


def find_by_name(conn: psycopg.Connection, name: str) -> list[dict]:
    """Every category with this name, closest to the root first.

    Ordering by depth then path makes the choice deterministic: the same
    CSV always resolves the same way, which matters because the answer is
    stored on thousands of rows.
    """
    return conn.execute(
        "SELECT c.id, ct.full_path, ct.depth FROM categories c "
        "JOIN v_category_tree ct ON ct.id = c.id "
        "WHERE lower(c.name) = lower(%s) "
        "ORDER BY ct.depth, ct.full_path",
        (name,),
    ).fetchall()


def pick_topmost(value: str, matches: list[dict]) -> tuple[int, str | None]:
    """Choose among several categories of the same name.

    Returns (id, warning). The match closest to the root wins: a bare leaf
    name in a CSV is almost always meant as the broader category, and
    guessing deeper would file spending under a subdivision the row never
    mentioned. Ties at the same depth fall back to alphabetical order so
    repeat imports agree with each other.
    """
    chosen = matches[0]
    if len(matches) == 1:
        return chosen["id"], None

    others = ", ".join(m["full_path"] for m in matches[1:4])
    if len(matches) > 4:
        others += f", +{len(matches) - 4} more"
    same_depth = sum(1 for m in matches if m["depth"] == chosen["depth"]) > 1
    tie = " (tie on depth, chose alphabetically)" if same_depth else ""
    warning = (
        f"category {value!r} matched {len(matches)} categories; used "
        f"{chosen['full_path']!r} as the one closest to the top{tie}. "
        f"Also matched: {others}"
    )
    return chosen["id"], warning


def resolve_or_create(
    conn: psycopg.Connection, value: str, spec: CategorySpec
) -> tuple[int | None, str | None]:
    """Resolve one category column value to (category_id, warning)."""
    value = (value or "").strip()
    if not value:
        return None, None

    if spec.separator and spec.separator in value:
        parts = [p.strip() for p in value.split(spec.separator) if p.strip()]
        if not parts:
            # A cell holding nothing but separators (" > ") means no
            # category, not a crash. Every other branch returns the
            # (id, warning) pair the caller unpacks.
            return None, None
        if spec.on_unknown == "create":
            return ensure_path(conn, parts), None
        # Walk without creating.
        parent_id = None
        for part in parts:
            row = _child(conn, parent_id, part)
            if not row:
                if spec.on_unknown == "ignore":
                    return None, None
                raise ValueError(f"no category {value!r} (stopped at {part!r})")
            parent_id = row["id"]
        return parent_id, None

    matches = find_by_name(conn, value)
    if matches:
        if len(matches) > 1 and spec.on_ambiguous == "error":
            paths = ", ".join(m["full_path"] for m in matches[:4])
            raise ValueError(
                f"category {value!r} is ambiguous ({paths}); qualify it in the "
                f"CSV as a path, e.g. 'Food {spec.separator} {value}', or set "
                f"category.on_ambiguous: topmost"
            )
        return pick_topmost(value, matches)

    if spec.on_unknown == "ignore":
        return None, None
    if spec.on_unknown == "error":
        raise ValueError(f"no category named {value!r}")

    # Create it under the holding parent so the tree keeps its shape.
    parent = _child(conn, None, spec.parent)
    parent_id = parent["id"] if parent else _create(conn, None, spec.parent)
    return _create(conn, parent_id, value), None


def resolve_existing(
    conn: psycopg.Connection, value: str
) -> tuple[int | None, str | None]:
    """Resolve a name or path to an existing category, creating nothing.

    Used where inventing categories would be wrong - resolving a merchant
    table's category column, for instance.
    """
    value = (value or "").strip()
    if not value:
        return None, None
    if ">" in value:
        from .merchants import resolve_category
        try:
            return resolve_category(conn, value), None
        except ValueError:
            return None, None
    matches = find_by_name(conn, value)
    if not matches:
        return None, None
    return pick_topmost(value, matches)


def move(conn: psycopg.Connection, name_or_path: str, under: str | None) -> dict:
    """Re-parent a category. `under` None moves it to the top level."""
    from .merchants import resolve_category

    if ">" in name_or_path:
        category_id = resolve_category(conn, name_or_path)
    else:
        matches = find_by_name(conn, name_or_path)
        if not matches:
            raise ValueError(f"no category named {name_or_path!r}")
        if len(matches) > 1:
            paths = ", ".join(m["full_path"] for m in matches)
            raise ValueError(f"{name_or_path!r} is ambiguous: {paths}")
        category_id = matches[0]["id"]

    new_parent_id = None
    if under:
        new_parent_id = resolve_category(conn, under)
        if new_parent_id == category_id:
            raise ValueError("a category cannot be its own parent")
        # Refuse to move a category underneath one of its own descendants.
        descendants = conn.execute(
            "SELECT 1 FROM v_category_tree WHERE id = %s AND %s = ANY(id_path)",
            (new_parent_id, category_id),
        ).fetchone()
        if descendants:
            raise ValueError(
                "that would put the category inside its own subtree"
            )

    conn.execute(
        "UPDATE categories SET parent_id = %s WHERE id = %s",
        (new_parent_id, category_id),
    )
    return conn.execute(
        "SELECT id, full_path, depth FROM v_category_tree WHERE id = %s",
        (category_id,),
    ).fetchone()


def unfiled(conn: psycopg.Connection, parent: str = "Imported") -> list[dict]:
    """Categories sitting in the holding parent, waiting to be re-parented."""
    return conn.execute(
        """
        SELECT ct.id, ct.name_path[array_upper(ct.name_path, 1)] AS name,
               ct.full_path,
               count(t.id)    AS transactions,
               sum(-t.amount) FILTER (WHERE t.amount < 0) AS outflow
        FROM v_category_tree ct
        LEFT JOIN transactions t ON t.category_id = ct.id AND t.status <> 'void'
        WHERE ct.name_path[1] = %s
          AND ct.depth > 1
        GROUP BY ct.id, ct.name_path, ct.full_path
        ORDER BY count(t.id) DESC
        """,
        (parent,),
    ).fetchall()
