"""trading_audit becomes append-only in the database (docs/plans/2026-10-31-1800-review-fix-plan.md P3 item 5;
docs/operations.md, "Postgres: the append-only audit").

Standard mechanisms only, no new columns and no row changes:
- Postgres: a statement-level BEFORE UPDATE OR DELETE OR TRUNCATE trigger that raises, so even the table's owner (or a
  superuser) cannot change a row by accident. Then, when the administrator's `public.settleview_seal_audit()` exists
  (deploy/postgres/audit.sql: run as the superuser on a new volume before this migration, by hand on an existing one
  after it), it is called: the table, its sequence, the trigger function and the schema `app` go to the no-login role
  `settleview_audit`, and the app's role keeps SELECT and INSERT on trading_audit only (GRANT / REVOKE). Without that
  function (tests, a database made by hand) the trigger still applies.
- SQLite: BEFORE UPDATE and BEFORE DELETE triggers that RAISE(ABORT) (SQLite has no TRUNCATE or roles).

Maintenance (a correction an administrator decides on): ALTER TABLE ... DISABLE TRIGGER inside one transaction, as the
table's owner or a superuser (docs/operations.md, "Postgres: the append-only audit", Maintenance). A later migration
that changes trading_audit's rows or definition runs that way too; on SQLite a batch (copy-and-move) alter drops these
triggers with the old table and must create them again.

Revision ID: 0023
Revises: 0022
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

TABLE = "trading_audit"
TRIGGER = "trading_audit_append_only"
SQLITE_TRIGGERS = {f"{TRIGGER}_update": "UPDATE", f"{TRIGGER}_delete": "DELETE"}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for name, verb in SQLITE_TRIGGERS.items():
            op.execute(f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {verb} ON {TABLE} "
                       f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only: {verb} refused'); END")
        return
    bind.exec_driver_sql(
        f"CREATE FUNCTION {TRIGGER}() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$ "
        "BEGIN RAISE EXCEPTION USING ERRCODE = 'insufficient_privilege', "
        "MESSAGE = TG_TABLE_NAME || ' is append-only: ' || TG_OP || ' refused (docs/operations.md, Audit)'; END $$")
    bind.exec_driver_sql(f"CREATE TRIGGER {TRIGGER} BEFORE UPDATE OR DELETE OR TRUNCATE ON {TABLE} "
                         f"FOR EACH STATEMENT EXECUTE FUNCTION {TRIGGER}()")
    if bind.exec_driver_sql("SELECT to_regprocedure('public.settleview_seal_audit()') IS NOT NULL").scalar():
        bind.exec_driver_sql("SELECT public.settleview_seal_audit()")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
        return
    bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}")
    bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {TRIGGER}()")
