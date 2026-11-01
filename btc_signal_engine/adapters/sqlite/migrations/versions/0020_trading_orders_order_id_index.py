"""trading_orders.order_id index: the Polymarket gateway's restart lookups (client order id <-> venue order id), the
order resolution and the record read an order by the venue's id; without an index each was a scan of every order ever
placed (docs/plans/2026-10-31-1800-review-fix-plan.md B4#18). On a fresh database 0007 already creates it (it builds
from the current table definitions), so the upgrade checks before it acts; same on SQLite and Postgres. The table is
small, so the plain (locking) CREATE INDEX inside the migration's transaction takes milliseconds.

Revision ID: 0020
Revises: 0019
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"  # 0019: perp_orders stop / take profit (fix/p0-order-resolution)
branch_labels = None
depends_on = None

INDEX = "ix_trading_orders_order_id"


def _has() -> bool:
    return INDEX in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("trading_orders")}


def upgrade() -> None:
    if not _has():
        op.create_index(INDEX, "trading_orders", ["order_id"])


def downgrade() -> None:
    if _has():
        op.drop_index(INDEX, table_name="trading_orders")
