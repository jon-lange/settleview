"""Initial schema.

Revision ID: 0001
Revises:
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial revision creates the v1 schema from the table definitions;
    # later revisions are incremental.
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
