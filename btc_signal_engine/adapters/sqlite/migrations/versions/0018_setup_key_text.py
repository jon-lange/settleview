"""trading_brackets.setup_key becomes text: SQLite held both a Coach proposal's created_ts (a number) and the removed
strategy lab's keys ("pnw_cushion:<window>:<ms>") in this BIGINT column; Postgres needs one type. Found by the first
SQLite -> Postgres move of the live data (2026-10-31). Nothing reads the column. On SQLite only the declared type
changes (affinity), so the upgrade is a no-op there; a fresh database already gets text from the table definition.

Revision ID: 0018
Revises: 0017
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _type(bind) -> str:
    return sa.inspect(bind).get_columns("trading_brackets") and next(
        str(c["type"]) for c in sa.inspect(bind).get_columns("trading_brackets") if c["name"] == "setup_key")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql" and _type(bind).upper() == "BIGINT":
        op.execute("ALTER TABLE trading_brackets ALTER COLUMN setup_key TYPE text USING setup_key::text")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE trading_brackets ALTER COLUMN setup_key TYPE bigint "
                   "USING CASE WHEN setup_key ~ '^-?[0-9]+$' THEN setup_key::bigint END")
