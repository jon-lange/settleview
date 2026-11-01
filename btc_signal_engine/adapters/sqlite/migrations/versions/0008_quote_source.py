"""kalshi_quotes.source: where a live quote came from (ws | orderbook); NULL = the old, lagging /markets list.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("kalshi_quotes")}
    if "source" not in cols:
        with op.batch_alter_table("kalshi_quotes") as batch:
            batch.add_column(sa.Column("source", sa.String(16)))


def downgrade() -> None:
    with op.batch_alter_table("kalshi_quotes") as batch:
        batch.drop_column("source")
