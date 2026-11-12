# Plumb

A complete, self-hosted home for your financial history: PostgreSQL for
the data, Grafana for reading it, and a Python CLI that turns bank CSV
exports into clean, deduplicated transactions. Everything runs in Docker
on your own machine. Nothing leaves it.

Named for the plumb line: the job is not to make the numbers look good,
it is to make them true. Transfers are not spending, a loan is not an
asset, and `make reconcile` compares every computed balance against what
the bank actually said.

It handles the parts that make this annoying to do yourself — seven
different CSV shapes, transfers between your own accounts that would
otherwise double-count as spending, merchant names your bank spells
three ways, a category tree you can roll up at any depth, envelope
budgeting for bills that arrive once a year, and freeform tags for the
questions a category tree cannot answer.

## Quickstart

```bash
make quickstart
```

That creates `.env`, starts the stack, applies the schema, and imports
the bundled sample data so there is something to look at. Then open
**http://localhost:3000** — user `admin`, password from `.env` — and
the five dashboards are waiting under the *Finance* folder.

When you are ready to use your own data:

```bash
make reset && make up && make migrate   # empty database
cp ~/Downloads/*.csv data/inbox/        # name them <mapping>__<period>.csv
make import-inbox
```

Change the passwords in `.env` before you put anything real in it. Ports
bind to `127.0.0.1` only, so nothing is exposed to your network.

**Requirements:** Docker with Compose. Nothing else — Python, Postgres
and Grafana all run in containers.

## What's in the box

```
plumb/
├── docker-compose.yml          db + grafana (+ a one-shot etl container)
├── .env.example                copy to .env, change the passwords
├── Makefile                    every command; `make help` lists them
├── db/
│   ├── migrations/             the schema, applied by `make migrate`
│   │   ├── 001_schema.sql          tables, indexes, functions, triggers
│   │   ├── 002_views.sql           31 reporting views
│   │   └── 003_seed.sql            starter category tree + merchant rules
│   ├── scripts/                runnable SQL per feature - see below
│   └── init/10-grafana-role.sh read-only login, created on first boot
├── etl/
│   ├── plumb/                 the CLI package
│   ├── mappings/               one YAML per bank; _template.yml to copy
│   └── tests/                  239 unit tests
├── grafana/
│   ├── provisioning/           datasource + dashboard wiring
│   ├── dashboards/             five dashboards, provisioned from disk
│   ├── dashboards-v1/          the same five in the older schema, as a fallback
│   └── v2-to-v1.py             regenerates dashboards-v1 from an export
└── data/
    ├── inbox/                  drop bank exports here
    ├── archive/                imported files move here
    └── samples/                seven formats to try it with
```

## Core conventions

Six decisions explain most of the schema. Knowing them makes the rest
predictable.

**1. Amounts are signed from your point of view.** Negative left your
accounts, positive arrived. A credit card purchase is negative, a
payment to that card is positive. No `type` column decides the meaning —
the sign does, everywhere.

That rule is applied at import, by the mapping, for every account type —
`amex.yml` sets `flip_sign: true` precisely because Amex exports charges
as positive. So a credit card fed by imported transactions carries a
*negative* balance when you owe on it.

**A balance you type in by hand works the other way.** On a `Credit` or
`Loan` account, `opening_balance` and anything recorded with
`make record-balance` mean the amount **owed**: positive is debt,
negative is real equity. Net worth subtracts those figures, so a loan
reduces it without you having to remember a minus sign:

```sql
-- a car loan you owe $18,500 on
UPDATE accounts SET opening_balance = 18500.00 WHERE name = 'Car Loan';
-- contributes -18,500.00 to net worth
```

The two sources are deliberately read differently, because they are
signed differently and only one of them is yours to control:

| source | owed shows as | net worth |
|---|---|---|
| `opening_balance` on Credit/Loan | positive | negated |
| a recorded balance on Credit/Loan | positive | negated |
| `sum(transactions.amount)` | negative | as-is |

So on a card, `opening_balance` is what you owed before the imported
history begins, and the transactions carry it forward from there.
`make stats` prints both numbers per account — `balance` as stored, and
`net_worth` as it contributes — so the difference is never hidden.

Only net worth converts. `make reconcile`, `v_account_running_balance`
and `v_account_latest_balance` all keep the raw signed figures, because
they compare against what the bank said.

**2. A transaction has exactly one category and any number of tags.**
One category is what makes roll-ups honest: every transaction lands in
exactly one bucket at each depth, so slices always sum to the total.
Tags overlap freely, which is what lets them answer "what did this trip
cost across groceries, fuel and lodging".

**3. Views are the interface.** Every dashboard panel and every script
reads a view, never a table, so columns can move without breaking
anything. `v_transactions` and `v_spending` are the two you will use
most.

**4. Internal movement is excluded from spending and income.** Moving
$500 from checking to savings appears twice — once negative, once
positive. Left alone it inflates spending *and* income. Three signals
mark a transaction as internal, and spending ignores it if **any** of
them fires:

- `make link-transfers` paired it with its opposite leg
- the source file's direction column said Transfer or Receive
- its **category is flagged** `is_internal` — inherited, so flagging a
  branch root covers everything under it

The third one matters more than it sounds. A credit card payment is
usually the largest "purchase" of the month, and it can only be paired if
you imported the card as well as the account that paid it. The seeded
`Transfer` branch — Account Transfer, Credit Card Payment, Cash
Withdrawal — ships flagged.

Nothing is hidden by this: `v_monthly_cashflow` reports `internal_in` and
`internal_out` separately, Monthly Review's ledger has an *Internal*
column, and net worth is unaffected either way, because moving money
between your own accounts never changed it.

**5. Money is `numeric`, never float.** Cents do not survive binary
floating point.

**6. Dates are stored as dates and displayed as text.** See
[Time zones](#time-zones) — this one has a sharp edge.

## Importing

One YAML file per bank format describes which column means what. No code
per bank:

```yaml
name: chase_credit
account:
  name: Chase Sapphire
  type: Credit
columns:
  posted_on: "Post Date"
  description: Description
  amount: Amount
  category: Category
amount:
  mode: signed
date_format: ["%m/%d/%Y"]
```

`make mappings` lists and validates what you have. Copy
`etl/mappings/_template.yml` to add a bank.

**What ships:**

| Mapping | The shape it reads |
|---|---|
| `chase_checking`, `chase_savings`, `chase_credit` | one signed amount column |
| `amex` | signed, but backwards - purchases arrive positive |
| `capital_one_credit` | separate debit and credit columns |
| `geo_card` | signed, plus merchant address and coordinates |
| `afcu_ledger` | several accounts per file, unsigned amount + a Direction column |
| `afcu_wide` | the above plus merchant, address, coordinates and a popularity score |
| `ledger` | several accounts per file, signed amount, a running balance, a category path, and nothing else |

`ledger` is the minimal useful shape -
`posted,description,amount,balance,account,category tree` - for an export
with no merchant and no location columns. With no Direction column and no
debit/credit pair, the **sign is the only thing saying which way the
money went**, so the amount has to be signed already; if yours arrives
with purchases positive, set `flip_sign: true` rather than editing the
CSV. One example row set is in `data/samples/formats/ledger__example.csv`.

A **balance column is worth having.** It lands in
`transactions.statement_balance`, which is what `make reconcile`
compares against, so you get the check for free - and it makes each
account's opening balance derivable rather than something you have to
go and look up:

```sql
-- what to pass to `make set-opening` for each account
SELECT account, -statement_diff AS opening_balance FROM v_reconciliation;
```

**Naming files `<mapping>__<period>.csv`** lets `make import-inbox` pick
the mapping itself, and moves each file to `data/archive/` when done.

### How amounts are read

Banks disagree about this more than anything else, so the mapping says
which convention the file uses:

| `mode` | The file looks like |
|---|---|
| `signed` | one amount column, already signed |
| `signed` + `flip_sign` | signed, but backwards (Amex-style: purchases positive) |
| `debit_credit` | two columns, at most one filled |
| `direction` | an unsigned amount plus Withdraw/Deposit/Transfer/Receive |
| `magnitude_with_type` | an unsigned amount plus a free-text type column |

A mis-mapped sign is the one error that silently inverts every chart, so
`direction` is stored on the transaction rather than just used and
discarded, and a `CHECK` keeps the two consistent. `make directions`
shows how each one is being used.

### Re-importing is safe

Every row gets an `external_id` — the bank's own id where the file has
one, otherwise a deterministic hash of the row — and
`UNIQUE (account_id, external_id)` does the rest. Overlapping exports,
the same file twice, a month you already loaded: all no-ops. `make
stats` shows what landed.

### One file, several accounts

A ledger-style export often covers everything at once. Name the column
and each row resolves its own account:

```yaml
columns:
  account: Account
account_types:
  AFCU Checking: Checking
  AFCU Visa Classic: Credit
```

List every account in `account_types`. A name that is not there gets its
type guessed from the text, and the guess is wrong for most card names —
"Chase Sapphire" and "Capital One Venture" both look like checking
accounts to a keyword match. Account names are matched exactly and
case-sensitively, so inconsistent capitalisation in one file creates two
accounts.

### Rows that don't parse

Nothing is dropped. Every row is stored verbatim in `staging_rows` as it
is read, so a row that fails is recorded with its reason rather than
lost:

```bash
make psql < db/scripts/maintenance.sql   # section 2 lists the failures
```

Fix the mapping and `make replay` re-runs the stored rows against
today's rules. Rows that already produced a transaction are **updated in
place**, so receipt line items, tags and transfer pairings survive;
nothing is deleted. `make replay-dry` shows what would change.

Two guards earn their keep here. A row with more fields than the header
is refused with the likely cause named — that is what an unquoted
`$2,450.00` looks like, and it shifts every later column. And an account
name that looks like money or a bare number is refused outright, because
that only happens when a row has shifted.

### Balances that match the bank

Transactions alone give you *change*, not *balance*. Set each account's
starting point once:

```bash
make set-opening ACCOUNT="Chase Checking" BALANCE=5170.11 ASOF=2026-09-30
make reconcile
```

`make reconcile` compares the computed balance against whatever the
bank told you — a statement balance carried in the CSV, or a snapshot
you recorded. If it disagrees, every chart above it is wrong, so this is
the first thing to check after an import.

## Categories

A self-referencing tree, three levels in practice. Every transaction
carries one category id; the views pre-compute `roll1`, `roll2` and
`roll3` labels so a chart can group at any depth and still total
correctly — anything tagged shallower than the level being charted shows
as `"Food (unspecified)"` rather than dropping out.

Refer to a category by its **full path** — `Food > Groceries`. A bare
leaf name is accepted, but if it exists in more than one branch it
resolves to whichever sits closest to the root and logs a warning. The
seeded tree has no duplicates; add `Health > Insurance` alongside
`Housing > Insurance` and bare `Insurance` becomes a coin flip.

A CSV naming a leaf the tree has never seen gets it created under
`Imported` rather than at the top level, where it would make the
roll-ups meaningless. File those afterwards:

```bash
make unfiled
make move-category NAME="Amazon Shopping" UNDER="Personal > Household Goods"
```

Renaming and re-parenting keep every transaction attached, because
transactions reference the id and not the name.
`db/scripts/categories.sql` has the subtree queries, the duplicate-name
check and the merge pattern.

## Merchants

A separate table, not a column, because one business arrives spelled
several ways: `SQ *BLUE BOTTLE 1234`, `BLUE BOTTLE COFFEE #12`,
`TST* BLUE BOTTLE`. A `merchant_aliases` row maps a pattern to a
merchant and optionally to a category, so all three land on one bar of a
chart:

```bash
make unmatched                      # descriptions no rule matches yet
make alias MERCHANT="King Soopers" PATTERN="%KING SOOPERS%"
make categorize                     # apply rules to rows already imported
```

Patterns are ILIKE unless `match_type` is `regex`, and the lowest
priority number wins — which is how `%KING SOOPERS FUEL%` → Transportation
beats `%KING SOOPERS%` → Food for the pump at the grocery store.

## Locations

A place is identified by **coordinates and name together**. Coordinates
alone are not enough in either direction: the same storefront jitters by
a few metres between visits, so exact matching splits it in two, while a
mall or a strip of units geocodes to a single point, so proximity
matching alone merges unrelated businesses. Matching is therefore within
a radius *and* only when the place name agrees.

`0,0` is dropped on import — that is a geocoder saying "no idea", not
the Gulf of Guinea. Rows imported before your mapping had coordinate
columns can be filled in later from the stored raw rows with
`make backfill-locations`, and `make locations` reports how much of your
spending has a place attached. Read any map against that number.

## Budgets

`budget_plans` holds **intent — one row per change**, not one per month.
Set a budget once and it rolls forward until you change it; changing it
is one new row, and the old value stays as history.

`amount` is the cost **per period**, so an annual $1,200 bill is
`(1200.00, 'annual')` and accrues at $100 a month.

### Two kinds of category

`carry_forward = false` is **flow spending** — groceries, fuel. Each
month stands alone with its own variance.

`carry_forward = true` is a **periodic cost**, and the comparison
becomes cumulative. An annual bill accrues monthly, so eleven quiet
months fund the twelfth instead of looking like underspend. Without
carry-forward, accrual budgeting is actively misleading: eleven months
of false underspend followed by one catastrophic month.

The envelope balance is **derived**, not stored — a stored balance
drifts out of step the moment a transaction is recategorised.

### An envelope never goes negative

Overspending an envelope **resets it to zero** rather than carrying a
deficit forward. An envelope is money set aside, and you cannot have
less than none of it:

```
before:  -200 -> -100 ->   0 -> 100     the deficit had to be repaid first
after:      0 ->  100 -> 200 -> 300     accrual resumes immediately
```

The overspend is forgiven, not hidden. Flooring the balance alone would
throw away the one thing worth knowing, so the forgiven amount is kept
as **`envelope_shortfall`**, set in the month the envelope bottomed out.
That column — not a minus sign — is the warning, and it is what the
*Underfunded* tile counts.

### Loading a budget

Design it in a spreadsheet and import it. Two shapes are accepted, and
the right one is detected from the headers:

```csv
category,effective_from,amount,period,carry_forward,opening_balance,note
Food,2026-01,800,monthly,no,,groceries and eating out
Health,2026-01,1200,annual,yes,400,insurance premium
```

```csv
Category,2026-01,2026-02,2026-03,2026-04,period,carry forward
Food,800,800,800,850,monthly,no
Personal,100,100,100,,monthly,no
```

```bash
make import-budget CSV=data/inbox/budget.csv DRY=1   # read it, write nothing
make import-budget CSV=data/inbox/budget.csv         # load it
make budget                                           # where you stand now
```

**Always dry-run first.** It prints every plan row it would write plus
the total monthly accrual, and comparing that one number against your
spreadsheet's total is the fastest check that the file was read the way
you meant.

A month-by-month grid is melted and then **collapsed**: a plan row is
written only where the amount changes, because storing one row per month
would turn the intent table back into the grid the schema exists to
avoid. A blank month amount stops budgeting that category from then on;
a blank month *before* the first value is simply a month before the
budget started. Spreadsheet money formats (`$1,200.50`, `1,500`), period
spellings (`semi-annual`, `Annually`, `6`, `12`) and yes/no variants all
parse. Dates like `2026-03-15` snap to the 1st and say so.

**Nothing is written unless every row resolves.** A budget file is small
and hand-made, so a typo that silently dropped one category would be
worse than a failed load. Pass `SKIPBAD=1` to load the rest anyway.
Budgets never create categories, which is why a misspelling is an error
rather than a new top-level category appearing in your charts. Files
live under `data/`, the only directory the container can see.

The samples in `data/samples/budget/` work with both commands.

### Budget at one level per branch

Actuals roll up, so a budget on a top-level category covers it and every
descendant. Budget at *either* the top level or its children, not both:
if `Food` and `Food > Groceries` both have plans, each row is correct on
its own but the totals count the same money twice.
`db/scripts/budgets.sql` has the plan patterns and the overspend
queries.

## Tags

A category says what a transaction *is*, exactly once. A tag says
anything else you want, as often as you like:

| Tag | What it answers |
|---|---|
| `reimbursable` | which spending is coming back to you |
| `vacation-mexico` | what one trip cost across every category |
| `2026-taxes`, `fsa-claim` | what to hand over in April |
| `one-off` | what not to read as a trend |

None of those work as categories. A client dinner is still
`Food > Dining`; filing it under "Reimbursable" would make your Food
total lie.

Tags are added by hand, in SQL. One statement does it:

```sql
SELECT tag_transaction(44, 'reimbursable');
SELECT untag_transaction(44, 'reimbursable');
```

It creates the tag if it is new, is harmless to run twice, and raises on
a transaction id that does not exist rather than quietly doing nothing.
Names normalise to lowercase and trimmed, so `Reimbursable` and
`  REIMBURSABLE  ` are one tag. The Tags dashboard's bottom tables list
candidate ids; `db/scripts/tags.sql` has the bulk patterns, including
tagging a whole category subtree.

### Never join `transaction_tags` directly

It is many-to-many, so joining it into a spending query multiplies rows
— a transaction with two tags is counted twice and the total silently
inflates:

```sql
-- correct
SELECT sum(v.outflow) FROM v_spending v
LEFT JOIN v_transaction_tags tt ON tt.transaction_id = v.id;

-- wrong: a two-tag transaction counted twice
SELECT sum(v.outflow) FROM v_spending v
LEFT JOIN transaction_tags tt ON tt.transaction_id = v.id;
```

Join **`v_transaction_tags`**, which is aggregated to one row per
transaction, or filter with `EXISTS`. For the same reason, the per-tag
figures on the dashboard do not add up to total spending and are not
meant to: each row answers "what did this tag cost".

## Dashboards

Five, provisioned from `grafana/dashboards/` and loaded within 30
seconds of a file changing. Panels query views, so the schema can shift
underneath them.

| Dashboard | What it is for |
|---|---|
| **Finance Overview** | one screen: in, out, net worth, where and on what |
| **Category Detail** | drill category → subcategory → subdivision, with averages, spread and a map |
| **Monthly Review** | one month in context, plus a budget strip and the full ledger |
| **Budget** | where the budget stands now and where it is heading |
| **Tags** | what each tag costs, net of anything that came back |

Four share an **Accounts** filter. Budget has none — a budget belongs to
a category, whichever card paid it.

Every multi-value filter is written as an array test rather than
`col IN (...)`. Grafana substitutes *nothing* for a variable with no
options, so the plain form becomes `IN ()`, a syntax error — which on a
fresh install, before the first import, is the first thing you would
see.

### Editing them

Grafana is the author. Dashboards are read-only by provisioning
(`allowUiUpdates: false`), so to keep a change made in the UI use **Save
as**, export the JSON, and commit it to `grafana/dashboards/`.

The committed files are **schema v2** (`dashboard.grafana.app/v2`), what
Grafana 13 exports: panels in `spec.elements` keyed by name, positions
in a separate `spec.layout`, variables in `spec.variables`.

**Strip the metadata before committing an export.** A fresh export
carries facts about the instance it came from - a `grafana.app/folder`
UID, a `resourceVersion`, a provisioning checksum. The folder UID is the
one that bites: it will not exist in another Grafana, and a mismatch
against the provider's own folder makes the dashboard fail to load, so
the folder looks empty with nothing said in the UI. Keep only the
identity:

```json
{
  "apiVersion": "dashboard.grafana.app/v2",
  "kind": "Dashboard",
  "metadata": { "name": "plumb-tags" },
  "spec": { ... }
}
```

**If dashboards still do not appear**, read the log - provisioning
failures are logged, never surfaced in the UI:

```bash
docker compose logs grafana | grep -iE "provision|dashboard.*error"
```

Classic file provisioning of v2 files has been patchy across 13.x
releases. If yours is one of the unhappy ones, the same five dashboards
are included in the older v1 schema under `grafana/dashboards-v1/`.
Point the provider at them in
`grafana/provisioning/dashboards/dashboards.yml`, mount that directory
in `docker-compose.yml` alongside the other, and
`docker compose restart grafana`.

Those files are generated from the v2 exports by `grafana/v2-to-v1.py`
and verified identical in content - same panels, titles, types, sizes,
positions, queries and variables. Re-run it after any future export:

```bash
python3 grafana/v2-to-v1.py grafana/dashboards/*.json -o grafana/dashboards-v1
```

## Time zones

**Never hand Grafana a bare `date` or a naive `timestamp`.** It reads
both as UTC and then shifts them by the dashboard time zone, so
`2026-11-02` displays as `2026-11-01 18:00` in Denver. This is the
single most confusing thing about the stack, and it is Grafana's
behaviour rather than something the schema can fix.

The rules the views follow:

- a **time axis** gets `timestamptz`, built as
  `(posted_on::timestamp AT TIME ZONE 'America/Denver')`
- a **displayed date** gets text: `to_char(posted_on, 'YYYY-MM-DD')`
- **grouping** happens on the `date` column, then casts back

Set your zone in `.env` (`TZ`), which the containers pick up. The views
name the zone explicitly where it matters; change it there if you move.

## The CLI

`plumb` runs in the one-shot `etl` container. Every command has a
`make` target — `make help` lists them all.

| Command | Does |
|---|---|
| `migrate` | apply the schema; re-asserts Grafana's read-only login |
| `import` / `import-inbox` | load one CSV, or a directory of them |
| `import-budget` | load a budget spreadsheet |
| `import-refs` | resolve a source system's merchant/location ids |
| `categorize` | apply merchant rules to rows already imported |
| `link-transfers` | pair the legs of account transfers |
| `replay` | re-run stored raw rows against today's rules |
| `backfill-locations` | attach places to rows imported without them |
| `move-category` | re-parent a category |
| `add-alias` | map a description pattern to a merchant |
| `set-opening` / `record-balance` | starting balances and snapshots |
| `budget`, `stats`, `categories`, `locations`, `directions`, `refs`, `unmatched`, `reconcile`, `mappings` | read-only reports |
| `doctor` | diagnose database and Grafana login problems |

## SQL scripts

`db/scripts/` holds runnable SQL per feature. Read-only queries run
as-is; anything that writes is commented out, so you can run a whole
file to see the state of something and then uncomment what you need.

```bash
make scripts                              # list them
make psql < db/scripts/maintenance.sql    # run one
```

| Script | Covers |
|---|---|
| `categories.sql` | the tree, subtree queries, duplicates, renaming and merging |
| `budgets.sql` | plan status, envelopes, overrides, setting budgets by hand |
| `tags.sql` | finding things to tag, bulk tagging, the fan-out demonstrated in dollars |
| `locations.sql` | coverage, top places, shared coordinates, fixing bad ones |
| `merchants.sql` | the rule to-do list, priorities, merging merchants |
| `maintenance.sql` | **start here after an import** — reconciliation, failed rows, duplicates, gaps |
| `signs.sql` | which way the money points — run it after adding an account or a mapping |

## Operating

### Running more than one stack

Two people on one machine, or a test copy alongside your real one. Put
each in its own directory and give each its own identity in `.env`:

```bash
# ~/alice/plumb/.env
COMPOSE_PROJECT_NAME=alice
POSTGRES_PORT=5432
GRAFANA_PORT=3000

# ~/bob/plumb/.env
COMPOSE_PROJECT_NAME=bob
POSTGRES_PORT=5433
GRAFANA_PORT=3001
```

That is the whole change. `COMPOSE_PROJECT_NAME` namespaces everything
Docker creates, and the ports are the only host resource the two copies
compete for:

| | alice | bob |
|---|---|---|
| volumes | `alice_db_data`, `alice_grafana_data` | `bob_db_data`, `bob_grafana_data` |
| network | `alice_default` | `bob_default` |
| containers | `alice-db`, `alice-grafana`, `alice-etl` | `bob-db`, `bob-grafana`, `bob-etl` |
| ETL image | `alice-etl:local` | `bob-etl:local` |
| Grafana | localhost:3000 | localhost:3001 |

Nothing is shared. `data/`, `backups/` and the dashboards live in each
directory, each Grafana keeps its own users and preferences in its own
volume, and `make reset` in one stack cannot touch the other.

**Why the project name matters more than it looks.** It is what
separates the *volumes*. Two copies sharing a project name would mount
the same Postgres data directory — one database holding two people's
finances. As shipped you would not get that far: the container names
collide first and Docker refuses to start the second stack. But it is a
confusing error for a dangerous mistake, so set the name deliberately
rather than relying on the collision.

The ETL image is keyed off the project name too. Without that, `make
build` in one stack would overwrite the image the other is running —
harmless while the code matches, silently confusing when it does not.

Every `make` target picks the name up from `.env`, and `make up` prints
which stack it started.

**Backups.** `make backup` writes a timestamped dump to `backups/`;
`make restore DUMP=backups/plumb-x.dump` puts one back. The database
lives in a Docker volume, so `make down` keeps it and `make reset`
destroys it.

**Passwords.** Only the Grafana reader's password can be changed by
editing `.env` — `make migrate` re-asserts it on every run. The
application user's password is set once, when the data directory is
initialised, and `make set-db-password` is what changes it afterwards.
`make check-db` tells you whether `.env` and the database agree.

**Grafana's login.** Grafana connects as a read-only role created on the
database's first boot. Because `db/init/` only ever runs on first boot,
`make migrate` re-asserts the role and its password from `.env` on every
run — so changing the password in `.env` is enough. `make doctor`
diagnoses it when Grafana cannot connect.

**Grafana version** is pinned in `docker-compose.yml`. Take a backup
before bumping, and expect provisioned dashboards to be migrated to a
newer schema on load.

**The schema** is a baseline, not a history: `001_schema.sql` plus
`002_views.sql` plus `003_seed.sql` is the whole thing, generated from a
running database and verified by rebuilding from it. `make migrate`
tracks what it has applied, with checksums, in `schema_migrations`.
Future changes go in as `004_*.sql` and up — append, never edit an
applied file.

**Tests.** `make test` runs 239 unit tests covering sign conventions,
date parsing, deduplication, location matching, category resolution,
budget parsing and replay scope. They need no database: every one runs
against the parsing and resolution layer directly, in under a second.

**Packaging your own build.** After changing anything, rebuild the
shareable archive with:

```bash
make package
```

It writes `dist/plumb-<timestamp>.tar.gz`, which extracts to
`plumb/` and is ready for `make quickstart`. `dist/` is
gitignored. To name it something else:

```bash
make package PKG_NAME=plumb-jason
```

The exclusions mirror `.gitignore` — no `.env`, no `__pycache__`, no
`backups/`, and nothing from `data/inbox` or `data/archive` but the
`.gitkeep` files. It then checks the archive it just built and **deletes
it rather than hand you one that leaks**:

- a `.env`, a cache, a backup or a `.dump` → refuses and removes the archive
- anything of yours left in `data/inbox` or `data/archive` → warns and
  names the files, but still builds; the extension filters only catch
  `.csv`, `.ofx` and `.qfx`, so a PDF or spreadsheet statement sitting
  there would otherwise ship
- a missing `Makefile`, `README.md`, `docker-compose.yml`, `.env.example`,
  migration or `Dockerfile` → fails, so an archive that cannot install
  never reaches anyone

Run that check against any archive on its own, including one you did not
build here:

```bash
make package-check ARCHIVE=dist/plumb-20261111-180532.tar.gz
```

Run `make test` before packaging. `make package` deliberately does not
run the tests for you, so that packaging still works when something is
mid-change — which also means a broken build will package quite happily.

**Upgrading an install that predates the name.** This project was called
`finance` before it was called Plumb. Your `.env` pins
`COMPOSE_PROJECT_NAME`, `POSTGRES_DB` and `POSTGRES_USER`, and those
override the new defaults — so your volumes, database and data are
untouched by the rename, and you do not have to change `.env` at all.

Two things do need doing, in this order:

```bash
make build      # the old image has the old CLI installed
make up
```

The ETL image is tagged from your project name and already exists, so
Docker will not rebuild it on its own — and the old one has `finetl`
inside, not `plumb`, so every `make` command that touches the ETL
container fails on the missing executable until you rebuild.

Grafana will also replace the five provisioned dashboards, because their
UIDs changed from `finance-*` to `plumb-*`. Nothing is lost — the
dashboards are files, and your data is in Postgres — but saved links and
stars point at the old UIDs and will 404.

If you would rather your containers and database carry the new name too,
that is a data migration rather than a rename: `make backup`, change the
three values in `.env`, `make reset`, `make up && make migrate`, then
`make restore DUMP=backups/plumb-<timestamp>.dump`.

## Troubleshooting

**`password authentication failed for user "plumb"`.** The database
volume is older than the password in `.env`. Postgres only applies
`POSTGRES_PASSWORD` when it initialises an **empty** data directory, so a
volume left from an earlier run still wants the password that was in
`.env` at that time — editing `.env` afterwards changes nothing, and
`make down` keeps the volume. The healthcheck does not authenticate, so
the stack looks healthy until something actually connects.

```bash
make set-db-password    # change the database to match .env
make reset              # or start over - DESTROYS the data
```

`make check-db` tests the credentials on their own, and `make quickstart`
now refuses to run at all when a volume for this project already exists,
since quickstart means starting from nothing.

**No dashboards appear in Grafana at all.** A provisioning failure, and
it is silent in the UI. `docker compose logs grafana | grep -i provision`
shows the reason. The usual cause is instance-specific metadata left in a
committed export — see [Editing them](#editing-them).

**Grafana shows "No data" everywhere.** Check `make stats` has rows,
then `make doctor` for the login.

**Spending looks far too high.** Transfers between your own accounts are
counting as spending. In order:

```bash
make link-transfers                          # pairs legs you have both of
make psql < db/scripts/categories.sql        # section 5b: what is flagged
```

If a transfer category is not flagged, flag it — that is what catches a
credit card payment whose card you have not imported, which pairing can
never reach:

```sql
UPDATE categories SET is_internal = true
 WHERE id = (SELECT id FROM v_category_tree WHERE full_path = 'Transfer');
```

`make directions` shows unpaired legs if your file has a direction
column.

**Balances disagree with the bank.** `make reconcile`. Almost always a
missing opening balance.

**Net worth looks wrong on a loan or card.** Check which number you are
reading. `make stats` shows `balance` (as stored) beside `net_worth` (as
it contributes), and on a `Credit` or `Loan` account those differ by
design — see [Core conventions](#core-conventions) #1.

If it still looks wrong, the signs are the thing to audit:

```bash
make psql < db/scripts/signs.sql
```

It separates the three cases, because they have three different fixes: a
hand-entered figure typed with the wrong sign (one `UPDATE`), imported
transactions that the mapping never normalised (fix the mapping, then
`plumb replay --fields amounts` — *not* SQL), and a plain missing
opening balance. Never flip transaction signs with a blanket `UPDATE` by
account type: the imported rows are normally correct, and that would
turn good data into bad.

**Net worth looks like one account, not all of them.** There are two net
worth views, reading two different sources. `v_net_worth_daily` computes
from opening balances plus transactions, and covers every account.
`v_net_worth` sums the balances you have recorded with
`make record-balance`, carrying each account's latest figure forward — so
it only covers accounts you have recorded at least once. Its `accounts`
column says how many that is; a number below your account count means a
partial total.

**A date is one day earlier than it should be.** A panel is handing
Grafana a bare date — see [Time zones](#time-zones).

**A category shows as `X (unspecified)`.** That transaction is
categorised shallower than the depth you are charting. It is deliberate:
the alternative is for it to vanish and the slices to stop summing.

**Everything in one category called `Imported`.** Your CSV named leaves
the tree had never seen. `make unfiled`, then `make move-category`.

**An import created an account called `$2` or `100.00`.** A row had
unquoted commas and shifted. The guard refuses the account; fix the
quoting in the CSV and re-import — duplicates are impossible, so
re-importing is free.
