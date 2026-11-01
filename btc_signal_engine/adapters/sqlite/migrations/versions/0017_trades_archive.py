"""trades_archive: Postgres's Coinbase trade archive (the daily maintenance moves old `trades` rows here, in the same
database). On SQLite the archive stays a separate file and this table stays empty. New table only; on a fresh database
0001 already creates it (it builds from the current table definitions), so the upgrade creates with checkfirst.

Revision ID: 0017
Revises: 0016
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0017, metadata

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0017), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0017), checkfirst=True)
