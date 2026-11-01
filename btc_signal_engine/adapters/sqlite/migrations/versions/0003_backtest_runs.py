"""Backtest runs.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0003, metadata

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0003), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0003), checkfirst=True)
