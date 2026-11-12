"""Settings, all overridable by environment variable."""
from __future__ import annotations

import os
from pathlib import Path

#: libpq connection string. Inside Compose this points at the `db` service.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://plumb:plumb@localhost:5432/plumb",
)

#: Where the numbered .sql migrations live.
MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR", "/app/migrations"))

#: Where the per-institution YAML mappings live.
MAPPINGS_DIR = Path(os.environ.get("MAPPINGS_DIR", "/app/mappings"))

#: How many days apart the two legs of a transfer may post.
TRANSFER_WINDOW_DAYS = int(os.environ.get("TRANSFER_WINDOW_DAYS", "5"))


def resolve_dirs() -> None:
    """Fall back to the repo layout when running outside the container."""
    global MIGRATIONS_DIR, MAPPINGS_DIR
    here = Path(__file__).resolve().parent.parent          # .../etl
    if not MIGRATIONS_DIR.is_dir():
        MIGRATIONS_DIR = here.parent / "db" / "migrations"
    if not MAPPINGS_DIR.is_dir():
        MAPPINGS_DIR = here / "mappings"
