"""perp_orders.stop_loss / take_profit: the stop and take profit the user approved with an entry, saved with the order
before it is sent, so an entry whose answer was lost and that is found filled later still gets its bracket (review
fix plan 2026-10-31, B3#1). Nullable; existing rows keep NULL. A fresh database already has the columns (0013 builds
from the current table definitions), so the upgrade checks before it acts.

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

TABLE, COLUMNS = "perp_orders", ("stop_loss", "take_profit")


def _cols() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    have = _cols()
    missing = [c for c in COLUMNS if c not in have]
    if missing:
        with op.batch_alter_table(TABLE) as batch:
            for c in missing:
                batch.add_column(sa.Column(c, sa.Float))


def downgrade() -> None:
    have = _cols()
    present = [c for c in COLUMNS if c in have]
    if present:
        with op.batch_alter_table(TABLE) as batch:
            for c in present:
                batch.drop_column(c)
