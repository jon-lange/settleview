"""Human-confirmed Kalshi trading: brackets, orders, audit log, kill switch, micro-live sessions.

Revision ID: 0007
Revises: 0006
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0007, metadata

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0007), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0007), checkfirst=True)
