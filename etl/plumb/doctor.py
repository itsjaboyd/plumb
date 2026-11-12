"""`plumb doctor` - find out why Grafana can't connect.

Checks the chain Grafana depends on, in order, and names the fix for the
first thing that is broken.
"""
from __future__ import annotations

import psycopg
from psycopg.conninfo import conninfo_to_dict

from . import config, db, roles

OK = "  ok   "
BAD = "  FAIL "
WARN = "  warn "


def masked_dsn() -> str:
    parts = conninfo_to_dict(config.DATABASE_URL)
    if parts.get("password"):
        parts["password"] = "***"
    return " ".join(f"{k}={v}" for k, v in sorted(parts.items()))


def run() -> int:
    problems = 0
    print(f"DATABASE_URL: {masked_dsn()}\n")

    # 1. Can the application user connect at all?
    try:
        with db.connect() as conn:
            version = conn.execute("SHOW server_version").fetchone()["server_version"]
            print(f"{OK} application user connects (PostgreSQL {version})")
            applied = conn.execute(
                "SELECT count(*) AS n FROM schema_migrations"
            ).fetchone()["n"]
            print(f"{OK} {applied} migration(s) applied")
    except Exception as exc:
        print(f"{BAD} application user cannot connect: {exc}")
        if "password authentication failed" in str(exc):
            print("       the password in .env is not the database's password.")
            print("       postgres only applies POSTGRES_PASSWORD when it")
            print("       initialises an EMPTY data directory, so a volume left")
            print("       from an earlier run still wants the password that was")
            print("       in .env back then - editing .env now changes nothing.")
            print("       fix:  make set-db-password     (adopt the .env value)")
            print("       or:   make reset               (DESTROYS the data)")
        else:
            print("       start the stack with `make up`, and check POSTGRES_* in .env")
        return 1

    # 2. Is the reader's configuration present in this environment?
    user, password = roles.reader_credentials()
    if not user or not password:
        print(f"{BAD} GRAFANA_DB_USER / GRAFANA_DB_PASSWORD are not set here")
        print("       they live in .env; docker compose passes them to this container")
        return 1
    print(f"{OK} reader credentials present for role {user!r}")

    if "$" in password:
        problems += 1
        print(f"{WARN} the password contains '$'. Grafana expands environment")
        print("       variables in provisioning files, so a '$' inside the password")
        print("       gets eaten. Use letters and digits instead.")
    if password.strip() != password:
        problems += 1
        print(f"{WARN} the password has leading or trailing whitespace")

    # 3. Does the role exist in the database?
    with db.connect() as conn:
        row = conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (user,)
        ).fetchone()
    if not row:
        problems += 1
        print(f"{BAD} role {user!r} does not exist in the database")
        print("       run `make migrate` - it now creates the role from .env")
    elif not row["rolcanlogin"]:
        problems += 1
        print(f"{BAD} role {user!r} exists but cannot log in")
        print(f"       run `make migrate`")
    else:
        print(f"{OK} role {user!r} exists and may log in")

    # 4. The real test: log in exactly as Grafana will.
    good, detail = roles.check_login(user, password)
    if good:
        print(f"{OK} logged in as {user}: {detail}")
    else:
        problems += 1
        print(f"{BAD} cannot log in as {user}: {detail}")
        if "password authentication failed" in detail:
            print("       The database has a different password than .env does.")
            print("       db/init only runs on the database's FIRST boot, so a")
            print("       password changed later never reached PostgreSQL.")
            print("       Fix: `make migrate` (re-asserts the role from .env),")
            print("       then `docker compose restart grafana`.")

    # 5. Read-only really means read-only.
    if good:
        try:
            with psycopg.connect(roles.reader_dsn(user, password)) as conn:
                conn.execute("CREATE TABLE doctor_should_fail (x int)")
            problems += 1
            print(f"{BAD} {user} was able to create a table - it is not read-only")
        except psycopg.errors.InsufficientPrivilege:
            print(f"{OK} {user} cannot write (as intended)")
        except psycopg.Error as exc:
            print(f"{WARN} unexpected error on the write test: {exc}")

    # 6. What Grafana itself was handed. Those variables live in the
    # grafana container, not this one, so point at how to read them there
    # rather than reporting this container's (empty) copy as a problem.
    print("\nGrafana reads its datasource settings from PLUMB_DB_* in its own")
    print("container. Check them with:")
    print("       docker compose exec grafana env | grep PLUMB_DB")
    print("Expect PLUMB_DB_USER to match the role above and PLUMB_DB_HOST to")
    print("be db:5432. If they are missing or stale, re-read .env with:")
    print("       docker compose up -d --force-recreate grafana")

    print()
    if problems:
        print(f"{problems} problem(s) found.")
        return 1
    print("No problems found. If Grafana still errors, restart it so it")
    print("re-reads provisioning: docker compose restart grafana")
    return 0
