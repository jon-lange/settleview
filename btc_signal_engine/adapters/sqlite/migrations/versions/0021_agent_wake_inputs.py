"""agent_wake_inputs: each agent wake's exact model input (the user message verbatim after the secret scrubber) and
its fingerprint (system prompt and tool specs hashes, model, effort, max_tokens, SDK, CLI, git sha), keyed by the
wake's journal row, so an eval can replay a wake exactly (docs/research/2026-10-31-eval-data-inventory.md,
"Completeness for replay"). New table only; on a fresh database 0001 already creates it (it builds from the current
table definitions), so the upgrade creates with checkfirst. Same on SQLite and Postgres (schema `app`).

Revision ID: 0021
Revises: 0020
"""
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0021, metadata

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), tables=list(REVISION_0021), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), tables=list(REVISION_0021), checkfirst=True)
