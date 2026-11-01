"""Two read-path indexes (docs/plans/2026-10-31-1800-review-fix-plan.md P1, "Event loop and database"):

- `ix_trading_brackets_session` (trading_brackets.session_id): the agent fleet reads every live session's brackets once
  a tick, and the budget, ownership, overlap, fleet-view and Telegram reads go by session; without it each was a scan of
  every bracket ever placed.
- `ix_agent_journal_kind` (agent_journal.kind, id): the rule scorekeeper's incremental read of "rule" rows (by id) and
  the rule links of Record / Telegram ("rule" and "decision" rows) no longer scan the whole journal, whose wake and
  tool rows are most of it.

On a fresh database 0007 / 0014 already create them (they build from the current table definitions), so the upgrade
checks first; same on SQLite and Postgres. Both tables are small on the live database (about 200 brackets and 7,600
journal rows on 2026-11-01), so the plain CREATE INDEX inside the migration's transaction takes milliseconds; its
SHARE lock only holds writes to that table for that long, and migrate() already retries a lock wait (pg_dump).

Revision ID: 0022
Revises: 0021
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

INDEXES = (("ix_trading_brackets_session", "trading_brackets", ["session_id"]),
           ("ix_agent_journal_kind", "agent_journal", ["kind", "id"]))


def _has(name: str, table: str) -> bool:
    return name in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    for name, table, cols in INDEXES:
        if not _has(name, table):
            op.create_index(name, table, cols)


def downgrade() -> None:
    for name, table, _ in INDEXES:
        if _has(name, table):
            op.drop_index(name, table_name=table)
