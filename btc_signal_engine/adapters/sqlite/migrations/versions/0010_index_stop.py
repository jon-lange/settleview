"""trading_brackets.stop_index_below / stop_index_above: an optional stop on the BTC index (dollars) instead of the
contract price (PNW cushion). Nullable; existing rows get NULL (no index stop). On a fresh database 0007 already
creates the columns (it builds from the current table definitions), so the upgrade checks before it acts.

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

COLUMNS = ("stop_index_below", "stop_index_above")


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trading_brackets")}
    missing = [c for c in COLUMNS if c not in cols]
    if missing:
        with op.batch_alter_table("trading_brackets") as batch:
            for c in missing:
                batch.add_column(sa.Column(c, sa.Float()))


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trading_brackets")}
    present = [c for c in COLUMNS if c in cols]
    if present:
        with op.batch_alter_table("trading_brackets") as batch:
            for c in present:
                batch.drop_column(c)
