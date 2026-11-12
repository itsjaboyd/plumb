"""A small, dependency-free migration runner.

Every file in db/migrations matching NNN_name.sql is applied once, in
numeric order, inside its own transaction, and recorded in the
schema_migrations ledger along with a checksum. If a file changes after
it has been applied, the runner refuses to continue rather than leaving
your database in a state the files no longer describe.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

from . import config, roles

LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""

FILENAME = re.compile(r"^(\d+)_([A-Za-z0-9_\-]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()[:16]


def discover(directory: Path | None = None) -> list[Migration]:
    directory = directory or config.MIGRATIONS_DIR
    if not directory.is_dir():
        raise FileNotFoundError(f"no migrations directory at {directory}")

    found: list[Migration] = []
    for path in sorted(directory.iterdir()):
        m = FILENAME.match(path.name)
        if not m:
            continue
        found.append(
            Migration(
                version=int(m.group(1)),
                name=m.group(2),
                path=path,
                sql=path.read_text(),
            )
        )

    versions = [x.version for x in found]
    dupes = {v for v in versions if versions.count(v) > 1}
    if dupes:
        raise ValueError(f"duplicate migration version(s): {sorted(dupes)}")
    return sorted(found, key=lambda x: x.version)


def applied(conn: psycopg.Connection) -> dict[int, dict]:
    conn.execute(LEDGER)
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations"
    ).fetchall()
    return {r["version"]: r for r in rows}


def status(conn: psycopg.Connection) -> list[tuple[Migration, str]]:
    done = applied(conn)
    out = []
    for mig in discover():
        if mig.version not in done:
            out.append((mig, "pending"))
        elif done[mig.version]["checksum"] != mig.checksum:
            out.append((mig, "CHANGED since applied"))
        else:
            out.append((mig, "applied"))
    return out


def grant_readonly(conn: psycopg.Connection) -> None:
    """Re-grant SELECT to any *_reader role, for tables just created."""
    roles = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname LIKE %s", ("%_reader",)
    ).fetchall()
    for role in roles:
        conn.execute(
            f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role["rolname"]}"'
        )


def run(conn: psycopg.Connection, dry_run: bool = False) -> list[Migration]:
    done = applied(conn)
    conn.commit()

    for mig in discover():
        if mig.version in done and done[mig.version]["checksum"] != mig.checksum:
            raise RuntimeError(
                f"{mig.path.name} changed after it was applied. Migrations are "
                f"append-only: revert the edit and add a new migration instead."
            )

    pending = [m for m in discover() if m.version not in done]
    if dry_run:
        return pending

    # .env is the source of truth for the read-only login, not the
    # first-boot init script, which never runs again once the data
    # volume exists.
    roles.ensure_from_env(conn)

    for mig in pending:
        print(f"  applying {mig.version:03d}_{mig.name} ...", end="", flush=True)
        try:
            conn.execute(mig.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) "
                "VALUES (%s, %s, %s)",
                (mig.version, mig.name, mig.checksum),
            )
            grant_readonly(conn)
            conn.commit()
            print(" ok")
        except Exception:
            conn.rollback()
            print(" FAILED")
            raise
    return pending
