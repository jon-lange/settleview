-- The append-only audit's privileges (docs/operations.md, "Postgres: the append-only audit"). Run as the superuser
-- (on a managed server: the admin, a member of settleview_app and settleview_audit). Idempotent:
--   - a new volume: the official image runs it after 10-settleview.sh and 20-roles.sh (compose.yaml mounts it as
--     /docker-entrypoint-initdb.d/30-audit.sql). The table does not exist yet, so it only defines the function below,
--     which migration 0023 calls once it has made the table;
--   - an existing volume, once after the migrate job has run 0023:
--       docker compose exec -T postgres psql -U postgres -d settleview -v ON_ERROR_STOP=1 \
--         -f /docker-entrypoint-initdb.d/30-audit.sql
--
--   settleview_audit  no login: owns app.trading_audit (and its id sequence), its trigger function and the schema
--                     `app` itself. Nobody logs in as it.
--   settleview_app    SELECT and INSERT on trading_audit, USAGE on its sequence; no UPDATE, DELETE or TRUNCATE and no
--                     ownership, so it cannot change, remove, drop or un-trigger a row. It keeps USAGE and CREATE on
--                     `app` and owns its other tables (its migrations still create and alter them).
--
-- Plain GRANT / REVOKE: once the app's role owns neither the table nor the schema, "may insert, may not update, delete
-- or truncate" is exactly what table privileges express. The schema moves too because a schema's owner may DROP any
-- table in it. The trigger from migration 0023 also stops the owner and superusers by accident.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'settleview_audit') THEN
    CREATE ROLE settleview_audit NOLOGIN;
  END IF;
END $$;

-- SECURITY DEFINER (this script's runner): the migrate job, connected as settleview_app, calls it at the end of
-- migration 0023 on a new volume. No arguments, every name qualified, a fixed search_path; it only ever takes the
-- audit away from the app's role, so calling it again changes nothing.
CREATE OR REPLACE FUNCTION public.settleview_seal_audit() RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE seq text;
BEGIN
  IF to_regclass('app.trading_audit') IS NULL OR to_regprocedure('app.trading_audit_append_only()') IS NULL THEN
    RETURN 'app.trading_audit (migration 0023) is not there yet: the migration calls this when it is';
  END IF;
  ALTER SCHEMA app OWNER TO settleview_audit;
  GRANT USAGE, CREATE ON SCHEMA app TO settleview_app;
  ALTER TABLE app.trading_audit OWNER TO settleview_audit;  -- its id sequence and indexes go with it
  ALTER FUNCTION app.trading_audit_append_only() OWNER TO settleview_audit;
  REVOKE ALL ON app.trading_audit FROM PUBLIC, settleview_app;
  GRANT SELECT, INSERT ON app.trading_audit TO settleview_app;
  seq := pg_get_serial_sequence('app.trading_audit', 'id');
  IF seq IS NOT NULL THEN
    EXECUTE format('REVOKE ALL ON SEQUENCE %s FROM PUBLIC, settleview_app', seq);
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO settleview_app', seq);
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'settleview_research') THEN
    GRANT SELECT ON app.trading_audit TO settleview_research;
  END IF;
  RETURN 'sealed: app.trading_audit is owned by settleview_audit; settleview_app may SELECT and INSERT';
END $$;
REVOKE ALL ON FUNCTION public.settleview_seal_audit() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.settleview_seal_audit() TO settleview_app;

SELECT public.settleview_seal_audit();
