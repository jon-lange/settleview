"""agent_sessions.pause_code: why a session paused, as a machine code next to the text (agent_sessions.PAUSE_CODES).
Nullable; existing rows keep NULL. On a fresh database 0014 already creates the column (it builds from the current table
definitions), so the upgrade checks before it acts.

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

TABLE, COLUMN = "agent_sessions", "pause_code"


def _cols() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    if COLUMN not in _cols():
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(COLUMN, sa.String(24)))


def downgrade() -> None:
    if COLUMN in _cols():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COLUMN)
