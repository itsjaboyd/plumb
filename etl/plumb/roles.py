"""Create and maintain the read-only role Grafana logs in with.

The db/init script only ever runs on the database's *first* boot, so any
later change to GRAFANA_DB_PASSWORD in .env never reached PostgreSQL -
Grafana then fails to authenticate with a password that looks correct in
every file you can see. This module makes .env the source of truth: every
`plumb migrate` re-asserts the role and its password, so the two can't
drift apart again.
"""
from __future__ import annotations

import os

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from . import config


def reader_credentials() -> tuple[str | None, str | None]:
    return os.environ.get("GRAFANA_DB_USER"), os.environ.get("GRAFANA_DB_PASSWORD")


def reader_dsn(user: str, password: str) -> str:
    """The same connection as DATABASE_URL, but as the read-only role."""
    parts = conninfo_to_dict(config.DATABASE_URL)
    parts["user"] = user
    parts["password"] = password
    return make_conninfo(**parts)


def database_name() -> str:
    return conninfo_to_dict(config.DATABASE_URL).get("dbname", "plumb")


def ensure_reader(
    conn: psycopg.Connection, user: str, password: str, quiet: bool = False
) -> bool:
    """Create or update the read-only role. Returns True if anything changed.

    CREATE ROLE and ALTER ROLE are utility statements, so the password
    cannot be sent as a query parameter - it is quoted as a literal
    instead, which psycopg escapes correctly.
    """
    role = sql.Identifier(user)
    secret = sql.Literal(password)
    db = sql.Identifier(database_name())

    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)
    ).fetchone()

    try:
        if exists:
            conn.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(role, secret)
            )
            action = "updated password for"
        else:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(role, secret)
            )
            action = "created"

        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        conn.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role)
        )
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}"
            ).format(role)
        )
        conn.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))
        conn.commit()
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        raise RuntimeError(
            f"this database user may not manage roles. Run this once as a "
            f"superuser:\n"
            f"  CREATE ROLE {user} LOGIN PASSWORD '<password>';\n"
            f"  GRANT CONNECT ON DATABASE {database_name()} TO {user};\n"
            f"  GRANT USAGE ON SCHEMA public TO {user};\n"
            f"  GRANT SELECT ON ALL TABLES IN SCHEMA public TO {user};"
        ) from None

    if not quiet:
        print(f"  {action} read-only role {user!r}")
    return True


def ensure_from_env(conn: psycopg.Connection, quiet: bool = False) -> bool:
    """Called by `plumb migrate`; silently does nothing if env is unset."""
    user, password = reader_credentials()
    if not user or not password:
        return False
    return ensure_reader(conn, user, password, quiet=quiet)


def check_login(user: str, password: str) -> tuple[bool, str]:
    """Try to actually log in as the reader, the way Grafana will."""
    try:
        with psycopg.connect(reader_dsn(user, password), connect_timeout=5) as conn:
            row = conn.execute("SELECT count(*) FROM v_spending").fetchone()
            return True, f"connected, {row[0]} rows visible in v_spending"
    except psycopg.OperationalError as exc:
        return False, str(exc).strip().splitlines()[0]
    except psycopg.Error as exc:
        return False, f"connected, but: {str(exc).strip().splitlines()[0]}"
