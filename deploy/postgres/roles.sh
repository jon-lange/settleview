#!/usr/bin/env bash
# Least privilege for the recorders (review B4#4): the settleview_recorder role and the `research` schema it owns.
# Idempotent, and the same on a new and an existing database:
#   - a new volume: the official image runs it after initdb.sh (compose.yaml mounts it as
#     /docker-entrypoint-initdb.d/20-roles.sh);
#   - an existing volume, once (docs/operations.md, "Postgres: the recorder role"):
#       docker compose exec -T postgres bash /docker-entrypoint-initdb.d/20-roles.sh
#
#   settleview_recorder  the research recorders: owns the schema `research` and its tables (creates them, writes them);
#                        no privilege at all on the schema `app` (trading, sessions, settings): it cannot read or
#                        change a trading table even with a bug or a leaked password
#   settleview_app       reads `research` (the data alert, freshness) and writes nothing there any more
#   settleview_research  reads both schemas, as before
#
# The password comes from the secret file (compose `secrets:` pg_recorder_password), read by psql itself (\set with a
# backtick command), so it never appears on a command line or in the log; running it again sets the same password
# (after a rotation: ALTER ROLE first, or write the new file and run this). On a managed server (Azure Flexible Server,
# RDS) run the SQL below as the admin user, which must be a member of settleview_app and settleview_recorder to move
# the ownership. No `set -u`: the image sources a non-executable init script into its own entrypoint shell.
set -eo pipefail

: "${SETTLEVIEW_RECORDER_PASSWORD_FILE:=/run/secrets/pg_recorder_password}"
if [ ! -s "$SETTLEVIEW_RECORDER_PASSWORD_FILE" ]; then
  echo "settleview roles: the secret file $SETTLEVIEW_RECORDER_PASSWORD_FILE is missing or empty" \
       "(python deploy/postgres/make_secrets.py writes it)" >&2
  exit 1
fi
export SETTLEVIEW_RECORDER_PASSWORD_FILE

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname "${POSTGRES_DB:-settleview}" \
     --set db="${POSTGRES_DB:-settleview}" <<'SQL'
\set recorder_pw `tr -d '\r\n' < "$SETTLEVIEW_RECORDER_PASSWORD_FILE"`
BEGIN;
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'settleview_recorder') AS make_role \gset
\if :make_role
CREATE ROLE settleview_recorder LOGIN;
\endif
ALTER ROLE settleview_recorder LOGIN PASSWORD :'recorder_pw';
\unset recorder_pw
GRANT CONNECT ON DATABASE :"db" TO settleview_recorder;
ALTER ROLE settleview_recorder SET search_path = research;

-- The schema and every table in it (an existing deployment's were settleview_app's) belong to the recorder role.
CREATE SCHEMA IF NOT EXISTS research AUTHORIZATION settleview_recorder;
ALTER SCHEMA research OWNER TO settleview_recorder;
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'research' AND c.relkind IN ('r', 'p')
             AND pg_get_userbyid(c.relowner) <> 'settleview_recorder' LOOP
    EXECUTE format('ALTER TABLE research.%I OWNER TO settleview_recorder', t.relname);  -- with its indexes
  END LOOP;
END $$;

-- The app and research scripts read research; nobody but the recorder role writes or creates there.
REVOKE ALL ON SCHEMA research FROM PUBLIC, settleview_app, settleview_research;
GRANT USAGE ON SCHEMA research TO settleview_app, settleview_research;
REVOKE ALL ON ALL TABLES IN SCHEMA research FROM PUBLIC, settleview_app, settleview_research;
GRANT SELECT ON ALL TABLES IN SCHEMA research TO settleview_app, settleview_research;
ALTER DEFAULT PRIVILEGES FOR ROLE settleview_recorder IN SCHEMA research
  GRANT SELECT ON TABLES TO settleview_app, settleview_research;
ALTER DEFAULT PRIVILEGES FOR ROLE settleview_app IN SCHEMA research REVOKE SELECT ON TABLES FROM settleview_research;

-- And nothing on the app's schema: no USAGE, so no table, sequence or function in `app` is reachable.
REVOKE ALL ON SCHEMA app FROM settleview_recorder;
COMMIT;
SQL

echo "settleview roles: settleview_recorder owns the schema research; settleview_app and settleview_research read it"
