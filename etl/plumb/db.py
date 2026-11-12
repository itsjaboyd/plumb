"""Database connection helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from . import config


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Yield a connection with dict rows, committing on clean exit."""
    with psycopg.connect(
        config.DATABASE_URL, row_factory=dict_row, autocommit=autocommit
    ) as conn:
        yield conn


def table_exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS ok", (name,)
    ).fetchone()
    return bool(row and row["ok"])
