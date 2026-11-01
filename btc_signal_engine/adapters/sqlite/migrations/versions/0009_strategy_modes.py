"""Strategy modes: per-mode accounts, orders, fills, window results, settings, selector features; and a
`strategy` column (default 'taker') on trading_brackets, trading_orders, trading_audit and trading_sessions.

Existing rows become the taker's through the server default. On a fresh database 0007 already creates the
column (it builds from the current table definitions), so each step here checks before it acts.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import (
    REVISION_0009,
    STRATEGY_COLUMN_TABLES,
    STRATEGY_DEFAULT,
    metadata,
)

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

STATE_INDEX = "ix_trading_brackets_strategy_state"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in STRATEGY_COLUMN_TABLES:
        if "strategy" not in {c["name"] for c in insp.get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("strategy", sa.String(16), nullable=False,
                                           server_default=STRATEGY_DEFAULT))
    if STATE_INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes("trading_brackets")}:
        op.create_index(STATE_INDEX, "trading_brackets", ["strategy", "state"])
    metadata.create_all(bind=bind, tables=list(REVISION_0009), checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    metadata.drop_all(bind=bind, tables=list(REVISION_0009), checkfirst=True)
    if STATE_INDEX in {i["name"] for i in sa.inspect(bind).get_indexes("trading_brackets")}:
        op.drop_index(STATE_INDEX, table_name="trading_brackets")
    for table in STRATEGY_COLUMN_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("strategy")
