"""The research recorders' tables (SQLAlchemy Core), the same columns on both stores:

- SQLite: `<research dir>/polymarket_us.db`, as the recorder scripts made it (2026-10-29 on). Existing files are never
  altered: the tables are created only when missing.
- Postgres: the schema `research` of the app's database (docs/plans/2026-10-31-0900-feat-postgres-plan.md PD5).

Not part of the app's Alembic schema: the recorders create their tables on start (checkfirst), as the scripts did. The
tables have no key on SQLite (the rowid orders them); Postgres adds an index on the receive time for the freshness
check (SQLite reads the largest rowid instead, so its existing files need no new index).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, Float, Index, MetaData, PrimaryKeyConstraint, Table, Text

research_metadata = MetaData()

pm_ws_quotes = Table(
    "pm_ws_quotes",
    research_metadata,
    Column("recv_ms", BigInteger, nullable=False),
    Column("span", Text),
    Column("slug", Text, nullable=False),
    Column("bid", Float),
    Column("ask", Float),
    Column("bid_depth", Float),
    Column("ask_depth", Float),
    Column("last", Float),
    Index("ix_pm_ws_quotes_slug_ts", "slug", "recv_ms"),
    Index("ix_pm_ws_quotes_recv_ms", "recv_ms").ddl_if(dialect="postgresql"),
)

pm_trades = Table(
    "pm_trades",
    research_metadata,
    Column("recv_ms", BigInteger, nullable=False),
    Column("trade_ms", BigInteger),
    Column("span", Text),
    Column("slug", Text, nullable=False),
    Column("price", Float),
    Column("qty", Float),
    Column("maker_side", Text),
    Column("raw", Text),
    Index("ix_pm_trades_slug_ts", "slug", "trade_ms"),
)

# Created for old readers (the REST quote recorder that preceded the WebSocket one); never written.
pm_quotes = Table(
    "pm_quotes",
    research_metadata,
    Column("ts_ms", BigInteger, nullable=False),
    Column("span", Text, nullable=False),
    Column("slug", Text, nullable=False),
    Column("window_start", BigInteger, nullable=False),
    Column("bid", Float),
    Column("ask", Float),
    Column("bid_depth", Float),
    Column("ask_depth", Float),
    Column("last", Float),
    Column("shares_traded", Float),
    PrimaryKeyConstraint("slug", "ts_ms"),
)

pm_windows = Table(
    "pm_windows",
    research_metadata,
    Column("slug", Text, primary_key=True),
    Column("span", Text, nullable=False),
    Column("window_start", BigInteger, nullable=False),
    Column("window_end", BigInteger, nullable=False),
    Column("price_to_beat", Float),
    Column("settlement_price", Float),
    Column("result", Text),
    Column("raw", Text),
    Column("fetched_ms", BigInteger),
)

QUOTES_TABLES = (pm_ws_quotes, pm_trades)  # the quotes recorder's
SETTLE_TABLES = (pm_quotes, pm_windows)  # the settlement recorder's
RESEARCH_TABLES = {t.name: t for t in research_metadata.sorted_tables}
