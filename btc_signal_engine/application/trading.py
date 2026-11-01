"""Human-confirmed trading for the phone Play screens (plan 2026-10-28-1130: R1-R14, KTD1-KTD4).

The app automates everything except judgment. The human approves each bracket with one Yes: the entry (an
immediate-or-cancel LIMIT buy at the approved max price), the take profit (a resting maker SELL limit at the
approved price, placed after the entry fills, sized to what filled) and the optional stop (watched by `tick`; on
trigger the take profit is cancelled first, then the held contracts are sold with a reduce-only marketable limit at
the bid minus the slippage cap). Whichever exit fills first ends the other. Nothing else is ever sent without a
fresh Yes, and new entries need a present human. There is no auto-approve and no unattended entry of any kind.

Modes (persisted; any credential problem falls back to the safe mode):
  sandbox  Kalshi DEMO exchange (fake money). Gated on: demo trading key configured, human present, kill switch off.
  shadow   The production code path with a simulated exchange that fills on the REAL public order book (no keys,
           no money). Gated on: human present, kill switch off.
  prod     REAL MONEY. Gated on: trading_enabled, prod trading key configured, explicit typed confirmation of the
           switch, human present, kill switch off.
Every mode then runs the same OrderGate: reconciled with the exchange, market open in the series with more than the
strategy's entry cutoff left, approval token fresh and bound to the exact ticket, price not beyond the limit, and all
limits (per trade, per window, per day, open positions, production cash) against logged exposure.

Market buys (phone strategies only, `price_mode` "market"; plan: Kalshi-style buying, 2026-10-29): an IoC buy sized at
the live ask with its limit at that ask + the user's market protection (`market_band_fn`, default 15c; or MARKET_CEILING,
99c, with none), so it fills at the best offers like the venues' own apps instead of being refused when the ask ticks
up. Its max_cost is the WORST case (every contract at that limit plus fee) and must fit the
per-trade, window and day limits and the gateway's order cap, else it is refused (never shrunk). The gate skips only
the "price moved past the limit" check for it; every other check stays, and the spread check uses its own explicit
ceiling (`max_market_spread`, 10c) instead of the limit's 5c.

Live feed (user-directed: WebSocket primary, polling backup): an optional `TradingStream` per environment pushes
order updates, fills and top-of-book quotes into `on_stream_event`, which applies the same state transitions the
poller would (take-profit fills close the bracket, an unknown entry is resolved, a quote at or through the stop
starts the same once-only stop). Every transition is keyed on cumulative fill counts and bracket state, so the
stream and the poller may see the same fact in any order: the second is a no-op. Polling never stops: while the
stream for an environment is healthy, take-profit orders are re-polled every `reconcile_backup_ms` and the REST
quote is used only when the stream's quote for that market is older than `stream_quote_max_age_ms`; when it is
unhealthy (disconnected or a sequence gap) the old cadence returns (`reconcile_every_ms`, a REST quote every tick),
audited as `stream_down` / `stream_up`. Those REST quotes are read concurrently BEFORE `tick` takes the monitor lock
(review fix plan 2026-10-31 P1, B1#6): a slow quote no longer holds an approve, an exit or another bracket's stop
behind it; under the lock each bracket uses the quote read for it (a bracket that appeared meanwhile reads its own).

Threads: the sync routes (status, Play's view) run in FastAPI's worker threads while the event loop changes the
watched brackets, so every read outside the loop iterates a snapshot (`_watched()`, one C-level copy), never the live
dict. Reconciliation and the external-fill check look up only the order ids they are judging (resting orders, fills),
never every order ever placed.

Strategies: each service trades for one phone `strategy` (one per Play tab of the market table, domain/markets.py:
"you", "you_poly", "you_poly5"). Every trading_brackets/orders/audit row it reads is filtered by that strategy and
every row it writes is stamped with it; settings keys are prefixed by the strategy except the kill switch (shared: a
kill from any service halts all of them). The entry cutoff is per strategy (the table's), never 5 s or less.

Index stops (the phone's optional stop): a bracket may carry a stop on the BTC index instead of (or besides) the
contract price:
`stop_index_below` (UP: stop once the live index is at or below it) or `stop_index_above` (DOWN: at or above it), in
dollars. The stop monitor (`tick`, `_watch`) reads the injected `index_price_fn()` -> (exchange ts ms, price); an
index that is missing, failing or more than INDEX_STALE_MS old never triggers (a contract-price stop, if set, still
does). A trigger takes exactly the contract stop's path: open -> stopping once (audited once), cancel the take
profit and learn its final fills, then a reduce-only IoC sale at the bid minus the slippage cap, retried every tick
while anything is held. The stored `stop` column is 0.0 when a bracket has no contract stop (it can never trigger).

Exits made by hand (2026-10-29: the user sold two positions in the exchanges' own apps while the brackets stayed
open with their take profits resting): every `external_check_ms` the monitor reads the exchange's
RAW positions (`exchange_positions()` when the gateway has it) for each market with an open bracket. When the
exchange holds LESS of the bracket's side than the bracket does, none of our orders on that market is in flight or
unresolved, none of our own fills on that market is under `external_grace_ms` old (Polymarket's list lags our own
fills by 1-3 s) and the window has not closed (positions vanish at settlement), and the shortfall is still there
`external_grace_ms` later, it is an EXTERNAL (manual) EXIT: the resting take profit is cancelled (its fills learnt
first), `held` drops to the exchange's quantity (partial exits allowed), and at 0 the bracket closes with exit_reason
"manual". Its exit price comes from the exchange's fills in that market that are not ours (`market_fills`), or else
is ESTIMATED from the bid at detection (the audit says which; `trades()` gives `pnl_source`). Audit event
`manual_exit`. No order is ever placed in response. The gateway is told (`note_external_position`) so its own books
follow. MORE on the exchange than in our brackets (a manual add) is only audited (`external_position`): it is never
adopted, and our sales stay sized by our own held quantity.

Ticket quotes (2026-10-29: a $1 exit took ~5 s, its REST order-book read failing once on the way): prepare,
prepare_exit and the approve-time recheck read the LIVE WebSocket book when it is fresh (`stream_quote_max_age_ms`):
first `market_quote_fn(env, ticker, now)` (wired to the production market-data stream for prod Kalshi; a full quote
with status/open/close), else this service's trading stream (prices) plus the gateway's cached market metadata
(`market_meta`); the REST quote only when neither is fresh. Polymarket's gateway quote is its WebSocket cache already.
Each ticket quote logs its source (ws | rest) at INFO, and prepare / prepare_exit / approve log their step timings
(quote, gate, issue or lock wait, send) in ms.

Already closed (2026-10-29: the user slid to sell while the 85c auto-sell was filling): an exit prepared or approved
for a bracket that its take profit, its stop or a manual sale has already closed answers ok:false with code
`already_closed` and a reason built from the bracket's recorded fills ("Your auto-sell already sold 2.65 @ 85c
(+$1.26)."); a stop that is still selling answers `already_closing`. Nothing is sent for either.

Settlement (code review #4, 2026-10-30): a bracket still holding at the window's close is NOT closed as a loss. It
moves to state `settling` (audit `settle_pending`) and the monitor asks the gateway for the market's official result
(`market_result(ticker)` -> {result: yes|no|None, final, status, source}; Kalshi: GET /markets/{ticker} `result` once
the status is determined/finalized/settled; Polymarket US: the market's MARKET_STATUS_RESOLVED `outcomePrices`; the
shadow exchanges answer the same from the real public market) every `settle_poll_ms`, every `settle_slow_poll_ms`
once `settle_slow_after_ms` have passed. A read failure, or no result yet, leaves it settling (a failure is audited
`settle_read` on the first try and every 20th). When the result is final the bracket closes with exit_reason "settled":
proceeds += held x $1 for the winning side (0 for the losing side), less SETTLEMENT_FEE per contract (booked as an exit
fee), audited `settled` with the result and its source (`trades()` gives it as `pnl_source`). A gateway without
`market_result` leaves the bracket settling (audited once, `settle_unsupported`): never a silent loss or win. While
settling, a bracket's P&L is None (not finished), it is not a live position (`live_brackets`), and agent budgets count
it at its worst case (the held contracts paying $0).
Settlement fixes (code review 2026-10-30): a resting take profit keeps its id while the bracket settles, so its late
fills (stream, reconcile) still reduce `held`; once the result is final its order is read again and nothing is booked
until that read answers with a finished order (audit `settle_tp_read` on a failure), so no contract is paid twice. The
result reads run after `tick` releases the monitor lock, concurrently, each capped at `settle_read_timeout_ms`, and the
booking re-checks the bracket under the lock. Reconcile leaves brackets whose window closed to the monitor (a settled
market's 0 position is not a sale). A bracket still settling `settle_warn_after_ms` (60 min) after the close gets a
visible warning ("settling for N min — check the venue": `settle_warnings`, in status, Play and the Agent panel) and one
`settle_overdue` audit; nothing closes or voids it automatically and there is no manual close action yet.

Lost answers (review fix plan 2026-10-31, P0 #2; the rule is domain/order_resolution.py, shared with Polymarket US
and perps): an entry whose answer was lost stays `unknown` (its approved max_cost committed, the market blocked)
until the venue proves the outcome. Each pass looks it up by client_order_id (`find_order`); a list miss is never
proof of no fill. After FIND_GIVE_UP_MS (60 s), every PROOF_EVERY_MS the gateway's `lost_order` reads the fills
(Kalshi: fills since the send whose order, read by id, carries our client_order_id; Polymarket US: unclaimed
activity trades since the send) and, when they show nothing, the position is compared with what our own brackets
hold: only no fill AND no unexplained position closes it as `not_found`. A found order is booked at its real
average fill price (fees from it, else the fee formula) and gets its take profit; the stop is watched as for any
bracket. Unprovable (a read failed, or the position holds contracts no bracket explains) stays unknown, audited
once `order_unresolved` and listed in `settle_warnings` (kind `unresolved_order`) until it is resolved. A 409
duplicate is never closed on a timer either: it exists, so it closes only when its fills and the position show
it filled nothing (`unfilled`).

Exits after a mode change (P0 #3): an exit on a bracket is checked against the bracket's OWN environment
(`mode_of(env)`), never the current mode, so a production position can still be sold by hand or by its agent after
a credential problem dropped the service back to the safe mode.

Ride to settlement (agent rules): a proposal with `ride_to_settlement: true` and NO take_profit places no take-profit
order at all (the bracket is `no_tp`; its stops still act) and is paid by settlement. A proposal without take_profit
and without that flag is refused, as before.

Units: prices are dollars per contract on the bracket's own side (YES for UP, NO for DOWN); counts are contracts
to 0.01; times are UTC ms. Exposure counts what an approval may spend (contracts x limit + taker fee) until the
fill is known, then the actual entry cost incl. fees; days are the user's days (`day_tz`, domain/days.py). Persistence goes through the Repository
port (tables trading_brackets, trading_orders, trading_audit, trading_settings).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import secrets
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import tzinfo
from typing import Any, Protocol

from btc_signal_engine.application.actor import actor, agent_session
from btc_signal_engine.application.ports import Repository, TradingStream
from btc_signal_engine.domain.coach.money import position_for_stake
from btc_signal_engine.domain import days
from btc_signal_engine.domain.coach.rules import EPS
from btc_signal_engine.domain.markets import market, phone_markets
from btc_signal_engine.domain.order_resolution import (FIND_GIVE_UP_MS, NOT_FILLED, PROOF_EVERY_MS, UNKNOWN, decide,
                                                        excess_on_side)
from btc_signal_engine.domain.settlement.fees import kalshi_fee

log = logging.getLogger(__name__)

DUPLICATE_PENDING = "unknown_duplicate"  # audit code: entry answered 409 duplicate, not listed yet (never timed out)
BRACKETS, ORDERS, AUDIT, SETTINGS = ("trading_brackets", "trading_orders", "trading_audit", "trading_settings")
STRATEGY_TABLES = (BRACKETS, ORDERS, AUDIT)  # rows carry `strategy` (migration 0009)
BALANCE_MAX_AGE_MS = 300_000  # a balance older than this never sizes an entry
SHARED_SETTINGS = ("killed",)  # one kill switch halts every strategy
MIN_LEFT_FLOOR_MS = 5_000  # no strategy ever enters with 5 s or less left (R27)
# Per-strategy entry cutoff: one phone strategy per Play tab of the market table (domain/markets.py): "you" (Kalshi
# 15m), "you_poly" (Polymarket 15m), "you_poly5" (Polymarket 5m). None = TradingConfig.min_left_ms (2:00); the 5m tab:
# 1:00 left. A person approves every trade, so the stop is optional (by default the bracket is the resting sell, the
# auto-sell at 99c = ride, and nothing else; a BTC index stop only when the user sets one: the 2026-10-29 lesson was
# that default stops closed two trades within 20 s), and a market buy is allowed (`price_mode` "market", see _prepare).
STRATEGY_CUTOFFS: dict[str, int | None] = {m.phone_strategy: m.entry_cutoff_ms for m in phone_markets()}
# Market buys (2026-10-29: a limit at ask + tolerance was refused "price moved past the 47c limit (now 49c)" and the
# user missed a fast move). Neither venue gives us a dollar-capped market order we can use (Kalshi's V2 create order
# has no notional field; Polymarket US's cashOrderQty market order is unverified, see _prepare), so a market buy is an
# immediate-or-cancel limit at the market's ceiling, sized at the ask seen, and its WORST case (every contract at the
# ceiling, plus fee) is what the per-trade / window / day limits and the gateway's order cap are checked against.
PRICE_MODES = ("limit", "market")
MARKET_CEILING = 0.99  # the highest price a contract of these markets trades at: a market buy's limit
INDEX_STALE_MS = 2_000  # an index print older than this never triggers an index stop
MODES = {"sandbox": "demo", "shadow": "shadow", "prod": "prod"}
PROD_CONFIRM = "I understand this uses real money"
LIVE_STATES = ("sending", "unknown", "open", "stopping")
# Window closed while holding: waiting for the venue's official result (see "Settlement" in the module docstring). Not
# a live position (nothing can be sold, no stop or take profit acts, it never counts as an open position), but still
# watched by the monitor until its proceeds are booked.
SETTLING = "settling"
WATCHED_STATES = LIVE_STATES + (SETTLING,)
SETTLEMENT_FEE = 0.0  # $ per contract at settlement: Kalshi charges none; Polymarket US assumed none (to verify live)
DAY_MS = 86_400_000
MODE_LABELS = {
    "sandbox": "sandbox (Kalshi demo exchange, fake money). Tests the mechanics; prices are the demo exchange's, "
               "not the real market's.",
    "shadow": "shadow (simulated fills on the real order book). No money moves.",
    "prod": "PRODUCTION: real money on Kalshi.",
}


def venue_mode_labels(venue: str, modes: Iterable[str]) -> dict[str, str]:
    """Mode labels for a service on another exchange (Kalshi keeps MODE_LABELS unchanged)."""
    labels = {"sandbox": f"sandbox ({venue} test exchange, fake money).",
              "shadow": f"shadow (simulated fills on {venue}'s live order book). No money moves.",
              "prod": f"PRODUCTION: real money on {venue}."}
    return {m: labels[m] for m in modes}


class TradingGateway(Protocol):
    """What the trading service needs from an exchange (adapters/kalshi/trading.py implements it).

    Exceptions with `definitive = True` mean the order certainly does not exist; any other exception from
    `place_limit` means the outcome is unknown and must be reconciled. `status` 401/403 is a credential problem.
    """

    env: str

    # The gateway's own backstop on a buy's notional (count x limit, $): a market buy's worst case is checked against
    # it before anything is sent, so it is refused with a plain reason instead of by the gateway.
    max_order_cost: float

    async def place_limit(self, *, ticker: str, side: str, action: str, count: float, price: float,
                          client_order_id: str, tif: str = ..., post_only: bool = ..., reduce_only: bool = ...) -> dict: ...

    async def cancel(self, order_id: str, ticker: str) -> dict: ...

    async def get_order(self, order_id: str, side: str | None = None) -> dict: ...

    async def orders(self, *, ticker: str | None = None, status: str | None = None) -> list[dict]: ...

    async def find_order(self, ticker: str, client_order_id: str, side: str | None = None) -> dict | None: ...

    async def positions(self) -> list[dict]: ...

    async def quote(self, ticker: str) -> dict: ...


@dataclass(frozen=True)
class TradingConfig:
    enabled: bool = False  # master switch for PRODUCTION (sandbox/shadow risk no money)
    max_stake_per_trade: float = 5.0
    max_dollars_per_window: float = 10.0
    max_dollars_per_day: float = 25.0
    max_open_positions: int = 1
    stop_slippage: float = 0.05
    approval_ttl_s: int = 10
    presence_timeout_s: int = 180
    fee_rate: float = 0.07
    series: str = market("kalshi_15m").series
    window_ms: int = 900_000  # the series' window length (the per-window limit's window)
    min_left_ms: int = 120_000
    reconcile_every_ms: int = 2_000  # take-profit re-poll while the live feed is down (or absent)
    reconcile_backup_ms: int = 10_000  # take-profit re-poll while the live feed is healthy (backup only)
    external_check_ms: int = 5_000  # how often open brackets' markets are compared with the exchange's positions
    external_grace_ms: int = 10_000  # a shortfall must outlast this, and our own last fill there be older than it
    stream_quote_max_age_ms: int = 2_000  # older stream quotes fall back to a REST quote
    max_entry_spread: float = 0.05  # refuse entries when ask - bid is wider: a stop cannot protect a thin book
    # A market buy's spread ceiling: wider than a limit's (it is meant to fill in a fast market), but never none — a
    # book whose best offer is more than this over the best bid is too thin to buy "at the market" (the IoC at 99c
    # could walk far above value).
    max_market_spread: float = 0.10
    whole_contracts: bool = False  # size entries in whole contracts (Polymarket US BTC markets trade whole contracts)
    settle_poll_ms: int = 15_000  # a settling bracket asks for its market's result this often...
    settle_slow_after_ms: int = 600_000  # ... and, once its window closed this long ago,
    settle_slow_poll_ms: int = 60_000  # this often (retried politely; it never gives up)
    settle_read_timeout_ms: int = 5_000  # one result read may take this long (read outside the monitor lock)
    settle_warn_after_ms: int = 3_600_000  # still settling this long after the close: a "check the venue" warning


@dataclass(frozen=True)
class GateResult:
    ok: bool
    code: str = "ok"
    reason: str = ""


OK = GateResult(True)


def _cents(x: float) -> str:
    return f"{x * 100:.0f}c" if abs(x * 100 - round(x * 100)) < 1e-6 else f"{x * 100:.1f}c"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _r2(x: float) -> float:
    return round(x + 0.0, 2)


def _floor_cent(x: float) -> float:
    return math.floor(x * 100 + 1e-6) / 100


SIDE_CONTRACT = {"UP": "yes", "DOWN": "no"}  # our side -> the contract it buys; nothing else is a side


def _contract_side(side: str) -> str:
    """"yes" for UP, "no" for DOWN; anything else raises (never quietly the NO side)."""
    try:
        return SIDE_CONTRACT[side]
    except (KeyError, TypeError):
        raise ValueError(f"unknown side {side!r} (expected UP or DOWN)") from None


def _is_up(side: str) -> bool:
    return _contract_side(side) == "yes"


def _bid(side: str, q: dict | None) -> float | None:
    if not q:
        return None
    b = q.get(f"{_contract_side(side)}_bid")
    return b if b is not None and 0.0 < b < 1.0 else None


def _ask(side: str, q: dict | None) -> float | None:
    if not q:
        return None
    a = q.get(f"{_contract_side(side)}_ask")
    return a if a is not None and 0.0 < a < 1.0 else None


class _Steps:
    """Wall-clock step timings of one ticket request, logged at INFO as one line ("quote 3 ms (ws), gate 0 ms, ...")."""

    def __init__(self) -> None:
        self.t0 = self.t = time.perf_counter()
        self.parts: list[str] = []

    def mark(self, name: str, note: str | None = None) -> None:
        now = time.perf_counter()
        self.parts.append(f"{name} {(now - self.t) * 1000:.0f} ms" + (f" ({note})" if note else ""))
        self.t = now

    def log(self, strategy: str, op: str) -> None:
        total = (time.perf_counter() - self.t0) * 1000
        log.info("%s %s timing: %s; total %.0f ms", strategy, op, ", ".join(self.parts) or "-", total)


def _signed_money(x: float) -> str:
    return ("+" if x >= 0 else "-") + _money(abs(x))


def closed_sentence(b: dict) -> str:
    """What closed a bracket, from its recorded fills: "Your auto-sell already sold 2.65 @ 85¢ (+$1.26)." The P&L is
    the bracket's (proceeds - exit and take-profit fees - entry cost), as in `trades()`."""
    pnl = round((b.get("proceeds") or 0.0) - (b.get("tp_fees") or 0.0) - (b.get("exit_fees") or 0.0)
                - (b.get("entry_cost") or 0.0), 2)
    tp_n, ex_n = float(b.get("tp_filled") or 0.0), float(b.get("exit_filled") or 0.0)
    parts = []
    if tp_n > 0:
        parts.append(f"{tp_n:.2f} @ {_cents(b['take_profit']).replace('c', '¢')}")
    if ex_n > 0 and b.get("exit_avg") is not None:
        parts.append(f"{ex_n:.2f} @ {_cents(b['exit_avg']).replace('c', '¢')}")
    sold = " and ".join(parts)
    why = b.get("exit_reason")
    if why == "tp":
        head = f"Your auto-sell already sold {sold}" if sold else "Your auto-sell already sold your contracts"
    elif why == "stop":
        head = f"Your stop already sold {sold}" if sold else "Your stop already sold your contracts"
    elif why == "manual":
        head = "You already sold this position outside the app" + (f" ({sold})" if sold else "")
    elif why == "settled":
        return f"This position is already closed: its window ended and it settled ({_signed_money(pnl)})."
    elif sold:
        head = f"This position is already closed ({sold})"
    else:
        return "This position is already closed; nothing was sent."
    return f"{head} ({_signed_money(pnl)})."


def stop_triggered(stop: float, bid: float | None) -> bool:
    """A contract-price stop: the held side's bid at or below the stop. With no bid there is nothing to sell into, so
    nothing triggers. A stop of 0 (none) never triggers."""
    return bid is not None and bid <= stop + EPS


BOUND_FIELDS = ("kind", "env", "ticker", "side", "stake", "contracts", "limit_price", "take_profit", "stop",
                "bracket_id", "stop_index_below", "stop_index_above", "price_mode", "no_tp")


def ticket_hash(token: str, ticket: dict) -> str:
    """Binds an approval token to the exact ticket fields the human saw (KTD3)."""
    canon = {k: ticket.get(k) for k in BOUND_FIELDS}
    for k in ("stake", "contracts", "limit_price", "take_profit", "stop", "stop_index_below", "stop_index_above"):
        if canon[k] is not None:
            canon[k] = f"{float(canon[k]):.4f}"
    return hashlib.sha256((token + json.dumps(canon, sort_keys=True)).encode("utf-8")).hexdigest()


class OrderGate:
    """KTD2: every check an order must pass before anything is sent, in order. The first failure refuses."""

    def __init__(self, svc: TradingService) -> None:
        self._svc = svc

    def check(self, ticket: dict, now: int, quote: dict | None, *, token_error: GateResult | None = None,
              exit_order: bool = False) -> GateResult:
        s, cfg = self._svc, self._svc.config
        if exit_order:
            # An exit reduces risk on a position that lives in its bracket's own environment: it is checked against
            # that environment, never the current mode (a credential blip that falls back to sandbox must never trap a
            # production position; review fix plan P0 #3).
            mode = s.mode_of(ticket.get("env"))
            if mode is None:
                return GateResult(False, "mode_changed", "This position's trading environment is not one of this "
                                                         "service's modes; nothing was sent.")
        else:
            mode = s.mode
            if ticket.get("env") != s.modes[mode]:
                return GateResult(False, "mode_changed", "The trading mode changed since this ticket was prepared.")
        # 1. the mode (for an exit: the position's own environment) is allowed to trade at all
        r = s.mode_ready(mode, now, for_exit=exit_order)
        if not r.ok:
            return r
        # 2. an agent order passes its session's check (it stands in for presence, for this order only); anyone else
        # needs a person at the screen (entries only; an exit reduces risk and still needs its own Yes)
        if agent_session() is not None:
            r = s.agent_check(ticket, now, exit_order)
            if not r.ok:
                return r
        elif not exit_order and not s.present(now):
            return GateResult(False, "not_present", "Nobody is at the screen: new orders need the Play screen open, "
                                                    "visible and used within the last "
                                                    f"{cfg.presence_timeout_s // 60} minutes.")
        # 3. market open, in series, enough time left
        ticker = ticket.get("ticker") or ""
        if not ticker.startswith(cfg.series + "-"):
            return GateResult(False, "wrong_series", f"Only {cfg.series} markets can be traded ({ticker} refused).")
        if not quote or quote.get("ticker") != ticker:
            return GateResult(False, "no_quote", "There is no live price for this market right now.")
        if quote.get("status") not in (None, "open", "active") or not (
                (quote.get("open_ts") or 0) <= now < (quote.get("close_ts") or 0)):
            return GateResult(False, "market_closed", "This market is not open for trading.")
        min_left = s.min_left_ms()
        if not exit_order and quote["close_ts"] - now <= min_left:
            left = (f"{min_left // 60_000} minutes" if min_left >= 60_000 and min_left % 60_000 == 0
                    else f"{min_left / 1000:g} seconds")
            return GateResult(False, "too_late", f"Too close to the close: {left} or less left.")
        # 4. approval token
        if token_error is not None and not token_error.ok:
            return token_error
        # 5. price at send time
        if exit_order:
            bid = _bid(ticket["side"], quote)
            if bid is None or bid < ticket["limit_price"] - EPS:
                return GateResult(False, "price_moved", f"The bid moved below the approved {_cents(ticket['limit_price'])}"
                                                        " minimum; nothing was sold.")
            return OK
        ask = _ask(ticket["side"], quote)
        market_buy = ticket.get("price_mode") == "market"
        if market_buy:  # fills at the best offers up to the ceiling: the ask moving is the point, not a refusal
            if ask is None:
                return GateResult(False, "no_offer", "Nobody is offering this contract right now; nothing was sent.")
        elif ask is None or ask > ticket["limit_price"] + EPS:
            return GateResult(False, "price_moved", f"The price moved past the {_cents(ticket['limit_price'])} limit "
                                                    f"(now {_cents(ask) if ask else 'no offer'}); nothing was sent.")
        # 5b. a stop must be able to protect the position: someone has to be bidding near the price we pay.
        # (Sandbox test 2026-10-28: bought at 50c with the best bid at 2c; the stop fired at once and sold at 1c.)
        # A market buy keeps a spread ceiling of its own (max_market_spread), never none.
        bid = _bid(ticket["side"], quote)
        spread_cap = cfg.max_market_spread if market_buy else cfg.max_entry_spread
        if ticket.get("stop") is None:  # no contract stop: only the spread check
            if bid is None or ask - bid > spread_cap + EPS:
                return GateResult(False, "thin_market", "Skip: the market is too thin to enter "
                                                        f"(bid {_cents(bid) if bid else 'none'}, ask {_cents(ask)}); "
                                                        "nothing was sent.")
        elif bid is None or ask - bid > spread_cap + EPS or bid <= ticket["stop"] + EPS:
            return GateResult(False, "thin_market", "Skip: the market is too thin to protect this trade with a stop "
                                                    f"(bid {_cents(bid) if bid else 'none'}, ask {_cents(ask)}, "
                                                    f"stop {_cents(ticket['stop'])}); nothing was sent.")
        # 6. limits against logged exposure
        return s.limits_check(ticket, now)


class TradingService:
    # The user's day zone for "today" (the day limit, Play's day total): bootstrap sets the one it resolved at startup
    # (domain/days.py), the same one Record and the daily summary use.
    day_tz: tzinfo = days.zone(None)

    def day_start(self, now: int) -> int:
        return days.day_start(now, self.day_tz)

    def __init__(self, repo: Repository, gateways: dict[str, TradingGateway | None], config: TradingConfig, *,
                 strategy: str, paused_fn: Callable[[], bool] | None = None,
                 notify_fn: Callable[[dict], None] | None = None,
                 id_fn: Callable[[], str] = lambda: uuid.uuid4().hex, token_fn: Callable[[], str] = lambda:
                 secrets.token_urlsafe(24), now: int = 0,
                 streams: dict[str, TradingStream] | None = None,
                 known_order_ids_fn: Callable[[set[str]], Iterable[str]] | None = None,
                 index_price_fn: Callable[[], tuple[int, float] | None] | None = None,
                 balance_fn: Callable[[], tuple[float | None, int | None]] | None = None,
                 modes: dict[str, str] | None = None, venue: str = "Kalshi",
                 fee_fn: Callable[[float, float, float], float] | None = None,
                 default_mode: str | None = None,
                 limits_fn: Callable[[], dict] | None = None, cash_backstop: bool = False,
                 market_quote_fn: Callable[[str, str, int], dict | None] | None = None,
                 market_band_fn: Callable[[], float | None] | None = None,
                 committed_today_fn: Callable[[str, int], float] | None = None,
                 agent_gate_fn: Callable[[str, dict, int, bool], GateResult] | None = None,
                 agent_takeover_fn: Callable[[str, dict, int], None] | None = None) -> None:
        """`strategy` (required: one of STRATEGY_CUTOFFS) scopes every trading_* row this service reads or writes
        (and its settings keys, except the shared kill switch). `known_order_ids_fn` returns order ids and
        client_order_ids placed by other strategies on the same account (the other phone tabs, and the retired bots'
        past orders) so reconciliation never counts them as external orders.
        `known_order_ids_fn(ids)` is asked about the ids being judged only (a set; it returns those of them the other
        strategies placed): reconciliation's resting orders, the external-fill check's fills.
        `index_price_fn()` -> (exchange ts ms, price) of the live BTC index, or None: the source for index stops.
        `modes` maps each allowed mode to its environment name (default MODES: sandbox->demo, shadow, prod); a
        service on another exchange uses its own environment names (e.g. {"shadow": "pm_shadow", "prod": "pm_prod"})
        so its rows, streams and gateways never mix with Kalshi's. `venue` names the exchange in messages; `fee_fn`
        (contracts, price, rate) -> taker fee in dollars (default `kalshi_fee`). `default_mode` = the mode on the
        very first start (no stored mode yet; e.g. a Polymarket tab follows the previous tab's); it is stored at once,
        so later starts use this service's own stored mode. Production still needs credentials, as for any stored mode.
        `limits_fn()` -> {"per_trade", "per_window", "per_day"} ($, None = no limit) replaces the config's three dollar
        limits, read at every check (the phone's user-set limits, application/play_limits.py); a ValueError from it
        refuses entries with code `limits_unreadable`. `cash_backstop`: in production every entry's max_cost (a market
        buy's worst case incl. fee) must fit the venue's cash from `balance_fn` no older than BALANCE_MAX_AGE_MS;
        missing or stale refuses (`balance_unknown`), never assumed to fit. `market_quote_fn(env, ticker, now)` -> a
        full quote ({ticker, yes/no bid/ask, status, open_ts, close_ts}) from a fresh live WebSocket book, or None:
        tickets use it before anything else (see "Ticket quotes"). `market_band_fn()` -> the market protection in
        dollars (a market buy's limit = the ask it is sized at + this, at most MARKET_CEILING) or None (up to
        MARKET_CEILING), read at every check; a ValueError refuses market buys (`band_unreadable`). Without it a
        market buy's limit is MARKET_CEILING. `committed_today_fn(env, now)` -> the one daily total of every service
        (KD7: event stakes and perps collateral, play_limits.CommittedToday.total; this service's own included) that
        the per-day limit is checked against instead of this service's own day; a failure refuses
        (`limits_unreadable`). Without it the per-day check uses this service's own day, as before."""
        self.modes: dict[str, str] = dict(modes or MODES)
        if not set(self.modes) <= set(MODES) or "shadow" not in self.modes:
            raise ValueError("modes must be a subset of sandbox/shadow/prod that includes shadow")
        self.venue = str(venue)
        self.mode_labels = dict(MODE_LABELS) if modes is None else venue_mode_labels(self.venue, self.modes)
        self._fee_fn = fee_fn or kalshi_fee
        self._fallback = "sandbox" if "sandbox" in self.modes else "shadow"  # where a credential problem lands
        self.repo = repo
        self.strategy = str(strategy)
        if self.strategy not in STRATEGY_CUTOFFS:  # a wiring error, never some default profile
            raise ValueError(f"strategy {self.strategy!r} is not a phone strategy ({', '.join(STRATEGY_CUTOFFS)})")
        self._known_order_ids_fn = known_order_ids_fn
        self._index_price_fn = index_price_fn
        self._conn_ok = True  # connectivity guard state (note_connectivity)
        self._own_fill_ms: dict[str, int] = {}  # "env:ticker" -> our last fill there (manual-exit detector)
        self._ext_next: dict[str, int] = {}  # env -> next external-position check
        self._short_since: dict[str, int] = {}  # "env:ticker" -> first seen holding less than our brackets
        self._ext_seen: dict[str, float] = {}  # "env:ticker" -> extra quantity already audited
        self._balance_fn = balance_fn  # () -> (venue cash $, read at ms): the production cash backstop
        self._limits_fn = limits_fn
        # Agent mode: `agent_gate_fn(strategy, ticket, now, exit_order)` is the session's own check for an order an agent
        # sends (application/agent_sessions.py); `agent_takeover_fn(strategy, bracket, now)` is told when a person
        # changes or sells a position an agent session opened (R11). Without them every agent order is refused.
        self._agent_gate_fn = agent_gate_fn
        self._agent_takeover_fn = agent_takeover_fn
        self._market_band_fn = market_band_fn
        self._committed_today_fn = committed_today_fn
        self.cash_backstop = bool(cash_backstop)
        self._market_quote_fn = market_quote_fn
        self.streams: dict[str, TradingStream] = dict(streams or {})
        self._stream_up: dict[str, bool] = {}
        self._stream_quotes: dict[str, tuple[dict, int]] = {}  # "env:ticker" -> (quote, received at)
        self.gateways = {e: gateways.get(e) for e in self.modes.values()}
        self.config = config
        self.gate = OrderGate(self)
        self.audit_observers: list[Callable[[dict], None]] = []
        self._paused_fn = paused_fn
        self._notify = notify_fn
        self._id = id_fn
        self._token = token_fn
        self._lock = asyncio.Lock()
        self._tp_fills_from = "none"  # how the last cancelled take profit's fills were learnt (_cancel_tp)
        self._tickets: dict[str, dict] = {}
        self._last_seen: int | None = None
        self._ready: dict[str, bool] = {e: False for e in self.gateways}
        self._external: dict[str, dict] = {e: {"positions": [], "orders": []} for e in self.gateways}
        self._brackets: dict[str, dict] = {}
        self._last_poll: dict[str, int] = {}
        self._reconcile_try: dict[str, int] = {}
        self._recent: deque = deque(maxlen=200)
        self._quotes: dict[str, dict] = {}  # "env:ticker" -> the last quote the monitor or a ticket saw
        self._killed = bool(self._setting("killed", {}).get("on"))
        fallback = self._fallback
        saved = self._setting("mode", {}).get("mode")
        mode = saved or default_mode or fallback
        self.mode = mode if mode in self.modes else fallback
        if mode != self.mode:  # still the safe non-production mode, but never silently
            log.warning("trading %s: stored mode %r is not one of %s; using %s", self.strategy, mode,
                        sorted(self.modes), self.mode)
            self._audit("mode_invalid", now, code="mode_invalid",
                        reason=f"The stored mode {mode!r} is not a mode of this service; using {self.mode}.",
                        detail={"stored": mode, "using": self.mode})
        if saved is None and default_mode is not None:
            self._put_setting("mode", {"mode": self.mode}, now)
        if self.mode == "prod" and not self._prod_configured():
            self._set_mode(self._fallback, now, "Production credentials are missing or trading is disabled; "
                                                f"switched back to {self._fallback}.")
        self._settle_next: dict[str, int] = {}  # bracket id -> next result read (settling brackets)
        self._settle_tries: dict[str, int] = {}  # bracket id -> result reads so far
        self._settle_last: dict[str, str] = {}  # bracket id -> what the last result read said (for the warning)
        self._settle_overdue: set[str] = set()  # bracket ids whose "settling too long" audit is written
        self._proof_next: dict[str, int] = {}  # bracket id -> next lost-order proof check (order_resolution)
        self._unresolved: dict[str, dict] = {}  # bracket id -> why its lost entry is still unprovable (warnings)
        for b in self._select(BRACKETS, filters=[("state", "in", WATCHED_STATES)]):
            self._brackets[b["id"]] = b

    # -- persistence scoped to this strategy, settings, audit -------------------------------------------
    def _select(self, table: str, where: dict | None = None, **kw: Any) -> list[dict]:
        """repo.select over this strategy's rows only (the trading_* tables with a `strategy` column)."""
        if table in STRATEGY_TABLES:
            where = {**(where or {}), "strategy": self.strategy}
        return self.repo.select(table, where, **kw)

    def _insert(self, table: str, rows: list[dict], **kw: Any) -> Any:
        """repo.insert stamping this strategy on every row of the trading_* tables with a `strategy` column."""
        if table in STRATEGY_TABLES:
            for r in rows:
                r["strategy"] = self.strategy
        return self.repo.insert(table, rows, **kw)

    def _skey(self, key: str) -> str:
        """Settings key for this strategy ("you.mode"); the kill switch is shared ("killed")."""
        if key in SHARED_SETTINGS:
            return key
        return f"{self.strategy}.{key}"

    def _setting(self, key: str, default: Any) -> Any:
        rows = self._select(SETTINGS, {"key": self._skey(key)})
        return rows[0]["value"] if rows else default

    def _put_setting(self, key: str, value: Any, now: int) -> None:
        self._insert(SETTINGS, [{"key": self._skey(key), "value": value, "updated_ts": now}], ignore_conflicts=False)

    def _fee(self, contracts: float, price: float, rate: float) -> float:
        return self._fee_fn(contracts, price, rate)

    def min_left_ms(self) -> int:
        """Entries need more than this left before the close: the strategy's cutoff, else the configured value
        (2 minutes); never 5 s or less."""
        v = STRATEGY_CUTOFFS[self.strategy]
        return max(MIN_LEFT_FLOOR_MS, int(self.config.min_left_ms if v is None else v))

    def _audit(self, event: str, now: int, *, bracket: dict | None = None, code: str | None = None,
               reason: str | None = None, detail: dict | None = None, env: str | None = None) -> dict:
        row = {"ts": now, "event": event, "env": env or (bracket or {}).get("env") or self.modes[self.mode],
               "bracket_id": (bracket or {}).get("id"), "session_id": agent_session(), "code": code, "reason": reason,
               "detail": detail, "actor": actor()}
        self._insert(AUDIT, [row])
        self._recent.appendleft(row)
        log.info("trading %s %s %s", event, code or "", reason or "")
        for observe in self.audit_observers:  # the composition root's telemetry (e.g. stream flaps); never raises
            try:
                observe(row)
            except Exception:
                log.debug("trading audit observer failed", exc_info=True)
        if self._notify:
            try:
                self._notify(row)
            except Exception:
                log.warning("trading notify failed", exc_info=True)
        return row

    def _refuse(self, r: GateResult, now: int, detail: dict | None = None) -> dict:
        self._audit("refused", now, code=r.code, reason=r.reason, detail=detail)
        return {"ok": False, "code": r.code, "reason": r.reason}

    def _save(self, b: dict, now: int) -> None:
        b["updated_ts"] = now
        self._insert(BRACKETS, [b], ignore_conflicts=False)
        if b["state"] in WATCHED_STATES:
            self._brackets[b["id"]] = b
        else:
            self._brackets.pop(b["id"], None)
            self._settle_next.pop(b["id"], None)
            self._settle_tries.pop(b["id"], None)
            self._settle_last.pop(b["id"], None)

    def _save_order(self, row: dict, now: int) -> None:
        row["updated_ts"] = now
        self._insert(ORDERS, [row], ignore_conflicts=False)

    # -- mode, presence, kill switch ----------------------------------------------------------------
    def mode_of(self, env: str | None) -> str | None:
        """The mode whose environment is `env` (a bracket's own), or None."""
        return next((m for m, e in self.modes.items() if e == env), None)

    def _prod_configured(self) -> bool:
        return bool(self.config.enabled and self.gateways.get(self.modes.get("prod", "")) is not None)

    def _set_mode(self, mode: str, now: int, reason: str) -> None:
        self.mode = mode
        self._put_setting("mode", {"mode": mode}, now)
        self._audit("mode", now, code=mode, reason=reason, env=self.modes[mode])

    async def set_mode(self, mode: str, now: int, confirm: str | None = None) -> dict:
        if mode not in self.modes:
            return {"ok": False, "code": "invalid", "reason": f"mode must be {', '.join(self.modes)}"}
        async with self._lock:
            if mode == "prod":
                if (confirm or "").strip() != PROD_CONFIRM:
                    return self._refuse(GateResult(False, "confirm", f'Type exactly "{PROD_CONFIRM}" to switch '
                                                                     "to production."), now)
                if not self._prod_configured():
                    return self._refuse(GateResult(False, "not_configured", "Production trading is not configured: "
                                                   "turn trading on and set the production trading key."), now)
            self._set_mode(mode, now, f"Switched to {mode}.")
            self._ready[self.modes[mode]] = False
        await self.reconcile(self.modes[mode], now)
        return {"ok": True, **self.mode_view(now)}

    def heartbeat(self, now: int, visible: bool = True) -> dict:
        if visible:
            self._last_seen = now
        return {"present": self.present(now), "last_seen": self._last_seen,
                "timeout_s": self.config.presence_timeout_s}

    def present(self, now: int) -> bool:
        return self._last_seen is not None and 0 <= now - self._last_seen <= self.config.presence_timeout_s * 1000

    def killed(self) -> bool:
        self._killed = bool(self._setting("killed", {}).get("on"))  # shared by every strategy's service
        paused = False
        if self._paused_fn is not None:
            try:
                paused = bool(self._paused_fn())
            except Exception:
                paused = True
        return self._killed or paused

    async def kill(self, now: int) -> dict:
        """Kill switch: persisted; no new entries in any mode. Open positions keep their auto-sell and stop."""
        self._killed = True
        self._put_setting("killed", {"on": True}, now)
        self._audit("kill", now, reason="Kill switch on: no new orders.")
        return {"killed": True}

    def resume(self, now: int) -> dict:
        self._killed = False
        self._put_setting("killed", {"on": False}, now)
        self._audit("resume", now, reason="Kill switch off.")
        return {"killed": self.killed()}

    # -- agent mode ---------------------------------------------------------------------------------------
    def agent_check(self, ticket: dict, now: int, exit_order: bool) -> GateResult:
        """The acting agent session's own check (the gate's step 2 for agent orders). No session wiring refuses."""
        if self._agent_gate_fn is None:
            return GateResult(False, "no_agent_session", "Agent trading is not available here; nothing was sent.")
        return self._agent_gate_fn(self.strategy, ticket, now, exit_order)

    def _agent_adjust(self, b: dict, now: int) -> dict | None:
        """Before a take-profit or stop change: an agent may change only its own positions (the session's check with
        the bracket as an exit-like ticket); a person changing an agent's position takes it over (R11). None = go on."""
        if agent_session() is not None:
            r = self.agent_check({"kind": "adjust", "env": b["env"], "ticker": b["ticker"], "side": b["side"],
                                  "bracket_id": b["id"], "max_cost": 0.0}, now, True)
            return None if r.ok else {"ok": False, "code": r.code, "reason": r.reason}
        self._note_takeover(b, now)
        return None

    def _note_takeover(self, b: dict, now: int) -> None:
        if agent_session() is None and b.get("session_id") and self._agent_takeover_fn is not None:
            try:
                self._agent_takeover_fn(self.strategy, b, now)
            except Exception:
                log.warning("agent takeover note failed", exc_info=True)

    def mode_ready(self, mode: str, now: int, *, for_exit: bool = False) -> GateResult:
        env = self.modes[mode]
        if mode == "prod":
            if not self.config.enabled:
                return GateResult(False, "disabled", "Production trading is turned off in settings.")
            if self.gateways.get(env) is None:
                return GateResult(False, "not_configured", "No production trading key is configured.")
        elif mode == "sandbox" and self.gateways.get(env) is None:
            return GateResult(False, "not_configured", "No demo (sandbox) trading key is configured.")
        elif self.gateways.get(env) is None:
            return GateResult(False, "not_configured", "Shadow trading is not available.")
        if not self._ready.get(env):
            return GateResult(False, "not_reconciled", "Not yet reconciled with the exchange after start-up; "
                                                       "nothing can be sent until it is.")
        if for_exit:
            return OK
        if self.killed():
            return GateResult(False, "killed", "The kill switch is on: no new orders.")
        if self._external[env]["orders"]:
            return GateResult(False, "unknown_orders", "There are resting orders on the exchange that this app did "
                                                       f"not place; cancel them on {self.venue} first.")
        return OK

    # -- the live index (index stops) ---------------------------------------------------------------
    def index_price(self, now: int) -> float | None:
        """The live index price, or None when there is no source, it fails, or its print is older than
        INDEX_STALE_MS."""
        if self._index_price_fn is None:
            return None
        try:
            got = self._index_price_fn()
            if not got:
                return None
            ts, price = int(got[0]), got[1]
            if price is None or not float(price) > 0 or now - ts > INDEX_STALE_MS:
                return None
            return float(price)
        except Exception:
            return None

    def _index_stop_hit(self, b: dict, now: int) -> tuple[bool, float | None]:
        """(the bracket's index stop is hit, the index used). False when it has none or the index is unusable."""
        below, above = b.get("stop_index_below"), b.get("stop_index_above")
        if below is None and above is None:
            return False, None
        px = self.index_price(now)
        if px is None:
            return False, None
        return (below is not None and px <= below + EPS) or (above is not None and px >= above - EPS), px

    def note_connectivity(self, ok: bool, now: int, reason: str) -> None:
        """Connectivity guard (bootstrap watchdog): records when this service's live feed or the BTC index has been
        stale for a while, and when it is back, as audit events `connectivity_lost` / `connectivity_restored`.
        New entries are already refused on stale quotes or index; this makes an outage visible, with whether a
        position was exposed (its stop cannot act on stale data)."""
        if ok == self._conn_ok:
            return
        self._conn_ok = ok
        exposed = [b["ticker"] for b in self.live_brackets() if float(b.get("held") or 0) > 0]
        event = "connectivity_restored" if ok else "connectivity_lost"
        text = reason + (f" Open position(s) exposed: {', '.join(exposed)}." if exposed and not ok else "")
        (log.info if ok else log.warning)("%s %s %s", self.strategy, event, text)
        self._audit(event, now, code="connectivity", reason=text[:300], detail={"exposed": exposed})

    def _watched(self) -> list[dict]:
        """The watched brackets as a snapshot: `list(dict.values())` copies in one C call under the GIL, so a sync
        route's worker thread never iterates the dict while the event loop adds or drops a bracket (a Python-level
        loop over the live dict could raise "dictionary changed size during iteration")."""
        return list(self._brackets.values())

    def live_brackets(self) -> list[dict]:
        """This service's brackets that are still live (sending, unknown, open or stopping). Settling ones (window
        closed, result pending) are not: see `settling_brackets`."""
        return [b for b in self._watched() if b["state"] in LIVE_STATES]

    def settling_brackets(self) -> list[dict]:
        """Brackets whose window closed while holding, waiting for the market's official result."""
        return [b for b in self._watched() if b["state"] == SETTLING]

    # -- exposure and limits ------------------------------------------------------------------------
    @staticmethod
    def committed(b: dict) -> float:
        """Dollars a bracket has committed: actual entry cost once known, the approved maximum while unknown."""
        if b.get("filled") and b.get("entry_cost") is not None:
            return float(b["entry_cost"])
        if b["state"] in ("sending", "unknown"):
            return float(b["max_cost"])
        return 0.0

    def _env_brackets(self, env: str, since: int) -> list[dict]:
        rows = {r["id"]: r for r in self._select(BRACKETS, {"env": env}, filters=[("created_ts", ">=", since)])}
        rows.update({v["id"]: v for v in self._watched() if v["env"] == env and v["created_ts"] >= since})
        return list(rows.values())

    def exposure(self, env: str, now: int, ticker: str | None = None) -> dict:
        """Committed dollars today (the user's day, `day_tz`: domain/days.py) and in one window: the market `ticker`'s,
        or (no ticker) the window containing `now`. A window can start before local midnight (a zone a half hour off
        UTC, a 1 h window), so its brackets are read from the earlier of the two starts."""
        day0 = self.day_start(now)
        w = self.config.window_ms
        window_end = now - now % w + w
        rows = self._env_brackets(env, min(day0, window_end - w))
        day = [b for b in rows if b["created_ts"] >= day0]
        live = [b for b in self.live_brackets() if b["env"] == env]
        in_window = (lambda b: b["ticker"] == ticker) if ticker else (
            lambda b: b.get("window_end") is not None and window_end - w < b["window_end"] <= window_end)
        return {"day": round(sum(self.committed(b) for b in day), 4),
                "window": round(sum(self.committed(b) for b in rows if in_window(b)), 4),
                "window_end": None if ticker else window_end,
                "open_positions": len(live) + len(self._external[env]["positions"])}

    def limits(self) -> tuple[dict | None, GateResult]:
        """The dollar limits right now: {"per_trade", "per_window", "per_day"} ($, None = no limit). With `limits_fn`
        it is read now (a change applies to the next check, no restart); otherwise the config's. (None, refusal) when
        the setting cannot be read: never treated as no limit or as some number."""
        if self._limits_fn is None:
            cfg = self.config
            return {"per_trade": cfg.max_stake_per_trade, "per_window": cfg.max_dollars_per_window,
                    "per_day": cfg.max_dollars_per_day}, OK
        try:
            got = self._limits_fn()
            lim = {k: (None if got[k] is None else float(got[k])) for k in ("per_trade", "per_window", "per_day")}
        except (ValueError, KeyError, TypeError) as e:
            return None, GateResult(False, "limits_unreadable", f"Trade limits setting unreadable: {e}. Save your "
                                                                "limits again; nothing was sent.")
        return lim, OK

    def market_limit(self, ask: float) -> tuple[float | None, GateResult]:
        """A market buy's limit price for an order sized at `ask`: ask + the market protection (user setting), at most
        MARKET_CEILING; MARKET_CEILING with no protection. (None, refusal) when the setting cannot be read."""
        if self._market_band_fn is None:
            return MARKET_CEILING, OK
        try:
            band = self._market_band_fn()
            band = None if band is None else float(band)
        except (ValueError, TypeError) as e:
            return None, GateResult(False, "band_unreadable", f"Market protection setting unreadable: {e}. Save it "
                                                               "again in settings; nothing was sent.")
        if band is None:
            return MARKET_CEILING, OK
        return round(min(MARKET_CEILING, round(float(ask), 2) + band), 2), OK

    def cash_cap(self, env: str, now: int | None) -> tuple[float | None, GateResult]:
        """The venue cash an entry in `env` must fit: (cash, OK) in production with the backstop on; (None, OK) when it
        does not apply (no backstop, or shadow / sandbox, where no money moves); (None, refusal) when the balance
        reading is missing or older than BALANCE_MAX_AGE_MS (never assumed to fit)."""
        if not self.cash_backstop or env != self.modes.get("prod"):
            return None, OK
        bal, at = self._balance_fn() if self._balance_fn else (None, None)
        if bal is None or at is None or now is None or now - int(at) > BALANCE_MAX_AGE_MS:
            return None, GateResult(False, "balance_unknown", f"Balance unknown: can't check the order fits your "
                                                              f"{self.venue} cash; nothing was sent.")
        return float(bal), OK

    def limits_check(self, ticket: dict, now: int) -> GateResult:
        cfg = self.config
        cost = ticket["max_cost"]
        lim, r = self.limits()
        if not r.ok:
            return r
        if lim["per_trade"] is not None and ticket["stake"] > lim["per_trade"] + EPS:
            return GateResult(False, "over_trade_limit", f"{_money(ticket['stake'])} is over the "
                                                         f"{_money(lim['per_trade'])} per-trade limit.")
        if ticket.get("price_mode") == "market":
            r = self._market_caps(ticket, lim)
            if not r.ok:
                return r
        cash, r = self.cash_cap(ticket["env"], now)
        if not r.ok:
            return r
        if cash is not None and cost > cash + EPS:
            how = (" at worst (every contract at the ceiling, with the fee)" if ticket.get("price_mode") == "market"
                   else " with the fee")
            return GateResult(False, "over_cash", f"This order could cost {_money(cost)}{how}, more than your "
                                                  f"{_money(cash)} {self.venue} cash. Lower the amount; nothing was "
                                                  "sent.")
        ex = self.exposure(ticket["env"], now, ticket["ticker"])
        if ex["open_positions"] >= cfg.max_open_positions:
            return GateResult(False, "max_open", f"Already {ex['open_positions']} open position(s); the limit is "
                                                 f"{cfg.max_open_positions}.")
        if lim["per_window"] is not None and ex["window"] + cost > lim["per_window"] + EPS:
            return GateResult(False, "over_window_limit", f"This window already has {_money(ex['window'])} in; "
                              f"{_money(cost)} more would pass the {_money(lim['per_window'])} window limit.")
        if lim["per_day"] is not None and self._committed_today_fn is not None:  # KD7: one total with perps
            try:
                day = float(self._committed_today_fn(ticket["env"], now))
            except Exception as e:
                return GateResult(False, "limits_unreadable", f"Today's total across event contracts and perps could "
                                                              f"not be read ({type(e).__name__}); nothing was sent.")
            if day + cost > lim["per_day"] + EPS:
                return GateResult(False, "over_day_limit", f"Today already has {_money(day)} in across event "
                                  f"contracts and perps; {_money(cost)} more would pass the "
                                  f"{_money(lim['per_day'])} daily limit.")
        elif lim["per_day"] is not None and ex["day"] + cost > lim["per_day"] + EPS:
            return GateResult(False, "over_day_limit", f"Today already has {_money(ex['day'])} in; {_money(cost)} "
                              f"more would pass the {_money(lim['per_day'])} daily limit.")
        return OK

    def committed_by_env(self, now: int) -> dict[str, float]:
        """Dollars this service has committed today (the user's day) in each of its envs: a CommittedToday source
        (KD7)."""
        return {env: self.exposure(env, now)["day"] for env in self.gateways}

    def _market_caps(self, ticket: dict, lim: dict) -> GateResult:
        """A market buy's WORST case (every contract at the ceiling, plus fee: its `max_cost`) must fit the per-trade
        limit (when there is one), and its notional (contracts x ceiling) the gateway's own order cap. Refused, never
        shrunk."""
        cost = ticket["max_cost"]
        if lim["per_trade"] is not None and cost > lim["per_trade"] + EPS:
            return GateResult(False, "over_trade_limit",
                              f"At the market this could cost up to {_money(cost)} (every contract at "
                              f"{_cents(ticket['limit_price'])} with the fee), over the "
                              f"{_money(lim['per_trade'])} per-trade limit. Lower the amount or buy with a "
                              "limit; nothing was sent.")
        cap = getattr(self.gateways.get(ticket["env"]), "max_order_cost", None)
        if cap is None:
            return GateResult(False, "no_order_cap", f"The {self.venue} connection does not state its order cap, so a "
                                                     "market buy cannot be checked; nothing was sent.")
        notional = ticket["contracts"] * ticket["limit_price"]
        if notional > float(cap) + EPS:
            return GateResult(False, "over_order_cap",
                              f"At the market this order could reach {_money(notional)}, over the "
                              f"{_money(float(cap))} cap on a single order. Lower the amount; nothing was sent.")
        return OK

    def size(self, stake: float, price: float) -> float:
        """Contracts whose cost at `price` incl. this venue's fee fits `stake` (2 dp; whole contracts where the
        venue trades whole contracts). 0 when nothing can be bought."""
        cfg = self.config
        contracts = position_for_stake(stake, price, cfg.fee_rate)
        if cfg.whole_contracts:  # whole contracts whose cost incl. this venue's fee fits the stake
            contracts = float(math.floor(contracts + EPS))
            while contracts > 0 and contracts * price + self._fee(contracts, price, cfg.fee_rate) > stake + EPS:
                contracts -= 1.0
        return contracts

    def market_terms(self, stake: float, ask: float | None, now: int | None = None) -> dict | None:
        """A market buy of `stake` with the ask at `ask`: contracts sized at the ask, the cost if it all fills there,
        and the worst case (every contract at its limit, `market_limit`, plus fee; the cost of a contract incl. either venue's fee
        rises with its price, so the ceiling is the worst). `fits` = something is bought, the worst case is within
        the per-trade limit (if any) and the production cash at `now` (with the cash backstop on) and its notional
        within the current gateway's order cap (prepare checks these again, with the window / day limits); `why` = the
        first reason it does not fit, else None. None when there is no ask."""
        if ask is None or not 0 < ask < 1:
            return None
        rate, ask = self.config.fee_rate, round(float(ask), 2)
        n = self.size(stake, ask)
        top, r = self.market_limit(ask)
        ceiling = MARKET_CEILING if top is None else top
        worst = round(n * ceiling + self._fee(n, ceiling, rate), 4) if n > 0 else 0.0
        env = self.modes[self.mode]
        cap = getattr(self.gateways.get(env), "max_order_cost", None)
        lim, cash = None, None
        if r.ok:
            lim, r = self.limits()
        if r.ok:
            cash, r = self.cash_cap(env, now)
        if not r.ok:
            why = r.reason
        elif n <= 0:
            why = f"{_money(stake)} buys no contracts at the {_cents(ask)} ask."
        elif lim["per_trade"] is not None and worst > lim["per_trade"] + EPS:
            why = f"At worst this costs {_money(worst)}, over your {_money(lim['per_trade'])} per-trade limit."
        elif cash is not None and worst > cash + EPS:
            why = f"At worst this costs {_money(worst)}, more than your {_money(cash)} {self.venue} cash."
        elif cap is None or n * ceiling > float(cap) + EPS:
            why = "Over the single-order cap of this connection."
        else:
            why = None
        return {"price_mode": "market", "max_price": ceiling, "sized_at": ask, "contracts": n,
                "cost_at_ask": round(n * ask + self._fee(n, ask, rate), 4) if n > 0 else 0.0,
                "worst_case_cost": worst, "fits": why is None, "why": why}

    # -- tickets --------------------------------------------------------------------------------------
    async def _quote(self, env: str, ticker: str, now: int | None = None, why: str | None = None) -> dict | None:
        """The market's quote for a ticket or the monitor: the live WebSocket book when it is fresh (`now` given; see
        `_live_quote`), else the gateway's quote (REST for Kalshi). `why` names a ticket step ("prepare", "exit",
        "approve", "auto-sell"): its source is logged at INFO. The answer carries `source` ("ws" | "rest")."""
        gw = self.gateways.get(env)
        if gw is None:
            return None
        q = await self._live_quote(env, ticker, now) if now is not None else None
        if q is None:
            try:
                q = await gw.quote(ticker)
            except Exception as exc:
                log.warning("quote for %s failed: %s: %s", ticker, type(exc).__name__, str(exc)[:200])
                return None
            if q is not None and not q.get("source"):
                q = {**q, "source": "rest"}
        if why is not None:
            log.info("%s %s quote %s: %s", self.strategy, why, ticker, (q or {}).get("source") or "none")
        self._remember_quote(env, ticker, q)
        return q

    async def _live_quote(self, env: str, ticker: str, now: int) -> dict | None:
        """A full quote (prices + status/open_ts/close_ts) from a fresh WebSocket book, or None (-> REST). First the
        injected `market_quote_fn` (the production market-data stream), then this service's trading stream's prices
        with the gateway's cached market metadata (`market_meta`, which reads REST only when its cache is old)."""
        if self._market_quote_fn is not None:
            try:
                q = self._market_quote_fn(env, ticker, now)
            except Exception as exc:
                log.warning("live quote for %s failed: %s", ticker, type(exc).__name__)
                q = None
            if q and q.get("ticker") == ticker and q.get("close_ts"):
                return {**q, "source": q.get("source") or "ws"}
        s = self.stream_quote(env, ticker, now)
        meta_fn = getattr(self.gateways.get(env), "market_meta", None)
        if s is None or meta_fn is None:
            return None
        try:
            meta = await meta_fn(ticker)
        except Exception as exc:
            log.warning("market metadata for %s failed: %s", ticker, type(exc).__name__)
            return None
        return {**meta, **s, "ticker": ticker, "source": "ws"}

    def _remember_quote(self, env: str, ticker: str, q: dict | None) -> None:
        if q and q.get("ticker") == ticker:
            self._quotes[f"{env}:{ticker}"] = q
            if len(self._quotes) > 64:
                self._quotes.pop(next(iter(self._quotes)))

    def last_bid(self, b: dict) -> float | None:
        """The held side's bid from the last quote seen for the bracket's market on its exchange."""
        return _bid(b["side"], self._quotes.get(f"{b['env']}:{b['ticker']}"))

    def _issue(self, ticket: dict, now: int) -> dict:
        token = self._token()
        ticket["hash"] = ticket_hash(token, ticket)
        self._tickets[hashlib.sha256(token.encode()).hexdigest()] = ticket
        for k in [k for k, t in self._tickets.items() if now - t["created_ts"] > 60_000]:
            del self._tickets[k]
        self._audit("proposed", now, detail={k: v for k, v in ticket.items() if k != "hash"})
        self._sync_streams(now)
        return {"ok": True, "ticket": {k: v for k, v in ticket.items() if k != "hash"}, "token": token}

    async def prepare(self, proposal: dict, now: int) -> dict:
        """A ticket + single-use approval token for an entry bracket. Prices are the proposal's (the Coach card);
        in sandbox the entry is re-anchored to the demo exchange's ask, keeping the same take-profit and stop
        distances (demo books are independent of the real market)."""
        steps = _Steps()
        try:
            return await self._prepare(proposal, now, steps=steps)
        finally:
            steps.log(self.strategy, "prepare")

    async def preview(self, proposal: dict, now: int) -> dict:
        """The ticket `prepare` would build for `proposal` (re-priced now), with NO approval token: nothing can be
        approved from it (the agent's quote_entry). Instead of refusing at the order gate it says whether the gate
        would pass now (`would_send`, `gate`); a proposal refused before a ticket exists is refused as in prepare."""
        return await self._prepare(proposal, now, preview=True)

    async def _prepare(self, proposal: dict, now: int, steps: _Steps | None = None, *,
                       preview: bool = False) -> dict:
        """`price_mode` (explicit): "limit" = an IoC buy at `max_price` or less (sized at it; refused at approval when
        the ask is above it), "market" = an IoC buy at MARKET_CEILING with NO max_price (sized at the live ask on this
        mode's exchange; the ask moving is not a refusal; its worst case — every contract at the ceiling plus fee — is
        the ticket's max_cost and must fit the limits and the gateway's order cap). Absent = "limit", which then
        needs max_price (every caller before price_mode sent max_price); neither = refused.

        Why not a native notional order: Kalshi's V2 create order (POST /portfolio/events/orders, the only one we
        use; the legacy /portfolio/orders with `buy_max_cost` was retired in June 2026) requires count and price and
        has no notional field (kalshi_python_async 3.35.0 CreateOrderV2Request). Polymarket US documents
        ORDER_TYPE_MARKET with `cashOrderQty` + `slippageTolerance`, but not whether the fee is inside the amount,
        how the tolerance's reference price works for BUY_SHORT, or which time in force applies, and it has never
        been penny-tested here; so both venues use the same count + ceiling IoC, which our gateways' guards (whole
        contracts, cent grid, notional cap, lost-answer resolution) already understand."""
        cfg, env = self.config, self.modes[self.mode]
        price_mode = proposal.get("price_mode")
        if price_mode is None and proposal.get("max_price") is not None:
            price_mode = "limit"  # every caller before price_mode existed sends max_price: today's limit order
        if price_mode not in PRICE_MODES:
            return {"ok": False, "code": "invalid", "reason": "price_mode must be \"limit\" (with max_price) or "
                                                              "\"market\"."}
        market_buy = price_mode == "market"
        ride = proposal.get("ride_to_settlement", False)  # explicit: no take-profit order at all
        if not isinstance(ride, bool):
            return {"ok": False, "code": "invalid", "reason": "ride_to_settlement must be true or false."}
        if ride and proposal.get("take_profit") is not None:
            return {"ok": False, "code": "invalid", "reason": "A ride-to-settlement entry places no take-profit order, "
                                                              "so it has no take_profit; send one or the other."}
        if market_buy and proposal.get("max_price") is not None:
            return {"ok": False, "code": "invalid", "reason": "A market buy has no max_price (it fills up to "
                                                              f"{_cents(MARKET_CEILING)}); send one or the other."}
        try:
            side = str(proposal["side"]).upper()
            ticker = str(proposal["ticker"])
            stake = float(proposal["stake"])
            limit = MARKET_CEILING if market_buy else round(float(proposal["max_price"]), 2)
            # ride: no take-profit order; the NOT NULL column keeps the nominal 99c and the bracket is no_tp
            tp = MARKET_CEILING if ride else round(float(proposal["take_profit"]), 2)
            stop = None if proposal.get("stop") is None else round(float(proposal["stop"]), 2)
            below, above = proposal.get("stop_index_below"), proposal.get("stop_index_above")
            below = None if below is None else round(float(below), 2)
            above = None if above is None else round(float(above), 2)
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "code": "invalid", "reason": "The proposal is missing side, ticker, stake, "
                                                              "max_price or take_profit."}
        if side not in ("UP", "DOWN") or stake <= 0:
            return {"ok": False, "code": "invalid", "reason": "side must be UP or DOWN and stake positive."}
        index_stop = below is not None or above is not None
        if index_stop and not ((side == "UP" and above is None and below is not None and below > 0)
                               or (side == "DOWN" and below is None and above is not None and above > 0)):
            return {"ok": False, "code": "invalid", "reason": "An Up bracket's index stop is a level below "
                                                              "(stop_index_below), a Down bracket's a level above."}
        if not ticker.startswith(cfg.series + "-"):
            return self._refuse(GateResult(False, "wrong_series", f"Only {cfg.series} markets can be traded."), now)
        quote = await self._quote(env, ticker, now, "prepare")
        if steps is not None:
            steps.mark("quote", (quote or {}).get("source"))
        repriced = False
        sized_at = limit  # the price the contracts are sized at: the limit, or a market buy's live ask
        if market_buy:
            ask = _ask(side, quote)
            if ask is None:  # nothing to size at: the gate says why (mode, presence, no quote, no offer, ...)
                probe = {"kind": "entry", "env": env, "ticker": ticker, "side": side, "price_mode": price_mode,
                         "limit_price": limit, "stop": stop}
                r = self.gate.check(probe, now, quote, token_error=OK)
                r = r if not r.ok else GateResult(False, "no_offer", "Nobody is offering this contract right now; "
                                                                     "nothing was sent.")
                return self._refuse(r, now, {"ticker": ticker, "side": side, "stake": stake})
            sized_at = round(ask, 2)
            top, r = self.market_limit(sized_at)
            if not r.ok:
                return self._refuse(r, now, {"ticker": ticker, "side": side, "stake": stake})
            limit = top
        elif self.mode == "sandbox" and _ask(side, quote) is not None:
            demo_ask = round(_ask(side, quote), 2)
            if demo_ask != limit:
                if not ride:
                    tp = round(min(0.99, demo_ask + (tp - limit)), 2)
                if stop is not None:
                    stop = round(max(0.01, demo_ask - (limit - stop)), 2)
                limit, sized_at, repriced = demo_ask, demo_ask, True
        if not ((stop is None or 0 < stop < sized_at) and 0 < sized_at < 1 and (ride or sized_at < tp < 1)):
            return {"ok": False, "code": "invalid", "reason": "Prices must satisfy 0 < stop < entry < take profit < 1."}
        contracts = self.size(stake, sized_at)
        if contracts < 0.01:
            return {"ok": False, "code": "invalid", "reason": f"{_money(stake)} buys no contracts at "
                                                              f"{_cents(sized_at)}."}
        # max_cost = what the order may spend: every contract at its limit plus fee (a market buy's worst case)
        max_cost = round(contracts * limit + self._fee(contracts, limit, cfg.fee_rate), 4)
        cost_at_ask = round(contracts * sized_at + self._fee(contracts, sized_at, cfg.fee_rate), 4)
        c = "Up" if _is_up(side) else "Down"
        stops = [f"at {_cents(stop)}"] if stop is not None else []
        if below is not None:
            stops.append(f"when BTC falls to {_money(below)} or lower")
        if above is not None:
            stops.append(f"when BTC rises to {_money(above)} or higher")
        exits = ("No take-profit order: it rides to settlement ($1 a contract if it wins, $0 if it loses); "
                 if ride else f"Take profit at {_cents(tp)}; ") + (
            f"stop {' or '.join(stops)} (sold at the bid, at most {_cents(cfg.stop_slippage)} below it)."
            if stops else "no automatic stop (you sell when you choose).")
        ticket = {"ticket_id": self._id(), "kind": "entry", "mode": self.mode, "env": env, "ticker": ticker,
                  "side": side, "contract_side": _contract_side(side), "stake": stake, "contracts": contracts,
                  "limit_price": limit, "take_profit": tp, "stop": stop, "max_cost": max_cost,
                  "no_tp": True if ride else None, "price_mode": price_mode, "sized_at": sized_at, "cost_at_sized": cost_at_ask,
                  # the most this order can buy and cost, stated for the screen ("up to 99c (market)")
                  "worst_case": {"max_price": limit, "contracts": contracts, "cost": max_cost},
                  "stop_index_below": below, "stop_index_above": above,
                  "window_end": (quote or {}).get("close_ts") or proposal.get("window_end"),
                  "fair_up": proposal.get("fair_up"),
                  "created_ts": now, "expires_ts": now + cfg.approval_ttl_s * 1000, "bracket_id": None,
                  "repriced_for_demo": repriced, "mode_label": self.mode_labels[self.mode], "strategy": self.strategy,
                  "sentence": ((f"Buy {c} at the market: {contracts:.2f} contracts, about {_money(cost_at_ask)} at the "
                                f"{_cents(sized_at)} ask with {self.venue}'s fee. It takes the best offers up to "
                                f"{_cents(limit)} (market), so if the price jumps first it can cost up to "
                                f"{_money(max_cost)}; anything not filled at once is cancelled. ")
                               if market_buy else
                               (f"Buy {_money(stake)} of {c} ({contracts:.2f} contracts) at {_cents(limit)} or less, "
                                f"up to {_money(max_cost)} with {self.venue}'s fee. ")) + exits}
        r = self.gate.check(ticket, now, quote, token_error=OK)
        if steps is not None:
            steps.mark("gate")
        if preview:
            return {"ok": True, "preview": True, "ticket": ticket, "would_send": r.ok,
                    "gate": {"code": r.code, "reason": r.reason or None}}
        if not r.ok:
            return self._refuse(r, now, {"ticker": ticker, "side": side, "stake": stake})
        out = self._issue(ticket, now)
        if steps is not None:
            steps.mark("issue")
        return out

    async def prepare_exit(self, bracket_id: str, now: int) -> dict:
        """A Yes/No card for a discretionary exit (R13): sell everything held at the current bid or better. A bracket
        its take profit, stop or a manual sale already closed answers `already_closed` (a stop still selling:
        `already_closing`) with what happened, from its recorded fills."""
        steps = _Steps()
        try:
            return await self._prepare_exit(bracket_id, now, steps)
        finally:
            steps.log(self.strategy, "prepare_exit")

    async def _prepare_exit(self, bracket_id: str, now: int, steps: _Steps) -> dict:
        b = self._brackets.get(bracket_id)
        done = self._exit_not_needed(bracket_id, b, now)
        if done is not None:
            return done
        if not b or b["state"] != "open" or b["held"] <= 0:
            return {"ok": False, "code": "not_found", "reason": "There is no open position to exit."}
        quote = await self._quote(b["env"], b["ticker"], now, "exit")
        steps.mark("quote", (quote or {}).get("source"))
        bid = _bid(b["side"], quote)
        if bid is None:
            return {"ok": False, "code": "no_bid", "reason": "Nobody is bidding for this contract right now."}
        mode = self.mode_of(b["env"]) or self.mode  # the position's own mode, whatever the current one is
        ticket = {"ticket_id": self._id(), "kind": "exit", "mode": mode, "env": b["env"], "ticker": b["ticker"],
                  "side": b["side"], "contract_side": _contract_side(b["side"]), "stake": b["stake"],
                  "contracts": b["held"], "limit_price": round(bid, 2), "take_profit": b["take_profit"],
                  "stop": b["stop"], "max_cost": 0.0, "bracket_id": b["id"], "created_ts": now,
                  "expires_ts": now + self.config.approval_ttl_s * 1000, "mode_label": self.mode_labels[mode],
                  "sentence": f"Sell {b['held']:.2f} contracts now at {_cents(bid)} or better?"}
        out = self._issue(ticket, now)
        steps.mark("issue")
        return out

    def _exit_not_needed(self, bracket_id: str, b: dict | None, now: int) -> dict | None:
        """None when the bracket is live and can be sold by hand. Otherwise the answer to a sell request: closed by
        its take profit, stop or a manual sale (`already_closed`, with the fills as recorded) or its stop already
        selling (`already_closing`). Nothing is sent. An id this service never had is left to the caller
        (not_found)."""
        if b is None:
            rows = self._select(BRACKETS, {"id": bracket_id}, limit=1)
            b = rows[0] if rows else None
        if b is None:
            return None
        if b["state"] == SETTLING:
            reason = (f"This window has closed: the position is waiting for {self.venue}'s official result (it pays $1 "
                      "a contract or $0) and cannot be sold; nothing was sent.")
            self._audit("exit_not_needed", now, bracket=b, code="settling", reason=reason)
            return {"ok": False, "code": "settling", "reason": reason, "bracket": self.bracket_view(b)}
        if b["state"] == "stopping":
            reason = "Your stop already fired; it is selling your contracts at the bid now, so nothing else was sent."
            self._audit("exit_not_needed", now, bracket=b, code="already_closing", reason=reason)
            return {"ok": False, "code": "already_closing", "reason": reason, "bracket": self.bracket_view(b)}
        if b["state"] != "closed" or not (b.get("filled") or 0) > 0:
            return None
        reason = closed_sentence(b)
        self._audit("exit_not_needed", now, bracket=b, code="already_closed", reason=reason)
        return {"ok": False, "code": "already_closed", "reason": reason, "bracket": self.bracket_view(b)}

    async def set_take_profit(self, bracket_id: str, price: float, now: int) -> dict:
        """Move an open bracket's resting take profit (the phone's "auto-sell"; 0.99 = ride to settlement). The tap
        is the approval. Cancel/replace through the monitor's own TP path: `_cancel_tp` (learns any partial fill of
        the old order at the old price first), then the new price is saved and audited, then `_place_tp`. A cancel
        that cannot be confirmed leaves the bracket exactly as it was. Under the lock, so the stop and the monitor
        never see a half-changed bracket; a failed placement is retried by the monitor like any TP, stop still on."""
        async with self._lock:
            b = self._brackets.get(bracket_id)
            if not b or b["state"] != "open" or b["held"] <= 0:
                return {"ok": False, "code": "not_found", "reason": "There is no open position to change."}
            if not self._ready.get(b["env"]):
                return {"ok": False, "code": "not_ready", "reason": "The exchange connection is not ready; nothing "
                                                                    "was changed."}
            mine = self._agent_adjust(b, now)
            if mine is not None:
                return mine
            try:
                price = round(float(price), 2)
            except (TypeError, ValueError):
                price = -1.0
            entry = b.get("entry_avg")
            if not 0.01 < price <= 0.99 or entry is None or price <= entry + EPS:
                return {"ok": False, "code": "invalid", "reason": "The auto-sell must be above your entry price "
                                                                  "and at most 99¢."}
            old = b["take_profit"]
            if abs(price - old) < EPS:
                return {"ok": True, "bracket": self.bracket_view(b)}
            try:
                q = await self._quote(b["env"], b["ticker"], now, "auto-sell")
            except Exception:
                q = None
            if not q or q.get("ticker") != b["ticker"]:
                return {"ok": False, "code": "no_quote", "reason": "There is no live price for this market right "
                                                                   "now; nothing was changed."}
            bid = _bid(b["side"], q)
            if bid is not None and price <= bid + EPS:
                return {"ok": False, "code": "at_bid", "reason": f"The bid is already {bid * 100:.0f}¢ — slide to sell "
                                                                 "now instead."}
            self._tp_fills_from = "none"
            if not await self._cancel_tp(b, now):
                return {"ok": False, "code": "cancel_failed", "reason": "Could not change the auto-sell yet; the old "
                                                                        "one is still working."}
            if b["state"] != "open" or b["held"] <= 0:  # the old take profit filled before the cancel landed
                return {"ok": False, "code": "filled", "reason": "Your auto-sell already filled; nothing was changed.",
                        "bracket": self.bracket_view(b)}
            if self._tp_fills_from == "unknown":  # cancelled, but its final fill count is not confirmed
                self._audit("error", now, bracket=b, code="tp_change_unconfirmed",
                            reason="Old take profit cancelled but its fills are unconfirmed; price left unchanged.")
                return {"ok": False, "code": "fills_unknown", "reason": "Could not confirm the old auto-sell's fills; "
                                                                        "the auto-sell was not changed."}
            b.update(take_profit=price, no_tp=False)
            self._save(b, now)
            self._audit("tp_changed", now, bracket=b, detail={"from": old, "to": price})
            await self._place_tp(b, now)
            return {"ok": True, "bracket": self.bracket_view(b)}

    async def set_index_stop(self, bracket_id: str, level: float | None, now: int) -> dict:
        """Set, move or remove an open bracket's BTC index stop (the phone's optional stop). No order is placed: the
        monitor sells at the bid when the index reaches the level. Refused when the level is already reached (that
        would sell at once; slide to sell instead)."""
        async with self._lock:
            b = self._brackets.get(bracket_id)
            if not b or b["state"] != "open" or b["held"] <= 0:
                return {"ok": False, "code": "not_found", "reason": "There is no open position to change."}
            mine = self._agent_adjust(b, now)
            if mine is not None:
                return mine
            old = {"below": b.get("stop_index_below"), "above": b.get("stop_index_above")}
            if level is None:
                b.update(stop_index_below=None, stop_index_above=None)
            else:
                try:
                    level = round(float(level), 2)
                except (TypeError, ValueError):
                    return {"ok": False, "code": "invalid", "reason": "The stop must be a BTC price."}
                px = self.index_price(now)
                if px is None:
                    return {"ok": False, "code": "no_index", "reason": "There is no live BTC price right now; nothing "
                                                                      "was changed."}
                up = _is_up(b["side"])
                if level <= 0 or (up and px <= level + EPS) or (not up and px >= level - EPS):
                    return {"ok": False, "code": "already_hit", "reason": f"BTC is {_money(px)}: that stop is already "
                                                                         "reached. Slide to sell instead."}
                b.update(stop_index_below=level if up else None, stop_index_above=None if up else level)
            self._save(b, now)
            self._audit("stop_changed", now, bracket=b, detail={"from": old, "to": level})
            return {"ok": True, "bracket": self.bracket_view(b)}

    # -- approval --------------------------------------------------------------------------------------
    async def approve(self, token: str, decision: str, shown: dict, now: int) -> dict:
        """The human's answer. `shown` is the ticket as displayed; any change to a bound field refuses it."""
        steps = _Steps()
        try:
            async with self._lock:
                steps.mark("lock")  # waiting for the monitor's tick (it holds the lock while it talks to the venue)
                return await self._approve_locked(token, decision, shown, now, steps=steps)
        finally:
            steps.log(self.strategy, "approve")

    async def _approve_locked(self, token: str, decision: str, shown: dict, now: int,
                              steps: _Steps | None = None) -> dict:
        """The human's YES: token, binding and OrderGate checks, then the order."""
        ticket = self._tickets.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)
        if ticket is None:
            return self._refuse(GateResult(False, "bad_token", "This approval was already used or never existed;"
                                                               " nothing was sent."), now)
        if decision != "yes":
            self._audit("declined", now, detail={"ticket_id": ticket["ticket_id"]})
            return {"ok": True, "declined": True}
        token_error = None
        if now - ticket["created_ts"] > self.config.approval_ttl_s * 1000:
            self._audit("expired", now, detail={"ticket_id": ticket["ticket_id"]})
            token_error = GateResult(False, "expired", f"The approval expired after {self.config.approval_ttl_s}"
                                                       " seconds; nothing was sent.")
        elif not self._bound(token, ticket, shown):
            token_error = GateResult(False, "altered", "The ticket was changed after it was prepared; nothing "
                                                       "was sent.")
        self._audit("approved", now, detail={"ticket_id": ticket["ticket_id"]})
        steps = steps or _Steps()
        if ticket["kind"] == "exit":  # closed (or stopping) since the card was shown: say what happened, send nothing
            done = self._exit_not_needed(ticket["bracket_id"], self._brackets.get(ticket["bracket_id"]), now)
            if done is not None:
                return done
        quote = await self._quote(ticket["env"], ticket["ticker"], now, "approve")
        steps.mark("quote", (quote or {}).get("source"))
        r = self.gate.check(ticket, now, quote, token_error=token_error, exit_order=ticket["kind"] == "exit")
        steps.mark("gate")
        if not r.ok:
            return self._refuse(r, now, {"ticket_id": ticket["ticket_id"]})
        if ticket["kind"] == "exit":
            b = self._brackets.get(ticket["bracket_id"])
            if b is None or b["state"] != "open" or b["held"] <= 0:
                return {"ok": False, "code": "not_found", "reason": "There is no open position to sell; nothing was "
                                                                    "sent."}
            self._note_takeover(b, now)
            out = await self._exit(b, ticket["limit_price"], "exit", now)
            steps.mark("send")
            if out.get("ok") and b["state"] == "closed" and b.get("exit_reason") != "exit":
                # the take profit filled while it was being cancelled: the auto-sell sold, not this slide
                reason = closed_sentence(b)
                self._audit("exit_not_needed", now, bracket=b, code="already_closed", reason=reason)
                return {"ok": False, "code": "already_closed", "reason": reason, "bracket": self.bracket_view(b)}
            return out
        out = await self._enter(ticket, quote, now)
        steps.mark("send")
        return out

    @staticmethod
    def _bound(token: str, ticket: dict, shown: dict | None) -> bool:
        shown = shown or {}
        if any(k not in shown for k in BOUND_FIELDS if ticket.get(k) is not None):
            return False
        try:
            return ticket_hash(token, {**ticket, **{k: shown[k] for k in BOUND_FIELDS if k in shown}}) == ticket["hash"]
        except (TypeError, ValueError):
            return False

    async def _enter(self, ticket: dict, quote: dict, now: int) -> dict:
        gw = self.gateways[ticket["env"]]
        # The stop column is NOT NULL: 0.0 stands for "no contract stop" (it can never trigger).
        b = {"id": self._id(), "env": ticket["env"], "session_id": agent_session(), "kind": ticket["kind"],
             "ticker": ticket["ticker"], "side": ticket["side"], "window_end": ticket["window_end"],
             "stake": ticket["stake"], "contracts": ticket["contracts"], "limit_price": ticket["limit_price"],
             "take_profit": ticket["take_profit"],
             "stop": 0.0 if ticket.get("stop") is None else ticket["stop"],
             "stop_index_below": ticket.get("stop_index_below"), "stop_index_above": ticket.get("stop_index_above"),
             "max_cost": ticket["max_cost"],
             "ticket_hash": ticket["hash"], "setup_key": None, "fair_up": ticket.get("fair_up"),
             "ask_at_send": _ask(ticket["side"], quote), "state": "sending", "filled": 0.0, "entry_avg": None,
             "entry_cost": None, "entry_fees": None, "held": 0.0, "tp_order_id": None, "tp_filled": 0.0,
             "tp_base": 0.0, "tp_fees": 0.0, "tp_fees_base": 0.0, "no_tp": bool(ticket.get("no_tp")), "exit_reason": None, "stop_trigger_ts": None, "stop_trigger_bid": None,
             "stop_fair": None, "exit_ts": None, "exit_avg": None, "exit_fees": 0.0, "exit_filled": 0.0,
             "proceeds": 0.0, "created_ts": now, "updated_ts": now, "closed_ts": None}
        self._save(b, now)  # persisted BEFORE sending: a crash mid-send is found by reconciliation
        self._sync_streams(now)
        order = self._order_row(b, "entry", "buy", b["contracts"], b["limit_price"], "immediate_or_cancel", now)
        self._audit("sent", now, bracket=b, detail={"client_order_id": order["client_order_id"],
                                                     "count": b["contracts"], "limit": b["limit_price"],
                                                     "price_mode": ticket.get("price_mode"),
                                                     "sized_at": ticket.get("sized_at")})
        try:
            res = await gw.place_limit(ticker=b["ticker"], side=_contract_side(b["side"]), action="buy",
                                       count=b["contracts"], price=b["limit_price"],
                                       client_order_id=order["client_order_id"], tif="immediate_or_cancel")
        except Exception as exc:
            return self._send_failed(b, order, exc, now)
        self._entry_result(b, order, res, now)
        if b["state"] == "open":
            await self._place_tp(b, now)
        return {"ok": True, "bracket": self.bracket_view(b)}

    def _order_row(self, b: dict, role: str, action: str, count: float, price: float, tif: str, now: int) -> dict:
        n = len(self._select(ORDERS, {"bracket_id": b["id"]}))
        row = {"client_order_id": f"bfe-{b['id'][:24]}-{role}{n}", "bracket_id": b["id"], "role": role,
               "order_id": None, "ticker": b["ticker"], "side": _contract_side(b["side"]), "action": action,
               "count": count, "price": price, "tif": tif, "status": "sending", "filled": 0.0, "avg_price": None,
               "fees": None, "created_ts": now, "updated_ts": now}
        self._save_order(row, now)
        return row

    def _send_failed(self, b: dict, order: dict, exc: Exception, now: int) -> dict:
        status = getattr(exc, "status", None)
        if getattr(exc, "definitive", False):
            order["status"] = "rejected"
            self._save_order(order, now)
            b.update(state="closed", exit_reason="rejected", closed_ts=now)
            self._save(b, now)
            self._audit("error", now, bracket=b, code="rejected", reason=str(exc)[:500])
            self._credential_problem(b["env"], status, now)
            return {"ok": False, "code": "rejected", "reason": f"{self.venue} refused the order; nothing was bought."}
        order["status"] = "unknown"
        self._save_order(order, now)
        b["state"] = "unknown"
        self._save(b, now)
        if status == 409:  # the exchange says this client_order_id exists: pending until found (see _resolve_unknown)
            self._audit("error", now, bracket=b, code=DUPLICATE_PENDING,
                        reason=f"{self.venue} says this order already exists but does not list it yet; it stays "
                               "pending and is looked up by its client_order_id until found.")
        else:
            self._audit("error", now, bracket=b, code="unknown", reason=f"{type(exc).__name__}: outcome unknown")
        return {"ok": False, "code": "unknown", "reason": f"We could not confirm the order with {self.venue}. It is being "
                                                          "checked; nothing else will be sent until it is resolved."}

    def _credential_problem(self, env: str, status: int | None, now: int) -> None:
        if status not in (401, 403):
            return
        if env == self.modes.get("prod") and self.mode == "prod":
            self._set_mode(self._fallback, now, f"{self.venue} refused the production credentials; switched back "
                                                f"to {self._fallback}.")

    def _entry_result(self, b: dict, order: dict, res: dict, now: int) -> None:
        filled = _r2(min(float(res.get("filled") or 0.0), b["contracts"]))
        order.update(order_id=res.get("order_id"), filled=filled, avg_price=res.get("avg_price"),
                     fees=res.get("fees"), status=res.get("status") or ("executed" if filled else "canceled"))
        self._save_order(order, now)
        if filled <= 0:
            b.update(state="closed", exit_reason="unfilled", closed_ts=now)
            self._save(b, now)
            self._audit("cancelled", now, bracket=b, code="unfilled",
                        reason=f"Nothing was offered at {_cents(b['limit_price'])} or less; nothing was bought.")
            return
        avg = res.get("avg_price") or b["limit_price"]
        fees = res.get("fees")
        if fees is None:
            fees = self._fee(filled, avg, self.config.fee_rate)
        b.update(state="open", filled=filled, held=filled, entry_avg=avg, entry_fees=fees,
                 entry_cost=round(filled * avg + fees, 4))
        self._own_fill(b, now)
        self._save(b, now)
        self._audit("partial" if filled < b["contracts"] - EPS else "filled", now, bracket=b,
                    detail={"filled": filled, "of": b["contracts"], "avg": avg, "fees": fees})

    async def _place_tp(self, b: dict, now: int) -> None:
        if b["held"] <= 0 or b.get("no_tp"):
            return
        gw = self.gateways[b["env"]]
        order = self._order_row(b, "tp", "sell", b["held"], b["take_profit"], "good_till_canceled", now)
        try:
            res = await gw.place_limit(ticker=b["ticker"], side=_contract_side(b["side"]), action="sell",
                                       count=b["held"], price=b["take_profit"], client_order_id=order["client_order_id"],
                                       tif="good_till_canceled", post_only=True)  # Kalshi: reduce_only is IoC-only (prod-verified)
        except Exception as exc:
            order["status"] = "rejected" if getattr(exc, "definitive", False) else "unknown"
            self._save_order(order, now)
            self._audit("error", now, bracket=b, code="tp_failed",
                        reason=f"Take-profit order not placed ({type(exc).__name__}); retrying. The stop stays on.")
            if order["status"] == "unknown":  # it may exist: find it before placing another
                b["tp_order_id"] = "?" + order["client_order_id"]
                self._save(b, now)
            return
        order.update(order_id=res.get("order_id"), status=res.get("status") or "resting",
                     filled=float(res.get("filled") or 0.0))
        self._save_order(order, now)
        b["tp_order_id"] = res.get("order_id")
        b["tp_base"] = b["tp_filled"]
        b["tp_fees_base"] = b.get("tp_fees") or 0.0
        self._save(b, now)
        self._audit("tp_placed", now, bracket=b, detail={"order_id": b["tp_order_id"], "count": b["held"],
                                                          "price": b["take_profit"]})

    # -- the monitor (stop, take profit, reconciliation) -----------------------------------------------
    async def tick(self, now: int, quotes: dict[str, dict] | None = None) -> None:
        """StopMonitor: call about once a second. `quotes` (ticker -> quote) overrides fetching from the exchange."""
        pre = await self._prefetch_quotes(now) if quotes is None else {}
        async with self._lock:
            self._sync_streams(now)
            for env in set(self.streams) | set(self._stream_up):
                self._stream_health(env, now)
            for env, ok in list(self._ready.items()):
                if not ok and self.gateways.get(env) is not None and now - self._reconcile_try.get(env, -10**15)                         >= 10_000 and (env == self.modes[self.mode] or any(b["env"] == env for b in self._brackets.values())):
                    self._reconcile_try[env] = now
                    await self._reconcile_locked(env, now)
            for b in list(self._brackets.values()):
                if not self._ready.get(b["env"]):
                    continue
                try:
                    await self._watch(b, now, quotes, pre)
                except Exception as exc:
                    self._audit("error", now, bracket=b, code="monitor", reason=f"{type(exc).__name__}: {exc}"[:300])
            try:
                await self._external_check(now)
            except Exception as exc:
                self._audit("error", now, code="external_check", reason=f"{type(exc).__name__}: {exc}"[:300])
            due = self._settle_due(now)
        if due:  # the result reads run OUTSIDE the lock: a slow venue never delays a live bracket's stop
            await self._settle_reads(due, now)

    def _needs_rest_quote(self, b: dict, now: int) -> bool:
        """`_watch` will read this bracket's quote from REST: it is open or stopping on a ready env, its window is
        open, and the stream has no fresh quote for its market."""
        return (b["state"] in ("open", "stopping") and bool(self._ready.get(b["env"])) and now < b["window_end"]
                and self.stream_quote(b["env"], b["ticker"], now) is None)

    async def _prefetch_quotes(self, now: int) -> dict[tuple[str, str], dict | None]:
        """The REST quotes this tick's `_watch` calls need, read concurrently and WITHOUT the lock, each market once
        (`_quote` logs a failure and answers None). Keyed (env, ticker); None = the read failed: no bid this tick, as
        a failed read under the lock gave."""
        keys = list(dict.fromkeys((b["env"], b["ticker"]) for b in self._watched() if self._needs_rest_quote(b, now)))
        if not keys:
            return {}
        got = await asyncio.gather(*(self._quote(env, ticker, now) for env, ticker in keys), return_exceptions=True)
        return {k: (q if isinstance(q, dict) else None) for k, q in zip(keys, got, strict=True)}

    async def _watch(self, b: dict, now: int, quotes: dict[str, dict] | None,
                     pre: dict[tuple[str, str], dict | None] | None = None) -> None:
        if b["state"] in ("sending", "unknown"):
            await self._resolve_unknown(b, now)
            return
        if b["state"] == SETTLING:
            return  # its result is read after the lock is released (tick -> _settle_reads)
        if now >= b["window_end"]:
            await self._window_closed(b, now)
            return
        every = self.config.reconcile_backup_ms if self.stream_ok(b["env"]) else self.config.reconcile_every_ms
        if b["tp_order_id"] and (b["state"] == "stopping" or now - self._last_poll.get(b["id"], 0) >= every):
            await self._poll_tp(b, now)
            if b["state"] == "closed":
                return
        if quotes is not None:
            q = quotes.get(b["ticker"])
        else:  # the stream's quote when it is fresh; otherwise the REST quote (the backup), read before the lock
            key = (b["env"], b["ticker"])
            q = self.stream_quote(b["env"], b["ticker"], now)
            if q is None:
                q = pre[key] if pre is not None and key in pre else await self._quote(b["env"], b["ticker"], now)
        if quotes is not None:
            self._remember_quote(b["env"], b["ticker"], q)
        bid = _bid(b["side"], q) if q and q.get("ticker") == b["ticker"] else None
        # Index stop: needs a bid to sell into, like the contract stop (stop_triggered); a stale index never fires.
        index_hit, index = self._index_stop_hit(b, now) if bid is not None else (False, None)
        if b["state"] == "stopping" or stop_triggered(b["stop"], bid) or index_hit:
            await self._stop(b, bid, now, index=index)
            return
        if not b["tp_order_id"] and not b.get("no_tp") and b["held"] > 0:
            if bid is not None and bid >= b["take_profit"] - EPS:
                await self._exit(b, b["take_profit"], "tp", now)  # the approved take-profit price, or better
            else:
                await self._place_tp(b, now)

    async def _window_closed(self, b: dict, now: int) -> None:
        """The window closed. A resting take profit's fills are read first (it dies with the market); then a bracket
        holding nothing closes at once and one still holding waits for the official result (`settling`). The take
        profit's id STAYS on the settling bracket (code review 2026-10-30 #1): its last fills can still arrive (the
        stream, reconcile, and the read `_book_settlement` makes before booking), and settlement is never booked
        until a read of it answered with a final status, so a contract the take profit sold is never paid twice."""
        if b.get("tp_order_id"):
            try:
                await self._poll_tp(b, now)
            except Exception as exc:  # learnt before the result is booked (_book_settlement reads it again)
                self._audit("error", now, bracket=b, code="tp_final_read", reason=f"{type(exc).__name__}"[:300])
            if b["state"] == "closed":
                return
        held = float(b.get("held") or 0.0)
        if held <= EPS:
            b.update(state="closed", exit_reason="settled", closed_ts=now, tp_order_id=None)
            self._save(b, now)
            self._audit("settled", now, bracket=b, reason="Window closed holding nothing; nothing to settle.",
                        detail={"held": 0.0, "payout": 0.0, "fee": 0.0, "pnl_source": "fills"})
            return
        b["state"] = SETTLING
        self._save(b, now)
        self._settle_next[b["id"]] = now  # read in this same tick, once the lock is released
        self._audit("settle_pending", now, bracket=b,
                    reason=f"Window closed holding {held:.2f} contracts; waiting for {self.venue}'s official result "
                           "($1 a contract if it won, $0 if it lost). Until then it counts at its worst case.",
                    detail={"held": held, "tp_order_id": b.get("tp_order_id")})

    def _settle_due(self, now: int) -> list[tuple[dict, Callable, int]]:
        """Under the lock: the settling brackets whose next result read is due, as (bracket, read, tries), with the
        polite schedule (see the module docstring) advanced. Also writes the once-per-bracket "settling too long"
        audit (`settle_overdue`, see `settle_warnings`)."""
        cfg, due = self.config, []
        for b in self.settling_brackets():
            if not self._ready.get(b["env"]):
                continue
            self._note_overdue(b, now)
            if now < self._settle_next.get(b["id"], now):
                continue
            waited = now - int(b["window_end"])
            self._settle_next[b["id"]] = now + (cfg.settle_poll_ms if waited < cfg.settle_slow_after_ms
                                                else cfg.settle_slow_poll_ms)
            tries = self._settle_tries[b["id"]] = self._settle_tries.get(b["id"], 0) + 1
            read = getattr(self.gateways.get(b["env"]), "market_result", None)
            if read is None:
                self._settle_last[b["id"]] = "this connection cannot read market results"
                if tries == 1:
                    self._audit("error", now, bracket=b, code="settle_unsupported",
                                reason=f"This {self.venue} connection cannot read market results, so this position "
                                       "stays pending settlement (counted at its worst case). Check its result on "
                                       "the venue.")
                continue
            due.append((b, read, tries))
        return due

    async def _settle_reads(self, due: list[tuple[dict, Callable, int]], now: int) -> None:
        """Read the due results concurrently, each capped at `settle_read_timeout_ms`, WITHOUT the lock (code review
        2026-10-30 #2); then book each under the lock after re-checking that it is still the same settling bracket
        (a stream event, an exit or a restart may have changed it meanwhile: never a double booking)."""
        timeout = self.config.settle_read_timeout_ms / 1000

        async def one(read: Callable, ticker: str) -> Any:
            return await asyncio.wait_for(read(ticker), timeout)

        got = await asyncio.gather(*(one(read, b["ticker"]) for b, read, _ in due), return_exceptions=True)
        async with self._lock:
            for (b, _, tries), res in zip(due, got, strict=True):
                if self._brackets.get(b["id"]) is not b or b["state"] != SETTLING:
                    continue
                try:
                    await self._book_settlement(b, res, tries, now)
                except Exception as exc:
                    self._audit("error", now, bracket=b, code="monitor", reason=f"{type(exc).__name__}: {exc}"[:300])

    async def _book_settlement(self, b: dict, got: Any, tries: int, now: int) -> None:
        """Under the lock, with one result read's answer (or its exception): once the result is final, and the take
        profit's final fills are known, close the bracket with its settlement proceeds. Anything else leaves it
        settling, to be read again."""
        if isinstance(got, BaseException):
            why = "timed out" if isinstance(got, asyncio.TimeoutError) else type(got).__name__
            self._settle_last[b["id"]] = f"the last result read failed ({why})"
            if tries == 1 or tries % 20 == 0:
                self._audit("error", now, bracket=b, code="settle_read",
                            reason=f"Could not read {b['ticker']}'s result ({why}); retrying. The position stays "
                                   "pending settlement."[:300], detail={"tries": tries})
            return
        got = got or {}
        result = got.get("result")
        if not got.get("final") or result not in ("yes", "no"):
            self._settle_last[b["id"]] = (f"the venue reports status {got.get('status')!r}, result "
                                          f"{result!r}: not final")
            return  # not determined yet (or disputed): ask again later
        if b.get("tp_order_id"):  # its last fills must be known first, from a read that answered (review #1)
            try:
                await asyncio.wait_for(self._poll_tp(b, now), self.config.settle_read_timeout_ms / 1000)
            except Exception as exc:
                why = "timed out" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__
                first = not (self._settle_last.get(b["id"]) or "").startswith("its take-profit order could not")
                self._settle_last[b["id"]] = f"its take-profit order could not be read ({why})"
                if first or tries % 20 == 0:
                    self._audit("error", now, bracket=b, code="settle_tp_read",
                                reason=f"The result is in but the take-profit order's final fills could not be read "
                                       f"({why}); retrying before booking.", detail={"tries": tries})
                return
            if b["state"] != SETTLING:
                return  # the take profit sold everything: closed as "tp"
            if b.get("tp_order_id"):  # not visible, or still reads as working: its fills are not final yet
                self._settle_last[b["id"]] = "its take-profit order does not read as finished yet"
                return
        won = (result == "yes") == _is_up(b["side"])
        held = float(b.get("held") or 0.0)
        payout = round(held * 1.0, 4) if won else 0.0
        fee = round(held * SETTLEMENT_FEE, 4)
        b.update(state="closed", exit_reason="settled", closed_ts=now,
                 proceeds=round(float(b.get("proceeds") or 0.0) + payout, 4),
                 exit_fees=round(float(b.get("exit_fees") or 0.0) + fee, 4))
        self._save(b, now)
        pnl = round(b["proceeds"] - (b.get("tp_fees") or 0.0) - (b.get("exit_fees") or 0.0)
                    - (b.get("entry_cost") or 0.0), 4)
        self._audit("settled", now, bracket=b,
                    reason=(f"{b['ticker']} settled {result.upper()}: {held:.2f} {b['side']} contracts "
                            + (f"won ${payout:.2f}" if won else "lost (paid $0)") + f"; P&L {_signed_money(pnl)}."),
                    detail={"result": result, "won": won, "held": held, "payout": payout, "fee": fee, "pnl": pnl,
                            "status": got.get("status"), "pnl_source": got.get("source") or "venue_result",
                            "tries": tries})

    def _note_overdue(self, b: dict, now: int) -> None:
        """Audit once per bracket (also across restarts) that it has been settling longer than
        `settle_warn_after_ms`. Nothing is closed or voided automatically: the user checks the venue."""
        if b["id"] in self._settle_overdue or now - int(b["window_end"]) < self.config.settle_warn_after_ms:
            return
        self._settle_overdue.add(b["id"])
        if self._select(AUDIT, {"bracket_id": b["id"], "event": "settle_overdue"}, limit=1):
            return  # written before a restart
        w = self._settle_warning(b, now)
        self._audit("settle_overdue", now, bracket=b, code="settle_overdue", reason=w["message"][:300],
                    detail={"minutes": w["minutes"], "last_read": w["last_read"], "held": w["held"]})

    def _settle_warning(self, b: dict, now: int) -> dict:
        minutes = max(0, (now - int(b["window_end"])) // 60_000)
        last = self._settle_last.get(b["id"])
        held = float(b.get("held") or 0.0)
        return {"id": b["id"], "ticker": b["ticker"], "side": b["side"], "held": held, "minutes": minutes,
                "last_read": last,
                "message": (f"{b['ticker']} {b['side']} ({held:.2f} contracts) has been settling for {minutes} min "
                            f"— check the venue ({self.venue}). " + (f"Last check: {last}. " if last else "")
                            + "It still counts at its worst case; nothing is closed or voided automatically.")}

    def settle_warnings(self, now: int) -> list[dict]:
        """Settling brackets whose result has not been booked `settle_warn_after_ms` after the close (a void,
        disputed or never-final market, a result endpoint that keeps failing, or a take profit that never reads as
        finished). Shown in trading status, on Play and in the Agent panel; there is no manual close action yet.
        Lost entries that are still unprovable follow (`unresolved_warnings`, kind "unresolved_order")."""
        return [self._settle_warning(b, now) for b in self.settling_brackets()
                if now - int(b["window_end"]) >= self.config.settle_warn_after_ms] + self.unresolved_warnings(now)

    async def _poll_tp(self, b: dict, now: int) -> None:
        gw = self.gateways[b["env"]]
        self._last_poll[b["id"]] = now
        tp = b["tp_order_id"]
        side = _contract_side(b["side"])
        if tp.startswith("?"):  # placement outcome unknown: look it up by client_order_id
            o = await gw.find_order(b["ticker"], tp[1:], side)
            if o is None:
                b["tp_order_id"] = None
                self._save(b, now)
                return
            b["tp_order_id"] = o["order_id"]
            b["tp_base"] = b["tp_filled"]
        else:
            o = await self._read_order(b, tp)
            if o is None:
                return  # not visible yet: the next poll (or the stream) brings it
        self._apply_tp(b, o, now)

    def _own_order_ids(self, ids: set[str]) -> set[str]:
        """Those of these venue order ids that this strategy placed: an indexed IN lookup (ix_trading_orders_order_id),
        never a scan of every order ever placed."""
        if not ids:
            return set()
        return {o["order_id"] for o in self._select(ORDERS, filters=[("order_id", "in", sorted(ids))])}

    def _coid_of(self, order_id: str) -> str | None:
        rows = self._select(ORDERS, {"order_id": order_id})
        return rows[0]["client_order_id"] if rows else None

    async def _read_order(self, b: dict, order_id: str) -> dict | None:
        """The order's state from the LIST endpoint (by client_order_id). Production's single-order GET
        (GET /portfolio/orders/{id}) was seen answering a stale "resting" for 20+ s, and 404, right after a cancel,
        while the list was already right (2026-10-28 $1 production test). The GET is used only when the order's
        client_order_id is not on record."""
        gw, side = self.gateways[b["env"]], _contract_side(b["side"])
        coid = self._coid_of(order_id)
        if coid:
            o = await gw.find_order(b["ticker"], coid, side)
            return o if o is None or o.get("order_id") in (None, order_id) else None
        return await gw.get_order(order_id, side)

    def _apply_tp(self, b: dict, o: dict, now: int) -> None:
        total = _r2(b.get("tp_base", 0.0) + float(o.get("filled") or 0.0))
        delta = _r2(total - b["tp_filled"])
        if delta > 0:
            self._own_fill(b, now)
            fees = float(o.get("fees") or 0.0)
            b["tp_filled"] = total
            b["held"] = _r2(max(0.0, b["held"] - delta))
            b["tp_fees"] = round(b.get("tp_fees_base", 0.0) + fees, 4)
            b["proceeds"] = round(b["proceeds"] + delta * b["take_profit"], 4)
            if b["held"] <= 0:
                b.update(state="closed", exit_reason="tp", closed_ts=now, exit_ts=now, tp_order_id=None)
                self._audit("tp_filled", now, bracket=b, detail={"filled": total, "price": b["take_profit"]})
            else:
                self._audit("partial", now, bracket=b, code="tp", detail={"tp_filled": total, "held": b["held"]})
        if o.get("status") in ("canceled", "executed") and b["state"] != "closed":
            b["tp_order_id"] = None
            b["tp_fees_base"] = b.get("tp_fees", 0.0)
        self._save(b, now)

    async def _cancel_tp(self, b: dict, now: int) -> bool:
        """Cancel the resting take profit and learn its final fills. False = could not confirm (do not sell)."""
        if not b["tp_order_id"]:
            return True
        gw = self.gateways[b["env"]]
        try:
            if b["tp_order_id"].startswith("?"):
                await self._poll_tp(b, now)
                if not b["tp_order_id"]:
                    return True
            tp = b["tp_order_id"]
            res = await gw.cancel(tp, b["ticker"])
        except Exception as exc:
            self._audit("error", now, bracket=b, code="cancel_failed",
                        reason=f"Could not cancel the take profit ({type(exc).__name__}); retrying before selling.")
            return False
        # A cancel that answered (200 with reduced_by, or 404 = no longer resting) is authoritative: the take
        # profit cannot fill any more. Its final fill count comes from the list endpoint; failing that, from the
        # cancel response (placed count - reduced_by); failing that, what we already know. The stop sale is
        # reduce-only, so an uncertain count can never make it sell more than is held. One exception skips the list
        # read (the exit path's slowest step): a cancel that took back the WHOLE placed count proves nothing filled.
        o, source = None, "list"
        placed = self._select(ORDERS, {"order_id": tp})
        if (placed and not res.get("not_found") and res.get("reduced_by") is not None
                and float(res["reduced_by"]) >= float(placed[0]["count"]) - EPS):
            o, source = {"filled": 0.0, "fees": 0.0}, "cancel"
        else:
            try:
                o = await self._read_order(b, tp)
            except Exception:
                o = None
        if o is None or o.get("status") == "resting":
            rows = self._select(ORDERS, {"order_id": tp})
            if rows and not res.get("not_found") and res.get("reduced_by") is not None:
                o, source = {"filled": _r2(max(0.0, float(rows[0]["count"]) - float(res["reduced_by"])))}, "cancel"
            else:
                o, source = {"filled": _r2(b["tp_filled"] - b.get("tp_base", 0.0))}, "unknown"
            o["fees"] = round((b.get("tp_fees") or 0.0) - (b.get("tp_fees_base") or 0.0), 4)
        self._tp_fills_from = source  # set_take_profit re-places only when the fill count is known
        self._audit("cancelled", now, bracket=b, code="tp", detail={"order_id": tp, "fills_from": source,
                                                                     "not_found": bool(res.get("not_found"))})
        self._apply_tp(b, {**o, "status": "canceled"}, now)
        b["tp_order_id"] = None
        self._save(b, now)
        return True

    async def _stop(self, b: dict, bid: float | None, now: int, index: float | None = None) -> None:
        if b["state"] == "open":
            b.update(state="stopping", stop_trigger_ts=now, stop_trigger_bid=bid)
            self._save(b, now)
            detail = {"bid": bid, "stop": b["stop"]}
            if b.get("stop_index_below") is not None or b.get("stop_index_above") is not None:
                detail.update(index=index, stop_index_below=b.get("stop_index_below"),
                              stop_index_above=b.get("stop_index_above"))
            self._audit("stop_triggered", now, bracket=b, detail=detail)
        if not await self._cancel_tp(b, now) or b["state"] == "closed":
            return
        if bid is None:
            return  # nobody bids: keep trying every tick
        price = max(0.01, _floor_cent(bid - self.config.stop_slippage))
        await self._sell(b, price, "stop", now)

    async def _exit(self, b: dict, min_price: float, reason: str, now: int) -> dict:
        if not await self._cancel_tp(b, now):
            return {"ok": False, "code": "cancel_failed", "reason": "Could not cancel the take profit; nothing sold."}
        if b["state"] == "closed":
            return {"ok": True, "bracket": self.bracket_view(b)}
        await self._sell(b, min_price, reason, now)
        return {"ok": True, "bracket": self.bracket_view(b)}

    async def _sell(self, b: dict, price: float, reason: str, now: int) -> None:
        """Sell what is held with a reduce-only IOC limit at `price` or better. Never more than held."""
        if b["held"] <= 0:
            return
        gw = self.gateways[b["env"]]
        count = b["held"]
        order = self._order_row(b, reason if reason != "tp" else "tpx", "sell", count, price, "immediate_or_cancel",
                                now)
        self._audit("stop_sent" if reason == "stop" else "sent", now, bracket=b,
                    detail={"count": count, "limit": price, "reason": reason, "client_order_id": order["client_order_id"]})
        try:
            res = await gw.place_limit(ticker=b["ticker"], side=_contract_side(b["side"]), action="sell", count=count,
                                       price=price, client_order_id=order["client_order_id"], tif="immediate_or_cancel",
                                       reduce_only=True)
        except Exception as exc:
            order["status"] = "rejected" if getattr(exc, "definitive", False) else "unknown"
            self._save_order(order, now)
            self._audit("error", now, bracket=b, code=f"{reason}_failed", reason=f"{type(exc).__name__}; retrying.")
            if order["status"] == "unknown":  # it may have sold: recheck the position before selling again
                await self._sync_held(b, now)
            return
        filled = _r2(min(float(res.get("filled") or 0.0), count))
        avg = res.get("avg_price") or price
        fees = res.get("fees")
        if fees is None:
            fees = self._fee(filled, avg, self.config.fee_rate)
        order.update(order_id=res.get("order_id"), filled=filled, avg_price=avg, fees=fees,
                     status=res.get("status") or "executed")
        self._save_order(order, now)
        if filled > 0:
            self._own_fill(b, now)
            prev = b["exit_filled"]
            b["exit_avg"] = round(((b["exit_avg"] or 0.0) * prev + avg * filled) / (prev + filled), 4)
            b["exit_filled"] = _r2(prev + filled)
            b["exit_fees"] = round(b["exit_fees"] + fees, 4)
            b["proceeds"] = round(b["proceeds"] + avg * filled, 4)
            b["held"] = _r2(b["held"] - filled)
        if b["held"] > 0 and filled < count - EPS:
            await self._sync_held(b, now)  # reduce-only sold less than we thought we held: ask Kalshi what is left
        if b["held"] <= 0:
            b.update(state="closed", exit_reason=reason, closed_ts=now, exit_ts=now)
            self._audit("closed", now, bracket=b, code=reason, detail={"avg": b["exit_avg"], "fees": b["exit_fees"]})
        elif reason == "stop":
            b["state"] = "stopping"  # keep selling the rest every tick (an exit or tp remainder keeps its bracket)
        self._save(b, now)

    async def _sync_held(self, b: dict, now: int) -> None:
        try:
            pos = {p["ticker"]: p["position"] for p in await self.gateways[b["env"]].positions()}
        except Exception:
            return
        p = pos.get(b["ticker"], 0.0)
        mine = max(p, 0.0) if _is_up(b["side"]) else max(-p, 0.0)
        if mine < b["held"] - EPS:
            b["held"] = _r2(mine)
            if b["held"] <= 0:
                b.update(state="closed", exit_reason=b.get("exit_reason") or "reconciled", closed_ts=now)
            self._save(b, now)

    def _duplicate_pending(self, b: dict) -> bool:
        """The bracket's entry was answered "already exists" (409) and has not been found yet (audit row)."""
        return bool(self._select(AUDIT, {"bracket_id": b["id"], "code": DUPLICATE_PENDING}, limit=1))

    async def _resolve_unknown(self, b: dict, now: int) -> None:
        """An entry still `sending` / `unknown`, resolved only by proof (domain/order_resolution.py; "Lost answers" in
        the module docstring). Found -> booked at its real average fill price, take profit placed; proven unfilled
        -> closed `not_found` (`unfilled` for a 409 duplicate); otherwise it stays unknown with max_cost committed."""
        gw = self.gateways[b["env"]]
        orders = self._select(ORDERS, {"bracket_id": b["id"], "role": "entry"})
        if not orders:  # the bracket is saved before its order row: no row = nothing was ever sent
            b.update(state="closed", exit_reason="not_found", closed_ts=now)
            self._save(b, now)
            return
        order = orders[0]
        side = _contract_side(b["side"])
        found = await gw.find_order(b["ticker"], order["client_order_id"], side)
        if found is None:
            # A list miss proves nothing (the lists lag placements). After FIND_GIVE_UP_MS the fills and the position
            # are checked every PROOF_EVERY_MS; a 409 duplicate (the venue HAS the order) goes through the same proof.
            sent = int(order.get("created_ts") or b["created_ts"])
            if now - sent <= FIND_GIVE_UP_MS or now < self._proof_next.get(b["id"], 0):
                return
            self._proof_next[b["id"]] = now + PROOF_EVERY_MS
            found = await self._prove_lost(b, order, now)
            if found is None:
                return  # unprovable: stays unknown (audited and warned once)
            if not float(found.get("filled") or 0.0) > 0:
                self._close_unfilled(b, order, found, now)
                return
        if found.get("status") == "resting":  # should not happen with IOC; never leave an entry resting
            await gw.cancel(found["order_id"], b["ticker"])  # answered: no longer resting (authoritative)
            again = await gw.find_order(b["ticker"], order["client_order_id"], side)
            found = {**(again or found), "status": "canceled"}
        self._lost_done(b)
        if found.get("proof"):
            self._audit("resolved", now, bracket=b, code="lost_entry_found",
                        reason=f"The lost entry was found from {self.venue}'s {found['proof']}: "
                               f"{float(found.get('filled') or 0):.2f} filled.",
                        detail={"proof": found["proof"], "order_id": found.get("order_id"),
                                "filled": found.get("filled"), "avg": found.get("avg_price")})
        # The order's own average fill price and fees (B1#4); the limit price only when the venue gives no average.
        self._entry_result(b, order, {"order_id": found.get("order_id"), "filled": found.get("filled"),
                                      "avg_price": found.get("avg_price"), "fees": found.get("fees"),
                                      "status": found.get("status")}, now)
        if b["state"] == "open":
            await self._place_tp(b, now)

    async def _prove_lost(self, b: dict, order: dict, now: int) -> dict | None:
        """The venue's fills/activities evidence for a lost entry the lists do not show, weighed with the position
        (domain/order_resolution.decide): the evidence when it proves filled or not filled, else None (unknown,
        noted once)."""
        gw = self.gateways[b["env"]]
        fn = getattr(gw, "lost_order", None)
        evidence, why = None, f"{self.venue} offers no fills read for it"
        if fn is not None:
            try:
                evidence = await fn(b["ticker"], order["client_order_id"], side=_contract_side(b["side"]),
                                    action="buy", since_ms=int(order.get("created_ts") or b["created_ts"]),
                                    count=float(order["count"]), price=float(order["price"]))
            except Exception as exc:
                evidence, why = None, f"its fills could not be read ({type(exc).__name__})"
        excess = None
        if evidence is not None and not float(evidence.get("filled") or 0.0) > 0:
            excess, why = await self._position_excess(b)
        verdict = decide(evidence, excess)
        if verdict == UNKNOWN:
            self._note_unresolved(b, now, why, excess)
            return None
        if verdict == NOT_FILLED and self._duplicate_pending(b):
            evidence = {**evidence, "duplicate": True}
        return evidence

    async def _position_excess(self, b: dict) -> tuple[float | None, str]:
        """(contracts on the bracket's side beyond what this env's other brackets on that market hold, why)."""
        gw = self.gateways[b["env"]]
        read = getattr(gw, "exchange_positions", None) or gw.positions
        try:
            pos = {p["ticker"]: float(p["position"]) for p in await read()}
        except Exception as exc:
            return None, f"the position could not be read ({type(exc).__name__})"
        p = pos.get(b["ticker"], 0.0)
        on_exchange = max(p, 0.0) if _is_up(b["side"]) else max(-p, 0.0)
        ours = _r2(sum(float(o.get("held") or 0.0) for o in self._brackets.values()
                       if o["id"] != b["id"] and o["env"] == b["env"] and o["ticker"] == b["ticker"]
                       and o["side"] == b["side"]))
        return excess_on_side(on_exchange, ours), (f"{self.venue} shows {on_exchange:.2f} {b['side']} contracts "
                                                   f"where our brackets hold {ours:.2f}, and no fill read explains it")

    def _note_unresolved(self, b: dict, now: int, why: str, excess: float | None) -> None:
        first = b["id"] not in self._unresolved
        self._unresolved[b["id"]] = {"why": why, "excess": excess}
        if first and not self._select(AUDIT, {"bracket_id": b["id"], "event": "order_unresolved"}, limit=1):
            self._audit("order_unresolved", now, bracket=b, code="order_unresolved",
                        reason=(f"Could not confirm whether the entry filled: {why}. It still counts at its approved "
                                f"maximum (${float(b['max_cost']):.2f}) and nothing else is sent on this market until "
                                f"it is known; check {self.venue}.")[:300],
                        detail={"excess": excess})

    def _lost_done(self, b: dict) -> None:
        self._proof_next.pop(b["id"], None)
        self._unresolved.pop(b["id"], None)

    def _close_unfilled(self, b: dict, order: dict, found: dict, now: int) -> None:
        """Proven: no fill since the send and no unexplained position. Nothing was bought."""
        self._lost_done(b)
        order.update(status="canceled", filled=0.0)
        self._save_order(order, now)
        b.update(state="closed", exit_reason="unfilled" if found.get("duplicate") else "not_found", closed_ts=now)
        self._save(b, now)
        self._audit("cancelled", now, bracket=b, code=b["exit_reason"],
                    reason=(f"{self.venue} shows no fill of this entry and no position for it since it was sent; "
                            "nothing was bought."), detail={"proof": found.get("proof")})

    def unresolved_warnings(self, now: int) -> list[dict]:
        """Lost entries still unprovable (domain/order_resolution.py), listed with the settle warnings."""
        out = []
        for bid, u in self._unresolved.items():
            b = self._brackets.get(bid)
            if b is None or b["state"] not in ("sending", "unknown"):
                continue
            minutes = max(0, (now - int(b["created_ts"])) // 60_000)
            out.append({"id": bid, "kind": "unresolved_order", "ticker": b["ticker"], "side": b["side"],
                        "held": 0.0, "minutes": minutes, "last_read": u["why"],
                        "message": (f"{b['ticker']} {b['side']}: we could not confirm whether your order filled (its "
                                    f"answer was lost {minutes} min ago; {u['why']}). It counts at its maximum "
                                    f"(${float(b['max_cost']):.2f}) and nothing else is sent on this market until it "
                                    f"is known — check {self.venue}.")})
        return out

    # -- exits (and additions) made by hand on the exchange -----------------------------------------
    def _own_fill(self, b: dict, now: int) -> None:
        self._own_fill_ms[f"{b['env']}:{b['ticker']}"] = now

    def _in_flight(self, b: dict, now: int) -> bool:
        """One of our orders for this bracket may still be working or is unresolved."""
        if b["state"] != "open" or str(b.get("tp_order_id") or "").startswith("?"):
            return True
        rows = self._select(ORDERS, {"bracket_id": b["id"]}, filters=[("status", "in", ["sending", "unknown"])])
        return any(now - int(r.get("created_ts") or 0) < 60_000 for r in rows)

    async def _external_check(self, now: int) -> None:
        cfg = self.config
        envs = {b["env"] for b in self._brackets.values() if b["state"] in ("open", "stopping")}
        for env in list(self._ext_next):
            if env not in envs:
                del self._ext_next[env]
        for env in envs:
            gw = self.gateways.get(env)
            if gw is None or not self._ready.get(env) or now < self._ext_next.get(env, now):
                continue
            # While the live feed is healthy any fill on the account pulls this check to within 1 s
            # (on_stream_event), so the positions poll is only the backup (reconcile_backup_ms).
            every = max(cfg.external_check_ms, cfg.reconcile_backup_ms) if self.stream_ok(env) else cfg.external_check_ms
            self._ext_next[env] = now + every
            read = getattr(gw, "exchange_positions", None) or gw.positions
            try:
                pos = {p["ticker"]: float(p["position"]) for p in await read()}
            except Exception:  # the next check retries; the stream and the gateway report outages themselves
                log.debug("external position check (%s): the positions read failed", env, exc_info=True)
                continue
            by_ticker: dict[str, list[dict]] = {}
            for b in self._brackets.values():
                if b["env"] == env and b["state"] in ("open", "stopping") and now < b["window_end"] - 1_000:
                    by_ticker.setdefault(b["ticker"], []).append(b)
            for key in [k for k in self._short_since if k.startswith(env + ":") and k[len(env) + 1:] not in by_ticker]:
                del self._short_since[key]
            for ticker, bs in by_ticker.items():
                if len({b["side"] for b in bs}) != 1:
                    continue
                key, side = f"{env}:{ticker}", bs[0]["side"]
                p = pos.get(ticker, 0.0)
                on_exchange = max(p, 0.0) if _is_up(side) else max(-p, 0.0)
                ours = _r2(sum(float(b["held"]) for b in bs))
                busy = (any(self._in_flight(b, now) for b in bs)
                        or now - self._own_fill_ms.get(key, -10**15) < cfg.external_grace_ms)
                if on_exchange < ours - EPS:
                    if busy:
                        self._short_since.pop(key, None)
                        continue
                    first = self._short_since.setdefault(key, now)
                    if now - first >= cfg.external_grace_ms:
                        self._short_since.pop(key, None)
                        await self._manual_exit(gw, env, ticker, bs, on_exchange, p, now)
                    continue
                self._short_since.pop(key, None)
                extra = _r2(on_exchange - ours)
                if extra > EPS and not busy and abs(self._ext_seen.get(key, -1.0) - extra) > EPS:
                    self._ext_seen[key] = extra
                    self._audit("external_position", now, bracket=bs[0], code="manual_add",
                                reason=f"The {self.venue} account holds {extra:.2f} more {side} contracts in {ticker} "
                                       "than our brackets; they are not ours to manage and are never sold by this "
                                       "app.", detail={"exchange": on_exchange, "ours": ours, "extra": extra})

    async def _manual_exit(self, gw: TradingGateway, env: str, ticker: str, bs: list[dict], on_exchange: float,
                           net: float, now: int) -> None:
        """The exchange holds less than our brackets: someone sold by hand. Never places an order."""
        for b in bs:
            if b["tp_order_id"] and not await self._cancel_tp(b, now):
                return  # could not confirm the cancel: look again at the next check
        live = [b for b in bs if b["state"] in ("open", "stopping") and b["held"] > 0]
        gone = _r2(sum(b["held"] for b in live) - on_exchange)
        if gone <= EPS:
            return  # the take profit had filled: our own exit, already booked
        side = live[0]["side"] if live else bs[0]["side"]
        q = self.stream_quote(env, ticker, now) or await self._quote(env, ticker, now)
        bid = _bid(side, q) if q and q.get("ticker") == ticker else self._quotes_bid(env, ticker, side)
        fills = await self._external_fills(gw, ticker, side, min(b["created_ts"] for b in bs), gone, bid)
        left = gone
        for b in sorted(live, key=lambda r: r["created_ts"], reverse=True):
            n = _r2(min(left, b["held"]))
            if n <= 0:
                continue
            left = _r2(left - n)
            if fills is not None:
                price, fee_each, source = fills["avg"], fills["fee_each"], "fills"
            else:
                price, fee_each, source = bid, None, ("estimated" if bid is not None else "unknown")
            held_before = b["held"]
            if price is not None:
                fee = round(fee_each * n, 4) if fee_each is not None else self._fee(n, price, self.config.fee_rate)
                prev = b["exit_filled"] or 0.0
                b["exit_avg"] = round(((b["exit_avg"] or 0.0) * prev + price * n) / (prev + n), 4)
                b["exit_fees"] = round((b["exit_fees"] or 0.0) + fee, 4)
                b["proceeds"] = round((b["proceeds"] or 0.0) + price * n, 4)
            else:
                fee = None
            b["exit_filled"] = _r2((b["exit_filled"] or 0.0) + n)
            b["held"] = _r2(b["held"] - n)
            if b["held"] <= 0:
                b.update(state="closed", exit_reason="manual", closed_ts=now, exit_ts=now, tp_order_id=None)
            self._save(b, now)
            how = {"fills": f"at {_cents(price)} on average (the exchange's fills)",
                   "estimated": f"P&L ESTIMATED at the bid {_cents(price)} (no matching exchange fills)",
                   "unknown": "price unknown (no exchange fills and no bid)"}[source]
            self._audit("manual_exit", now, bracket=b, code="manual",
                        reason=f"Sold by hand on {self.venue}: {n:.2f} of {held_before:.2f} contracts, {how}. "
                               + ("The bracket is closed." if b["state"] == "closed"
                                  else f"{b['held']:.2f} remain with their stop."),
                        detail={"held_before": held_before, "sold": n, "held_after": b["held"],
                                "exchange": on_exchange, "price": price, "fee": fee, "pnl_source": source,
                                "fills": (fills or {}).get("ids", [])})
        note = getattr(gw, "note_external_position", None)
        if note is not None:
            try:
                note(ticker, net)
            except Exception:
                log.warning("note_external_position failed", exc_info=True)

    def _quotes_bid(self, env: str, ticker: str, side: str) -> float | None:
        return _bid(side, self._quotes.get(f"{env}:{ticker}"))

    async def _external_fills(self, gw: TradingGateway, ticker: str, side: str, since: int, need: float,
                              bid: float | None) -> dict | None:
        """Average price (on the bracket's side) and fee of the exchange fills that are not ours and reduced our side,
        oldest first up to `need` contracts; None unless they cover all of it."""
        fn = getattr(gw, "market_fills", None)
        if fn is None:
            return None
        try:
            rows = await fn(ticker, since)
        except Exception as exc:
            log.warning("fills for %s failed: %s", ticker, type(exc).__name__)
            return None
        if not rows:
            return None
        ids = {f["order_id"] for f in rows if f.get("order_id")}
        mine = self._own_order_ids(ids)
        if self._known_order_ids_fn is not None and ids:
            try:
                mine |= set(self._known_order_ids_fn(ids) or ())
            except Exception:  # only this table's own orders count as ours then
                log.warning("the other services' order ids could not be read; only this table's count as ours",
                            exc_info=True)
        got, n, cost, fees, fees_known = [], 0.0, 0.0, 0.0, True
        for f in rows:
            if f.get("order_id") and f["order_id"] in mine:
                continue
            d, yes = f.get("yes_delta"), f.get("yes_price")
            if yes is None or not f.get("count"):
                continue
            if d is not None and ((side == "UP" and d >= 0) or (side == "DOWN" and d <= 0)):
                continue  # adds to our side: not a sale of it
            px = yes if _is_up(side) else round(1 - yes, 4)
            if d is None and bid is not None and abs((1 - px) - bid) < abs(px - bid):
                px = round(1 - px, 4)  # direction unknown (Polymarket): the side nearer our bid
            take = min(float(f["count"]), need - n)
            if take <= 0:
                break
            got.append(f.get("fill_id"))
            n, cost = n + take, cost + px * take
            if f.get("fee") is None:
                fees_known = False
            else:
                fees += float(f["fee"]) * take / float(f["count"])
        if n < need - EPS:
            return None
        return {"avg": round(cost / n, 4), "fee_each": (fees / n) if fees_known else None, "ids": got}

    # -- reconciliation after a restart or a mode switch ---------------------------------------------
    async def reconcile(self, env: str, now: int) -> dict:
        async with self._lock:
            return await self._reconcile_locked(env, now)

    async def _reconcile_locked(self, env: str, now: int) -> dict:
        gw = self.gateways.get(env)
        if gw is None:
            return {"ok": False, "reason": "not configured"}
        try:
            for b in self._select(BRACKETS, {"env": env}, filters=[("state", "in", WATCHED_STATES)]):
                self._brackets.setdefault(b["id"], b)
            mine = [b for b in self._brackets.values() if b["env"] == env]
            for b in mine:
                if b["state"] in ("sending", "unknown"):
                    await self._resolve_unknown(b, now)
                elif b["tp_order_id"]:
                    await self._poll_tp(b, now)
            positions = await gw.positions()
            resting = await gw.orders(status="resting")
            ids = {x for o in resting for x in (o.get("order_id"), o.get("client_order_id")) if x}
            elsewhere = (set(self._known_order_ids_fn(ids) or ()) if self._known_order_ids_fn is not None and ids
                         else set())
        except Exception as exc:
            self._ready[env] = False
            self._audit("error", now, code="reconcile", reason=f"Reconciliation failed: {type(exc).__name__}", env=env)
            self._credential_problem(env, getattr(exc, "status", None), now)
            return {"ok": False, "reason": type(exc).__name__}
        pos = {p["ticker"]: p["position"] for p in positions}
        # A bracket whose window has closed is left to the monitor (`_window_closed` -> settling -> the official
        # result), like `_external_check`: after the close a settled market's position reads 0, which is NOT a sale
        # (code review 2026-10-30 #3: a restart after the close booked a winner as a $0 "reconciled" loss).
        for b in [b for b in self._brackets.values() if b["env"] == env and b["state"] in ("open", "stopping")
                  and now < b["window_end"] - 1_000]:
            p = pos.get(b["ticker"], 0.0)
            held_kalshi = max(p, 0.0) if _is_up(b["side"]) else max(-p, 0.0)
            if held_kalshi < b["held"] - EPS:
                b["held"] = _r2(held_kalshi)
                if b["held"] <= 0:
                    b.update(state="closed", exit_reason=b.get("exit_reason") or "reconciled", closed_ts=now)
                self._save(b, now)
        known_orders = self._own_order_ids({o["order_id"] for o in resting if o.get("order_id")})
        known_orders |= elsewhere  # the other strategies' orders on this account: this app placed them
        series = self.config.series + "-"
        # a bracket that never bought anything (rejected, unfilled, not found) does not make a market's position ours;
        # only the markets the account holds are looked up (never every bracket ever placed)
        held = sorted({p["ticker"] for p in positions if (p.get("ticker") or "").startswith(series)})
        known_tickers = {r["ticker"] for r in (self._select(BRACKETS, {"env": env}, filters=[("ticker", "in", held)])
                                               if held else [])
                         if r["state"] != "closed" or float(r.get("filled") or 0.0) > 0}
        self._external[env] = {
            "positions": [p for p in positions if (p.get("ticker") or "").startswith(series)
                          and p["ticker"] not in known_tickers],
            "orders": [o for o in resting if (o.get("ticker") or "").startswith(series)
                       and o.get("order_id") not in known_orders and o.get("client_order_id") not in elsewhere]}
        self._ready[env] = True
        self._audit("reconciled", now, env=env, detail={"live": len([b for b in self._brackets.values()
                                                                     if b["env"] == env]),
                                                         "external": {k: len(v) for k, v in self._external[env].items()}})
        return {"ok": True, "external": self._external[env]}

    # -- live feed (WebSocket primary, polling backup) ----------------------------------------------
    def attach_stream(self, env: str, stream: TradingStream, now: int) -> None:
        self.streams[env] = stream
        self._sync_streams(now)
        self._stream_health(env, now)

    def detach_stream(self, env: str, now: int) -> None:
        self.streams.pop(env, None)
        self._stream_health(env, now)

    def stream_ok(self, env: str) -> bool:
        st = self.streams.get(env)
        if st is None:
            return False
        try:
            h = st.health() or {}
        except Exception:
            return False
        return bool(h.get("connected")) and not h.get("stale")

    def stream_quote(self, env: str, ticker: str, now: int) -> dict | None:
        """The stream's last quote for the market, if the stream is healthy and the quote is fresh enough."""
        got = self._stream_quotes.get(f"{env}:{ticker}")
        if got is None or not self.stream_ok(env) or now - got[1] > self.config.stream_quote_max_age_ms:
            return None
        return got[0]

    def stream_tickers(self, env: str, now: int) -> set[str]:
        """Markets the stream should watch: open brackets' markets plus any pending ticket's market."""
        out = {b["ticker"] for b in self.live_brackets() if b["env"] == env}
        out |= {t["ticker"] for t in self._tickets.values() if t["env"] == env and now - t["created_ts"] <= 60_000}
        return out

    def _sync_streams(self, now: int) -> None:
        for env, st in list(self.streams.items()):
            try:
                st.subscribe_tickers(self.stream_tickers(env, now))
            except Exception:
                log.warning("stream subscription update failed", exc_info=True)

    def _stream_health(self, env: str, now: int) -> None:
        ok, prev = self.stream_ok(env), self._stream_up.get(env)
        if ok and not prev:
            self._force_poll(env)  # anything that happened before the feed came up is caught by one poll
            self._audit("stream_up", now, env=env, reason="Live feed: WebSocket. Polling continues as the backup "
                                                          f"every {self.config.reconcile_backup_ms // 1000} s.")
        elif prev and not ok:
            self._audit("stream_down", now, env=env, reason="Live feed lost; back to polling every "
                                                            f"{self.config.reconcile_every_ms / 1000:g} s.")
        if ok or prev is not None:
            self._stream_up[env] = ok

    def _force_poll(self, env: str) -> None:
        for b in self._brackets.values():
            if b["env"] == env:
                self._last_poll.pop(b["id"], None)

    async def on_stream_event(self, ev: dict, now: int) -> None:
        """One event from a `TradingStream`. Applies the transition the poller would, at most once."""
        env, kind = ev.get("env"), ev.get("type")
        if env not in self.gateways:
            return
        async with self._lock:
            try:
                if kind == "quote":
                    await self._stream_on_quote(env, ev, now)
                elif kind == "order" and isinstance(ev.get("order"), dict):
                    await self._stream_on_order(env, ev["order"], now)
                elif kind == "fill":
                    await self._stream_on_fill(env, ev, now)
                if kind in ("fill", "order") and env in self._ext_next:
                    self._ext_next[env] = min(self._ext_next[env], now + 1_000)  # someone traded: look soon
                elif kind == "health":
                    self._stream_health(env, now)
                elif kind == "resync":
                    self._force_poll(env)
            except Exception as exc:
                self._audit("error", now, code="stream", reason=f"{type(exc).__name__}: {exc}"[:300], env=env)

    async def _stream_on_quote(self, env: str, ev: dict, now: int) -> None:
        t = ev.get("ticker")
        if not t:
            return
        q = {k: ev.get(k) for k in ("ticker", "yes_bid", "yes_ask", "no_bid", "no_ask")}
        self._stream_quotes[f"{env}:{t}"] = (q, now)
        if len(self._stream_quotes) > 64:
            self._stream_quotes.pop(next(iter(self._stream_quotes)))
        self._remember_quote(env, t, q)
        if not self._ready.get(env):
            return
        for b in [b for b in self._brackets.values() if b["env"] == env and b["ticker"] == t]:
            if b["state"] != "open" or now >= b["window_end"]:
                continue  # a stopping bracket is retried by the tick; settlement is the tick's too
            bid = _bid(b["side"], q)
            if stop_triggered(b["stop"], bid):
                await self._stop(b, bid, now)  # open -> stopping happens once: the guard

    def _entry_coid(self, b: dict) -> str | None:
        rows = self._select(ORDERS, {"bracket_id": b["id"], "role": "entry"})
        return rows[0]["client_order_id"] if rows else None

    async def _stream_on_order(self, env: str, o: dict, now: int) -> None:
        if not self._ready.get(env):
            return  # reconciliation will see it
        oid, coid = o.get("order_id"), o.get("client_order_id")
        for b in [b for b in self._brackets.values() if b["env"] == env]:
            tp = b.get("tp_order_id")
            if tp and ((oid and tp == oid) or (coid and tp == "?" + coid)):
                if tp.startswith("?"):  # the placement outcome was unknown: this is that order
                    b["tp_order_id"], b["tp_base"] = oid, b["tp_filled"]
                self._apply_tp(b, o, now)  # cumulative fill count: a repeat (or the poller's copy) is a no-op
                return
            if b["state"] in ("sending", "unknown") and coid and coid == self._entry_coid(b):
                await self._resolve_unknown(b, now)
                return
            if coid and coid.startswith(f"bfe-{b['id'][:24]}-") and b["state"] in ("open", "stopping"):
                rows = self._select(ORDERS, {"client_order_id": coid})
                if rows and rows[0]["status"] == "unknown":  # a sale whose outcome was lost: re-read the position
                    await self._sync_held(b, now)
                return

    async def _stream_on_fill(self, env: str, f: dict, now: int) -> None:
        if not self._ready.get(env):
            return
        oid, coid = f.get("order_id"), f.get("client_order_id")
        for b in [b for b in self._brackets.values() if b["env"] == env]:
            tp = b.get("tp_order_id")
            if tp and ((oid and tp == oid) or (coid and tp == "?" + coid)):
                await self._poll_tp(b, now)  # a fill is per trade: read the order's cumulative state instead
                return
            if b["state"] in ("sending", "unknown") and coid and coid == self._entry_coid(b):
                await self._resolve_unknown(b, now)
                return

    def stream_view(self, now: int) -> dict:
        out = {}
        for env in sorted(set(self.streams) | {self.modes[self.mode]}):
            st = self.streams.get(env)
            try:
                h = (st.health() or {}) if st is not None else {}
            except Exception:
                h = {}
            last = h.get("last_msg_ms")
            ok = self.stream_ok(env)
            out[env] = {"connected": bool(h.get("connected")), "last_msg_age_ms": (now - last) if last else None,
                        "stale": bool(h.get("stale", True)), "source": "websocket" if ok else "polling",
                        "attached": st is not None}
        return out

    def trades(self, since: int, now: int, limit: int | None = 100, *, ids: Iterable[str] | None = None) -> list[dict]:
        """This strategy's brackets created since `since` (newest first): entry, exit and P&L once closed (None while
        open or before any fill), for the Play scoreboard. The newest `limit` come from SQL (None = every one: the
        scoreboard's totals are over the whole record, never a silent newest-N), only those in `ids` when given, and
        every listed manual exit's audit rows come from one more query. `entry_cost` (with the entry fee) is the
        break-even's source."""
        filters: list = [("created_ts", ">=", since)]
        if ids is not None:
            ids = list(ids)
            if not ids:
                return []
            filters.append(("id", "in", ids))
        rows = {r["id"]: r for r in self._select(BRACKETS, filters=filters, order_by=["-created_ts"], limit=limit)}
        rows.update({v["id"]: v for v in self._watched()
                     if v["created_ts"] >= since and (ids is None or v["id"] in ids)})
        out = []
        for b in sorted(rows.values(), key=lambda r: r["created_ts"], reverse=True)[:limit]:
            closed = b["state"] == "closed"
            pnl = (round((b.get("proceeds") or 0.0) - (b.get("tp_fees") or 0.0) - (b.get("exit_fees") or 0.0)
                         - (b.get("entry_cost") or 0.0), 4) if closed and (b.get("filled") or 0) > 0 else None)
            out.append({"id": b["id"], "env": b["env"], "ticker": b["ticker"], "side": b["side"], "state": b["state"],
                        "created_ts": b["created_ts"], "window_end": b["window_end"], "filled": b.get("filled") or 0.0,
                        "entry_avg": b.get("entry_avg"), "entry_cost": b.get("entry_cost"),
                        "take_profit": b.get("take_profit"), "stop_index_below": b.get("stop_index_below"), "stop_index_above": b.get("stop_index_above"),
                        "exit": b.get("exit_reason"), "exit_avg": b.get("exit_avg"), "exit_ts": b.get("exit_ts"),
                        "fair_up": b.get("fair_up"), "pnl": pnl, "pnl_source": None})
        sources = self._pnl_sources([t["id"] for t in out if t["pnl"] is not None and t["exit"] == "manual"],
                                    [t["id"] for t in out if t["pnl"] is not None and t["exit"] == "settled"])
        for t in out:
            if t["pnl"] is not None:
                t["pnl_source"] = sources.get(t["id"], "fills")
        return out

    def _pnl_sources(self, manual_ids: list[str], settled_ids: list[str] | None = None) -> dict[str, str]:
        """{bracket id: source} where a closed trade's P&L is not purely from exchange fills: "estimated" for the
        manual exits whose price had to be estimated from the bid, and a settled trade's result source (e.g.
        "kalshi_market_result"; "unrecorded" for one settled before results were read, booked at $0). One audit
        query for all of them; every other closed trade's P&L is from exchange fills."""
        settled_ids = settled_ids or []
        if not manual_ids and not settled_ids:
            return {}
        rows = self._select(AUDIT, filters=[("event", "in", ["manual_exit", "settled"]),
                                            ("bracket_id", "in", list(manual_ids) + list(settled_ids))])
        out: dict[str, str] = {}
        manual, settled = set(manual_ids), set(settled_ids)
        got: dict[str, str | None] = {}
        for r in rows:
            src = (r.get("detail") or {}).get("pnl_source")
            if r["event"] == "manual_exit" and r["bracket_id"] in manual and src != "fills":
                out[r["bracket_id"]] = "estimated"
            elif r["event"] == "settled" and r["bracket_id"] in settled:
                got[r["bracket_id"]] = src
        out.update({i: got.get(i) or "unrecorded" for i in settled_ids})
        return out

    # -- read models ------------------------------------------------------------------------------
    def bracket_view(self, b: dict) -> dict:
        keys = ("id", "env", "ticker", "side", "state", "stake", "contracts", "limit_price",
                "take_profit", "stop", "filled", "held", "entry_avg", "entry_cost", "tp_order_id", "tp_filled",
                "exit_reason", "exit_avg", "window_end", "created_ts", "stop_index_below", "stop_index_above")
        out = {k: b.get(k) for k in keys}
        if b.get("no_tp"):
            out["take_profit"] = None  # no take-profit order: it rides to settlement
        out["no_tp"] = bool(b.get("no_tp"))
        if not b.get("stop") and (b.get("stop_index_below") is not None or b.get("stop_index_above") is not None):
            out["stop"] = None  # an index stop only: no contract-price stop
        bid = self.last_bid(b)
        unreal = None
        held, filled = float(b.get("held") or 0.0), float(b.get("filled") or 0.0)
        if bid is not None and held > 0 and filled > 0 and b.get("entry_cost") is not None:
            unreal = round(held * bid - self._fee(held, bid, self.config.fee_rate)
                           - float(b["entry_cost"]) * held / filled, 2)
        out.update(bid=bid, unrealized=unreal)
        return out

    def mode_view(self, now: int) -> dict:
        gated = {
            "sandbox": ["demo trading key configured", "human present", "kill switch off"],
            "shadow": ["human present", "kill switch off"],
            "prod": ["trading_enabled setting", "production trading key configured",
                     f'typed confirmation "{PROD_CONFIRM}"', "human present", "kill switch off"],
        }
        modes = {}
        for m in self.modes:
            r = self.mode_ready(m, now) if m == self.mode else (
                GateResult(False, "not_configured", "Not configured.") if m == "prod" and not self._prod_configured()
                else GateResult(False, "not_configured", "No demo trading key.") if m == "sandbox"
                and self.gateways.get(self.modes[m]) is None
                else GateResult(False, "not_configured", "Shadow trading is not available on this server.")
                if m == "shadow" and self.gateways.get(self.modes[m]) is None else OK)
            modes[m] = {"gated_on": gated[m], "label": self.mode_labels[m], "ready": r.ok, "reason": r.reason or None}
        return {"mode": self.mode, "env": self.modes[self.mode], "label": self.mode_labels[self.mode], "modes": modes}

    def status(self, now: int) -> dict:
        env = self.modes[self.mode]
        r = self.mode_ready(self.mode, now)
        reasons = [] if r.ok else [r.reason]
        if not self.present(now):
            reasons.append("Nobody is at the screen (no recent heartbeat).")
        return {**self.mode_view(now), "enabled": self.config.enabled, "strategy": self.strategy,
                "venue": self.venue,
                "configured": {e: g is not None for e, g in self.gateways.items()},
                "locked": bool(reasons), "reasons": reasons, "killed": self.killed(), "present": self.present(now),
                "ready": dict(self._ready), "limits": asdict(self.config), "exposure": self.exposure(env, now),
                "open": [self.bracket_view(b) for b in self.live_brackets()],
                "settling": [self.bracket_view(b) for b in self.settling_brackets()],
                "settle_warnings": self.settle_warnings(now),
                "external": self._external[env], "stream": self.stream_view(now), "recent": list(self._recent)[:20]}

    def audit(self, limit: int = 100) -> list[dict]:
        """This strategy's audit events, newest first."""
        return self._select(AUDIT, order_by=["-id"], limit=limit)
