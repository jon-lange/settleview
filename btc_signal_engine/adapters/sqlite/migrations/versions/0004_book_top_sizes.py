"""Best bid/ask sizes on book_1s.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("book_1s")}
    with op.batch_alter_table("book_1s") as batch:
        if "bid_size" not in cols:
            batch.add_column(sa.Column("bid_size", sa.Float()))
        if "ask_size" not in cols:
            batch.add_column(sa.Column("ask_size", sa.Float()))


def downgrade() -> None:
    with op.batch_alter_table("book_1s") as batch:
        batch.drop_column("ask_size")
        batch.drop_column("bid_size")
