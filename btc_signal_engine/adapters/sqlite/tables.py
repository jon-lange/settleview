"""The app's schema (SQLAlchemy Core), the same on SQLite and Postgres. All timestamps are integer UTC milliseconds.

Settlement is stored once per (horizon, window) in `windows`; engine decisions,
baselines and user picks are all scored against that single row, so they can never
disagree about what actually happened.
"""

from __future__ import annotations

import json

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import TypeDecorator


class JsonText(TypeDecorator):
    """JSON kept as text on Postgres, as SQLite keeps it: `json.dumps` in, `json.loads` out, so NaN and Infinity
    (which Postgres's json/jsonb types refuse) round-trip exactly as they do on SQLite, and None is stored as the JSON
    `null` (SQLAlchemy's JSON default), so a NOT NULL column accepts it on both. No query reads inside these columns;
    a column that is ever queried by its content gets a real JSONB column in its own migration."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


def Json() -> JSON:  # noqa: N802 (reads as a type in the table definitions)
    """SQLite: SQLAlchemy's JSON, unchanged. Postgres: JSON as text (`JsonText`)."""
    return JSON().with_variant(JsonText(), "postgresql")


@compiles(String, "postgresql")
def _pg_string(element, compiler, **kw) -> str:
    """Postgres: VARCHAR without a length. SQLite never enforced the declared lengths, so the app has never been held
    to them; enforcing them on Postgres would turn a long reason or code into a failed trading write. The lengths
    stay in the definitions as documentation."""
    return "VARCHAR"


metadata = MetaData()

trades = Table(
    "trades",
    metadata,
    Column("trade_id", BigInteger, primary_key=True, autoincrement=False),
    Column("ts", BigInteger, nullable=False),
    Column("ingest_ts", BigInteger, nullable=False),
    Column("price", Float, nullable=False),
    Column("size", Float, nullable=False),
    Column("buy", Boolean, nullable=False),
    Column("backfilled", Boolean, nullable=False, default=False),
    Index("ix_trades_ts", "ts"),
)

book_1s = Table(
    "book_1s",
    metadata,
    Column("ts", BigInteger, primary_key=True, autoincrement=False),
    Column("ingest_ts", BigInteger, nullable=False),
    Column("bid", Float, nullable=False),
    Column("ask", Float, nullable=False),
    Column("bid_depth_10", Float, nullable=False),
    Column("ask_depth_10", Float, nullable=False),
    Column("bid_depth_25", Float, nullable=False),
    Column("ask_depth_25", Float, nullable=False),
    # revision 0004: size resting at the best bid / ask (order-flow imbalance, microprice)
    Column("bid_size", Float),
    Column("ask_size", Float),
)

index_1s = Table(
    "index_1s",
    metadata,
    Column("ts", BigInteger, primary_key=True, autoincrement=False),
    Column("price", Float, nullable=False),
    Column("last_trade_ts", BigInteger, nullable=False),
    Column("mid", Float),
)

candles = Table(
    "candles",
    metadata,
    Column("granularity", Integer, primary_key=True, autoincrement=False),
    Column("ts", BigInteger, primary_key=True, autoincrement=False),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Float, nullable=False),
    Column("source", String(8), nullable=False),
)

gaps = Table(
    "gaps",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("start_ts", BigInteger, nullable=False),
    Column("end_ts", BigInteger),
    Column("cause", String(32), nullable=False),
    Column("from_trade_id", BigInteger),
    Column("to_trade_id", BigInteger),
    Column("repaired", Boolean, nullable=False, default=False),
    Column("created_ts", BigInteger, nullable=False),
)

data_quality = Table(
    "data_quality",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", BigInteger, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("value", Float),
    Column("detail", Json()),
    Index("ix_dq_ts", "ts"),
)

snapshots = Table(
    "snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", BigInteger, nullable=False),
    Column("feature_version", String(16), nullable=False),
    Column("features", Json(), nullable=False),
    Column("availability", Json(), nullable=False),
    Column("backfilled", Boolean, nullable=False, default=False),
    UniqueConstraint("ts", "feature_version", name="uq_snapshot_ts_version"),
)

rulesets = Table(
    "rulesets",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("hash", String(64), nullable=False, unique=True),
    Column("spec", Json(), nullable=False),
    Column("origin", String(32), nullable=False),
    Column("parent_hash", String(64)),
    Column("created_ts", BigInteger, nullable=False),
    UniqueConstraint("name", "version", name="uq_ruleset_name_version"),
)

# Role of a ruleset on one horizon: active | challenger | champion | previous | retired
ruleset_roles = Table(
    "ruleset_roles",
    metadata,
    Column("horizon", Integer, primary_key=True, autoincrement=False),
    Column("ruleset_hash", String(64), primary_key=True),
    Column("role", String(16), nullable=False),
    Column("since_ts", BigInteger, nullable=False),
    Column("note", Text),
)

decisions = Table(
    "decisions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("horizon", Integer, nullable=False),
    Column("boundary", BigInteger, nullable=False),
    Column("ruleset_hash", String(64), nullable=False),
    Column("run_id", Integer, nullable=False, default=0),
    Column("call", String(8), nullable=False),
    Column("explanation", Text, nullable=False),
    Column("conditions", Json(), nullable=False),
    Column("flags", Json(), nullable=False),
    Column("snapshot_ts", BigInteger),
    Column("reference_price", Float),
    Column("created_ts", BigInteger, nullable=False),
    UniqueConstraint("horizon", "boundary", "ruleset_hash", "run_id", name="uq_decision"),
    Index("ix_decisions_run_horizon_boundary", "run_id", "horizon", "boundary"),
)

user_picks = Table(
    "user_picks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("horizon", Integer, nullable=False),
    Column("boundary", BigInteger, nullable=False),
    Column("direction", String(4), nullable=False),
    Column("placed_ts", BigInteger, nullable=False),
    UniqueConstraint("horizon", "boundary", name="uq_pick_window"),
)

windows = Table(
    "windows",
    metadata,
    Column("horizon", Integer, primary_key=True, autoincrement=False),
    Column("boundary", BigInteger, primary_key=True, autoincrement=False),
    Column("status", String(12), nullable=False),  # PENDING | RESOLVED | UNRESOLVED
    Column("open_avg", Float),
    Column("close_avg", Float),
    Column("last_price", Float),  # index price at the boundary: known-component baseline input
    Column("actual", String(4)),  # UP | DOWN
    Column("tie", Boolean, nullable=False, default=False),
    Column("source", String(8)),  # proxy | kalshi
    Column("proxy_actual", String(4)),
    Column("kalshi_ticker", String(48)),
    Column("kalshi_strike", Float),
    Column("kalshi_result", String(4)),
    Column("yes_ask", Float),
    Column("no_ask", Float),
    Column("updated_ts", BigInteger, nullable=False),
    Index("ix_windows_status", "status"),
)

kalshi_markets = Table(
    "kalshi_markets",
    metadata,
    Column("ticker", String(48), primary_key=True),
    Column("open_ts", BigInteger, nullable=False),
    Column("close_ts", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("floor_strike", Float),
    Column("expiration_value", Float),
    Column("result", String(4)),
    Column("rules_primary", Text),
    Index("ix_kalshi_open", "open_ts"),
)

calibration = Table(
    "calibration",
    metadata,
    Column("ticker", String(48), primary_key=True),
    Column("boundary", BigInteger, nullable=False),
    Column("open_avg", Float),
    Column("close_avg", Float),
    Column("floor_strike", Float),
    Column("expiration_value", Float),
    Column("proxy_actual", String(4)),
    Column("kalshi_actual", String(4)),
    Column("agree", Boolean),
    Column("open_err", Float),
    Column("close_err", Float),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String(24), nullable=False),
    Column("status", String(12), nullable=False),  # queued | running | done | failed
    Column("params", Json(), nullable=False),
    Column("result", Json()),
    Column("error", Text),
    Column("created_ts", BigInteger, nullable=False),
    Column("started_ts", BigInteger),
    Column("finished_ts", BigInteger),
)

oos_checks = Table(
    "oos_checks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ruleset_hash", String(64), nullable=False),
    Column("horizon", Integer, nullable=False),
    Column("ts", BigInteger, nullable=False),
    Column("result", Json(), nullable=False),
)

promotions = Table(
    "promotions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("horizon", Integer, nullable=False),
    Column("kind", String(16), nullable=False),  # challenger | promote | retire | rollback
    Column("from_hash", String(64)),
    Column("to_hash", String(64)),
    Column("ts", BigInteger, nullable=False),
    Column("evidence", Json(), nullable=False),
)

# -- revision 0002: live Kalshi quotes and the read-only account link (U18, U19) --------
kalshi_quotes = Table(
    "kalshi_quotes",
    metadata,
    Column("ticker", String(48), primary_key=True),
    Column("ts", BigInteger, primary_key=True, autoincrement=False),
    Column("yes_bid", Float),
    Column("yes_ask", Float),
    Column("no_bid", Float),
    Column("no_ask", Float),
    Column("last", Float),
    Column("volume", Float),
    Column("source", String(16)),  # revision 0008: ws | orderbook (fresh); NULL = the old /markets list (stale)
)

kalshi_fills = Table(
    "kalshi_fills",
    metadata,
    Column("fill_id", String(64), primary_key=True),
    Column("ticker", String(48), nullable=False),
    Column("ts", BigInteger, nullable=False),
    Column("side", String(4), nullable=False),  # yes | no
    Column("action", String(4), nullable=False),  # buy | sell
    Column("count", Float, nullable=False),
    Column("price", Float, nullable=False),  # dollars paid per contract on `side`
    Column("is_taker", Boolean),
    Column("raw", Json(), nullable=False),
    Index("ix_fills_ticker", "ticker"),
)

kalshi_positions = Table(
    "kalshi_positions",
    metadata,
    Column("ticker", String(48), primary_key=True),
    Column("position", Float, nullable=False),  # >0 yes contracts, <0 no contracts
    Column("market_exposure", Float),
    Column("realized_pnl", Float),
    Column("fees_paid", Float),
    Column("updated_ts", BigInteger, nullable=False),
    Column("raw", Json(), nullable=False),
)

REVISION_0002 = (kalshi_quotes, kalshi_fills, kalshi_positions)

# -- revision 0003: backtest runs (U9) ---------------------------------------------------
backtest_runs = Table(
    "backtest_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_ts", BigInteger, nullable=False),
    Column("finished_ts", BigInteger),
    Column("start_ts", BigInteger, nullable=False),
    Column("end_ts", BigInteger, nullable=False),
    Column("feature_version", String(16), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("horizons", Json(), nullable=False),
    Column("rulesets", Json(), nullable=False),
    Column("splits", Json(), nullable=False),
)

REVISION_0003 = (backtest_runs,)

# -- revision 0005: Coach paper setups (15m Kalshi lens) -----------------------------------
coach_setups = Table(
    "coach_setups",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("horizon", Integer, nullable=False),
    Column("boundary", BigInteger, nullable=False),  # window start
    Column("window_end", BigInteger, nullable=False),
    Column("ticker", String(48)),
    Column("side", String(4), nullable=False),  # UP (buy YES) | DOWN (buy NO)
    Column("entry", Float, nullable=False),  # ask paid, dollars per contract
    Column("ask", Float, nullable=False),
    Column("fair_up", Float, nullable=False),  # model odds at entry
    Column("edge", Float, nullable=False),
    Column("tp", Float, nullable=False),  # take-profit bid at entry (fair(side) − 2¢); moves with the odds live
    Column("sl", Float, nullable=False),  # stop: exit if fair(side) falls below this
    Column("time_exit", BigInteger, nullable=False),  # start of the last-minute time rule
    Column("engine_call", String(8)),
    Column("created_ts", BigInteger, nullable=False),
    Column("outcome", String(12)),  # NULL while open | take_value | stop | time | settled | unscored | pre_fix | stale_quote (archived)
    Column("exit_price", Float),
    Column("exit_fair", Float),
    Column("exit_ts", BigInteger),
    Column("pnl", Float),  # dollars per contract after fees
    Column("fees", Float),
    UniqueConstraint("horizon", "created_ts", name="uq_coach_setup"),
    Index("ix_coach_open", "outcome"),
)

REVISION_0005 = (coach_setups,)

# -- revision 0006: Coach cockpit: the human's paper approvals and the kill switch ---------------
# One row per proposal (a coach setup, keyed by its created_ts): yes | no | expired. In paper mode a Yes
# records approval of the whole bracket (entry, take profit, stop); it never places an order.
coach_decisions = Table(
    "coach_decisions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("horizon", Integer, nullable=False),
    Column("setup_created_ts", BigInteger, nullable=False),  # coach_setups.created_ts of the proposal
    Column("boundary", BigInteger, nullable=False),
    Column("side", String(4), nullable=False),
    Column("max_price", Float, nullable=False),  # the entry limit shown on the card
    Column("decision", String(8), nullable=False),  # yes | no | expired
    Column("stake", Float, nullable=False),  # dollars per trade shown on the card
    Column("mode", String(8), nullable=False),  # paper (live is locked; no real-order code exists)
    Column("shown_ts", BigInteger),  # when the card was offered (the setup's quote time)
    Column("ts", BigInteger, nullable=False),  # when the answer was recorded
    UniqueConstraint("horizon", "setup_created_ts", name="uq_coach_decision"),
)

# Small persisted settings for the Coach (key -> JSON value). `paused` is the kill switch.
coach_settings = Table(
    "coach_settings",
    metadata,
    Column("key", String(32), primary_key=True),
    Column("value", Json(), nullable=False),
    Column("updated_ts", BigInteger, nullable=False),
)

REVISION_0006 = (coach_decisions, coach_settings)

# -- revision 0007: human-confirmed Kalshi trading (brackets, orders, audit, settings, micro sessions) --------
# Every real order belongs to a bracket the human approved with one Yes (entry + take profit + stop). Prices are
# dollars per contract on the bracket's own side (yes for UP, no for DOWN); counts are contracts (2 dp).
trading_brackets = Table(
    "trading_brackets",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("env", String(8), nullable=False),  # demo | prod
    Column("session_id", String(40)),  # micro-live session, NULL for full-limit trading
    Column("kind", String(8), nullable=False),  # entry (the bracket); exits are trading_orders rows
    Column("ticker", String(48), nullable=False),
    Column("side", String(4), nullable=False),  # UP (yes) | DOWN (no)
    Column("window_end", BigInteger, nullable=False),
    Column("stake", Float, nullable=False),
    Column("contracts", Float, nullable=False),  # approved size
    Column("limit_price", Float, nullable=False),  # approved max entry price
    Column("take_profit", Float, nullable=False),
    Column("stop", Float, nullable=False),  # contract-price stop; 0.0 = none (hold, or an index-only stop)
    Column("max_cost", Float, nullable=False),  # contracts * limit + taker fee: what the approval may spend
    Column("ticket_hash", String(64), nullable=False),
    # coach_setups.created_ts of a Coach proposal, or the removed strategy lab's text key ("pnw_cushion:<window>:<ms>");
    # text since 0018: SQLite stored both kinds in one column, Postgres needs one type
    Column("setup_key", String),
    Column("fair_up", Float),
    Column("state", String(12), nullable=False),  # sending | unknown | open | stopping | closed
    Column("filled", Float, nullable=False, default=0.0),
    Column("entry_avg", Float),
    Column("entry_cost", Float),  # actual dollars paid incl. fees
    Column("entry_fees", Float),
    Column("held", Float, nullable=False, default=0.0),
    Column("tp_order_id", String(64)),
    Column("tp_filled", Float, nullable=False, default=0.0),
    Column("tp_base", Float),  # tp_filled before the current take-profit order
    Column("tp_fees", Float),
    Column("tp_fees_base", Float),
    Column("no_tp", Boolean),  # take profit withdrawn (micro session ended); the stop still watches
    Column("ask_at_send", Float),  # the ask when the entry was sent (the paper price at that moment)
    Column("exit_filled", Float),
    Column("exit_reason", String(16)),  # tp | stop | exit | settled | unfilled | not_found
    Column("stop_trigger_ts", BigInteger),
    Column("stop_trigger_bid", Float),
    Column("stop_fair", Float),
    Column("exit_ts", BigInteger),
    Column("exit_avg", Float),
    Column("exit_fees", Float),
    Column("proceeds", Float),
    Column("created_ts", BigInteger, nullable=False),
    Column("updated_ts", BigInteger, nullable=False),
    Column("closed_ts", BigInteger),
    # Index stop (migration 0010; PNW cushion), BTC index dollars: an UP bracket stops once the live index is at or
    # below stop_index_below, a DOWN bracket once it is at or above stop_index_above. NULL = no index stop.
    Column("stop_index_below", Float),
    Column("stop_index_above", Float),
    Index("ix_trading_brackets_state", "state"),
    Index("ix_trading_brackets_created", "created_ts"),
    Index("ix_trading_brackets_session", "session_id"),  # migration 0022: the agent fleet's per-tick read
)

trading_orders = Table(
    "trading_orders",
    metadata,
    Column("client_order_id", String(64), primary_key=True),
    Column("bracket_id", String(40), nullable=False),
    Column("role", String(8), nullable=False),  # entry | tp | stop | exit
    Column("order_id", String(64)),
    Column("ticker", String(48), nullable=False),
    Column("side", String(4), nullable=False),  # yes | no
    Column("action", String(4), nullable=False),  # buy | sell
    Column("count", Float, nullable=False),
    Column("price", Float, nullable=False),  # limit, dollars on `side`
    Column("tif", String(24), nullable=False),
    Column("status", String(12), nullable=False),  # sending | resting | executed | canceled | unknown | rejected
    Column("filled", Float, nullable=False, default=0.0),
    Column("avg_price", Float),
    Column("fees", Float),
    Column("created_ts", BigInteger, nullable=False),
    Column("updated_ts", BigInteger, nullable=False),
    Index("ix_trading_orders_bracket", "bracket_id"),
    Index("ix_trading_orders_order_id", "order_id"),  # migration 0020: lookups by the venue's order id
)

trading_audit = Table(
    "trading_audit",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", BigInteger, nullable=False),
    Column("event", String(20), nullable=False),
    Column("env", String(8), nullable=False),
    Column("bracket_id", String(40)),
    Column("session_id", String(40)),
    Column("code", String(32)),
    Column("reason", Text),
    Column("detail", Json()),
    Column("actor", String(16)),  # migration 0012: "pc", "phone:<8 chars>" or "system"; NULL before it
    Index("ix_trading_audit_ts", "ts"),
    Index("ix_trading_audit_bracket_id", "bracket_id"),  # migration 0011: the scoreboard's manual-exit lookup
)

trading_settings = Table(
    "trading_settings",
    metadata,
    Column("key", String(32), primary_key=True),
    Column("value", Json(), nullable=False),
    Column("updated_ts", BigInteger, nullable=False),
)

trading_sessions = Table(
    "trading_sessions",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("env", String(8), nullable=False),
    Column("pool", Float, nullable=False),  # dollars that may ever be committed in the session
    Column("minutes", Integer, nullable=False),
    Column("rules", Json()),  # the strategy/take-profit/stop rules the human approved for the session
    Column("started_ts", BigInteger, nullable=False),
    Column("ends_ts", BigInteger, nullable=False),
    Column("ended_ts", BigInteger),
    Column("end_reason", String(16)),  # stopped | time | pool | kill
    Column("committed", Float, nullable=False, default=0.0),
    Column("report", Json()),
)

REVISION_0007 = (trading_brackets, trading_orders, trading_audit, trading_settings, trading_sessions)

# -- revision 0008: kalshi_quotes.source (added in place above; 0008 adds the column to existing databases) ------
# Rows recorded before 0008 (source NULL) came from the /markets list, measured lagging the book by ~4 s.

# -- revision 0009: strategy modes (taker | maker | final_minute), the shadow lab, per-mode accounts ---------------
# Every trading_* row belongs to one strategy. Rows written before 0009 (and by code that does not name one) are
# the Coach taker's, so the column defaults to 'taker' in the database itself.
STRATEGY_DEFAULT = "taker"


def _strategy_column() -> Column:
    return Column("strategy", String(16), nullable=False, server_default=STRATEGY_DEFAULT)


for _t in (trading_brackets, trading_orders, trading_audit, trading_sessions):
    _t.append_column(_strategy_column())
STRATEGY_COLUMN_TABLES = ("trading_brackets", "trading_orders", "trading_audit", "trading_sessions")
Index("ix_trading_brackets_strategy_state", trading_brackets.c.strategy, trading_brackets.c.state)

# One simulated (lab) or real (live) account per mode run. Lab accounts are simulations on real prices.
strategy_accounts = Table(
    "strategy_accounts",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("strategy", String(16), nullable=False),
    Column("kind", String(8), nullable=False),  # lab | live
    Column("env", String(8), nullable=False),  # shadow | demo | prod
    Column("session_id", String(40)),  # trading_sessions.id of the auto session (live only)
    Column("params", Json(), nullable=False),  # the mode's parameters (MakerParams, FinalMinuteParams, ...) for the run
    Column("budget", Float, nullable=False),  # dollars the account may commit
    Column("loss_limit", Float, nullable=False),  # dollars; the account stops when realized loss reaches it
    Column("cash", Float, nullable=False),  # dollars; settlement pays 1/0 per contract into it
    Column("started_ts", BigInteger, nullable=False),
    Column("ended_ts", BigInteger),
    Column("end_reason", String(16)),  # stopped | loss_limit | budget | kill | time | restart
    Column("updated_ts", BigInteger, nullable=False),
    Index("ix_strategy_accounts_strategy_kind", "strategy", "kind"),
)

# Quote and taker orders sent by a mode's QuoteEngine / lab, persisted before they are sent (KTD8).
# Prices are dollars per contract on `side`; counts are contracts.
strategy_orders = Table(
    "strategy_orders",
    metadata,
    Column("client_order_id", String(64), primary_key=True),
    Column("strategy", String(16), nullable=False),
    Column("account_id", String(40), nullable=False),
    Column("env", String(8), nullable=False),
    Column("order_id", String(64)),
    Column("ticker", String(48), nullable=False),
    Column("side", String(4), nullable=False),  # yes | no
    Column("action", String(4), nullable=False),  # buy | sell
    Column("count", Float, nullable=False),  # original size
    Column("price", Float, nullable=False),
    Column("tif", String(24), nullable=False),
    Column("post_only", Boolean, nullable=False),
    Column("reduce_only", Boolean, nullable=False, default=False),
    Column("order_group_id", String(64)),
    Column("tag", String(32)),  # the intent's tag (bid | ask | flatten | entry ...)
    Column("status", String(12), nullable=False),  # sending | resting | executed | canceled | unknown | rejected
    Column("filled", Float, nullable=False, default=0.0),
    Column("remaining", Float),
    Column("avg_price", Float),
    Column("fees", Float),
    Column("reason", Text),  # refusal / cancel reason
    Column("created_ts", BigInteger, nullable=False),
    Column("updated_ts", BigInteger, nullable=False),
    Index("ix_strategy_orders_account_status", "account_id", "status"),
    Index("ix_strategy_orders_strategy_status", "strategy", "status"),
)

# One row per fill of a mode's order, with the mid at the fill and 5/30/60 s later (markout, KTD11 (8)).
strategy_fills = Table(
    "strategy_fills",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("strategy", String(16), nullable=False),
    Column("account_id", String(40), nullable=False),
    Column("fill_id", String(64)),  # exchange or shadow fill id; unique per account when present
    Column("client_order_id", String(64)),
    Column("order_id", String(64)),
    Column("ticker", String(48), nullable=False),
    Column("side", String(4), nullable=False),  # yes | no
    Column("action", String(4), nullable=False),  # buy | sell
    Column("count", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("fee", Float, nullable=False, default=0.0),
    Column("is_taker", Boolean),
    Column("fill_model", String(8), nullable=False),  # trades | book (fallback) | exchange (real fill)
    Column("ts", BigInteger, nullable=False),
    Column("mid_at_fill", Float),  # YES mid
    Column("mid_5s", Float),
    Column("mid_30s", Float),
    Column("mid_60s", Float),
    UniqueConstraint("account_id", "fill_id", name="uq_strategy_fill"),
    Index("ix_strategy_fills_account_ts", "account_id", "ts"),
    Index("ix_strategy_fills_strategy_ts", "strategy", "ts"),
)

# Per-account, per-window results: the unit of evidence for scorecards and promotion bars (KTD15).
strategy_windows = Table(
    "strategy_windows",
    metadata,
    Column("account_id", String(40), primary_key=True),
    Column("boundary", BigInteger, primary_key=True, autoincrement=False),  # window start
    Column("strategy", String(16), nullable=False),
    Column("ticker", String(48)),
    Column("window_end", BigInteger, nullable=False),
    Column("pnl", Float, nullable=False),  # after fees, settlement included
    Column("spread_pnl", Float, nullable=False, default=0.0),
    Column("settle_pnl", Float, nullable=False, default=0.0),
    Column("fees", Float, nullable=False, default=0.0),
    Column("trades", Integer, nullable=False, default=0),
    Column("fills", Integer, nullable=False, default=0),
    Column("inventory_close", Float),  # net YES contracts held into settlement
    Column("markout", Float),  # average 30 s markout per fill, signed for our side
    Column("result", String(4)),  # yes | no (Kalshi's published result)
    Column("detail", Json()),
    Column("settled_ts", BigInteger, nullable=False),
    Index("ix_strategy_windows_strategy_boundary", "strategy", "boundary"),
)

# Per-mode settings (lab on/off, parameter overrides, promotion-bar approvals). The global kill switch stays in
# trading_settings.
strategy_settings = Table(
    "strategy_settings",
    metadata,
    Column("strategy", String(16), primary_key=True),
    Column("key", String(32), primary_key=True),
    Column("value", Json(), nullable=False),
    Column("updated_ts", BigInteger, nullable=False),
)

# Per-window context for the mode selector (R28, R29): features, the pick and its reason.
mode_window_features = Table(
    "mode_window_features",
    metadata,
    Column("boundary", BigInteger, primary_key=True, autoincrement=False),  # window start
    Column("window_end", BigInteger, nullable=False),
    Column("ticker", String(48)),
    Column("features", Json(), nullable=False),
    Column("selector", String(32)),  # which selector made the pick (rule | ...)
    Column("pick", String(16)),  # the chosen mode, NULL = none
    Column("reason", Text),
    Column("scores", Json()),
    Column("created_ts", BigInteger, nullable=False),
    Column("updated_ts", BigInteger, nullable=False),
)

REVISION_0009 = (strategy_accounts, strategy_orders, strategy_fills, strategy_windows, strategy_settings,
                 mode_window_features)

# -- revision 0013: Kalshi BTC perpetuals (KTD13) ----------------------------------------------------------------
# The exchange position is the truth (KTD8); these rows hold Settleview's intent and history. env is perp_demo |
# perp_prod. Prices are dollars per contract (one contract = 0.0001 BTC, tick $0.0001); counts are contracts (whole
# contracts for BTC, stored as Float like the other trading tables); money is dollars. Times are integer UTC ms. Audit and settings reuse trading_audit / trading_settings
# with strategy "perps".
perp_positions = Table(
    "perp_positions",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("env", String(12), nullable=False),  # perp_demo | perp_prod
    Column("ticker", String(48), nullable=False),  # KXBTCPERP (prod) | KXBTCPERP1 (demo)
    Column("subaccount", Integer, nullable=False),  # the configured perps subaccount (0 = the primary account)
    Column("direction", String(8), nullable=False),  # long | short
    Column("size", Float, nullable=False, default=0.0),  # contracts held, unsigned
    Column("avg_entry", Float),
    Column("committed_collateral", Float, nullable=False),  # the collateral the leverage slider sized the position from (KD5)
    Column("leverage_chosen", Float, nullable=False),
    # flat | sending | unknown | open | protected | unprotected | closing | diverged | closed
    Column("state", String(12), nullable=False),
    Column("close_reason", String(16)),  # user_close | stop | take_profit | liquidated | exchange_reduced | ...
    Column("realized_pnl", Float, nullable=False, default=0.0),  # from recorded fills, fees and funding included
    Column("fees", Float, nullable=False, default=0.0),
    Column("funding_total", Float, nullable=False, default=0.0),  # positive = received, negative = paid
    Column("opened_ms", BigInteger, nullable=False),
    Column("closed_ms", BigInteger),
    Column("updated_ms", BigInteger, nullable=False),
    Index("ix_perp_positions_env_state", "env", "state"),
)

# Every perp order, saved as 'sending' before it is sent (R18): an unknown outcome is resolved by client_order_id.
perp_orders = Table(
    "perp_orders",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("env", String(12), nullable=False),
    Column("position_id", String(40), nullable=False),
    Column("client_order_id", String(64), nullable=False, unique=True),
    Column("order_id", String(64)),  # the exchange's id, once known
    Column("role", String(12), nullable=False),  # entry | add | reduce | close | trigger_fire
    Column("side", String(4), nullable=False),  # bid | ask
    Column("price", Float, nullable=False),  # the protective limit ("at most $P")
    Column("count", Float, nullable=False),
    # an entry's or add's collateral (the per-trade and per-day limits count it, R9/KD7); None on exits
    Column("collateral", Float),
    Column("time_in_force", String(24), nullable=False),  # immediate_or_cancel | fill_or_kill (KTD5)
    Column("reduce_only", Boolean, nullable=False),
    Column("filled_count", Float, nullable=False, default=0.0),
    Column("avg_fill_price", Float),
    Column("fees", Float),
    Column("outcome", String(16), nullable=False),  # sending | filled | partial | nothing_filled | rejected | unknown
    Column("reason", Text),
    # since 0019: the stop / take profit approved with an entry, placed if the entry is found filled late (B3#1)
    Column("stop_loss", Float),
    Column("take_profit", Float),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    Index("ix_perp_orders_position", "position_id"),
    Index("ix_perp_orders_env_outcome", "env", "outcome"),
)

# The whole-position exit trigger (KTD6). exchange_updated_time is the trigger's `updated_time` exactly as Kalshi
# returned it (RFC 3339 text, compared verbatim), the "last update time" an approval binds (KTD9).
perp_triggers = Table(
    "perp_triggers",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("env", String(12), nullable=False),
    Column("position_id", String(40), nullable=False),
    Column("exchange_trigger_id", String(64)),
    Column("stop_loss_price", Float),
    Column("take_profit_price", Float),
    Column("state", String(12), nullable=False),  # none | pending | active | failed | filled | canceled
    Column("exchange_updated_time", String(40)),
    Column("fired_leg", String(16)),  # the exchange's triggered_leg
    Column("fired_order_id", String(64)),  # the exchange's triggered_order_id
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    Index("ix_perp_triggers_position", "position_id"),
)

# Funding ledger from GET /margin/funding_history: one payment per market per funding time (8-hour intervals).
perp_funding = Table(
    "perp_funding",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("env", String(12), nullable=False),
    Column("ticker", String(48), nullable=False),
    Column("funding_ms", BigInteger, nullable=False),
    Column("funding_rate", Float, nullable=False),
    Column("amount", Float, nullable=False),  # dollars; positive = received, negative = paid
    Column("position_id", String(40)),
    UniqueConstraint("env", "ticker", "funding_ms", name="uq_perp_funding"),
    Index("ix_perp_funding_position", "position_id"),
)

REVISION_0013 = (perp_positions, perp_orders, perp_triggers, perp_funding)

# -- revision 0014: Agent mode (docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md) ---------------------------
# One row per agent session the user launched. Its orders are ordinary trading rows stamped with the session id
# (trading_brackets.session_id, trading_audit.session_id) and actor "agent:<first 8 characters of the id>".
agent_sessions = Table(
    "agent_sessions",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("kind", String(12), nullable=False),  # in_app | external
    Column("state", String(10), nullable=False),  # running | paused | ended
    Column("limits", Json(), nullable=False),  # budget, per_trade, venues, spend cap, perps caps, sell_on_stop
    Column("pinned", Json(), nullable=False),  # strategy -> trading mode at launch (R6)
    Column("model", String(40)),  # in-app agent only
    Column("prompt_versions", Json()),  # {"system": n, "user": n}
    Column("token_hash", String(64)),  # external agent's write token (sha256); NULL for in-app
    Column("taken_over", Json()),  # bracket ids the user took over (R11; they still count against the budget)
    Column("usage", Json()),  # wakes, API spend (estimated), tokens
    Column("pause_reason", Text),
    Column("pause_code", String(24)),  # why it paused, as a code (agent_sessions.PAUSE_CODES); 0015
    Column("agent_id", String(40)),  # the lasting agent this session is a run of (agents.id); 0016
    Column("started_ts", BigInteger, nullable=False),
    Column("ends_ts", BigInteger, nullable=False),
    Column("ended_ts", BigInteger),
    Column("end_reason", String(24)),  # stopped | time | spend_cap | agent | invalid_key | restart
    Column("updated_ts", BigInteger, nullable=False),
)

agent_journal = Table(
    "agent_journal",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("session_id", String(40), nullable=False),
    Column("ts", BigInteger, nullable=False),
    Column("kind", String(16), nullable=False),  # wake | tool | note | state | skipped
    Column("data", Json(), nullable=False),
    Column("agent_id", String(40)),  # the session's agent (agents.id): the agent's journal across its runs; 0016
    Index("ix_agent_journal_session", "session_id", "id"),
    Index("ix_agent_journal_agent", "agent_id", "id"),
    Index("ix_agent_journal_kind", "kind", "id"),  # migration 0022: rule rows by id, rule links by kind
)

agent_prompts = Table(
    "agent_prompts",
    metadata,
    Column("layer", String(8), primary_key=True),  # system | user
    Column("version", Integer, primary_key=True),
    Column("text", Text, nullable=False),
    Column("created_ts", BigInteger, nullable=False),
)

REVISION_0014 = (agent_sessions, agent_journal, agent_prompts)

# Multi-agent L0 (docs/plans/2026-10-31-0010-feat-multi-agent-plan.md): an agent is a lasting identity (one market, a
# name); each agent session is one run of it. Archived agents keep their runs and journal and cannot run again.
agents = Table(
    "agents",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(60), nullable=False),
    Column("market", String(64), nullable=False),  # one market key; an archived earlier-runs agent: "a,b" (0016)
    Column("created_ts", BigInteger, nullable=False),
    Column("archived", Boolean, nullable=False, server_default="0"),
    Column("updated_ts", BigInteger, nullable=False),
)

REVISION_0016 = (agents,)

# -- revision 0017: Postgres's trade archive (docs/plans/2026-10-31-0900-feat-postgres-plan.md PD7) ------------------
# The daily maintenance moves Coinbase `trades` rows older than the retention window here, in the same database, on
# Postgres. On SQLite the archive stays a separate file (`<db>-archive.db`, same columns) and this table stays empty.
trades_archive = Table(
    "trades_archive",
    metadata,
    *(Column(c.name, c.type, primary_key=c.primary_key, autoincrement=False if c.primary_key else "auto",
             nullable=c.nullable) for c in trades.columns),
    Index("ix_trades_archive_ts", "ts"),
)

REVISION_0017 = (trades_archive,)

# -- revision 0021: each agent wake's exact model input (docs/plans/2026-10-31-1900-agent-evals-plan.md) ----------
# The one user message a wake sent (<memory> + <events> + <observe>, verbatim after the secret scrubber) and the
# fingerprint of everything else the model saw (system prompt / tool specs hashes, model, effort, max_tokens, SDK, CLI,
# git sha). Its own table, keyed by the wake's journal row: the message (p50 a few KB, the observe the largest part)
# stays out of `agent_journal`, which the UI pages through and the push channel sends. `sensitive` lists the parts an
# eval export must redact (the account balances are part of the input, so they are kept here).
agent_wake_inputs = Table(
    "agent_wake_inputs",
    metadata,
    Column("journal_id", Integer, primary_key=True, autoincrement=False),  # agent_journal.id of the "wake" row
    Column("session_id", String(40), nullable=False),
    Column("wake_id", Integer, nullable=False),
    Column("ts", BigInteger, nullable=False),
    Column("message", Text, nullable=False),
    Column("message_sha256", String(64), nullable=False),
    Column("fingerprint", Json(), nullable=False),
    Column("sensitive", Json(), nullable=False),
    Index("ix_agent_wake_inputs_session", "session_id", "wake_id"),
)

REVISION_0021 = (agent_wake_inputs,)

TABLES = {t.name: t for t in metadata.sorted_tables}
