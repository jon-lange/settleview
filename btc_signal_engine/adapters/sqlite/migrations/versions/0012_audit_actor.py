"""trading_audit.actor: who acted (R13): "pc", "phone:<first 8 characters of the session nonce>" or "system".
Nullable; existing rows keep NULL (written before the actor was recorded). On a fresh database 0007 already creates the
column (it builds from the current table definitions), so the upgrade checks before it acts.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

COLUMN = "actor"


def upgrade() -> None:
    if COLUMN not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trading_audit")}:
        with op.batch_alter_table("trading_audit") as batch:
            batch.add_column(sa.Column(COLUMN, sa.String(16)))


def downgrade() -> None:
    if COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trading_audit")}:
        with op.batch_alter_table("trading_audit") as batch:
            batch.drop_column(COLUMN)
