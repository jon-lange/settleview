#!/usr/bin/env bash
# Settleview's roles and schemas, made once when the `settleview-pg` volume is new (the official postgres image runs
# /docker-entrypoint-initdb.d/*.sh on an empty data directory only; compose.yaml mounts this file there).
#
#   settleview_app       the app and the migrate job: owns the schema `app` (reads and writes)
#   settleview_research  research scripts: read-only (SELECT on both schemas, every transaction read-only)
#   settleview_backup    the backup service: pg_read_all_data, read-only (pg_dump)
# The recorders' role (settleview_recorder) and their schema `research` come next, from roles.sh (20-roles.sh), which
# an existing volume runs once by hand.
#
# Passwords come from secret files (compose `secrets:`, mounted under /run/secrets); they are read by psql itself
# (\set with a backtick command), so they never appear on a command line or in the log. Nobody else may connect to the
# database. On a managed server (Azure Flexible Server, RDS) run the SQL below by hand as the admin user
# (docs/operations.md, "Postgres").
# No `set -u`: the image sources a non-executable init script into its own entrypoint shell.
set -eo pipefail

for secret_file in "${SETTLEVIEW_APP_PASSWORD_FILE:=/run/secrets/pg_app_password}" \
         "${SETTLEVIEW_RESEARCH_PASSWORD_FILE:=/run/secrets/pg_research_password}" \
         "${SETTLEVIEW_BACKUP_PASSWORD_FILE:=/run/secrets/pg_backup_password}"; do
  if [ ! -s "$secret_file" ]; then
    echo "settleview initdb: the secret file $secret_file is missing or empty" >&2
    exit 1
  fi
done
export SETTLEVIEW_APP_PASSWORD_FILE SETTLEVIEW_RESEARCH_PASSWORD_FILE SETTLEVIEW_BACKUP_PASSWORD_FILE

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set db="$POSTGRES_DB" <<'SQL'
\set app_pw `tr -d '\r\n' < "$SETTLEVIEW_APP_PASSWORD_FILE"`
\set research_pw `tr -d '\r\n' < "$SETTLEVIEW_RESEARCH_PASSWORD_FILE"`
\set backup_pw `tr -d '\r\n' < "$SETTLEVIEW_BACKUP_PASSWORD_FILE"`

CREATE ROLE settleview_app LOGIN PASSWORD :'app_pw';
CREATE ROLE settleview_research LOGIN PASSWORD :'research_pw';
CREATE ROLE settleview_backup LOGIN PASSWORD :'backup_pw';
\unset app_pw
\unset research_pw
\unset backup_pw

-- Only these roles (and the superuser) connect; nobody may create objects in public.
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO settleview_app;
GRANT CONNECT ON DATABASE :"db" TO settleview_research, settleview_backup;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- The app's tables (Alembic): the app's role owns them, so the migrate job runs as it. (`research`: roles.sh.)
CREATE SCHEMA app AUTHORIZATION settleview_app;

-- Research: read-only, on every table settleview_app makes now and later.
GRANT USAGE ON SCHEMA app TO settleview_research;
ALTER DEFAULT PRIVILEGES FOR ROLE settleview_app IN SCHEMA app GRANT SELECT ON TABLES TO settleview_research;
ALTER ROLE settleview_research SET default_transaction_read_only = on;
ALTER ROLE settleview_research SET search_path = research, app;

-- Backups: read everything (PG 14+ predefined role), write nothing.
GRANT pg_read_all_data TO settleview_backup;
ALTER ROLE settleview_backup SET default_transaction_read_only = on;

-- Query statistics (compose.yaml preloads the library): the top statements by time, read by the research role for the
-- read-only EXPLAIN workflow (docs/operations.md, "Resource limits and Postgres settings"). Statement texts are
-- normalized ($1 for each value). On a volume made before this line, run the same two statements once as postgres.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
GRANT pg_read_all_stats TO settleview_research;
SQL

echo "settleview initdb: roles settleview_app, settleview_research, settleview_backup and schema app created"
