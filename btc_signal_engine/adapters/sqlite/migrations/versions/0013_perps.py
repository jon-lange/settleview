"""Kalshi BTC perpetuals: positions, orders, exit triggers and the funding ledger (KTD13).

New tables only; nothing existing changes. On a fresh database 0001 already creates them (it builds from the current
table definitions), so the upgrade creates with checkfirst.

Revision ID: 0013
Revises: 0012
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0013, metadata

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0013), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0013), checkfirst=True)
