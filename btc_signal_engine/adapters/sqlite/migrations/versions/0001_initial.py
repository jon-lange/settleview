"""Initial schema (revision 0001): the v1 tables, frozen.

Revision ID: 0001
Revises:

The table definitions are written out here, not imported from adapters/sqlite/tables.py: a migration must create
the same schema forever, whatever the app's current definitions say (review B4#13). Later revisions create their own
tables and columns (each checks before it acts), so a fresh database migrated to head has exactly tables.py's schema
(tests/storage/test_schema.py compares them). The types render as tables.py's do: strings are VARCHAR without a length
on Postgres, JSON is text there.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import BigInteger, Boolean, Column, Float, Index, Integer, MetaData, Table, Text, UniqueConstraint

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _str(n: int) -> sa.types.TypeEngine:
    """String(n) on SQLite (never enforced there), VARCHAR without a length on Postgres."""
    return sa.String(n).with_variant(sa.String(), "postgresql")


def _json() -> sa.types.TypeEngine:
    """JSON on SQLite, text on Postgres (tables.Json: the app reads and writes it as text there)."""
    return sa.JSON().with_variant(sa.Text(), "postgresql")


v1 = MetaData()

Table(
    "trades",
    v1,
    Column("trade_id", BigInteger, primary_key=True, autoincrement=False),
    Column("ts", BigInteger, nullable=False),
    Column("ingest_ts", BigInteger, nullable=False),
    Column("price", Float, nullable=False),
    Column("size", Float, nullable=False),
    Column("buy", Boolean, nullable=False),
    Column("backfilled", Boolean, nullable=False, default=False),
    Index("ix_trades_ts", "ts"),
)

Table(
    "book_1s",
    v1,
    Column("ts", BigInteger, primary_key=True, autoincrement=False),
    Column("ingest_ts", BigInteger, nullable=False),
    Column("bid", Float, nullable=False),
    Column("ask", Float, nullable=False),
    Column("bid_depth_10", Float, nullable=False),
    Column("ask_depth_10", Float, nullable=False),
    Column("bid_depth_25", Float, nullable=False),
    Column("ask_depth_25", Float, nullable=False),
)

Table(
    "index_1s",
    v1,
    Column("ts", BigInteger, primary_key=True, autoincrement=False),
    Column("price", Float, nullable=False),
    Column("last_trade_ts", BigInteger, nullable=False),
    Column("mid", Float),
)

Table(
    "candles",
    v1,
    Column("granularity", Integer, primary_key=True, autoincrement=False),
    Column("ts", BigInteger, primary_key=True, autoincrement=False),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Float, nullable=False),
    Column("source", _str(8), nullable=False),
)

Table(
    "gaps",
    v1,
    Column("id", Integer, primary_key=True),
    Column("start_ts", BigInteger, nullable=False),
    Column("end_ts", BigInteger),
    Column("cause", _str(32), nullable=False),
    Column("from_trade_id", BigInteger),
    Column("to_trade_id", BigInteger),
    Column("repaired", Boolean, nullable=False, default=False),
    Column("created_ts", BigInteger, nullable=False),
)

Table(
    "data_quality",
    v1,
    Column("id", Integer, primary_key=True),
    Column("ts", BigInteger, nullable=False),
    Column("kind", _str(32), nullable=False),
    Column("value", Float),
    Column("detail", _json()),
    Index("ix_dq_ts", "ts"),
)

Table(
    "snapshots",
    v1,
    Column("id", Integer, primary_key=True),
    Column("ts", BigInteger, nullable=False),
    Column("feature_version", _str(16), nullable=False),
    Column("features", _json(), nullable=False),
    Column("availability", _json(), nullable=False),
    Column("backfilled", Boolean, nullable=False, default=False),
    UniqueConstraint("ts", "feature_version", name="uq_snapshot_ts_version"),
)

Table(
    "rulesets",
    v1,
    Column("id", Integer, primary_key=True),
    Column("name", _str(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("hash", _str(64), nullable=False, unique=True),
    Column("spec", _json(), nullable=False),
    Column("origin", _str(32), nullable=False),
    Column("parent_hash", _str(64)),
    Column("created_ts", BigInteger, nullable=False),
    UniqueConstraint("name", "version", name="uq_ruleset_name_version"),
)

# Role of a ruleset on one horizon: active | challenger | champion | previous | retired
Table(
    "ruleset_roles",
    v1,
    Column("horizon", Integer, primary_key=True, autoincrement=False),
    Column("ruleset_hash", _str(64), primary_key=True),
    Column("role", _str(16), nullable=False),
    Column("since_ts", BigInteger, nullable=False),
    Column("note", Text),
)

Table(
    "decisions",
    v1,
    Column("id", Integer, primary_key=True),
    Column("horizon", Integer, nullable=False),
    Column("boundary", BigInteger, nullable=False),
    Column("ruleset_hash", _str(64), nullable=False),
    Column("run_id", Integer, nullable=False, default=0),
    Column("call", _str(8), nullable=False),
    Column("explanation", Text, nullable=False),
    Column("conditions", _json(), nullable=False),
    Column("flags", _json(), nullable=False),
    Column("snapshot_ts", BigInteger),
    Column("reference_price", Float),
    Column("created_ts", BigInteger, nullable=False),
    UniqueConstraint("horizon", "boundary", "ruleset_hash", "run_id", name="uq_decision"),
    Index("ix_decisions_run_horizon_boundary", "run_id", "horizon", "boundary"),
)

Table(
    "user_picks",
    v1,
    Column("id", Integer, primary_key=True),
    Column("horizon", Integer, nullable=False),
    Column("boundary", BigInteger, nullable=False),
    Column("direction", _str(4), nullable=False),
    Column("placed_ts", BigInteger, nullable=False),
    UniqueConstraint("horizon", "boundary", name="uq_pick_window"),
)

Table(
    "windows",
    v1,
    Column("horizon", Integer, primary_key=True, autoincrement=False),
    Column("boundary", BigInteger, primary_key=True, autoincrement=False),
    Column("status", _str(12), nullable=False),  # PENDING | RESOLVED | UNRESOLVED
    Column("open_avg", Float),
    Column("close_avg", Float),
    Column("last_price", Float),  # index price at the boundary: known-component baseline input
    Column("actual", _str(4)),  # UP | DOWN
    Column("tie", Boolean, nullable=False, default=False),
    Column("source", _str(8)),  # proxy | kalshi
    Column("proxy_actual", _str(4)),
    Column("kalshi_ticker", _str(48)),
    Column("kalshi_strike", Float),
    Column("kalshi_result", _str(4)),
    Column("yes_ask", Float),
    Column("no_ask", Float),
    Column("updated_ts", BigInteger, nullable=False),
    Index("ix_windows_status", "status"),
)

Table(
    "kalshi_markets",
    v1,
    Column("ticker", _str(48), primary_key=True),
    Column("open_ts", BigInteger, nullable=False),
    Column("close_ts", BigInteger, nullable=False),
    Column("status", _str(16), nullable=False),
    Column("floor_strike", Float),
    Column("expiration_value", Float),
    Column("result", _str(4)),
    Column("rules_primary", Text),
    Index("ix_kalshi_open", "open_ts"),
)

Table(
    "calibration",
    v1,
    Column("ticker", _str(48), primary_key=True),
    Column("boundary", BigInteger, nullable=False),
    Column("open_avg", Float),
    Column("close_avg", Float),
    Column("floor_strike", Float),
    Column("expiration_value", Float),
    Column("proxy_actual", _str(4)),
    Column("kalshi_actual", _str(4)),
    Column("agree", Boolean),
    Column("open_err", Float),
    Column("close_err", Float),
)

Table(
    "jobs",
    v1,
    Column("id", Integer, primary_key=True),
    Column("kind", _str(24), nullable=False),
    Column("status", _str(12), nullable=False),  # queued | running | done | failed
    Column("params", _json(), nullable=False),
    Column("result", _json()),
    Column("error", Text),
    Column("created_ts", BigInteger, nullable=False),
    Column("started_ts", BigInteger),
    Column("finished_ts", BigInteger),
)

Table(
    "oos_checks",
    v1,
    Column("id", Integer, primary_key=True),
    Column("ruleset_hash", _str(64), nullable=False),
    Column("horizon", Integer, nullable=False),
    Column("ts", BigInteger, nullable=False),
    Column("result", _json(), nullable=False),
)

Table(
    "promotions",
    v1,
    Column("id", Integer, primary_key=True),
    Column("horizon", Integer, nullable=False),
    Column("kind", _str(16), nullable=False),  # challenger | promote | retire | rollback
    Column("from_hash", _str(64)),
    Column("to_hash", _str(64)),
    Column("ts", BigInteger, nullable=False),
    Column("evidence", _json(), nullable=False),
)


def upgrade() -> None:
    v1.create_all(bind=op.get_bind())


def downgrade() -> None:
    v1.drop_all(bind=op.get_bind())
