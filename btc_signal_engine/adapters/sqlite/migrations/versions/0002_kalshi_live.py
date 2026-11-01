"""Live Kalshi quotes, fills and positions.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0002, metadata

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0002), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0002), checkfirst=True)
