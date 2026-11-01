"""trading_audit.bracket_id index: the Play scoreboard reads every listed bracket's manual_exit audit rows in one
query. On a fresh database 0007 already creates the index (it builds from the current table definitions), so the
upgrade checks before it acts.

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

INDEX = "ix_trading_audit_bracket_id"


def upgrade() -> None:
    if INDEX not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("trading_audit")}:
        op.create_index(INDEX, "trading_audit", ["bracket_id"])


def downgrade() -> None:
    if INDEX in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("trading_audit")}:
        op.drop_index(INDEX, table_name="trading_audit")
