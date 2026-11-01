"""Coach paper setups.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0005, metadata

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0005), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0005), checkfirst=True)
