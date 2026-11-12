#!/bin/bash
# Runs once, on the database's very first boot (the postgres image only
# executes /docker-entrypoint-initdb.d when the data directory is empty).
# Creates the read-only login Grafana uses, so the dashboard physically
# cannot write to your financial data.
set -euo pipefail

: "${GRAFANA_DB_USER:?}" "${GRAFANA_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v role="$GRAFANA_DB_USER" -v pw="$GRAFANA_DB_PASSWORD" -v db="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role')
\gexec

GRANT CONNECT ON DATABASE :"db" TO :"role";
GRANT USAGE ON SCHEMA public TO :"role";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"role";

-- Applies to tables later migrations create, as long as they are created
-- by this same role (which is how `plumb migrate` connects).
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"role";

-- Belt and braces: this login can read and nothing else.
REVOKE CREATE ON SCHEMA public FROM :"role";
SQL

echo "init: read-only role '$GRAFANA_DB_USER' ready"
