"""Coach cockpit: paper approvals (coach_decisions) and the kill switch (coach_settings).

Revision ID: 0006
Revises: 0005
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0006, metadata

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0006), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0006), checkfirst=True)
