"""Agent mode: agent sessions, their journal and the prompt versions.

New tables only; nothing existing changes (agent orders reuse trading_brackets.session_id and
trading_audit.session_id, which already exist). On a fresh database 0001 already creates them (it builds from the
current table definitions), so the upgrade creates with checkfirst.

Revision ID: 0014
Revises: 0013
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0014, metadata

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0014), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0014), checkfirst=True)
