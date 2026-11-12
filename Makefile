# Thin wrappers around docker compose. `make help` lists everything.
SHELL := /bin/bash
COMPOSE := docker compose
ETL := $(COMPOSE) run --rm etl
DATE := $(shell date +%Y%m%d-%H%M%S)
# What `make package` calls the archive and the folder inside it. Override
# per build: make package PKG_NAME=plumb-jason
PKG_NAME ?= plumb

# The etl service mounts ./data at /data, and its workdir is /app - so a
# host-relative path like data/inbox/x.csv has to gain a leading slash to
# be found inside the container. Absolute paths are passed through.
container_path = $(if $(filter /%,$(1)),$(1),/$(1))

-include .env
export

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: init
init: ## Create .env from the template (first-time setup)
	@test -f .env || (cp .env.example .env && echo "Created .env - edit the passwords, then run: make up")

.PHONY: quickstart
quickstart: ## Nothing to a working dashboard: .env, stack, schema, sample data
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env. Open it and set:"; \
		echo "  - the three passwords"; \
		echo "  - COMPOSE_PROJECT_NAME, if this is not your only stack"; \
		echo "  - POSTGRES_PORT and GRAFANA_PORT, if those are taken"; \
		echo; \
		echo "Then run 'make quickstart' again."; \
		exit 1; \
	fi
	@P=$${COMPOSE_PROJECT_NAME:-plumb}; \
	if docker volume inspect "$$P"_db_data >/dev/null 2>&1; then \
		echo "A database volume for project '$$P' already exists:"; \
		echo "  $$P""_db_data"; \
		echo; \
		echo "quickstart starts from nothing, and the password in .env will NOT"; \
		echo "apply to an existing database - postgres only sets it when the data"; \
		echo "directory is empty. Pick one:"; \
		echo; \
		echo "  keep that data:   make up && make migrate"; \
		echo "                    (if the app cannot connect: make set-db-password)"; \
		echo "  start over:       make reset   (DESTROYS it), then make quickstart"; \
		exit 1; \
	fi
	@$(MAKE) --no-print-directory up
	@$(MAKE) --no-print-directory check-db
	@echo "waiting for the database..."
	@until $(COMPOSE) exec -T db pg_isready -q 2>/dev/null; do sleep 1; done
	@$(MAKE) --no-print-directory migrate
	@$(MAKE) --no-print-directory demo
	@echo
	@echo "Grafana: http://localhost:3000  (admin / the password in .env)"
	@echo "Five dashboards are provisioned under the Finance folder."
	@echo
	@echo "That was sample data. To start on your own:"
	@echo "  make reset && make up && make migrate"
	@echo "  then drop your exports in data/inbox and: make import-inbox"

.PHONY: demo
demo: ## Load the bundled sample exports, so there is something to look at
	$(ETL) plumb import-inbox /data/samples
	$(ETL) plumb link-transfers
	$(ETL) plumb import-budget --csv /data/samples/budget/budget_long.csv
	$(ETL) plumb stats
	@echo
	@echo "One row fails on purpose - a latitude of 999 in geo_card, so you can"
	@echo "see how a bad row is reported rather than lost. It is kept verbatim:"
	@echo "  make psql < db/scripts/maintenance.sql    (section 2)"

.PHONY: up
up: ## Start database and Grafana
	@P=$${COMPOSE_PROJECT_NAME:-plumb}; \
	OTHER=$$(docker inspect "$$P-db" \
		--format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
		2>/dev/null); \
	if [ -n "$$OTHER" ] && [ "$$OTHER" != "$$(pwd)" ]; then \
		echo "refusing to start: project name '$$P' is already in use by"; \
		echo "  $$OTHER"; \
		echo "Starting here would take over that stack's containers and its"; \
		echo "database volume. Set a different COMPOSE_PROJECT_NAME in .env."; \
		exit 1; \
	fi
	$(COMPOSE) up -d db grafana
	@echo "Stack '$${COMPOSE_PROJECT_NAME:-plumb}' is up."
	@echo "Grafana: http://localhost:$${GRAFANA_PORT:-3000}"

.PHONY: down
down: ## Stop the stack (data volumes are kept)
	$(COMPOSE) down

.PHONY: logs
logs: ## Follow container logs
	$(COMPOSE) logs -f

.PHONY: build
build: ## Rebuild the ETL image
	$(COMPOSE) build etl

.PHONY: check-db
check-db: ## Verify the credentials in .env actually work against the database
	@U=$${POSTGRES_USER:-plumb}; D=$${POSTGRES_DB:-plumb}; \
	if ! $(COMPOSE) exec -T db pg_isready -q 2>/dev/null; then \
		echo "the database is not accepting connections yet - is it up?"; \
		echo "  make up        start it"; \
		echo "  make logs      see why it stopped"; \
		exit 1; \
	fi; \
	if $(COMPOSE) exec -T -e PGPASSWORD="$$POSTGRES_PASSWORD" db \
		   psql -h 127.0.0.1 -U "$$U" -d "$$D" -c 'SELECT 1' >/dev/null 2>&1; then \
		echo "database credentials in .env work."; \
	else \
		echo "the password in .env is NOT the database's password."; \
		echo; \
		echo "postgres only applies POSTGRES_PASSWORD when it initialises an"; \
		echo "empty data directory. If this volume was created by an earlier run,"; \
		echo "it still wants the password that was in .env at that time, and"; \
		echo "changing .env afterwards has no effect."; \
		echo; \
		echo "  adopt the .env password:  make set-db-password"; \
		echo "  or start over (DESTROYS the data):  make reset"; \
		exit 1; \
	fi

.PHONY: set-db-password
set-db-password: ## Re-set the database password to whatever .env now says
	@U=$${POSTGRES_USER:-plumb}; D=$${POSTGRES_DB:-plumb}; \
	echo "setting role '$$U' password from .env..."; \
	$(COMPOSE) exec -T db psql -v ON_ERROR_STOP=1 -U "$$U" -d "$$D" \
		-c "ALTER USER \"$$U\" PASSWORD '$$POSTGRES_PASSWORD'" >/dev/null
	@$(MAKE) --no-print-directory check-db

.PHONY: migrate
migrate: ## Apply pending database migrations
	$(ETL) plumb migrate

.PHONY: migrate-status
migrate-status: ## Show which migrations have been applied
	$(ETL) plumb migrate --status

.PHONY: import
import: ## Import one CSV: make import FILE=data/inbox/x.csv MAPPING=chase_credit
	@test -n "$(FILE)" || (echo "usage: make import FILE=data/inbox/x.csv MAPPING=<name>"; exit 1)
	@test -n "$(MAPPING)" || (echo "usage: make import FILE=data/inbox/x.csv MAPPING=<name>"; exit 1)
	$(ETL) plumb import --mapping $(MAPPING) /$(FILE)

.PHONY: import-inbox
import-inbox: ## Import every CSV in data/inbox using filename prefixes as mappings
	$(ETL) plumb import-inbox /data/inbox --archive /data/archive

.PHONY: categorize
categorize: ## Apply merchant rules to rows imported before the rule existed
	$(ETL) plumb categorize

.PHONY: record-balance
record-balance: ## Record what the bank says: make record-balance ACCOUNT="x" BALANCE=1234.56 [ASOF=2026-11-30]
	@test -n "$(ACCOUNT)" || (echo 'usage: make record-balance ACCOUNT="x" BALANCE=1234.56 [ASOF=YYYY-MM-DD]'; exit 1)
	@test -n "$(BALANCE)" || (echo 'usage: make record-balance ACCOUNT="x" BALANCE=1234.56 [ASOF=YYYY-MM-DD]'; exit 1)
	$(ETL) plumb record-balance --account "$(ACCOUNT)" --balance $(BALANCE) \
		$(if $(ASOF),--as-of $(ASOF),)

.PHONY: link-transfers
link-transfers: ## Pair up opposite legs of account-to-account transfers
	$(ETL) plumb link-transfers

.PHONY: unmatched
unmatched: ## List bank descriptions with no merchant alias yet
	$(ETL) plumb unmatched --limit 40

.PHONY: alias
alias: ## Add a merchant alias: make alias MERCHANT="King Soopers" PATTERN="%KING SOOPERS%"
	$(ETL) plumb add-alias --merchant "$(MERCHANT)" --pattern "$(PATTERN)"

.PHONY: directions
directions: ## Direction usage and monthly cash flow
	$(ETL) plumb directions

.PHONY: refs
refs: ## Source merchant/location ids not yet resolved
	$(ETL) plumb refs

.PHONY: import-refs
import-refs: ## Resolve source ids: make import-refs KIND=merchant FILE=data/inbox/merchants.csv
	@test -n "$(KIND)" || (echo 'usage: make import-refs KIND=merchant|location FILE=path'; exit 1)
	@test -n "$(FILE)" || (echo 'usage: make import-refs KIND=merchant|location FILE=path'; exit 1)
	$(ETL) plumb import-refs --kind $(KIND) --csv /$(FILE)

.PHONY: unfiled
unfiled: ## Categories auto-created on import, waiting to be filed
	$(ETL) plumb categories --unfiled

.PHONY: move-category
move-category: ## Re-parent: make move-category NAME="Amazon Shopping" UNDER="Personal > Household Goods"
	@test -n "$(NAME)" || (echo 'usage: make move-category NAME="x" UNDER="A > B"'; exit 1)
	$(ETL) plumb move-category --name "$(NAME)" --under "$(UNDER)"

.PHONY: import-budget
import-budget: ## Load a budget spreadsheet: make import-budget CSV=budget.csv [DRY=1]
	@test -n "$(CSV)" || (echo 'usage: make import-budget CSV=data/inbox/budget.csv [DRY=1] [YEAR=2026]'; exit 1)
	$(ETL) plumb import-budget --csv "$(call container_path,$(CSV))" \
		$(if $(DRY),--dry-run,) $(if $(YEAR),--year $(YEAR),) \
		$(if $(SHAPE),--shape $(SHAPE),) $(if $(SKIPBAD),--skip-bad,)

.PHONY: budget
budget: ## Where each category stands against budget this month
	$(ETL) plumb budget $(if $(HISTORY),--history $(HISTORY),)

.PHONY: replay
replay: ## Re-run the stored raw rows against today's rules (safe: no deletes)
	$(ETL) plumb replay $(if $(BATCH),--batch $(BATCH),)

.PHONY: replay-dry
replay-dry: ## Show what a replay would change, writing nothing
	$(ETL) plumb replay --dry-run $(if $(BATCH),--batch $(BATCH),)

.PHONY: backfill-locations
backfill-locations: ## Attach locations to rows imported before the mapping had coordinates
	$(ETL) plumb backfill-locations

.PHONY: locations
locations: ## Location coverage and your top places
	$(ETL) plumb locations

.PHONY: doctor
doctor: ## Diagnose database / Grafana login problems
	$(ETL) plumb doctor

.PHONY: ensure-reader
ensure-reader: ## Create or repair Grafana's read-only login from .env
	$(ETL) plumb ensure-reader
	$(COMPOSE) restart grafana

.PHONY: reconcile
reconcile: ## Compare computed balances against the bank's
	$(ETL) plumb reconcile

.PHONY: set-opening
set-opening: ## Set a starting balance: make set-opening ACCOUNT="Chase Checking" BALANCE=5170.11 ASOF=2026-09-30
	@test -n "$(ACCOUNT)" || (echo 'usage: make set-opening ACCOUNT="name" BALANCE=0.00 [ASOF=YYYY-MM-DD]'; exit 1)
	$(ETL) plumb set-opening --account "$(ACCOUNT)" --balance "$(BALANCE)" \
		$(if $(ASOF),--as-of $(ASOF),)

.PHONY: mappings
mappings: ## List and validate the CSV mappings
	$(ETL) plumb mappings

.PHONY: stats
stats: ## Row counts and date coverage per account
	$(ETL) plumb stats

.PHONY: test
test: ## Run the ETL unit tests
	$(COMPOSE) run --rm --no-deps etl pytest -q

.PHONY: scripts
scripts: ## List the SQL scripts under db/scripts
	@echo "Runnable with: make psql < db/scripts/<name>.sql"
	@for f in db/scripts/*.sql; do \
		printf '  \033[36m%-16s\033[0m %s\n' "$$(basename $$f)" \
		"$$(sed -n '2s/^-- [a-z]*\.sql - //p' $$f)"; \
	done

.PHONY: categories
categories: ## Print the category tree as rows
	$(ETL) plumb categories

.PHONY: psql
psql: ## Open a psql shell
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-plumb} -d $${POSTGRES_DB:-plumb}

.PHONY: backup
backup: ## Dump the database to backups/
	@mkdir -p backups
	$(COMPOSE) exec -T db pg_dump -U $${POSTGRES_USER:-plumb} -d $${POSTGRES_DB:-plumb} -Fc \
		> backups/plumb-$(DATE).dump
	@echo "Wrote backups/plumb-$(DATE).dump"

.PHONY: restore
restore: ## Restore a dump: make restore DUMP=backups/plumb-x.dump
	@test -n "$(DUMP)" || (echo "usage: make restore DUMP=backups/plumb-x.dump"; exit 1)
	$(COMPOSE) exec -T db pg_restore -U $${POSTGRES_USER:-plumb} -d $${POSTGRES_DB:-plumb} \
		--clean --if-exists < $(DUMP)

.PHONY: reset
reset: ## DESTRUCTIVE: delete all data and volumes
	@read -p "Delete the database and all imported data? [y/N] " ok; [[ $$ok == [yY] ]]
	$(COMPOSE) down -v

.PHONY: package
package: ## Build a shareable tar.gz of the project into dist/
	@test -f db/migrations/001_schema.sql || \
		(echo "error: run this from the project root"; exit 1)
	@mkdir -p dist
	@tar --exclude-vcs \
		--exclude='./dist' --exclude='./backups' \
		--exclude='__pycache__' --exclude='*.py[cod]' \
		--exclude='.pytest_cache' --exclude='*.egg-info' \
		--exclude='.ruff_cache' --exclude='.venv' --exclude='venv' \
		--exclude='./.env' --exclude='*.dump' --exclude='*.sql.gz' \
		--exclude='./data/inbox/*.csv'   --exclude='./data/inbox/*.ofx' \
		--exclude='./data/inbox/*.qfx' \
		--exclude='./data/archive/*.csv' --exclude='./data/archive/*.ofx' \
		--exclude='./data/archive/*.qfx' \
		--transform 's,^\./,$(PKG_NAME)/,' \
		-czf dist/$(PKG_NAME)-$(DATE).tar.gz . 2>/dev/null
	@$(MAKE) --no-print-directory package-check \
		ARCHIVE=dist/$(PKG_NAME)-$(DATE).tar.gz
	@echo
	@echo "Wrote dist/$(PKG_NAME)-$(DATE).tar.gz"
	@echo "  extracts to $(PKG_NAME)/ - unpack it anywhere and run: make quickstart"

.PHONY: package-check
package-check: ## Verify an archive carries no secrets or junk: make package-check ARCHIVE=x.tar.gz
	@test -n "$(ARCHIVE)" || (echo "usage: make package-check ARCHIVE=dist/x.tar.gz"; exit 1)
	@echo "Checking $(ARCHIVE):"
	@leaked=$$(tar -tzf $(ARCHIVE) | grep -E '(^|/)\.env$$|__pycache__|\.pyc$$|\.pytest_cache|\.egg-info|(^|/)backups/|\.dump$$' || true); \
	if [ -n "$$leaked" ]; then \
		echo "  REFUSING: the archive contains files that must not be shared:"; \
		echo "$$leaked" | sed 's/^/    /'; \
		rm -f $(ARCHIVE); \
		echo "  (deleted $(ARCHIVE))"; \
		exit 1; \
	fi
	@echo "  no .env, no caches, no backups"
	@data=$$(tar -tzf $(ARCHIVE) | grep -E '^[^/]+/data/(inbox|archive)/.+' | grep -v '\.gitkeep$$' || true); \
	if [ -n "$$data" ]; then \
		echo "  WARNING: your own financial data is in the archive:"; \
		echo "$$data" | sed 's/^/    /'; \
		echo "  Remove it before sharing, or move those files out of data/."; \
	else \
		echo "  no personal transaction data"; \
	fi
	@for f in Makefile README.md docker-compose.yml .env.example \
	          db/migrations/001_schema.sql db/migrations/002_views.sql \
	          db/migrations/003_seed.sql etl/Dockerfile; do \
		tar -tzf $(ARCHIVE) | grep -q "/$$f$$" \
			|| { echo "  MISSING: $$f"; exit 1; }; \
	done
	@echo "  every file a fresh install needs is present"
	@echo "  $$(tar -tzf $(ARCHIVE) | grep -vc '/$$') files, $$(du -h $(ARCHIVE) | cut -f1)"
