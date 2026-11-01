"""Composition root (KTD20): the only place adapters are built and wired into use cases.

`python -m btc_signal_engine`            run the Play app: collector + engine + Coach evaluation + Kalshi / Polymarket
                                         feeds + the phone trading services + Kalshi perps (when BTCFLOW_PERPS_ENABLED)
                                         + API + the web app
`python -m btc_signal_engine backfill`   backfill Coinbase history and Kalshi results (BTCFLOW_BACKFILL_DAYS)
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import signal
import sys
import threading
import time
import contextlib
import dataclasses
import functools
import importlib.metadata
import logging
from collections.abc import Callable, Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import uvicorn

from btc_signal_engine.bootstrap import telemetry
from btc_signal_engine.bootstrap.logs import (
    LOG_FORMAT as _LOG_FORMAT,
    UtcFormatter,
    log_to_stderr,
    mask,
    mask_secrets,
    set_http_client_log_level,
)
from btc_signal_engine.bootstrap.maintenance import DailyMaintenance
from btc_signal_engine.bootstrap.stall import StallWatch
from btc_signal_engine.bootstrap.supervisor import Supervisor

from btc_signal_engine.adapters.clock import ExchangeClock
from btc_signal_engine.adapters.coinbase.backfill import backfill, repair_gaps
from btc_signal_engine.adapters.coinbase.collector import CoinbaseCollector
from btc_signal_engine.adapters.coinbase.rest import CoinbaseRest
from btc_signal_engine.adapters.agent_sdk.harness import SDK_VERSION as AGENT_SDK_VERSION
from btc_signal_engine.adapters.agent_sdk.harness import SdkHarness
from btc_signal_engine.adapters.anthropic.llm import AnthropicLlm
from btc_signal_engine.adapters.http.access import AccessGuard, PinStore, install_access, is_loopback_host
from btc_signal_engine.adapters.pc_credentials import PcCredentials
from btc_signal_engine.adapters.http.agent_feed import AgUiFeed, FleetFeeds, ObservedTools
from btc_signal_engine.adapters.http.agent_routes import (KeyState, register_agent_routes, wake_hook,
                                                          wake_start_hook)
from btc_signal_engine.adapters.http.app import Services, create_app
from btc_signal_engine.adapters.http.coach_routes import register_coach_routes
from btc_signal_engine.adapters.http.copilot_routes import register_copilot_routes
from btc_signal_engine.adapters.http.perp_routes import Inflight, PerpPublisher, register_perp_routes
from btc_signal_engine.adapters.http.play_routes import register_play_routes
from btc_signal_engine.adapters.http.record_routes import register_record_routes
from btc_signal_engine.adapters.http.push import FeedHub, PushHub, TickStreams
from btc_signal_engine.adapters.http.telegram_routes import register_preset_routes, register_telegram_routes
from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from btc_signal_engine.adapters.kalshi.auth import load_private_key
from btc_signal_engine.adapters.kalshi.backfill import backfill_kalshi
from btc_signal_engine.adapters.kalshi.margin import MarginAuth, MarginClient
from btc_signal_engine.adapters.kalshi.margin_stream import MarginStream
from btc_signal_engine.adapters.kalshi.prints import TradePrints
from btc_signal_engine.adapters.kalshi.client import KalshiClient
from btc_signal_engine.adapters.kalshi.portfolio import portfolio_from_settings
from btc_signal_engine.adapters.kalshi.shadow import ShadowExchange, ShadowStream, shadow_trading
from btc_signal_engine.adapters.kalshi.stream import market_stream_from_settings, stream_from_settings
from btc_signal_engine.adapters.kalshi.trading import trading_from_settings
from btc_signal_engine.adapters.retry import full_jitter
from btc_signal_engine.adapters.polymarket_us.client import sdk_client
from btc_signal_engine.adapters.polymarket_us.market_data import PolymarketMarketData, PolymarketMarketStream
from btc_signal_engine.adapters.polymarket_us.markets import SERIES as PM_SERIES, MarketTerms, slug_for
from btc_signal_engine.adapters.polymarket_us.shadow import SHADOW_ENV as PM_SHADOW, polymarket_shadow
from btc_signal_engine.adapters.polymarket_us.stream import PolymarketTradingStream
from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
from btc_signal_engine.adapters.ruleset_files import read_seed_files
from btc_signal_engine.adapters.sqlite.db import (
    SchemaNotMigrated,
    describe,
    migrate,
    read_engine,
    require_head,
    write_engine,
)
from btc_signal_engine.adapters.sqlite.store import SqliteStore
from btc_signal_engine.adapters.sqlite.urls import use_pg_timeouts
from btc_signal_engine.adapters.sqlite.writer import BufferedStore, is_transient
from btc_signal_engine.adapters.telegram.bot import TelegramBot
from btc_signal_engine.application.actor import SYSTEM
from btc_signal_engine.application.agent_presets import AgentPresets
from btc_signal_engine.application.agent_prompts import AgentPrompts
from btc_signal_engine.application.agent_proposals import AgentProposals
from btc_signal_engine.application.agent_events import EventsConfig, SignalBoard
from btc_signal_engine.application.agent_fleet import AgentFleet
from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.agent_sessions import (BRACKETS, VENUE_LABELS, VENUES, AgentSessions,
                                                          position_view)
from btc_signal_engine.application.agent_tools import AgentTools
from btc_signal_engine.application.coach import CoachService
from btc_signal_engine.application.index_tail import IndexTail
from btc_signal_engine.application.copilot import Copilot
from btc_signal_engine.application.engine.engine import Engine
from btc_signal_engine.application.engine.hydrate import LOOKBACK_MS, hydrate
from btc_signal_engine.application.engine.lean import compute_lean
from btc_signal_engine.application.engine.loop import due_boundaries
from btc_signal_engine.application.kalshi_sync import KalshiSync
from btc_signal_engine.application.notifier import RemoteControl
from btc_signal_engine.application.recorder_health import RecorderWatch
from btc_signal_engine.adapters.sqlite.research import ResearchDb
from btc_signal_engine.application.perp_trading import PerpConfig, PerpTradingService
from btc_signal_engine.application.perp_view import PerpView
from btc_signal_engine.application.play import WINDOW_MS as PLAY_WINDOW_MS, PlayService, PlayVenue
from btc_signal_engine.application.play_limits import CommittedToday, PlayLimits, day_pool
from btc_signal_engine.application.rule_scores import RuleBook
from btc_signal_engine.application.rulesets import RulesetService
from btc_signal_engine.application.settlement import SettlementService
from btc_signal_engine.application.trading import GateResult, TradingConfig, TradingService
from btc_signal_engine.application.your_record import YourRecord
from btc_signal_engine.bootstrap.config import Settings
from btc_signal_engine.domain import days
from btc_signal_engine.domain.coach.fair_value import HISTORY_MS, fair_from_index
from btc_signal_engine.domain.markets import PERP_ENVS, MarketSpec, market, perp, phone_markets
from btc_signal_engine.domain.models.events import Trade
from btc_signal_engine.domain.settlement.fees import POLYMARKET_FEE_RATE, polymarket_fee
from btc_signal_engine.domain.strategy.crowd import crowd_summary

log = logging.getLogger("btc_signal_engine")
ROOT = Path(__file__).resolve().parents[2]
STALE_MS = 15_000
SKEW_LIMIT_MS = 1_000
# The user's own calls from the phone Play screens: human Yes only. One service per Play tab of the market table
# (domain/markets.py): "you" (Kalshi 15m), "you_poly" (Polymarket 15m), "you_poly5" (Polymarket 5m).
YOU = market("kalshi_15m").phone_strategy
YOU_TICK_S = 0.5
RV_P95_REFRESH_S = 3_600  # the engine's rv_p95 (7 days of snapshots) is recomputed hourly, off the loop
# A startup read that hits a transient database error (a statement timeout on a cold cache, a dropped connection) is
# tried this many times, waiting 2 s, then 4 s, before the start stops (docs/solutions/2026-11-01-startup-trades-probe-
# walked-the-pkey.md). Each failed try is a warning, the last one an error naming the read.
STARTUP_READ_ATTEMPTS = 3
STARTUP_READ_BACKOFF_S = 2.0
# Loops whose repeated failure turns the kill switch on (KTD7): the phone services' monitor, the writer, the web server.
CRITICAL_LOOPS = frozenset({"you", "writer", "server"})
# Strategies of the retired bots (taker, PNW cushion, PNW on Polymarket). Their past orders are still in the database
# on the same accounts: the phone services count them as this app's (never external) during reconciliation.
RETIRED_KALSHI = ("taker", "pnw_cushion")
RETIRED_PM = ("pnw_poly",)
# Every strategy whose orders live on the shared Kalshi / Polymarket US account.
KALSHI_LIVE = (*RETIRED_KALSHI, *(m.phone_strategy for m in phone_markets("kalshi")))
PM_LIVE = (*RETIRED_PM, *(m.phone_strategy for m in phone_markets("polymarket")))
PM_PROD = "pm_prod"
PM_MODES = {"shadow": PM_SHADOW, "prod": PM_PROD}
# Graceful stop for deploys (KTD9): scripts/deploy_stop.ps1 creates this file in the data folder; the app sees it within
# STOP_POLL_S, shuts down the normal way (writer flushed, tasks awaited, clients closed), deletes it and exits with 0.
STOP_FILE = "stop.request"
PID_FILE = "app.pid"  # this process's id, for scripts/deploy_stop.ps1 (it trusts nothing else to find the app)
STOP_POLL_S = 1.0
MEMORY_LOG_S = 600  # one INFO line with the process's private bytes every 10 minutes
KALSHI_LIVE_POLL_S = 0.5
KALSHI_POLL_WARN_MS = 60_000  # a failing Kalshi live poll warns at most once a minute (DEBUG in between)
LOG_FORMAT = _LOG_FORMAT
LOG_FILE_BYTES = 10 * 2**20  # data/logs/app.log, 10 MB x 10 files
LOG_FILE_BACKUPS = 10
# Kalshi perps (docs/plans/2026-10-29-2356-feat-kalshi-perps-tab-plan.md U8). The monitor tick reads status, market,
# positions, risk, triggers, resting orders, the funding estimate, the notional risk limit and the balance WITH
# compute_available_balance (10x a plain read: 50 tokens): about 90 read tokens a tick, ~45 a second at 2 s, far under
# the basic tier's 200 (Kalshi's perps buckets are separate from event contracts').
PERPS_TICK_S = 2.0
PERPS_DRAIN_S = 15.0  # shutdown waits this long for a perps approval's save-send-record step
# Agent mode (docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md U7): the default system prompt is the user's
# text in the repo, read at start-up; the review loop ends a session at its end time and pauses it on the kill switch
# or a mode change also when no in-app runner ticks (an external session).
AGENT_DEFAULT_PROMPT = ROOT / "docs" / "agent" / "default-system-prompt.md"
AGENT_REVIEW_S = 1.0
AGENT_STOP_S = 15.0  # shutdown waits this long for the in-app agent's order in flight to finish and be recorded
try:  # the agent runtime's client-app tag (CLAUDE_AGENT_SDK_CLIENT_APP)
    APP_VERSION = importlib.metadata.version("settleview")
except importlib.metadata.PackageNotFoundError:  # run from a checkout that was never installed
    APP_VERSION = "unknown"
PERP_KEY_NAMES = {"perp_demo": ("demo", "BTCFLOW_TRADING_DEMO_KEY_ID / _KEY_PATH"),
                  "perp_prod": ("production", "BTCFLOW_TRADING_PROD_KEY_ID / _KEY_PATH")}


async def _cancel_task(task: asyncio.Task | None, label: str) -> None:
    """Cancel a stream task and wait for it; an error it ended with is logged, never raised."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.warning("%s ended with an error", label, exc_info=True)


def margin_from_settings(env: str, key_id: str | None, key_path: str | Path | None, *, subaccount: int,
                         max_notional: float, transport=None) -> tuple[MarginClient | None, MarginAuth | None, str]:
    """The perps client and signer for `env` (perp_demo: the trading DEMO key against Kalshi's demo hosts; perp_prod:
    the trading PROD key against production), or (None, None, why) when that env's own key is missing or unreadable.
    Each signer refuses any host but its own env's before signing (MarginAuth). `transport` None = the network."""
    which, names = PERP_KEY_NAMES[env]
    if not key_id or not key_path:
        return None, None, f"No {which} trading key is configured ({names}): perps {which} is unavailable."
    try:
        key = load_private_key(key_path)
    except Exception as exc:  # never log the path or its contents
        log.warning("%s perps key could not be loaded (%s); %s stays unavailable", which, type(exc).__name__, env)
        return None, None, (f"The {which} trading key could not be loaded ({type(exc).__name__}): perps {which} is "
                            "unavailable.")
    auth = MarginAuth(key_id, key, env=env)
    return MarginClient(auth, env=env, subaccount=subaccount, max_notional=max_notional, transport=transport), auth, ""


async def perps_access_check(client: MarginClient) -> str | None:
    """Margin access and the configured subaccount (U8, run first in every perps reconcile): None when both hold,
    else the reason trading is refused. `GET /margin/enabled` must say true, and the subaccount must appear in
    `/margin/balance` (Kalshi has no list endpoint; LEARNINGS)."""
    if not await client.enabled():
        return f"Kalshi says perps (margin) trading is not enabled on this {client.env} account; nothing can be sent."
    bal = await client.balance(compute_available_balance=False)
    subs = sorted(int(b["subaccount"]) for b in bal["subaccount_balances"])
    if client.subaccount not in subs:
        return (f"Kalshi lists no perps subaccount {client.subaccount} on this account (it lists "
                f"{', '.join(map(str, subs)) or 'none'}): check BTCFLOW_PERPS_SUBACCOUNT; nothing can be sent.")
    return None


def private_bytes() -> int | None:
    """This process's private memory in bytes (Windows: PrivateUsage via the process-memory API; elsewhere the peak
    resident size, which `resource` reports); None when it cannot be read."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            *((f, ctypes.c_size_t) for f in (
                                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage"))]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
            c = Counters()
            c.cb = ctypes.sizeof(c)
            if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return None
            return int(c.PrivateUsage)
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(rss if sys.platform == "darwin" else rss * 1024)  # bytes on macOS, KiB on Linux
    except Exception:
        return None


class FeedState:
    """What the dashboard shows about the live feed."""

    def __init__(self, clock: ExchangeClock) -> None:
        self.clock = clock
        self.last_trade: Trade | None = None
        self.last_msg_ms: int | None = None

    def on_trade(self, t: Trade) -> None:
        self.last_trade = t
        self.last_msg_ms = self.clock.now_ms()

    def snapshot(self) -> dict:
        now = self.clock.now_ms()
        if self.last_msg_ms is None:
            status = "connecting"
        else:
            status = "live" if now - self.last_msg_ms <= STALE_MS else "stale"
        t = self.last_trade
        return {"status": status, "lag_ms": (now - t.ts) if t else None, "last_trade_ts": t.ts if t else None,
                "clock_offset_ms": self.clock.offset_ms,
                "price": {"last": t.price, "ts": t.ts} if t else None}



class SharedStream:
    """One environment's live feed shared by several trading services: each service's view keeps its own market
    list and the feed watches the union (a service's subscribe call never drops another service's markets)."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.wants: dict[str, set[str]] = {}

    def view(self, name: str) -> _StreamView:
        return _StreamView(self, name)

    def subscribe(self, name: str, tickers) -> None:
        self.wants[name] = {t for t in tickers if t}
        self.stream.subscribe_tickers(set().union(*self.wants.values()))


class _StreamView:
    def __init__(self, shared: SharedStream, name: str) -> None:
        self._shared, self._name = shared, name
        self.env = shared.stream.env

    def subscribe_tickers(self, tickers) -> None:
        self._shared.subscribe(self._name, tickers)

    def health(self) -> dict:
        return self._shared.stream.health()


def _feed_trimmed(n: int) -> dict:
    """An agent feed's event for a slow client that lost `n` events."""
    return {"type": "CUSTOM", "name": "feed_trimmed", "value": {"dropped": n}}


async def startup_read(what: str, read: Callable[[], Any], *, attempts: int = STARTUP_READ_ATTEMPTS,
                       backoff_s: float = STARTUP_READ_BACKOFF_S) -> Any:
    """`read()`, a read-only store call (safe to repeat), retried on a transient database error with doubling, fully
    jittered waits (adapters/retry.py). A non-transient error, or the last failure, is raised after an error line
    naming the read: never a silent default."""
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")
    for attempt in range(1, attempts + 1):
        try:
            return read()
        except Exception as exc:
            first = (str(exc).strip().splitlines() or [""])[0][:300]
            if not is_transient(exc) or attempt == attempts:
                log.error("startup: %s failed (%s: %s) after %d attempt(s); the start stops here", what,
                          type(exc).__name__, first, attempt)
                raise
            wait = full_jitter(attempt - 1, backoff_s, backoff_s * 2 ** (attempts - 1))  # adapters/retry.py
            log.warning("startup: %s failed (%s: %s), attempt %d of %d; retrying in %.0f s", what,
                        type(exc).__name__, first, attempt, attempts, wait)
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # OpenTelemetry (bootstrap/telemetry.py), set by main(): its gauges and hooks are bound in run()
        self.telemetry: telemetry.Telemetry | None = None
        use_pg_timeouts(settings.pg_timeouts)  # every Postgres engine below: statement / lock / idle limits
        db = settings.database  # the Postgres URL (BTCFLOW_DATABASE_URL) or the SQLite file (BTCFLOW_DB_PATH)
        data_dir = settings.data_dir  # logs, pid and stop files: a local folder either way
        log.info("database: %s", describe(db))
        if settings.db_migrate == "check":  # the one-off migrate job owns the schema (compose's `migrate` service)
            log.info("database schema at revision %s (migrated by the migrate job)", require_head(db))
        else:
            migrate(db)
        we = write_engine(db)
        self.store = SqliteStore(we, read_engine(db))
        # A database outage is retried until it ends; only a refused write (or, past the queue cap, market data) goes
        # to data/logs/writer-dead-letter.jsonl, never silently dropped (R7): `ops replay-dead-letter` replays it.
        self.writer = BufferedStore(self.store, we,
                                    dead_letter_path=data_dir / "logs" / "writer-dead-letter.jsonl")
        # Every background loop runs under the supervisor (KTD7): restarted with backoff; a critical one failing 5
        # times in 10 minutes turns the kill switch on.
        self.supervisor = Supervisor(on_trip=self.trip_kill_switch)
        self.stop_file = data_dir / STOP_FILE
        self.pid_file = data_dir / PID_FILE
        # The daily online backup and Coinbase trade archive (R18, R19), in a worker thread at a quiet minute.
        self.maintenance = DailyMaintenance(db, settings.trades_retention_days, now_ms=lambda: self.clock.now_ms(),
                                            engine=we,
                                            beat=lambda: self.supervisor.beat("maintenance"),
                                            stopping=lambda: self.supervisor.stopping)
        self.started = time.monotonic()
        self.stall_watch: StallWatch | None = None  # set by run()
        self._deploy_stop = False  # set when the stop file asked for the shutdown
        self.clock = ExchangeClock()
        self.feed = FeedState(self.clock)
        self.horizons = tuple(h.seconds for h in settings.horizons)
        self.rulesets = RulesetService(self.store, settings.max_active_per_horizon)
        self.settlement = SettlementService(self.store, self.writer, settings.tie_policy, settings.decision_lag_ms)
        self.engine = Engine(self.writer, self.rulesets, self.settlement, self.horizons,
                             flags=self.flags, rv_p95=self.rv_p95)
        self.kalshi = KalshiClient(series_ticker=settings.kalshi_series)
        self.kalshi_sync = KalshiSync(self.kalshi, self.writer, self.settlement)
        # Read-only account link: the Kalshi balance (cash and positions value) for Play. None unless a key id and
        # key file are configured.
        self.portfolio_gw = portfolio_from_settings(settings.kalshi_key_id, settings.kalshi_private_key_path,
                                                    base_url=settings.kalshi_base_url)
        # The recent per-second index, read incrementally: the Coach's tick and series, Play's fair odds and chart.
        self.index_tail = IndexTail(self.store)
        self.coach = CoachService(self.writer, self.index_tail, self.horizons)
        s = settings
        # The gateways' own notional ceiling on one buy: a bug guard only (a wrong count or price can never send an
        # absurd order), not a trading limit. The real bounds are the phone's user-set PlayLimits and, in production,
        # the venue's fresh cash (cash_backstop).
        gw_cap = s.trading_order_hard_ceiling
        # The phone strategies' per-trade / per-day limits: user-set in the app DB, default none / none, read at check
        # time by every phone service (application/play_limits.py).
        self.play_limits = PlayLimits(self.store)
        # KD7: ONE daily total across every phone service's stakes and the perps collateral (the user's day). Every service's
        # per-day check reads it (play_limits.CommittedToday); the sources are added once the services exist.
        self.committed_today = CommittedToday()
        # The user's day (domain/days.py): ONE zone for every "today" (the day limit, Play's day P&L, Record, the
        # agents' global limits, the daily summary), from the process's TZ (compose sets it; unset = UTC). A name the
        # zone database does not know stops the start here, never a silent other zone.
        self.day_tz = days.zone(os.environ.get("TZ"))
        log.info("days run midnight to midnight %s (TZ)", days.zone_name(self.day_tz))
        self.shadow_ex = ShadowExchange()  # shared by the shadow gateway and the shadow stream
        # One live feed (WebSocket primary) per environment any phone service is in, shared by them all.
        self.trading_streams: dict[str, object] = {}
        self.balance: tuple[float | None, int | None] = (None, None)  # Kalshi balance $, read at (ms)
        self.kalshi_positions_value: float | None = None  # Kalshi portfolio_value $ from the same balance reading
        self.market_stream = None  # production order-book stream for the Coach's live Kalshi prices
        self.market_quotes: dict[str, tuple[dict, int]] = {}  # ticker -> (stream quote, received at)
        # Staging (BTCFLOW_STAGING): no production gateway is built at all, whatever else is set (the settings refuse
        # production credentials there too, bootstrap/config.py), so production is never "configured": no mode
        # switch, agent session or reconcile can reach it.
        self.trading_gws = {
            "demo": trading_from_settings("demo", s.trading_demo_key_id, s.trading_demo_key_path, max_order_cost=gw_cap),
            "prod": None if s.staging else trading_from_settings("prod", s.trading_prod_key_id,
                                                                  s.trading_prod_key_path, max_order_cost=gw_cap),
            "shadow": shadow_trading(max_order_cost=gw_cap, exchange=self.shadow_ex)}
        if s.staging:
            log.warning("STAGING (BTCFLOW_STAGING): no production trading is wired; shadow and Kalshi demo only")
        # The synchronous store, not the buffered writer: a bracket must be on disk before its order is sent.
        trading_config = TradingConfig(
            enabled=s.trading_enabled, max_open_positions=s.trading_max_open_positions,
            stop_slippage=s.trading_stop_slippage, approval_ttl_s=s.trading_approval_ttl_s,
            presence_timeout_s=s.trading_presence_timeout_s, fee_rate=s.kalshi_fee_rate, series=s.kalshi_series)
        self.trading_services: dict[str, TradingService] = {}
        self.leans: dict[int, dict] = {}
        self.kalshi_live: dict | None = None
        self._hydrated_max_id = 0
        self._rv_p95: tuple[int, float | None] = (0, None)
        # Store reads the event loop must not wait for (docs/plans/2026-10-31-1800-review-fix-plan.md P0 #5): the
        # decision loop's flags, computed in a worker thread before each boundary, and the BTC price a minute ago
        # behind the crowd line (refreshed off the loop at most once a second).
        self._flags_at: tuple[int, frozenset[str]] | None = None
        self._btc_ref: tuple[int, float | None] = (0, None)
        self._btc_ref_busy = False
        # Play's Kalshi "crowd, last minute" signal: the production market stream's public trade prints.
        self.prints = TradePrints(clock_ms=self.clock.now_ms)
        # "you" (the Kalshi Play tab): the user's own calls. Every entry and exit is a human Yes on a server-prepared
        # ticket; the shared kill switch, the Coach pause, the user's limits and the index stop apply.
        self.you_trading = TradingService(
            self.store, self.trading_gws, trading_config, strategy=YOU, paused_fn=lambda: self.coach.paused,
            now=self.clock.now_ms(), index_price_fn=self.live_price, balance_fn=lambda: self.balance,
            known_order_ids_fn=functools.partial(self._other_order_ids, YOU), limits_fn=self.play_limits.for_trading,
            market_band_fn=self.play_limits.market_band, committed_today_fn=self.committed_today.total,
            cash_backstop=True, **self._agent_hooks())
        self.trading_services[YOU] = self.you_trading
        # Every Play tab's service, by market key in table order (None: that venue is not wired on this server).
        self.phone_trading: dict[str, TradingService | None] = {m.key: None for m in phone_markets()}
        self.phone_trading["kalshi_15m"] = self.you_trading
        # Polymarket US: only with Polymarket keys (the markets WebSocket needs them). Shadow fills on Polymarket's
        # live book, or production; no sandbox.
        self.pm_client = None
        self.pm_public = None
        self.pm_data: PolymarketMarketData | None = None
        self.pm_market_stream: PolymarketMarketStream | None = None
        self.pm_gw: PolymarketTrading | None = None
        self.pm_terms: MarketTerms | None = None
        self.pm_shadow_ex = None
        self.you_poly_trading: TradingService | None = None  # phone_trading["polymarket_15m"]
        self.you_poly5_trading: TradingService | None = None  # phone_trading["polymarket_5m"]
        if s.polymarket_key_id and s.polymarket_secret_key is not None and not s.staging:  # staging: no pm_prod
            self._build_polymarket(trading_config)
        # The Play tabs, keyed by the table's play keys ("kalshi", "polymarket", "polymarket_5m").
        venues = {market("kalshi_15m").play_key: PlayVenue(
            self.you_trading, quote_fn=self._play_kalshi_quote, crowd_fn=self.crowd,
            balance_fn=lambda: {"cash": self.balance[0], "at": self.balance[1],
                                "positions": self.kalshi_positions_value})}
        venues.update({m.play_key: self._pm_play_venue(m) for m in phone_markets("polymarket")})
        self.play = PlayService(
            venues,
            evaluation_fn=lambda: self.coach.evaluation, index_fn=self.live_price, index_series_fn=self.index_prices)
        self.play.day_tz = self.day_tz
        for svc in self.trading_services.values():
            svc.day_tz = self.day_tz
            self.committed_today.add_source(svc.committed_by_env)
        self._build_perps()
        self._build_agent()
        self._build_copilot()
        self._build_telegram()
        self._warn_unwatched_brackets()

    # -- Kalshi perps ---------------------------------------------------------------------------------------------------
    def _build_perps(self) -> None:
        """The Perps tab (plan U8). Off (`perps_enabled` false): no margin client, stream or loop; the view says so.
        On: one MarginClient + signer + MarginStream per env whose key is configured (perp_prod also needs the
        production master switch `trading_enabled`), one PerpTradingService (strategy "perps", envs perp_demo /
        perp_prod, kept OUT of `trading_services` so the window services' stream pump never sees a perp event), and
        its collateral added to the one daily total. Nothing talks to Kalshi until `perps_loop` runs."""
        s = self.settings
        self.perps: PerpTradingService | None = None
        self.perps_publisher: PerpPublisher | None = None  # set below, once the view exists
        self.perp_clients: dict[str, MarginClient] = {}
        self.perp_streams: dict[str, MarginStream] = {}
        self.perps_hub = PushHub()
        self.play_streams = TickStreams()  # Play's live view, one stream per open screen (GET /api/play/stream)
        self.perps_inflight = Inflight()
        self.perp_events: asyncio.Queue = asyncio.Queue()  # fills, orders and resyncs for the service, in order
        self._perp_stream: tuple[str | None, asyncio.Task | None] = (None, None)
        warn = {"band": s.perps_protective_band, "amber": s.perps_warn_amber, "red": s.perps_warn_red}
        if not s.perps_enabled:
            self.perp_view = PerpView(None, settings=warn)
            self.perps_publisher = PerpPublisher(self.perp_view, self.perps_hub, self.clock.now_ms)
            log.info("perps are switched off (BTCFLOW_PERPS_ENABLED)")
            return
        keys = {"perp_demo": (s.trading_demo_key_id, s.trading_demo_key_path),
                "perp_prod": (s.trading_prod_key_id, s.trading_prod_key_path)}
        unavailable: dict[str, str] = {}
        spec = perp("kalshi_btc_perp")
        for env in PERP_ENVS:
            if env == "perp_prod" and (s.staging or not s.trading_enabled):
                unavailable[env] = ("This is a staging server: no production trading (BTCFLOW_STAGING)." if s.staging
                                    else "Production trading is turned off on this server (BTCFLOW_TRADING_ENABLED).")
                continue
            client, auth, why = margin_from_settings(env, *keys[env], subaccount=s.perps_subaccount,
                                                     max_notional=s.perps_order_hard_ceiling)
            if client is None:
                unavailable[env] = why
                log.info("perps %s unavailable: %s", env, why)
                continue
            self.perp_clients[env] = client
            self.perp_streams[env] = MarginStream(env, spec, auth, self._on_perps_event)
        config = PerpConfig(prod_enabled=s.trading_enabled, approval_ttl_s=s.trading_approval_ttl_s,
                            presence_timeout_s=s.trading_presence_timeout_s, default_band=s.perps_protective_band,
                            amber=s.perps_warn_amber, red=s.perps_warn_red)
        # notify_fn republishes the view: every service state change writes an audit row.
        self.perps = PerpTradingService(
            self.store, dict(self.perp_clients), config, spec=spec,
            quotes={env: st.quotes for env, st in self.perp_streams.items()},
            limits_fn=self.play_limits.for_trading, committed_today_fn=self.committed_today.total,
            notify_fn=self._perps_changed, access_check=perps_access_check,
            now=self.clock.now_ms())
        self.perps.day_tz = self.day_tz
        self.committed_today.add_source(self.perps.committed_by_env)
        self.perp_view = PerpView(self.perps, settings=warn, modes_unavailable=unavailable)
        self.perps_publisher = PerpPublisher(self.perp_view, self.perps_hub, self.clock.now_ms)

    # -- Ask Settleview (Play's Manual-level copilot) --------------------------------------------------------------------
    def _build_copilot(self) -> None:
        """Ask Settleview's lights, line and warnings (application/copilot.py): always wired; the screen asks only
        while its Play setting is on. The rule statuses are the agent sessions' own RuleBook when agent mode is on (one
        cached copy of the stored statuses), else a RuleBook of its own over the same setting."""
        book = self.agent_sessions.rule_book if self.agent_sessions is not None else RuleBook(self.store)
        modes = {m.phone_strategy: svc.modes for m in phone_markets()
                 if (svc := self.phone_trading.get(m.key)) is not None}
        self.copilot = Copilot(self.play, book, YourRecord(self.store, modes, now_fn=self.clock.now_ms))

    # -- Agent mode -----------------------------------------------------------------------------------------------------
    def _agent_hooks(self) -> dict:
        """The phone services' agent gate step and takeover hook (only with agent mode on; without them every agent
        order is refused). Late-bound: the sessions are built after the services they control."""
        if not self.settings.agent_enabled:
            return {}
        return {"agent_gate_fn": self._agent_check, "agent_takeover_fn": self._agent_takeover}

    def _agent_check(self, strategy: str, ticket: dict, now: int, exit_order: bool) -> GateResult:
        if getattr(self, "agent_sessions", None) is None:
            return GateResult(False, "no_agent_session", "Agent sessions are not running in this app; nothing was "
                                                         "sent.")
        return self.agent_sessions.check(strategy, ticket, now, exit_order)

    def _agent_takeover(self, strategy: str, bracket: dict, now: int) -> None:
        if getattr(self, "agent_sessions", None) is not None:
            self.agent_sessions.takeover(strategy, bracket, now)

    def _build_agent(self) -> None:
        """Agent mode (U3-U7), only with `agent_enabled`: sessions over the phone services (production refused until
        the paper hour is reviewed), the tool registry, the live feed, and the in-app runner when an Anthropic key is
        set (else in-app launches say "no key"). The runner's model loop is the Claude Agent SDK harness: one sandboxed
        Claude Code CLI process per wake (`agent_cli_path`, else the SDK's bundled CLI, else `claude` on PATH), its
        private files under the database's directory; the key check at launch stays on the plain Anthropic client.
        A session left live by the last run is ended "restart" here."""
        s = self.settings
        self.agent_sessions: AgentSessions | None = None
        self.agent_fleet: AgentFleet | None = None
        self.agent_feed: FleetFeeds | None = None
        self.agent_key: KeyState | None = None
        self.agent_proposals: AgentProposals | None = None
        if not s.agent_enabled:
            return
        now = self.clock.now_ms()
        self.agent_prompts = AgentPrompts(self.store, AGENT_DEFAULT_PROMPT.read_text(encoding="utf-8"))
        self.agent_prompts.seed(now)
        self.agent_hub = FeedHub(trimmed=_feed_trimmed)
        # Decision rows (record_decision) when the tool registry records them (feature-detected).
        decisions = (lambda sid, after_id=0: self.agent_tools.decisions(sid, after_id=after_id))             if hasattr(AgentTools, "decisions") else None
        # Multi-agent U5: the all-sessions feed (its state: the latest session, deprecated) and one feed per session
        # whose stream is open (FleetFeeds)
        main = AgUiFeed(self.agent_hub, self._agent_state, decisions_fn=decisions, now_fn=self.clock.now_ms)
        self.agent_feed = feed = FleetFeeds(
            main, lambda sid: AgUiFeed(FeedHub(trimmed=_feed_trimmed), lambda: self._agent_session_state(sid),
                                       decisions_fn=decisions, now_fn=self.clock.now_ms),
            live_fn=lambda sid: self.agent_sessions.get(sid) is not None)
        self.agent_sessions = AgentSessions(self.store, dict(self.trading_services),
                                            allow_production=s.agent_allow_production,
                                            notify_fn=self._agent_session_event, overlap_fn=self._agent_overlap)
        self.agent_sessions.restore(now)
        self.agent_sessions.rule_book.seed(now)  # HPANIC retired by the user (2026-10-30), persisted once
        # Assist proposals (the user approves or skips a rule's ticket); open ones left by the last run expire here.
        self.agent_proposals = AgentProposals(self.agent_sessions, self.play, notify_fn=self._agent_proposal_event)
        self.agent_proposals.restore(now)
        self.agent_tools = AgentTools(self.agent_sessions, self.play,
                                      events_fn=lambda sid: self.agent_fleet.events(sid) if self.agent_fleet else None,
                                      odds_fn=self._agent_odds)
        key = s.anthropic_api_key.get_secret_value() if s.anthropic_api_key is not None else ""
        self.agent_llm = AnthropicLlm(key) if key.strip() else None
        if self.agent_llm is not None:
            # The SDK runs `claude -v` before every spawn unless this is in the app's own environment; the CLI's
            # version is checked on each wake's init report instead (harness.MIN_CLI).
            os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"
            # one CLI process per wake: at most the fleet's wake cap at once, each with its own config dir
            self.agent_harness = SdkHarness(key, Path(s.db_path).parent, cli_path=s.agent_cli_path,
                                            client_app=f"settleview/{APP_VERSION}",
                                            concurrency=s.agent_wake_concurrency, pass_env=s.agent_env_pass)

            # Multi-agent U3/U4: one fleet (one tick loop, one wake queue, one shared signal board) and a runner per
            # in-app session, made at its launch
            board = SignalBoard(EventsConfig().signal_hold_ms)
            tools = ObservedTools(self.agent_tools, feed)

            harness = self.agent_harness

            def build_info() -> dict:
                # the wake fingerprint's build parts (agent_runner, "agent_wake_inputs"): the image's commit
                # (SETTLEVIEW_GIT_SHA, from the Dockerfile's GIT_SHA build arg), the SDK and the CLI it last reported
                return {"git_sha": (os.environ.get("SETTLEVIEW_GIT_SHA") or "").strip() or "unknown",
                        "sdk": f"claude-agent-sdk {AGENT_SDK_VERSION}",
                        "cli_version": getattr(harness, "cli_version", None) or "unknown"}

            def scrub_input(text: str) -> str:
                return mask(text)  # the settings' exact secret values (bootstrap/logs.py mask_secrets)

            def make_runner() -> AgentRunner:
                box: dict = {}
                box["r"] = AgentRunner(
                    self.agent_sessions, tools, self.play, self.agent_harness,
                    prompts_fn=self.agent_prompts.prompts_fn(lambda: self.agent_sessions.get(box["r"].session_id)),
                    clock=self.clock, notify_fn=wake_hook(self.agent_sessions, feed), live_fn=feed.live,
                    start_fn=wake_start_hook(self.agent_sessions, feed), proposals=self.agent_proposals,
                    signal_board=board, scrub_fn=scrub_input, build_fn=build_info)
                return box["r"]
            self.agent_fleet = AgentFleet(self.agent_sessions, self.play, make_runner, clock=self.clock,
                                          cap=s.agent_wake_concurrency, proposals=self.agent_proposals,
                                          tools=self.agent_tools)
        else:
            log.info("agent mode: no Anthropic key (BTCFLOW_ANTHROPIC_API_KEY); in-app agent sessions are unavailable")

    def _agent_odds(self, key: str, now: int) -> dict | None:
        """get_odds: the Coach's recorded series for the current Kalshi 15m window; no other market records odds."""
        if key != "kalshi_15m":
            return None
        w = market("kalshi_15m").window_ms
        return self.coach.series(now - now % w, now)

    def _agent_state(self) -> dict:
        """The all-sessions feed's state (DEPRECATED single-session panel): the latest session's view, its positions,
        prompt versions, and how many sessions are live."""
        now = self.clock.now_ms()
        view = self.agent_sessions.latest_view(now)
        positions = []
        if view is not None:
            positions = [position_view(b)  # exact entry cost, fees and realized net per position
                         for b in self.store.select(BRACKETS, {"session_id": view["id"]}, order_by=["created_ts"])]
        return {"session": view, "positions": positions, "prompts": self.agent_prompts.versions(),
                "live_count": len(self.agent_sessions.live())}

    def _agent_session_state(self, sid: str) -> dict:
        """One session's panel state (its own stream and status): its view, positions, prompt versions, wake queue."""
        now = self.clock.now_ms()
        view = self.agent_sessions.view(sid, now)
        positions = [position_view(b) for b in self.store.select(BRACKETS, {"session_id": sid},
                                                                 order_by=["created_ts"])]
        wakes = self.agent_fleet.metrics(sid, now) if self.agent_fleet is not None else None
        return {"session": view, "positions": positions, "prompts": self.agent_prompts.versions(), "wakes": wakes}

    def _agent_overlap(self, w: dict) -> None:
        """KD2: two agents hold opposite sides of one window: CUSTOM "overlap_warning" on the feeds, and Telegram."""
        for sid in sorted({x["session_id"] for side in ("UP", "DOWN") for x in w["sides"][side]}):
            self.agent_feed.custom("overlap_warning", {**w, "session_id": sid})
        remote = getattr(self, "remote", None)
        if remote is not None:
            remote.on_overlap(w)

    def _agent_daily(self, now: int) -> dict:
        """The one daily total (KD7) the agent shares with you: the per-day limit and what is used in each pool."""
        used = {}
        for svc in self.trading_services.values():
            env = svc.modes[svc.mode]
            used[day_pool(env)] = self.committed_today.total(env, now)
        return {"per_day": self.play_limits.current().per_day, "used": used}

    def _agent_proposal_event(self, name: str, view: dict) -> None:
        """An assist proposal shown or answered: CUSTOM proposal_new / proposal_resolved on the agent feed (and the
        Telegram remote's proposal alert)."""
        self.agent_feed.custom(name, view)
        self.agent_feed.refresh()
        remote = getattr(self, "remote", None)
        if remote is not None:
            remote.on_proposal(name, view)

    def _agent_session_event(self, row: dict | None = None) -> None:
        """A session's state changed: the agent feed, and the Telegram remote's pause / end alerts."""
        self.agent_feed.session_changed(row)
        remote = getattr(self, "remote", None)
        if remote is not None:
            remote.on_session(row)

    # -- Telegram remote (U11) ------------------------------------------------------------------------------------------
    def _build_telegram(self) -> None:
        """The Telegram remote (application/notifier.py) and the saved launch presets. The remote always exists (its
        status route says configured: false without a token); the bot (adapters/telegram/bot.py, long polling) runs
        only with BTCFLOW_TELEGRAM_BOT_TOKEN set. Presets and the agent commands exist only with agent mode on; the
        launch path (agent_routes.AgentActions) is attached when the routes are registered."""
        self.agent_actions = None
        self.agent_presets: AgentPresets | None = None
        if self.agent_sessions is not None:
            self.agent_presets = AgentPresets(self.store, self._preset_validate, self._venue_modes,
                                              target_fn=self.agent_sessions.preset_target)
        tok = self.settings.telegram_bot_token
        token = tok.get_secret_value().strip() if tok is not None else ""
        # The research recorders run as their own processes / services; the app only reads their newest rows
        # (read-only) for the data alert, the daily coverage line and /api/health's `recorders`.
        research = ResearchDb.for_target(self.settings.database, self.settings.research_path)
        self.recorder_watch = RecorderWatch(research, db_label=research.label)
        if not research.available():
            log.info("recorders: no research data at %s (the data alert says so once)", research.where)
        self.remote = RemoteControl(self.store, now_fn=self.clock.now_ms, sessions=self.agent_sessions,
                                    proposals=self.agent_proposals, presets=self.agent_presets,
                                    lights_fn=self._telegram_lights, settle_fn=self._telegram_settle_warnings,
                                    view_fn=self._telegram_view, configured=bool(token),
                                    recorders=self.recorder_watch,
                                    bot_username=self.settings.telegram_bot_username, tz=self.day_tz)
        if self.agent_sessions is not None:  # "today" for global limits and the fleet = Record's day
            self.agent_sessions.day_tz = self.day_tz
        self.telegram_bot: TelegramBot | None = TelegramBot(token, self.remote) if token else None
        if self.telegram_bot is None:
            log.info("telegram: no bot token (BTCFLOW_TELEGRAM_BOT_TOKEN); the bot is off")

    def _preset_validate(self, spec: dict, now: int) -> dict | None:
        if self.agent_actions is None:
            return {"ok": False, "code": "agent_unavailable", "reason": "Agent mode is not running in this app."}
        return self.agent_actions.validate(spec, now)

    def _venue_modes(self) -> dict:
        """Each agent venue's trading mode now (None: not in this app)."""
        return {k: getattr(self.trading_services.get(strategy), "mode", None) for k, strategy in VENUES.items()}

    def _telegram_lights(self, now: int) -> list[dict]:
        """Ask Settleview's setup lights on every Play market wired here (for /lights and the setups alert)."""
        out = []
        for m in phone_markets():
            if self.play.venues.get(m.play_key) is None:
                continue
            v = self.copilot.view(now, m.venue, m.span, mode="full", warnings=())
            if not v.get("available"):
                continue
            out.append({"market": m.key, "market_label": m.label, "window_end": v.get("window_end"),
                        "lights": [{"rule": x["rule"], "state": x["state"], "line": x.get("line"),
                                    "side": x.get("side"), "ask": x.get("ask")}
                                   for x in v.get("lights") or []]})
        return out

    def _telegram_view(self, key: str, now: int) -> dict | None:
        """One Play market's BTC, strike and window end now (the remote's heads-ups and /status); None when the
        market is not wired here or not available."""
        m = market(key)
        if self.play.venues.get(m.play_key) is None:
            return None
        v = self.play.now_view(now, m.venue, span=m.span)
        if not v.get("available"):
            return None
        b, w = v.get("btc") or {}, v.get("window") or {}
        return {"btc": b.get("btc"), "strike": b.get("strike"), "window_end": w.get("end")}

    def _telegram_settle_warnings(self, now: int) -> list[dict]:
        """Every Play venue's positions settling long after the close (shown, never closed)."""
        out, seen = [], set()
        for k, strategy in VENUES.items():
            fn = getattr(self.trading_services.get(strategy), "settle_warnings", None)
            if fn is not None and strategy not in seen:
                seen.add(strategy)
                out += [{**w, "venue": k, "venue_label": VENUE_LABELS[k]} for w in fn(now)]
        return out

    async def agent_review_loop(self) -> None:
        while True:
            self.agent_sessions.review(self.clock.now_ms())  # every live session (nothing to do with none)
            # open proposals expire (also once their session ended); missed ones are scored when the result is in
            self.agent_proposals.expire(self.clock.now_ms())
            try:
                await self.agent_proposals.settle_due(self.clock.now_ms())
            except Exception:
                log.warning("scoring missed proposals failed", exc_info=True)
            self.supervisor.beat("agent_review")
            await asyncio.sleep(AGENT_REVIEW_S)

    def _on_control(self) -> None:
        """After the shared kill / resume / heartbeat routes: the Perps view republishes; an agent session pauses at
        once on the kill switch (an async route, on the event loop). From a sync route (FastAPI's threadpool: resume,
        heartbeat) the session is left to the 1 s review loop, so it is only ever changed on the event loop."""
        self.perps_publisher.notify()
        if self.agent_sessions is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.agent_sessions.review(self.clock.now_ms())

    def _start_agent_task(self, name: str, factory):
        return self.supervisor.start(name, factory)

    async def _agent_shutdown(self) -> None:
        """Stop every live in-app session (an order in flight finishes and is recorded first; an external one ends at
        the next start, as before); close the panel streams."""
        ids = [x["id"] for x in self.agent_sessions.live() if x["kind"] == "in_app"]
        if self.agent_fleet is not None and ids:
            now = self.clock.now_ms()
            try:
                await asyncio.wait_for(asyncio.gather(*(self.agent_fleet.stop(sid, now, "restart",
                                                                              "The app is shutting down.")
                                                        for sid in ids)), AGENT_STOP_S)
            except Exception:
                log.error("stopping the agent sessions at shutdown failed", exc_info=True)
        await self.agent_feed.close()

    def _perps_changed(self, _row: dict | None = None) -> None:
        if self.perps_publisher is not None:
            self.perps_publisher.notify()

    async def _close_push_on_exit(self, server) -> None:
        """uvicorn waits for open responses before it stops: end the Perps screen's never-ending streams as soon as it
        is asked to exit (Ctrl+C), so the shutdown is not held open by a browser tab."""
        while not server.should_exit:  # noqa: ASYNC110 - uvicorn's should_exit is a plain flag, there is no event to await
            await asyncio.sleep(0.2)
        await self.perps_hub.close()
        await self.play_streams.close()
        if self.agent_feed is not None:
            await self.agent_feed.close()

    def make_servers(self, app) -> list:
        """The web servers for `app`: the main one (BTCFLOW_API_HOST:BTCFLOW_API_PORT); with BTCFLOW_PC_PORT set, the
        PC listener on BTCFLOW_PC_HOST:BTCFLOW_PC_PORT (the only place a browser pairs as the PC and its PC cookie
        counts); with BTCFLOW_AGENT_PORT set, the agent listener on BTCFLOW_AGENT_HOST:BTCFLOW_AGENT_PORT (external
        agents' tool API only). Same app, same process; the access middleware tells them apart by the port a request
        arrived on. Only the main server runs the ASGI lifespan and owns the signal handlers: the others run with
        lifespan off and leave signals alone, and `serve_all` stops them together. uvicorn's `proxy_headers` is off
        on the PC and agent listeners always, and on the main one unless BTCFLOW_FORWARDED_ALLOW_IPS names the reverse
        proxy (the Tailscale sidecar): uvicorn's default would let a loopback peer rewrite the client address from
        X-Forwarded-For. With it, uvicorn's own ProxyHeadersMiddleware sets the scheme and client from that peer's
        headers only (adapters/http/access.py reads the result)."""
        s = self.settings
        proxies = s.forwarded_allow_ips.strip()
        servers = [uvicorn.Server(uvicorn.Config(app, host=s.api_host, port=s.api_port, log_level="warning",
                                                 proxy_headers=bool(proxies),
                                                 forwarded_allow_ips=proxies or None))]

        def extra(host: str, port: int):
            srv = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off",
                                                proxy_headers=False))
            # Two servers each swapping in their own SIGINT handler would leave Ctrl+C reaching only the last one and
            # re-raise it into the other on the way out: the main server alone takes the signals.
            srv.capture_signals = contextlib.nullcontext
            servers.append(srv)

        if s.pc_port is not None:
            extra(s.pc_bind_host, s.pc_port)
            log.info("PC listener: http://localhost:%d (a browser pairs there once: python -m btc_signal_engine.ops "
                     "pair-pc)", s.pc_port)
            if not is_loopback_host(s.pc_bind_host):
                log.info("PC listener bound to %s:%d (inside a container, for its 127.0.0.1 port publish): being the "
                         "PC there still needs a paired browser's cookie", s.pc_bind_host, s.pc_port)
        if s.agent_port is not None:
            extra(s.agent_bind_host, s.agent_port)
            log.info("agent listener: http://localhost:%d (external agents' tool API only; each call needs its "
                     "session's token)", s.agent_port)
        return servers

    def _handle_sigterm(self, loop: asyncio.AbstractEventLoop) -> Callable[[], None]:
        """SIGTERM (docker stop, a platform's scale-in, tini forwarding it) is a graceful stop, as the stop file: the
        writer flushes, agents and perps drain, the shutdown is logged. Without this handler Python's default action
        killed the process at once: uvicorn catches SIGTERM while it serves, then re-raises it after its own exit
        with the previous (default) handler restored (exit 143, nothing flushed). Returns the restore function."""
        if not hasattr(signal, "SIGTERM") or threading.current_thread() is not threading.main_thread():
            return lambda: None

        def stop() -> None:
            if not self.supervisor.stopping:
                log.info("SIGTERM: stopping gracefully")
            self.supervisor.request_stop()

        previous = signal.signal(signal.SIGTERM, lambda _sig, _frame: loop.call_soon_threadsafe(stop))
        return lambda: signal.signal(signal.SIGTERM, previous)

    async def serve_all(self, servers: list) -> bool:
        """Run every server until one stops (a signal reaches the main one; a listener that fails), then stop the
        rest and wait for them. True when a server was asked to exit (Ctrl+C), as one uvicorn server reported it."""
        tasks = [asyncio.ensure_future(s.serve()) for s in servers]
        # Each server's never-ending streams end as soon as it is asked to exit (as before, for every server).
        helpers = [asyncio.ensure_future(self._close_push_on_exit(s)) for s in servers]
        helpers.append(asyncio.ensure_future(self._stop_together(servers)))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for s in servers:  # one listener stopped (or failed): the others stop too
                s.should_exit = True
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for t in (*tasks, *helpers):
                t.cancel()
            await asyncio.gather(*tasks, *helpers, return_exceptions=True)
            # A cancelled uvicorn server skips its own shutdown, so its listening sockets stay bound (seen on Linux:
            # the supervisor's restart of the server would then fail to bind). Close them here; a no-op after a
            # normal exit, when uvicorn already closed them.
            for s in servers:
                for listener in getattr(s, "servers", None) or ():
                    listener.close()
        for r in results:
            if isinstance(r, BaseException):
                raise r
        return any(s.should_exit for s in servers)

    @staticmethod
    async def _stop_together(servers: list) -> None:
        """When any server is asked to exit, ask the others at once (so an open stream on one cannot hold the
        shutdown while the other is already stopping)."""
        while not any(s.should_exit for s in servers):  # noqa: ASYNC110 - plain uvicorn flags (no event to await)
            await asyncio.sleep(0.2)
        for s in servers:
            s.should_exit = True

    def _on_perps_event(self, ev: dict) -> None:
        """The margin stream's callback (on the loop; it must not block): fills, orders and resyncs queue for the
        perps service only; every event (book top, trade, health) also marks the view changed for the push channel."""
        if ev.get("type") in ("fill", "order", "resync"):
            self.perp_events.put_nowait(ev)
        self.perps_publisher.notify()

    async def _perps_apply(self, ev: dict) -> None:
        try:
            await self.perps.on_stream_event(ev, self.clock.now_ms())
        except Exception:
            log.warning("perps stream event failed (%s)", ev.get("type"), exc_info=True)
        self.perps_publisher.notify()

    async def _perps_stream_follow(self) -> None:
        """Run the margin stream of the service's mode (none while off); a mode switch stops the old one first."""
        env = self.perps.env
        if env == self._perp_stream[0]:
            return
        await self._perps_stream_stop()
        st = self.perp_streams.get(env) if env is not None else None
        self._perp_stream = (env, self.supervisor.start(f"perps_stream_{env}", st.run) if st is not None else None)
        if st is not None:
            log.info("perps stream started for %s", env)

    async def _perps_stream_stop(self) -> None:
        env, task = self._perp_stream
        self._perp_stream = (None, None)
        await _cancel_task(task, f"perps stream {env}")

    async def perps_loop(self) -> None:
        """Startup then monitor (plan U8; runs after the routes are registered): the margin access and subaccount
        check and a REST reconcile (the service is "ready" only after both), then the stream of the active mode,
        then, in order on this one task, stream fills / orders / resyncs as they arrive and the monitor tick every
        PERPS_TICK_S (status, positions, risk, triggers, funding, balance). Network reads are awaited, never run
        synchronously on the loop."""
        svc = self.perps
        loop = asyncio.get_running_loop()
        if svc.env is not None:
            await svc.reconcile(self.clock.now_ms())
            self.perps_publisher.notify()
        next_tick = loop.time() + PERPS_TICK_S
        try:
            while True:
                await self._perps_stream_follow()
                try:
                    ev = await asyncio.wait_for(self.perp_events.get(), max(0.0, min(next_tick - loop.time(), 0.5)))
                except TimeoutError:
                    ev = None
                if ev is not None:
                    await self._perps_apply(ev)
                if loop.time() >= next_tick:
                    while not self.perp_events.empty():  # queued fills / orders / resyncs first, not behind the tick
                        await self._perps_apply(self.perp_events.get_nowait())
                    try:
                        await svc.tick(self.clock.now_ms())
                    except Exception:
                        log.warning("perps monitor tick failed", exc_info=True)
                    self.perps_publisher.notify()
                    next_tick = loop.time() + PERPS_TICK_S
                self.supervisor.beat("perps")
        finally:
            await self._perps_stream_stop()

    def perps_health(self) -> dict:
        """/api/health's perps block: the feature flag, mode, readiness and the margin stream's state."""
        if self.perps is None:
            return {"enabled": False, "mode": None, "ready": False, "ready_reason": "Perps are switched off",
                    "stream": None}
        v = self.perps.readiness()
        env = self._perp_stream[0]
        st = self.perp_streams.get(env) if env is not None else None
        stream = None
        if st is not None:
            h = st.health()
            last = h.get("last_msg_ms")
            stream = {"venue": "kalshi", "env": env, "connected": bool(h["connected"]), "book_live": h["book_live"],
                      "reason": h["reason"], "last_msg_age_ms": None if last is None
                      else time.time_ns() // 1_000_000 - int(last)}
        return {"enabled": True, "mode": v["mode"], "ready": v["ready"], "ready_reason": v["ready_reason"],
                "stream": stream}

    def _warn_unwatched_brackets(self) -> None:
        """Nothing monitors a live bracket of a strategy this server no longer runs (the retired bots): its resting
        take profit stays at the exchange and its stop does not act. Never silent: each one is logged at start."""
        live = self.store.select("trading_brackets",
                                 filters=[("state", "in", ["sending", "unknown", "open", "stopping", "settling"])])
        for b in live:
            if b["strategy"] not in self.trading_services:
                log.warning("%s has a live bracket %s (%s %s, env %s, state %s) and nothing on this server monitors "
                            "it: close it on the venue", b["strategy"], b["id"], b["ticker"], b["side"], b["env"],
                            b["state"])

    # -- Polymarket US ----------------------------------------------------------
    def _build_polymarket(self, trading_config: TradingConfig) -> None:
        s, now_ms = self.settings, self.clock.now_ms
        self.pm_client = sdk_client(s.polymarket_key_id, s.polymarket_secret_key)
        self.pm_public = sdk_client(None, None)  # public market terms: no key needed
        # The market cache judges freshness (2 s) on the plain system clock: the exchange-corrected clock re-estimates
        # its offset from trade timestamps and can jump by seconds (after the 10:26 outage it did), which made fresh
        # quotes look stale and refused a live entry with "no live price" (2026-10-29 10:51).
        wall_ms = lambda: time.time_ns() // 1_000_000  # noqa: E731
        self.pm_data = PolymarketMarketData(clock_ms=wall_ms)
        self.pm_market_stream = PolymarketMarketStream(self.pm_client.ws.markets, self.pm_data, clock_ms=wall_ms)
        self.pm_terms = MarketTerms(self.pm_public.markets.retrieve_by_slug)
        self.pm_shadow_ex, pm_shadow_gw = polymarket_shadow(
            self.pm_data, max_order_cost=s.trading_order_hard_ceiling, clock_ms=now_ms,
            place_latency_ms=s.modes_shadow_place_latency_ms, cancel_latency_ms=s.modes_shadow_cancel_latency_ms,
            markets_fn=self.pm_public.markets.retrieve_by_slug)  # settlement: Polymarket's own resolution
        self.pm_gw = PolymarketTrading(self.pm_client, quote_fn=self.pm_data.quote, env=PM_PROD,
                                       max_order_cost=s.trading_order_hard_ceiling,
                                       coid_lookup=lambda c: self._pm_order_field("client_order_id", c, "order_id"),
                                       oid_lookup=lambda o: self._pm_order_field("order_id", o, "client_order_id"),
                                       clock_ms=now_ms, seed_fn=self._pm_bracket_positions,
                                       note_fn=self._pm_position_note,
                                       markets_fn=self.pm_public.markets.retrieve_by_slug)
        config = dataclasses.replace(trading_config, fee_rate=POLYMARKET_FEE_RATE, series=PM_SERIES,
                                     whole_contracts=True)
        # "you" on Polymarket US, one service per Play tab in the market table (you_poly: 15m, you_poly5: 5m): same
        # gateways, modes, gate, shared kill switch, index stop, manual-exit detection and connectivity guard. Each has
        # its own record, limits and positions in its own series and window; its entry cutoff is the table's. On the
        # very first start a tab follows the previous tab's mode (the first uses the service's safe default, shadow),
        # then it keeps its own.
        follow: TradingService | None = None
        for m in phone_markets("polymarket"):
            svc = TradingService(
                self.store, {PM_SHADOW: pm_shadow_gw, PM_PROD: self.pm_gw},
                dataclasses.replace(config, series=m.series, window_ms=m.window_ms), strategy=m.phone_strategy,
                paused_fn=lambda: self.coach.paused, now=now_ms(), index_price_fn=self.live_price,
                balance_fn=lambda: self.pm_gw.balance if self.pm_gw else (None, None),
                modes=PM_MODES, venue="Polymarket US", fee_fn=polymarket_fee,
                known_order_ids_fn=functools.partial(self._other_pm_order_ids, m.phone_strategy),
                default_mode=follow.mode if follow is not None else None, limits_fn=self.play_limits.for_trading,
                market_band_fn=self.play_limits.market_band, committed_today_fn=self.committed_today.total,
                cash_backstop=True, **self._agent_hooks())
            self.phone_trading[m.key] = self.trading_services[m.phone_strategy] = follow = svc
        self.you_poly_trading = self.phone_trading["polymarket_15m"]
        self.you_poly5_trading = self.phone_trading["polymarket_5m"]

    def _pm_bracket_positions(self) -> dict[str, float]:
        """Net YES contracts the live production Polymarket brackets hold, per market (seeds the gateway's position
        book after a restart)."""
        out: dict[str, float] = {}
        for b in self.store.select("trading_brackets", {"env": PM_PROD},
                                   filters=[("state", "in", ["sending", "unknown", "open", "stopping"]),
                                            ("strategy", "in", list(PM_LIVE))]):
            held = float(b.get("held") or 0.0)
            out[b["ticker"]] = out.get(b["ticker"], 0.0) + (held if b["side"] == "UP" else -held)
        return out

    def _pm_position_note(self, row: dict) -> None:
        """A position disagreement noticed by the Polymarket gateway, into the audit trail of the service with a live
        bracket in that market (the 15-minute tab's when none has). The gateway calls this on the event loop: the
        lookup and the audit write run in a worker thread there (the row is audit only; nothing waits for it)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._pm_position_note_now(row)
            return

        def done(f) -> None:
            if not f.cancelled() and f.exception() is not None:
                log.warning("recording a Polymarket position note failed", exc_info=f.exception())

        loop.run_in_executor(None, self._pm_position_note_now, row).add_done_callback(done)

    def _pm_position_note_now(self, row: dict) -> None:
        live = self.store.select("trading_brackets", {"ticker": row["slug"]},
                                 filters=[("state", "in", ["sending", "unknown", "open", "stopping"]),
                                          ("strategy", "in", list(PM_LIVE))], limit=1)
        self.store.insert("trading_audit", [{"ts": row["ts"], "event": "position_check", "env": row["env"], "actor": SYSTEM,
                                             "code": row["code"], "reason": row["reason"],
                                             "detail": {**row.get("detail", {}), "ticker": row["slug"]},
                                             "strategy": live[0]["strategy"] if live else
                                             market("polymarket_15m").phone_strategy}])

    def _pm_order_field(self, key: str, value: str, want: str) -> str | None:
        """The Polymarket order row's `want` for the row whose `key` is `value` (the gateway's id map after a
        restart)."""
        rows = self.store.select("trading_orders", {key: value}, filters=[("strategy", "in", list(PM_LIVE))],
                                 limit=1)
        return rows[0].get(want) if rows else None

    def index_prices(self, start: int, end: int) -> list[tuple[int, float]]:
        """Per-second index rows (ts, price) with start <= ts < end (the recent ones from the incremental cache)."""
        return [(r.ts, r.price) for r in self.index_tail.index_range(start, end)]

    def _index_minute_ago(self, now: int) -> float | None:
        rows = self.store.select("index_1s", filters=[("ts", ">=", now - 62_000)], order_by=["ts"], limit=1)
        return float(rows[0]["price"]) if rows else None

    def _btc_move_1m(self, now: int) -> float | None:
        live = self.live_price()
        if live is None:
            return None
        ref = self._btc_minute_ago(now)
        return live[1] - ref if ref is not None else None

    def _btc_minute_ago(self, now: int) -> float | None:
        """The index price about a minute before `now`. Off the loop (a web worker thread) it is read directly. On the
        event loop (the agent fleet's views) it never waits for the database: the last value read (at most 5 s old)
        is used and a fresh read is started in a worker thread when that value is over a second old."""
        at, ref = self._btc_ref
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if not 0 <= now - at < 1_000:
                ref = self._index_minute_ago(now)
                self._btc_ref = (now, ref)
            return ref
        if not 0 <= now - at < 1_000 and not self._btc_ref_busy:
            self._btc_ref_busy = True

            def done(f) -> None:  # on the loop thread
                self._btc_ref_busy = False
                if not f.cancelled() and f.exception() is None:
                    self._btc_ref = (now, f.result())

            loop.run_in_executor(None, self._index_minute_ago, now).add_done_callback(done)
        return ref if 0 <= now - at <= 5_000 else None

    def crowd(self) -> dict | None:
        """Crowd flow for the current KXBTC15M market over the last minute (descriptive; see domain/strategy/crowd.py)."""
        ticker = (self.kalshi_live or {}).get("ticker")
        if not ticker:
            return None
        now = self.clock.now_ms()
        out = crowd_summary(self.prints.recent_trades(ticker), now, btc_move=self._btc_move_1m(now))
        return {**out, "ticker": ticker}

    def pm_crowd(self, slug: str, now: int) -> dict | None:
        """The same summary from Polymarket US's public trade feed, or None when the feed cannot vouch for the whole
        last minute (not subscribed long enough, a disconnect, or a silent stream)."""
        if self.pm_data is None:
            return None
        prints = self.pm_data.trades_since(slug, time.time_ns() // 1_000_000 - 60_000)  # the cache's own clock
        if prints is None:
            return None
        return {**crowd_summary(prints, now, btc_move=self._btc_move_1m(now)), "ticker": slug}

    def live_price(self) -> tuple[int, float] | None:
        """Provisional live index (Play's BTC price, the index stops): the newest Coinbase trade the collector passed
        to on_event, as (exchange ts, price). None until the ExchangeClock has learned its offset (25 trades, as
        decision_loop waits for), since its age is measured against that clock."""
        t = self.feed.last_trade
        if t is None or len(self.clock._samples) < 25:
            return None
        return t.ts, t.price

    def _order_ids(self, strategies: list[str], ids: Iterable[str] | None = None) -> set[str]:
        """Order ids and client_order_ids these strategies placed on a shared account, so reconciliation never mistakes
        them for orders this app did not place. With `ids` (what the trading service is judging: resting orders,
        fills) only those of them are looked up, by the two indexed columns; never every order ever placed."""
        if not strategies:
            return set()
        if ids is None:
            return {x for o in self.store.select("trading_orders", filters=[("strategy", "in", strategies)])
                    for x in (o.get("order_id"), o.get("client_order_id")) if x}
        want = sorted({x for x in ids if x})
        if not want:
            return set()
        rows = [*self.store.select("trading_orders", filters=[("strategy", "in", strategies), ("order_id", "in", want)]),
                *self.store.select("trading_orders", filters=[("strategy", "in", strategies),
                                                              ("client_order_id", "in", want)])]
        return {x for o in rows for x in (o.get("order_id"), o.get("client_order_id")) if x in want}

    def _other_pm_order_ids(self, strategy: str, ids: Iterable[str] | None = None) -> set[str]:
        """Every other strategy's Polymarket order ids (the phone tabs and the retired pnw_poly share one account)."""
        return self._order_ids([s for s in PM_LIVE if s != strategy], ids)

    def _other_order_ids(self, strategy: str, ids: Iterable[str] | None = None) -> set[str]:
        """Every other strategy's Kalshi order ids (the retired taker and pnw_cushion share the account with "you")."""
        return self._order_ids([s for s in KALSHI_LIVE if s != strategy], ids)

    def _play_kalshi_quote(self, now: int) -> dict | None:
        """The Kalshi quote the Play screens use: the Coach's live quote (WebSocket book, REST fallback) for the
        current window; PlayService judges its age."""
        kl = self.kalshi_live
        if not kl or not kl.get("ticker"):
            return None
        return {k: kl.get(k) for k in ("ticker", "yes_bid", "yes_ask", "no_bid", "no_ask", "close_ts", "open_ts")} | {
            "age_ms": None if kl.get("ts") is None else now - int(kl["ts"])}

    def _play_pm_quote(self, now: int, span: str) -> dict | None:
        """Polymarket US's WebSocket top of book for the current `span` window (the cache judges its own age on its
        own clock and returns None when stale; never the REST BBO)."""
        if self.pm_data is None:
            return None
        return self.pm_data.quote(slug_for(now, span))

    def _play_pm_crowd(self, span: str) -> dict | None:
        now = self.clock.now_ms()
        return self.pm_crowd(slug_for(now, span), now)

    def _pm_play_venue(self, m: MarketSpec) -> PlayVenue:
        """One Polymarket Play tab. The tab on the Coach's 15-minute window uses the Coach's strike and odds and
        carries the account balance; any other span is its own window with Polymarket's own price to beat and our fair
        odds for it."""
        svc, span = self.phone_trading[m.key], m.span
        quote_fn, crowd_fn = (lambda now: self._play_pm_quote(now, span)), (lambda: self._play_pm_crowd(span))
        if m.window_ms == PLAY_WINDOW_MS:
            return PlayVenue(svc, quote_fn=quote_fn, crowd_fn=crowd_fn, yes_only=True,
                             balance_fn=lambda: ({"cash": self.pm_gw.balance[0], "at": self.pm_gw.balance[1],
                                                  "positions": self._pm_positions_value()}
                                                 if self.pm_gw is not None else None),
                             missing="No live Polymarket price (Polymarket US is not connected on this server).")
        return PlayVenue(svc, quote_fn=quote_fn, crowd_fn=crowd_fn, yes_only=True, window_ms=m.window_ms,
                         strike_fn=functools.partial(self._pm_span_strike, span=span), fair_fn=self._span_fair,
                         missing=f"Polymarket US {m.window_ms // 60_000}-minute windows are not available on this "
                                 "server.")

    def _pm_span_strike(self, start: int, span: str) -> float | None:
        """A non-15-minute window's price to beat: Polymarket's own priceToBeat (fetched by you_loop), never Kalshi's."""
        return self.pm_terms.cached_strike(slug_for(start, span)) if self.pm_terms else None

    def _span_fair(self, now: int, start: int, end: int, strike: float) -> dict | None:
        """The fair odds for a window that is not the Coach's (e.g. 5 minutes): the Coach's own random-walk model on
        the recorded per-second index, against that window's strike. fair_from_index's whole result (fair_up, sigma,
        at, window_end, mean_close, sd_close), so the Play cone uses the same sigma as the odds."""
        at = now - now % 1000
        return fair_from_index(at, self.index_prices(at - HISTORY_MS, at), strike, end)

    async def you_loop(self) -> None:
        """The "you", "you_poly" and "you_poly5" services' monitor (fills, the resting 99c sell, index stops, settlement) every 0.5 s. It never
        enters: entries come only from a human Yes."""
        while True:
            if self.pm_terms is not None:  # own-window tabs' price to beat (cached; re-asked at most every 10 s)
                for m in phone_markets("polymarket"):
                    if m.window_ms == PLAY_WINDOW_MS:
                        continue  # the 15-minute tab uses the Coach's strike
                    try:
                        await self.pm_terms.strike(slug_for(self.clock.now_ms(), m.span))
                    except Exception:
                        log.debug("%s strike read failed", m.span, exc_info=True)
            for svc in self.phone_trading.values():
                if svc is None:
                    continue
                try:
                    await svc.tick(self.clock.now_ms())
                except Exception:
                    log.warning("%s tick failed", svc.strategy, exc_info=True)
            self.supervisor.beat("you")
            await asyncio.sleep(YOU_TICK_S)

    async def trip_kill_switch(self, loop_name: str) -> None:
        """The supervisor's answer to a critical loop failing repeatedly: the same shared kill switch as the Kill
        button (/api/trading/kill), reaching every phone service; no new entries. Open positions keep their
        auto-sell and stop (the `you` monitor keeps running). It never places, amends or cancels an order."""
        now = self.clock.now_ms()
        for svc in self.trading_services.values():
            await svc.kill(now)
        if self.perps is not None:  # KTD16: after the event services; a failure there never stops them
            try:
                await self.perps.kill(now)
            except Exception as exc:
                self.perps.record_failure("kill", now, exc)
            self.perps_publisher.notify()

    # -- engine context -------------------------------------------------------
    def flags(self, t: int) -> frozenset[str]:
        """The engine's data-quality flags at boundary t: the value decision_loop computed off the loop for t, else
        read now."""
        cached = self._flags_at
        if cached is not None and cached[0] == t:
            return cached[1]
        return self._compute_flags(t)

    def _compute_flags(self, t: int) -> frozenset[str]:
        out = set()
        for g in self.store.select("gaps", {"repaired": False}):
            if g["start_ts"] <= t and (g["end_ts"] is None or g["end_ts"] >= t - 900_000):
                out.add("data_gap")
                break
        skew = self.store.select("data_quality", {"kind": "skew"}, order_by=["-ts"], limit=1)
        if skew and skew[0]["ts"] >= t - 120_000 and abs(skew[0]["value"]) > SKEW_LIMIT_MS:
            out.add("clock_skew")
        return frozenset(out)

    def rv_p95(self) -> float | None:
        """The cached value only: startup() computes it before any loop runs, then rv_p95_loop refreshes it hourly,
        off the loop (None only with too little history or when every scan so far failed)."""
        return self._rv_p95[1]

    def _compute_rv_p95(self, now: int) -> float | None:
        rows = self.store.select("snapshots", filters=[("ts", ">=", now - 7 * 86_400_000)])
        rvs = sorted(r["features"].get("rv_60") for r in rows if r["features"].get("rv_60") is not None)
        return rvs[int(0.95 * (len(rvs) - 1))] if len(rvs) >= 600 else None  # need >= 10h of history

    async def refresh_rv_p95(self) -> None:
        """Scan 7 days of snapshots in a worker thread (KTD1) and swap the cached value; a failed scan keeps the
        previous one."""
        now = self.clock.now_ms()
        try:
            value = await asyncio.to_thread(self._compute_rv_p95, now)
        except Exception:
            log.warning("rv_p95 refresh failed; keeping the previous value", exc_info=True)
            return
        self._rv_p95 = (now, value)

    async def rv_p95_loop(self) -> None:
        while True:  # startup() did the first refresh
            await asyncio.sleep(RV_P95_REFRESH_S)
            await self.refresh_rv_p95()
            self.supervisor.beat("rv_p95")

    # -- live event path ----------------------------------------------------------
    async def on_event(self, ev) -> None:
        self.supervisor.beat("collector")
        if isinstance(ev, Trade):
            self.clock.observe(ev.ts)
            self.feed.on_trade(ev)
            if ev.trade_id <= self._hydrated_max_id:
                return  # already fed to the engine during hydration
        self.engine.on_event(ev)

    async def startup(self) -> None:
        now = self.clock.now_ms()
        self.rulesets.load_seeds(read_seed_files(ROOT / "rulesets"), now)
        rest = CoinbaseRest()
        recent = await startup_read("the recent-trades probe", lambda: self.store.has_trades_since(now - LOOKBACK_MS))
        try:
            if not recent:
                log.info("no recent history: backfilling %.0fh of Coinbase trades for feature warm-up", LOOKBACK_MS / 3.6e6)
                await backfill(self.store, rest, days=LOOKBACK_MS / 86_400_000, now_ms=now)
            repaired = await repair_gaps(self.store, rest)
            if repaired:
                log.info("repaired %d data gaps", repaired)
            await backfill_kalshi(self.store, self.kalshi, max_pages=1)
        except Exception:  # network trouble must not stop live collection
            log.exception("startup backfill failed; continuing with what is stored")
        try:
            n = hydrate(self.engine, self.store, now)
        except Exception as exc:  # not retried in-process: the engine already holds the events read before the failure
            log.error("startup: hydrating the engine from stored history failed (%s); the start stops here and the "
                      "supervisor or container restart tries again", type(exc).__name__)
            raise
        self._hydrated_max_id = await startup_read("the newest trade id", self.store.max_trade_id) or 0
        log.info("engine hydrated from %d stored events", n)
        # Before any loop runs, so the first boundaries after a restart see rv_p95 as before it (skip_high_vol).
        await self.refresh_rv_p95()

    async def _settle_due(self, now: int) -> int:
        try:
            return await asyncio.to_thread(self.settlement.settle_due, now)
        except Exception as exc:
            log.warning("settling due windows failed (%s); next pass", type(exc).__name__)
            return 0

    async def decision_loop(self) -> None:
        # Wait for the clock to learn the exchange offset before the first decision.
        for _ in range(60):
            if self.feed.last_trade is not None and len(self.clock._samples) >= 25:
                break
            await asyncio.sleep(0.5)
        log.info("clock offset vs exchange: %+d ms", self.clock.offset_ms)
        last_done: int | None = None
        last_quote: int | None = None
        while True:
            now = self.clock.now_ms()
            # Settlement reads and writes only the store: off the loop (a database outage made each call a stall),
            # and a failed read waits for the next pass instead of restarting the decision loop.
            if await self._settle_due(now):
                await self.writer.flush()
            missed, decide = due_boundaries(last_done, now)
            for t in missed:
                self.engine.record_missed(t, now)
                last_done = t
            if decide is not None:
                try:
                    self._flags_at = (decide, await asyncio.to_thread(self._compute_flags, decide))
                except Exception:
                    log.warning("data-quality flags at %d could not be read", decide, exc_info=True)
                try:
                    rows = self.engine.on_boundary(decide, now)
                    log.info("boundary %d: %d decisions", decide, len(rows))
                except Exception:
                    log.exception("decision at %d failed", decide)
                last_done = decide
                if decide % 900_000 == 0:
                    last_quote = decide
            if last_quote is not None:
                try:
                    if await self.kalshi_sync.capture_quote(last_quote, now, self.fresh_market_quote):
                        last_quote = None
                except Exception:
                    log.warning("kalshi quote fetch failed", exc_info=True)
            await self._settle_due(now)
            self.update_leans(now)
            try:
                # A copy taken here, on the loop: the snapshot runs in a worker thread while new trades keep arriving.
                await self.coach.tick(now, self.engine.state.copy().snapshot, self.leans, self.kalshi_live)
            except Exception:
                log.warning("coach tick failed", exc_info=True)
            self.supervisor.beat("decision")
            await asyncio.sleep(0.5)

    def update_leans(self, now: int) -> None:
        at = now - now % 1000
        for h in self.horizons:
            champion = self.rulesets.champion(h)
            if not champion:
                continue
            try:
                self.leans[h] = compute_lean(self.engine.state, self.rulesets.get(champion)["spec"], h, at)
            except Exception:
                log.debug("lean for %d failed", h, exc_info=True)

    def _on_market_event(self, ev: dict) -> None:
        try:  # Play's crowd prints; never blocks the Coach's quote
            self.prints.on_event(ev)
        except Exception:
            log.warning("trade print event failed", exc_info=True)
        if ev.get("type") == "quote" and ev.get("ticker"):
            self.market_quotes[ev["ticker"]] = (ev, self.clock.now_ms())
            if len(self.market_quotes) > 8:
                self.market_quotes.pop(next(iter(self.market_quotes)))

    def fresh_market_quote(self, ticker: str, now: int) -> dict | None:
        """The stream's quote for `ticker` while the stream is healthy (the book is seq-checked and re-sent every
        second). Its `ts` is when data last arrived from Kalshi, not now: the 1 s requote re-stores the same book, so
        the stored time alone would hide a frozen socket. None (-> REST order book) when the stream is down, stale
        (including 12 s of silence) or has not re-sent this market's book."""
        st = self.market_stream
        if st is None:
            return None
        h = st.health()
        got = self.market_quotes.get(ticker)
        if not h["connected"] or h["stale"] or got is None or now - got[1] > 2_000:
            return None
        return {**got[0], "ts": min(got[1], h.get("last_msg_ms") or got[1])}

    async def kalshi_live_loop(self) -> None:
        """The Coach's live Kalshi quote for the current window: WebSocket order book first, REST order book as the
        fallback; the /markets list only discovers which market is current."""
        s = self.settings
        key = ((s.kalshi_key_id, s.kalshi_private_key_path) if s.kalshi_key_id and s.kalshi_private_key_path
               else (s.trading_prod_key_id, s.trading_prod_key_path))  # read-only key preferred: least privilege
        self.market_stream = (market_stream_from_settings(*key, self._on_market_event, clock_ms=self.clock.now_ms,
                                                          trades=True)
                              if s.trading_stream else None)
        task = (self.supervisor.start("kalshi_market_stream", self.market_stream.run)
                if self.market_stream is not None else None)
        warned_at: int | None = None
        failures = 0  # since the last warning
        try:
            while True:
                now = self.clock.now_ms()
                try:
                    m = await self.kalshi_sync.current_market(now)
                    if self.market_stream is not None:
                        self.market_stream.subscribe_tickers({m["ticker"]} if m else set())
                    self.kalshi_live = await self.kalshi_sync.poll_live(now, self.fresh_market_quote)
                except Exception as e:
                    failures += 1
                    if warned_at is None or now - warned_at >= KALSHI_POLL_WARN_MS:
                        log.warning("kalshi live poll failed: %s: %s (%d failure(s) since the last warning)",
                                    type(e).__name__, e, failures)
                        warned_at, failures = now, 0
                    else:
                        log.debug("kalshi live poll failed", exc_info=True)
                self.supervisor.beat("kalshi_live")
                await asyncio.sleep(KALSHI_LIVE_POLL_S)
        finally:
            if self.market_stream is not None:
                self.market_stream.close()
            if task is not None:
                task.cancel()

    def _pm_positions_value(self) -> float | None:
        """Polymarket US positions marked at what they would sell for now (YES at the bid, NO at 1 - ask), from the
        gateway's last positions read (under 5 min old) and the live WebSocket book. None when either is unknown;
        Polymarket's balance call does not report a positions value itself."""
        gw, data = self.pm_gw, self.pm_data
        if gw is None or data is None or gw.positions_ms is None or self.clock.now_ms() - gw.positions_ms > 300_000:
            return None
        total = 0.0
        for slug, net in gw.positions_cache.items():
            q = data.quote(slug)
            px = None if q is None else (q.get("yes_bid") if net > 0 else q.get("no_bid"))
            if px is None:
                return None
            total += abs(net) * px
        return round(total, 2)

    # -- trading live feed: WebSocket primary, REST polling (you_loop's 0.5 s tick) as the backup ---------------
    def _make_stream(self, env: str, put):
        s = self.settings
        if not s.trading_stream:
            return None
        if env == "shadow":
            return ShadowStream(self.shadow_ex, put, clock_ms=self.clock.now_ms)
        if env == PM_SHADOW:
            return ShadowStream(self.pm_shadow_ex, put, clock_ms=self.clock.now_ms, poll_s=0.25) \
                if self.pm_shadow_ex is not None else None
        if env == PM_PROD:
            return PolymarketTradingStream(self.pm_client.ws.private, self.pm_gw, self.pm_data, put,
                                           market_stream=self.pm_market_stream, env=PM_PROD,
                                           clock_ms=self.clock.now_ms) if self.pm_gw is not None else None
        key_id, key_path = ((s.trading_demo_key_id, s.trading_demo_key_path) if env == "demo"
                            else (s.trading_prod_key_id, s.trading_prod_key_path))
        return stream_from_settings(env, key_id, key_path, put, clock_ms=self.clock.now_ms)

    async def _stream_pump(self, q: asyncio.Queue) -> None:
        """Feeds stream events, in order, to every phone service (each acts only on its own orders and brackets); only
        the newest quote per market is kept."""
        while True:
            batch = [await q.get()]
            while not q.empty():
                batch.append(q.get_nowait())
            newest = {(e.get("env"), e.get("ticker")): i for i, e in enumerate(batch) if e.get("type") == "quote"}
            for i, e in enumerate(batch):
                if e.get("type") == "quote" and newest[(e.get("env"), e.get("ticker"))] != i:
                    continue
                for svc in list(self.trading_services.values()):
                    try:
                        await svc.on_stream_event(e, self.clock.now_ms())
                    except Exception:
                        log.warning("trading stream event failed (%s)", svc.strategy, exc_info=True)

    async def stream_loop(self) -> None:
        """Runs one live feed per environment that any phone service is in (e.g. Kalshi in shadow and Polymarket in
        production), attached to every service in it, and follows mode changes."""
        q: asyncio.Queue = asyncio.Queue()
        pump = self.supervisor.start("stream_pump", functools.partial(self._stream_pump, q))
        tasks: dict[str, asyncio.Task | None] = {}
        try:
            while True:
                want = {svc.modes[svc.mode] for svc in self.trading_services.values()}
                for env in [e for e in tasks if e not in want]:
                    await self._stop_stream(env, tasks.pop(env))
                    for svc in self.trading_services.values():
                        if env in svc.gateways:
                            svc.detach_stream(env, self.clock.now_ms())
                for env in [e for e in want if e not in tasks]:
                    st = self._make_stream(env, q.put_nowait)
                    tasks[env] = None
                    if st is None:
                        log.info("trading live feed off for %s; polling only", env)
                        continue
                    shared = SharedStream(st)
                    self.trading_streams[env] = st
                    tasks[env] = self.supervisor.start(f"stream_{env}", st.run)
                    for svc in self.trading_services.values():
                        if env in svc.gateways:  # a Polymarket feed never attaches to a Kalshi service
                            svc.attach_stream(env, shared.view(svc.strategy), self.clock.now_ms())
                    log.info("trading live feed started for %s", env)
                self.supervisor.beat("stream")
                await asyncio.sleep(0.5)
        finally:
            for env, task in list(tasks.items()):
                await self._stop_stream(env, task)
            pump.cancel()

    async def _stop_stream(self, env: str, task) -> None:
        st = self.trading_streams.pop(env, None)
        if st is not None:
            st.close()
        await _cancel_task(task, f"trading live feed {env}")

    async def connectivity_loop(self, stale_after_ms: int = 30_000) -> None:
        """Connectivity guard: every 5 s, for each phone service in a live mode, a feed that is stale or a BTC index
        older than 10 s for more than `stale_after_ms` is recorded as connectivity_lost (and restored later)."""
        bad_since: dict[str, int] = {}
        while True:
            await asyncio.sleep(5)
            self.supervisor.beat("connectivity")
            now = self.clock.now_ms()
            live = self.live_price()
            index_ok = live is not None and now - int(live[0]) <= 10_000
            for name, svc in list(self.trading_services.items()):
                env = svc.modes[svc.mode]
                if svc.mode == "paper" or env not in (getattr(svc, "streams", None) or {}):
                    continue
                ok = index_ok and svc.stream_ok(env)
                if ok:
                    bad_since.pop(name, None)
                    svc.note_connectivity(True, now, "Live feed and BTC index are fresh again.")
                    continue
                first = bad_since.setdefault(name, now)
                if now - first >= stale_after_ms:
                    why = ("the BTC index is stale" if not index_ok else f"the {env} live feed is stale")
                    svc.note_connectivity(False, now, f"For {(now - first) // 1000} s {why}: new entries are refused "
                                                      "and stops cannot act until it is back.")

    async def balance_loop(self) -> None:
        """Reads the venue balances every 60 s: Kalshi's from the read-only portfolio client, Polymarket's from its
        gateway (a backup to the private stream's balance updates). Play shows them; production entries must fit."""
        while True:
            if self.pm_gw is not None:
                try:
                    await self.pm_gw.read_balance()
                except Exception as e:
                    log.warning("Polymarket balance read failed: %s: %s", type(e).__name__, e)
            if self.portfolio_gw is not None:
                try:
                    b = await self.portfolio_gw.balance()
                    v = b.get("balance_dollars")
                    v = float(v) if v is not None else float(b.get("balance", 0)) / 100.0
                    pv = b.get("portfolio_value_dollars")
                    pv = float(pv) if pv is not None else (float(b["portfolio_value"]) / 100.0
                                                            if b.get("portfolio_value") is not None else None)
                    self.kalshi_positions_value = round(pv, 2) if pv is not None else None
                    self.balance = (round(v, 2), self.clock.now_ms())
                except Exception:
                    log.warning("Kalshi balance read failed", exc_info=True)
            self.supervisor.beat("balance")
            await asyncio.sleep(60)

    async def periodic(self) -> None:
        rest = CoinbaseRest()
        tick = 0
        while True:
            await asyncio.sleep(30)
            self.supervisor.beat("periodic")
            tick += 1
            now = self.clock.now_ms()
            try:
                await self.kalshi_sync.sync_settled(now)
            except Exception:
                log.warning("kalshi settled sync failed", exc_info=True)
            if tick % 4 == 0:
                try:
                    await repair_gaps(self.store, rest)
                except Exception:
                    log.warning("gap repair failed", exc_info=True)

    def write_pid_file(self) -> None:
        """Record this process's id next to the database, where scripts/deploy_stop.ps1 looks for the app to stop."""
        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text(str(os.getpid()), encoding="ascii")
        except OSError:
            log.warning("could not write %s: scripts/deploy_stop.ps1 will not find this app", self.pid_file, exc_info=True)

    def remove_pid_file(self) -> None:
        """Delete the pid file if it is still this process's (a newer process may already have replaced it)."""
        try:
            if self.pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                self.pid_file.unlink()
        except OSError:
            pass

    async def stop_file_loop(self) -> None:
        """Watch for the deploy's stop file (KTD9); when it appears, ask the app to shut down the normal way."""
        while True:
            self.supervisor.beat("stop_file")
            if self.stop_file.exists():
                log.info("stop requested (%s): shutting down", self.stop_file)
                self._deploy_stop = True
                self.supervisor.request_stop()
                return
            await asyncio.sleep(STOP_POLL_S)

    async def memory_loop(self) -> None:
        while True:
            b = private_bytes()
            log.info("memory: private bytes %s", "unknown" if b is None else f"{b / 2**20:.1f} MiB")
            self.supervisor.beat("memory")
            await asyncio.sleep(MEMORY_LOG_S)

    def register_extra_routes(self, app) -> None:
        register_coach_routes(app, self.coach, self.clock.now_ms)
        register_trading_routes(app, self.trading_services, self.clock.now_ms,
                                extra=[self.perps] if self.perps is not None else [],
                                on_control=self._on_control)
        register_perp_routes(app, self.perp_view, self.perps_hub, self.clock.now_ms,
                             notify=self.perps_publisher.notify, inflight=self.perps_inflight)
        register_play_routes(app, self.play, self.clock.now_ms, limits=self.play_limits, streams=self.play_streams)
        register_copilot_routes(app, self.copilot, self.clock.now_ms)
        register_record_routes(app, self.store, self.clock.now_ms, tz=self.day_tz)  # the user's day (domain/days.py)
        if self.agent_sessions is not None:
            self.agent_key = register_agent_routes(
                app, sessions=self.agent_sessions, tools=self.agent_tools, prompts=self.agent_prompts,
                feed=self.agent_feed, now_fn=self.clock.now_ms, llm=self.agent_llm, fleet=self.agent_fleet,
                start_task=self._start_agent_task, limits=self.play_limits, daily_fn=self._agent_daily,
                proposals=self.agent_proposals, session_state_fn=self._agent_session_state)
            self.agent_actions = self.remote.actions = app.state.agent_actions
        register_telegram_routes(app, self.remote, self.clock.now_ms)
        if self.agent_presets is not None:
            register_preset_routes(app, self.agent_presets, self.clock.now_ms)
        install_access(app, AccessGuard(PinStore(self.store), session_hours=self.settings.access_session_hours),
                       api_host=self.settings.api_host, port=self.settings.api_port,
                       allowed_hosts=self.settings.allowed_hosts, local_clients=self.settings.local_clients,
                       pc_port=self.settings.pc_port, agent_port=self.settings.agent_port,
                       pc=PcCredentials(self.settings.data_dir), public_url=self.settings.public_url)

    def services(self) -> Services:
        return Services(store=self.store, feed=self.feed.snapshot, health=self.health_parts,
                        extra_routes=self.register_extra_routes)

    def health_parts(self) -> dict:
        """/api/health's parts beyond the Coinbase feed (R16): each venue stream (connected, stale, last message age),
        the writer backlog (new writes plus those in failed batches waiting for a retry), how long the database has not
        taken writes and how many writes went to the dead-letter file, each supervised loop (last
        heartbeat age, restarts), event-loop stalls in the last hour, uptime and private memory."""
        now, wall = self.clock.now_ms(), time.time_ns() // 1_000_000

        def entry(st, venue: str, env: str, at: int) -> dict:
            h = st.health()
            last = h.get("last_msg_ms")
            return {"venue": venue, "env": env, "connected": bool(h.get("connected")), "stale": bool(h.get("stale")),
                    "last_msg_age_ms": None if last is None else at - int(last)}

        streams = {}
        if self.market_stream is not None:
            streams["kalshi_market"] = entry(self.market_stream, "kalshi", "prod", now)
        if self.pm_market_stream is not None:  # Polymarket's market data runs on wall time
            streams["polymarket_market"] = entry(self.pm_market_stream, "polymarket", "prod", wall)
        for env, st in list(self.trading_streams.items()):
            pm = env.startswith("pm_")
            streams[f"trading_{env}"] = entry(st, "polymarket" if pm else "kalshi", env.removeprefix("pm_"), now)
        t = time.time()
        loops = {name: {"last_beat_age_s": None if b["last_beat"] is None else round(t - b["last_beat"], 1),
                        "restarts": b["restarts"], "critical": b["critical"]}
                 for name, b in self.supervisor.heartbeats().items()}
        mem = private_bytes()
        return {"streams": streams, "writer_pending": self.writer.pending,
                "writer_failed_batches": self.writer.failed_batches, "writer_outage_s": self.writer.outage_s,
                "writer_dead_lettered": self.writer.dead_lettered, "loops": loops,
                "stalls_last_hour": self.stall_watch.stalls_last_hour() if self.stall_watch is not None else 0,
                "loop_lag": self.stall_watch.lag_view() if self.stall_watch is not None else None,
                "perps": self.perps_health(), "recorders": self.recorder_watch.status(wall),
                "uptime_s": round(time.monotonic() - self.started), "memory_mb": None if mem is None
                else round(mem / 2**20, 1)}

    def loops(self) -> dict:
        """The background loops run() starts (besides the Coinbase collector and the web server), by name."""
        out = {"writer": functools.partial(self.writer.run, beat=lambda: self.supervisor.beat("writer")),
               "decision": self.decision_loop, "periodic": self.periodic,
               "kalshi_live": self.kalshi_live_loop, "stream": self.stream_loop}
        if self.pm_market_stream is not None:
            out["pm_market_stream"] = self.pm_market_stream.run
        out.update(you=self.you_loop, balance=self.balance_loop, connectivity=self.connectivity_loop,
                   rv_p95=self.rv_p95_loop, stop_file=self.stop_file_loop, memory=self.memory_loop,
                   maintenance=self.maintenance.run)
        if self.perps is not None:  # none of these exist with perps switched off
            out.update(perps=self.perps_loop, perps_push=self.perps_publisher.run)
        if self.agent_sessions is not None:
            out["agent_review"] = self.agent_review_loop
        if self.telegram_bot is not None:  # long polling; Telegram unreachable only delays the bot, never the app
            out["telegram"] = self.telegram_bot.run
        return out

    async def run(self) -> None:
        await self.startup()
        collector = CoinbaseCollector.live(self.writer, self.clock, on_event=self.on_event)
        host = self.settings.api_host
        if not is_loopback_host(host):
            log.warning("dashboard listening on the network (%s): other devices are refused until a PIN is set on the "
                        "PC (Phone access card), then must sign in. No HTTPS: home LAN / VPN only, never "
                        "port-forward it.", host)
        app = create_app(self.services())
        if self.telemetry is not None:
            self.telemetry.instrument_app(app)  # request traces and metrics (nothing when telemetry is off)
        servers = self.make_servers(app)
        log.info("Play: http://%s:%d", host, self.settings.api_port)
        sup = self.supervisor

        async def serve() -> None:
            if await self.serve_all(servers):  # stopped on a signal (Ctrl+C): shut the app down, not a failure
                sup.request_stop()

        loops = self.loops()
        log.info("background loops: %s", ", ".join(loops))
        self.stall_watch = StallWatch(asyncio.get_running_loop())  # logs the loop thread's stack on a lag > 500 ms
        self.stall_watch.start()
        if self.telemetry is not None:
            self.telemetry.bind_runtime(self)  # the app's gauges and hooks (nothing when telemetry is off)
        # Every loop is supervised (KTD7): a failing one is logged and restarted; nothing but a stop request ends run.
        self.write_pid_file()
        restore_sigterm = self._handle_sigterm(asyncio.get_running_loop())
        sup.start("collector", collector.run)
        sup.start("server", serve, critical=True)
        for name, fn in loops.items():
            sup.start(name, fn, critical=name in CRITICAL_LOOPS)
        try:
            await sup.wait_stop()
        finally:
            restore_sigterm()
            if self.agent_sessions is not None:
                await self._agent_shutdown()  # before the services stop: an agent order in flight finishes first
            await self.perps_hub.close()  # end the Perps screen's streams so the web server can stop
            await self.play_streams.close()  # and the Play screens'
            await self.perps_inflight.drain(PERPS_DRAIN_S)  # a perps approval's save-send-record step finishes first
            collector.close()
            for st in list(self.trading_streams.values()):
                st.close()
            if self.market_stream is not None:
                self.market_stream.close()
            await sup.stop()  # cancel every task and wait for each to finish ...
            self.writer.close()  # ... and only then flush and close the writer
            # Every HTTP client is closed even if another's close fails (each failure is logged).
            for name, c in [("kalshi public", self.kalshi), ("kalshi portfolio", self.portfolio_gw),
                            *((f"trading {k}", gw) for k, gw in self.trading_gws.items()),
                            *((f"perps {k}", c) for k, c in self.perp_clients.items())]:
                if c is not None:
                    try:
                        await c.aclose()
                    except Exception:
                        log.warning("closing the %s client failed", name, exc_info=True)
            if self.pm_market_stream is not None:
                self.pm_market_stream.close()
            for c in (self.pm_client, self.pm_public):
                if c is not None:
                    with contextlib.suppress(Exception):
                        await c.close()
            if self._deploy_stop:
                try:
                    self.stop_file.unlink(missing_ok=True)
                except OSError:
                    log.warning("could not delete %s: the next start will stop again at once", self.stop_file,
                                exc_info=True)
                log.info("clean shutdown (deploy)")
            self.remove_pid_file()


async def run_backfill(settings: Settings) -> None:
    migrate(settings.database)
    we = write_engine(settings.database)
    store = SqliteStore(we, read_engine(settings.database))
    clock = ExchangeClock()
    result = await backfill(store, CoinbaseRest(), days=settings.backfill_days, now_ms=clock.now_ms())
    log.info("coinbase backfill: %s", result)
    kalshi = KalshiClient(series_ticker=settings.kalshi_series)
    try:
        stored = await backfill_kalshi(store, kalshi)
    finally:
        await kalshi.aclose()
    log.info("kalshi backfill: %d markets", stored)


def install_file_log(log_dir: Path, *, max_bytes: int = LOG_FILE_BYTES,
                     backups: int = LOG_FILE_BACKUPS) -> RotatingFileHandler:
    """The app's own log file, `<log_dir>/app.log`, rotated at `max_bytes` keeping `backups` old files; added to the
    root logger next to the stderr handler (which the watchdog redirects to its own file)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(log_dir / "app.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.setFormatter(UtcFormatter())
    logging.getLogger().addHandler(h)
    return h


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="btc_signal_engine")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "backfill"))
    args = parser.parse_args(argv)
    log_to_stderr(logging.INFO)
    started = time.monotonic()
    # Every exit that passes through Python leaves this line; an exit without it was a forced kill.
    atexit.register(lambda: log.info("process exiting (pid %d, up %.0f s)", os.getpid(), time.monotonic() - started))
    settings = Settings()
    mask_secrets(telemetry.secret_values(settings))  # every log line from here on: the exact secret values masked
    set_http_client_log_level(settings.http_client_log_level)
    if settings.file_log:  # off in containers (BTCFLOW_FILE_LOG=false): stderr only, for the platform's collector
        install_file_log(Path(settings.db_path).parent / "logs")
    # OpenTelemetry: nothing unless OTEL_EXPORTER_OTLP_ENDPOINT (or a per-signal endpoint) is set; a bad OTEL_* setting
    # stops the start here, naming the variable.
    tel = telemetry.setup("settleview-app")
    try:
        if args.command == "backfill":
            asyncio.run(run_backfill(settings))
        else:
            rt = Runtime(settings)
            rt.telemetry = tel
            asyncio.run(rt.run())
    except KeyboardInterrupt:
        pass
    except SchemaNotMigrated as exc:  # BTCFLOW_DB_MIGRATE=check and the migrate job has not run (yet)
        log.error("not starting: %s", exc)
        raise SystemExit(75) from None  # EX_TEMPFAIL: the restart policy tries again once the job has run
    finally:
        tel.shutdown()  # the last spans, metrics and logs go out (at most a few seconds)
