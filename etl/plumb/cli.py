"""plumb - command line entry point.

    plumb migrate [--status]
    plumb import --mapping chase_credit /data/inbox/activity.csv
    plumb import-inbox /data/inbox [--archive /data/archive]
    plumb categorize
    plumb link-transfers [--dry-run] [--window 5]
    plumb unmatched [--limit 25]
    plumb add-alias --merchant "King Soopers" --pattern "%KING SOOPERS%" \
                     --category "Food > Groceries"
    plumb record-balance --account "Vanguard" --as-of 2026-10-31 --balance 42000
    plumb import-budget --csv budget.csv [--dry-run]
    plumb budget [--history 20]
    plumb stats
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import (budgets, categories, config, db, doctor, locations, mapping,
               merchants, migrate, refs, replay, roles, transfers)
from .importer import import_file


# --------------------------------------------------------------------- utils
def _print_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("  (nothing to show)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  " + "  ".join(c.ljust(widths[c]) for c in columns))
    print("  " + "  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  " + "  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


def _input_file(value: str) -> Path | None:
    """The file the person meant, or None with an explanation printed.

    These commands normally run inside the etl container, whose workdir is
    /app while ./data is mounted at /data. So a host-relative path like
    `data/inbox/budget.csv` - the obvious thing to type, and what the
    Makefile's own help shows - points at nothing. Rather than reporting
    "no such file" and leaving the person to work out why, try the mounted
    path and say what happened.
    """
    path = Path(value)
    if path.is_file():
        return path
    if not path.is_absolute():
        mounted = Path("/") / path
        if mounted.is_file():
            print(f"  (reading {mounted}: paths are resolved inside the "
                  f"container, where ./data is mounted at /data)")
            return mounted
    print(f"error: no such file: {value}", file=sys.stderr)
    if Path("/data").is_dir() and not value.startswith("/data"):
        print(f"  this runs inside the etl container, where your ./data "
              f"directory is mounted at /data. Try /{value.lstrip('/')} - or "
              f"use the Makefile, which translates the path for you.",
              file=sys.stderr)
    return None


def _mapping_from_filename(path: Path) -> str:
    """data/inbox/chase_credit__2026-10.csv -> 'chase_credit'."""
    stem = path.stem
    return stem.split("__")[0] if "__" in stem else stem


# ------------------------------------------------------------------ commands
def cmd_migrate(args: argparse.Namespace) -> int:
    config.resolve_dirs()
    with db.connect() as conn:
        if args.status:
            print(f"migrations in {config.MIGRATIONS_DIR}:")
            for mig, state in migrate.status(conn):
                print(f"  {mig.version:03d}_{mig.name:<24} {state}")
            return 0
        pending = migrate.run(conn, dry_run=args.dry_run)
        if not pending:
            print("database is up to date")
        elif args.dry_run:
            for mig in pending:
                print(f"  would apply {mig.version:03d}_{mig.name}")
        else:
            print(f"applied {len(pending)} migration(s)")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    path = _input_file(args.csv)
    if path is None:
        return 2
    spec = mapping.load(args.mapping)
    with db.connect() as conn:
        result = import_file(conn, path, spec, force=args.force,
                             categorize=not args.no_categorize)
        conn.commit()
        print(f"  {result.summary()}")
        if result.matched_merchants:
            print(f"  matched {result.matched_merchants} row(s) to merchants")
        if result.errors:
            print(f"  see: SELECT line_no, error, raw FROM staging_rows "
                  f"WHERE batch_id = {result.batch_id} AND status = 'error';")
    return 0


def cmd_import_inbox(args: argparse.Namespace) -> int:
    inbox = Path(args.directory)
    files = sorted(p for p in inbox.glob("*.csv"))
    if not files:
        print(f"no CSV files in {inbox}")
        return 0

    archive = Path(args.archive) if args.archive else None
    failures = 0
    with db.connect() as conn:
        for path in files:
            name = _mapping_from_filename(path)
            print(f"{path.name}  (mapping: {name})")
            try:
                spec = mapping.load(name)
                result = import_file(conn, path, spec, force=args.force,
                                     categorize=not args.no_categorize)
                print(f"  {result.summary()}")
                conn.commit()
            except Exception as exc:                  # keep going on the rest
                conn.rollback()
                failures += 1
                print(f"  skipped: {exc}")
                continue
            if archive:
                archive.mkdir(parents=True, exist_ok=True)
                target = archive / f"{date.today():%Y%m%d}-{path.name}"
                shutil.move(str(path), target)
                print(f"  archived to {target}")
    return 1 if failures else 0


def cmd_categorize(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        matched = merchants.apply_aliases(conn)
        conn.commit()
        print(f"matched {matched} transaction(s) to merchants")
    return 0


def cmd_link_transfers(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        pairs = transfers.link(conn, window_days=args.window, dry_run=args.dry_run)
        conn.commit()
        verb = "would link" if args.dry_run else "linked"
        by_direction = sum(1 for p in pairs if p.get("method") == "direction")
        print(f"{verb} {len(pairs)} transfer pair(s): "
              f"{by_direction} from the direction column, "
              f"{len(pairs) - by_direction} inferred from amounts")
        _print_table(
            [
                {
                    "out": p["out_id"], "in": p["in_id"],
                    "amount": p["out_amount"],
                    "posted": p["out_posted"], "gap_days": p["day_gap"],
                    "matched_by": p.get("method", "amount"),
                }
                for p in pairs[:20]
            ],
            ["out", "in", "amount", "posted", "gap_days", "matched_by"],
        )
        leftover = transfers.unpaired_legs(conn)
        if leftover:
            print("\nInternal legs with no counterpart (the other account may "
                  "not be imported).\nThese are still excluded from spending:")
            _print_table(leftover, ["account", "direction", "legs", "gross",
                                    "first_seen", "last_seen"])
    return 0


def cmd_unmatched(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT raw_description, txn_count, total_outflow, last_seen "
            "FROM v_unmatched_descriptions LIMIT %s",
            (args.limit,),
        ).fetchall()
        print("Bank descriptions with no merchant alias yet:")
        _print_table(rows, ["txn_count", "total_outflow", "last_seen", "raw_description"])
        if rows:
            print('\n  plumb add-alias --merchant "Name" --pattern "%SOME TEXT%" '
                  '--category "Food > Groceries"')
    return 0


def cmd_add_alias(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        try:
            merchant_id, matched = merchants.add_alias(
                conn,
                merchant=args.merchant,
                pattern=args.pattern,
                match_type=args.match_type,
                priority=args.priority,
                category_path=args.category,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        conn.commit()
        print(f"alias -> merchant #{merchant_id}; applied to {matched} transaction(s)")
    return 0


def cmd_record_balance(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM accounts WHERE name = %s", (args.account,)
        ).fetchone()
        if not row:
            print(f"error: no account named {args.account!r}", file=sys.stderr)
            return 2
        conn.execute(
            """
            INSERT INTO account_balances (account_id, as_of, balance, source)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, as_of) DO UPDATE
                SET balance = EXCLUDED.balance, source = EXCLUDED.source
            """,
            (row["id"], args.as_of, Decimal(args.balance), args.source),
        )
        conn.commit()
        print(f"recorded {args.account} = {args.balance} as of {args.as_of}")
    return 0


def cmd_set_opening(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "UPDATE accounts SET opening_balance = %s, opening_balance_as_of = %s "
            "WHERE name = %s RETURNING id",
            (Decimal(args.balance), args.as_of, args.account),
        ).fetchone()
        if not row:
            print(f"error: no account named {args.account!r}", file=sys.stderr)
            return 2
        conn.commit()
        print(f"{args.account} opens at {args.balance}"
              + (f" as of {args.as_of}" if args.as_of else ""))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT account, opening_balance, computed_balance, statement_balance, "
            "statement_diff, snapshot_balance, snapshot_diff "
            "FROM v_reconciliation ORDER BY account"
        ).fetchall()
        print("Computed balance vs what the bank last told you:")
        _print_table(
            rows,
            ["account", "opening_balance", "computed_balance", "statement_balance",
             "statement_diff", "snapshot_balance", "snapshot_diff"],
        )
        bad = [r for r in rows if r["statement_diff"] not in (None, 0)]
        if bad:
            print("\n  Non-zero statement_diff means a missing import, a duplicate,")
            print("  or an opening balance that still needs setting:")
            print("    plumb set-opening --account \"NAME\" --balance N --as-of YYYY-MM-DD")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        try:
            result = replay.replay(
                conn,
                batch_id=args.batch,
                mapping_override=args.mapping,
                fields=args.fields,
                only_missing=args.only_missing,
                retry_errors=not args.no_retry_errors,
                dry_run=args.dry_run,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    print(("would change " if args.dry_run else "replayed ") + result.summary())
    for example in result.examples:
        print(f"  {example}")
    for note in result.skipped_batches:
        print(f"  skipped batch {note}")
    if result.unchanged:
        print(f"  {result.unchanged} row(s) already matched today's rules")
    if args.dry_run:
        print("\n  nothing was written (--dry-run)")
    elif args.fields == "classification":
        print("\n  amounts were not touched; use --fields all to re-derive "
              "those too")
    return 0


def cmd_backfill_locations(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        stats = locations.backfill_from_staging(conn, batch_id=args.batch)
        conn.commit()
    print(f"attached a location to {stats['updated']} transaction(s) "
          f"across {stats['batches']} batch(es)")
    if stats["no_location_in_row"]:
        print(f"  {stats['no_location_in_row']} row(s) carried no coordinates "
              f"or address")
    if stats["errors"]:
        print(f"  {stats['errors']} row(s) had coordinates that would not parse")
    for note in stats["skipped_batches"]:
        print(f"  skipped batch {note}")
    return 0


def cmd_locations(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        cov = locations.coverage(conn)
        print(f"Spending rows: {cov['spending_rows']}, with a location: "
              f"{cov['with_location']} ({cov['pct_with_location'] or 0}%)\n")
        print("Top places:")
        _print_table(
            conn.execute(
                "SELECT place_label, city, state, visits, total_outflow, "
                "latitude, longitude FROM v_location_spend "
                "ORDER BY total_outflow DESC LIMIT %s",
                (args.limit,),
            ).fetchall(),
            ["place_label", "city", "state", "visits", "total_outflow",
             "latitude", "longitude"],
        )
    return 0


def cmd_import_refs(args: argparse.Namespace) -> int:
    path = _input_file(args.csv)
    if path is None:
        return 2
    with db.connect() as conn:
        if args.kind == "merchant":
            stats = refs.import_merchant_refs(
                conn, path, ref_column=args.ref_column,
                name_column=args.name_column, category_column=args.category_column,
            )
        else:
            stats = refs.import_location_refs(
                conn, path, ref_column=args.ref_column,
                label_column=args.name_column,
                latitude_column=args.latitude_column,
                longitude_column=args.longitude_column,
                address_column=args.address_column,
                city_column=args.city_column,
                state_column=args.state_column,
                zip_column=args.zip_column,
            )
        conn.commit()
    print(f"{args.kind}s: {stats['rows']} row(s) read, {stats['created']} created, "
          f"{stats['updated']} updated, {stats['skipped']} skipped")
    if stats.get("bad_coordinates"):
        print(f"  {stats['bad_coordinates']} row(s) had unusable coordinates")
    if stats.get("no_location_data"):
        print(f"  {stats['no_location_data']} row(s) had no coordinates or "
              f"address at all - nothing to place, so those refs stay "
              f"unresolved (normal for online merchants)")
    print(f"  attached to {stats['attached']} transaction(s) by reference")
    return 0


def cmd_refs(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        rows = refs.unresolved(conn, limit=args.limit)
        print("Source ids with nothing to point at yet:")
        _print_table(rows, ["kind", "ref", "transactions", "gross_amount"])
        if rows:
            print("\n  Export the source's merchant/location table, then:")
            print("    plumb import-refs --kind merchant --csv merchants.csv")
            print("  Location refs for online merchants have no place to "
                  "resolve to and will stay listed.")
    return 0


def cmd_directions(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        print("How each direction is used:")
        _print_table(
            conn.execute(
                "SELECT direction, transactions, gross_amount, unpaired_legs, "
                "first_seen, last_seen FROM v_direction_summary "
                "ORDER BY transactions DESC"
            ).fetchall(),
            ["direction", "transactions", "gross_amount", "unpaired_legs",
             "first_seen", "last_seen"],
        )
        print("\nMonthly cash flow (internal movement kept separate):")
        _print_table(
            conn.execute(
                'SELECT "time"::date AS month, income, spending, net, '
                "internal_out, internal_in FROM v_monthly_cashflow "
                'ORDER BY "time" DESC LIMIT %s',
                (args.months,),
            ).fetchall(),
            ["month", "income", "spending", "net", "internal_out", "internal_in"],
        )
    return 0


def cmd_categories(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        if args.unfiled:
            rows = categories.unfiled(conn, parent=args.parent)
            print(f"Categories sitting under {args.parent!r}, waiting to be filed:")
            _print_table(rows, ["id", "name", "transactions", "outflow"])
            if rows:
                print('\n  plumb move-category --name "NAME" '
                      '--under "Food > Groceries"')
            return 0
        rows = conn.execute(
            "SELECT depth, full_path FROM v_category_tree ORDER BY full_path"
        ).fetchall()
        _print_table(rows, ["depth", "full_path"])
    return 0


def cmd_move_category(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        try:
            moved = categories.move(conn, args.name, args.under)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        conn.commit()
    print(f"moved to {moved['full_path']} (depth {moved['depth']})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor.run()


def cmd_ensure_reader(args: argparse.Namespace) -> int:
    user, password = roles.reader_credentials()
    if args.user:
        user = args.user
    if args.password:
        password = args.password
    if not user or not password:
        print("error: set GRAFANA_DB_USER and GRAFANA_DB_PASSWORD (they come "
              "from .env), or pass --user/--password", file=sys.stderr)
        return 2
    with db.connect() as conn:
        roles.ensure_reader(conn, user, password)
    good, detail = roles.check_login(user, password)
    print(("  verified: " if good else "  STILL FAILING: ") + detail)
    return 0 if good else 1


def cmd_stats(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        print("Accounts:")
        # Two balance columns, because the two sources are signed
        # differently on an owe-money account and one number cannot say
        # both things:
        #   balance    what is stored - opening_balance plus transactions.
        #              Summing transactions alone reported 0 for a loan,
        #              whose balance lives entirely in opening_balance.
        #   net_worth  what the account contributes, which is what
        #              v_net_worth_daily sums. On a Credit or Loan
        #              account the hand-entered opening_balance is the
        #              amount owed, so it is subtracted; transactions are
        #              already signed and are used as they are.
        accounts = conn.execute(
            """
            SELECT a.name AS account, a.account_type AS type, count(t.id) AS txns,
                   min(t.posted_on) AS first, max(t.posted_on) AS last,
                   a.opening_balance + coalesce(sum(t.amount), 0) AS balance,
                   CASE WHEN a.account_type IN (SELECT account_type
                                                  FROM v_liability_types)
                        THEN -a.opening_balance ELSE a.opening_balance END
                     + coalesce(sum(t.amount), 0) AS net_worth
            FROM accounts a
            LEFT JOIN transactions t ON t.account_id = a.id AND t.status <> 'void'
            GROUP BY a.id, a.name, a.account_type, a.opening_balance ORDER BY 1
            """
        ).fetchall()
        _print_table(
            accounts,
            ["account", "type", "txns", "first", "last", "balance", "net_worth"],
        )
        row = conn.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE merchant_id IS NULL) AS no_merchant,
                   count(*) FILTER (WHERE category_id IS NULL) AS no_category,
                   count(*) FILTER (WHERE transfer_group IS NOT NULL) AS transfer_legs
            FROM transactions WHERE status <> 'void'
            """
        ).fetchone()
        print(
            f"\nTransactions: {row['total']} total, {row['no_merchant']} without a "
            f"merchant, {row['no_category']} without a category, "
            f"{row['transfer_legs']} transfer legs"
        )
        cov = conn.execute("SELECT * FROM v_location_coverage").fetchone()
        print(f"Locations: {cov['with_location']} of {cov['spending_rows']} "
              f"spending rows have one ({cov['pct_with_location'] or 0}%)")

        print("\nRecent imports:")
        _print_table(
            conn.execute(
                "SELECT id, mapping, rows_read, rows_inserted, rows_duplicate, "
                "rows_error, started_at::date AS on FROM import_batches "
                "ORDER BY id DESC LIMIT 10"
            ).fetchall(),
            ["id", "mapping", "rows_read", "rows_inserted", "rows_duplicate",
             "rows_error", "on"],
        )
    return 0


def cmd_import_budget(args: argparse.Namespace) -> int:
    path = _input_file(args.csv)
    if path is None:
        return 2

    with db.connect() as conn:
        result = budgets.load(conn, path, shape=args.shape, year=args.year,
                              skip_bad=args.skip_bad, dry_run=args.dry_run)

        if result.errors and not args.skip_bad:
            print(f"{path.name}: nothing written. {len(result.errors)} "
                  f"problem(s) - a budget file is small enough that a silent "
                  f"skip would be worse than a failed load:")
            for problem in result.errors[:20]:
                print(f"  {problem}")
            if len(result.errors) > 20:
                print(f"  ... and {len(result.errors) - 20} more")
            print("\n  Fix the file, or re-run with --skip-bad to load the rest.")
            conn.rollback()
            return 1

        # Settle the transaction BEFORE reporting. Piping this command into
        # `head` closes stdout, and the BrokenPipeError that follows would
        # otherwise skip the commit and silently write nothing.
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

        verb = "would load" if args.dry_run else "loaded"
        print(f"{path.name}: {result.rows_read} row(s) read as the "
              f"{result.shape} shape, {verb} {len(result.plans)} plan row(s)")
        print(f"  {result.inserted} new, {result.updated} changed, "
              f"{result.unchanged} already current")
        if result.collapsed:
            print(f"  {result.collapsed} month(s) collapsed into an earlier "
                  f"row: the amount had not changed, so no new plan is needed")
        if result.stopped:
            print(f"  {result.stopped} row(s) stop budgeting a category from "
                  f"that month (a blank amount)")
        if result.snapped:
            print(f"  {result.snapped} date(s) moved to the first of the "
                  f"month; a budget applies to whole months")
        for warning in result.warnings:
            print(f"  warning: {warning}")
        if result.errors:
            print(f"  skipped {len(result.errors)} bad row(s):")
            for problem in result.errors[:10]:
                print(f"    {problem}")

        if result.plans:
            print()
            _print_table(
                [{"category": p.category,
                  "from": p.valid_from.strftime("%Y-%m"),
                  "amount": "stop" if p.amount is None else f"{p.amount:.2f}",
                  "period": p.period,
                  "rolls over": "yes" if p.carry_forward else "no",
                  "monthly": "-" if p.accrual is None else f"{p.accrual:.2f}"}
                 for p in result.plans],
                ["category", "from", "amount", "period", "rolls over",
                 "monthly"])
            print(f"\n  monthly accrual across these plans: "
                  f"{result.monthly_total:.2f}")
            print("  (compare that with your spreadsheet's total - it is the "
                  "fastest check that the file was read the way you meant it)")

        if args.dry_run:
            print("\n  --dry-run: nothing written.")
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        if not db.table_exists(conn, "budget_plans"):
            print("budget_plans does not exist yet - run `plumb migrate`.")
            return 1
        rows = budgets.current(conn)
        if not rows:
            print("No budget plans loaded. Build one in a spreadsheet and:")
            print("    plumb import-budget --csv budget.csv --dry-run")
            print("  or set them by hand: db/scripts/budgets.sql, section 7")
            return 0

        flow = [r for r in rows if not r["carry_forward"]]
        env = [r for r in rows if r["carry_forward"]]

        if flow:
            print("This month, monthly categories:")
            _print_table(flow, ["category", "budget", "spent", "variance",
                                "pct_used"])
        if env:
            print("\nEnvelopes (periodic costs, accrued):")
            _print_table(env, ["category", "period", "budget", "spent",
                               "envelope_balance", "months_of_cover"])
            behind = [r for r in env if (r["envelope_balance"] or 0) < 0]
            if behind:
                print(f"\n  {len(behind)} envelope(s) are negative - you are "
                      f"behind on funding a bill you know is coming:")
                for row in behind:
                    print(f"    {row['category']}: "
                          f"{row['envelope_balance']}")
        if args.history:
            print("\nPlan history:")
            _print_table(budgets.history(conn, limit=args.history),
                         ["category", "from_month", "amount", "period",
                          "carry_forward", "monthly"])
    return 0


def cmd_mappings(args: argparse.Namespace) -> int:
    paths = mapping.available_mappings()
    print(f"mappings in {config.MAPPINGS_DIR}:")
    for path in paths:
        try:
            spec = mapping.load(str(path))
            print(f"  {path.stem:<20} -> {spec.account.name} ({spec.account.type}, "
                  f"{spec.amount.mode})")
        except Exception as exc:
            print(f"  {path.stem:<20} -> INVALID: {exc}")
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plumb", description="Load bank CSV exports into your database."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply pending database migrations")
    p.add_argument("--status", action="store_true", help="show state and exit")
    p.add_argument("--dry-run", action="store_true", help="list pending, change nothing")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("import", help="import one CSV file")
    p.add_argument("csv")
    p.add_argument("--mapping", required=True, help="mapping name or path to a .yml")
    p.add_argument("--force", action="store_true",
                   help="import even if this exact file was imported before")
    p.add_argument("--no-categorize", action="store_true",
                   help="skip merchant alias matching afterwards")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("import-inbox", help="import every CSV in a directory")
    p.add_argument("directory", nargs="?", default="/data/inbox")
    p.add_argument("--archive", help="move files here once imported")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-categorize", action="store_true")
    p.set_defaults(func=cmd_import_inbox)

    p = sub.add_parser("categorize", help="apply merchant aliases to unmatched rows")
    p.set_defaults(func=cmd_categorize)

    p = sub.add_parser("link-transfers", help="pair the legs of account transfers")
    p.add_argument("--window", type=int, default=None, help="max days apart")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_link_transfers)

    p = sub.add_parser("unmatched", help="descriptions with no merchant alias")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_unmatched)

    p = sub.add_parser("add-alias", help="map a description pattern to a merchant")
    p.add_argument("--merchant", required=True)
    p.add_argument("--pattern", required=True,
                   help="ILIKE pattern, e.g. '%%KING SOOPERS%%'")
    p.add_argument("--match-type", choices=["like", "regex"], default="like")
    p.add_argument("--priority", type=int, default=100)
    p.add_argument("--category", help="'Food > Groceries'; sets the merchant default")
    p.set_defaults(func=cmd_add_alias)

    p = sub.add_parser("record-balance", help="record a point-in-time account balance")
    p.add_argument("--account", required=True)
    p.add_argument("--as-of", required=True,
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--balance", required=True)
    p.add_argument("--source", default="manual")
    p.set_defaults(func=cmd_record_balance)

    p = sub.add_parser("set-opening", help="set an account's opening balance")
    p.add_argument("--account", required=True)
    p.add_argument("--balance", required=True)
    p.add_argument("--as-of", default=None,
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.set_defaults(func=cmd_set_opening)

    p = sub.add_parser("reconcile", help="compare computed balances with the bank's")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser(
        "replay",
        help="re-run the raw rows in staging_rows against today's rules")
    p.add_argument("--batch", type=int, default=None,
                   help="only this import batch (default: every batch)")
    p.add_argument("--mapping", default=None,
                   help="use this mapping instead of the one each batch "
                        "recorded")
    p.add_argument("--fields", choices=sorted(replay.FIELD_SETS),
                   default="classification",
                   help="classification (category/merchant/location, the "
                        "default) | amounts | all")
    p.add_argument("--only-missing", action="store_true",
                   help="fill blanks only; never overwrite an existing value")
    p.add_argument("--no-retry-errors", action="store_true",
                   help="leave previously-failed rows alone")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change and write nothing")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("backfill-locations",
                       help="attach locations to rows imported before the "
                            "mapping had coordinate columns")
    p.add_argument("--batch", type=int, default=None, help="only this import batch")
    p.set_defaults(func=cmd_backfill_locations)

    p = sub.add_parser("locations", help="location coverage and top places")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_locations)

    p = sub.add_parser("import-refs",
                       help="resolve the source system's merchant/location ids")
    p.add_argument("--csv", required=True, dest="csv",
                   help="an export of the source's merchant or location table")
    p.add_argument("--kind", choices=["merchant", "location"], required=True)
    p.add_argument("--ref-column", default="id")
    p.add_argument("--name-column", default="name")
    p.add_argument("--category-column", default=None, help="merchants only")
    p.add_argument("--latitude-column", default="latitude", help="locations only")
    p.add_argument("--longitude-column", default="longitude", help="locations only")
    p.add_argument("--address-column", default=None, help="locations only")
    p.add_argument("--city-column", default=None, help="locations only")
    p.add_argument("--state-column", default=None, help="locations only")
    p.add_argument("--zip-column", default=None, help="locations only")
    p.set_defaults(func=cmd_import_refs)

    p = sub.add_parser("refs", help="source ids not yet resolved to a row")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("directions", help="direction usage and monthly cash flow")
    p.add_argument("--months", type=int, default=12)
    p.set_defaults(func=cmd_directions)

    p = sub.add_parser("categories", help="list the category tree")
    p.add_argument("--unfiled", action="store_true",
                   help="only those auto-created under the holding parent")
    p.add_argument("--parent", default="Imported")
    p.set_defaults(func=cmd_categories)

    p = sub.add_parser("move-category", help="re-parent a category")
    p.add_argument("--name", required=True, help="name, or a full path")
    p.add_argument("--under", default=None,
                   help="'Food > Groceries'; omit to move to the top level")
    p.set_defaults(func=cmd_move_category)

    p = sub.add_parser("doctor", help="diagnose database and Grafana login problems")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("ensure-reader", help="create or repair Grafana's read-only login")
    p.add_argument("--user", help="defaults to $GRAFANA_DB_USER")
    p.add_argument("--password", help="defaults to $GRAFANA_DB_PASSWORD")
    p.set_defaults(func=cmd_ensure_reader)

    p = sub.add_parser("stats", help="row counts and coverage")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "import-budget",
        help="load a budget from a spreadsheet export into budget_plans",
        description="Accepts either shape. A long file has one row per "
                    "change (category, effective_from, amount, period, "
                    "carry_forward, opening_balance, note). A wide file has "
                    "one row per category and one column per month, and is "
                    "collapsed so that only the months where the amount "
                    "changes become plan rows. A blank month amount stops "
                    "budgeting that category from then on.")
    p.add_argument("--csv", required=True, help="the budget file to read")
    p.add_argument("--shape", choices=["auto", "long", "wide"], default="auto",
                   help="default auto: two or more month-shaped headers "
                        "means wide")
    p.add_argument("--year", type=int,
                   help="year for bare month headers like Jan..Dec")
    p.add_argument("--skip-bad", action="store_true",
                   help="load what parses instead of refusing the whole file")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would change, write nothing")
    p.set_defaults(func=cmd_import_budget)

    p = sub.add_parser("budget", help="where each category stands this month")
    p.add_argument("--history", type=int, metavar="N",
                   help="also show the last N plan changes")
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("mappings", help="list available CSV mappings")
    p.set_defaults(func=cmd_mappings)

    return parser


def main(argv: list[str] | None = None) -> int:
    config.resolve_dirs()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:      # piped into head/less; not an error
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
