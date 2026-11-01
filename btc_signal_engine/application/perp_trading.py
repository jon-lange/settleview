"""Human-confirmed trading of Kalshi's BTC perpetual (perps plan 2026-10-29, U6; KTD1, KTD4-KTD10, KTD13-KTD16).

A separate service, not another strategy of TradingService (KTD1: that one assumes window contracts throughout); it
copies its proven patterns: ordered gate refusals with codes, tickets bound to approval tokens, the shared kill
switch, the user's limits, persist-before-send, unknown-order resolution, `_Steps` timings and the shared audit trail
(trading_audit, strategy "perps"). One position at a time; the human approves every write with a slide.

Account (KD4 as changed by the user 2026-10-30): Settleview trades from the configured perps subaccount (0, the
PRIMARY account, in production). There are no transfers and no sweeps. Kalshi margins the whole perps balance (cross),
so every entry ticket says the whole balance backs the position; the leverage slider still sizes it from a collateral
amount the user chooses (KD5), and that collateral is what the per-trade and per-day limits count (R9). Positions in
other subaccounts (64: Kalshi's app, isolated) are shown read-only as "app_isolated": they count toward Kalshi's
notional risk limit check here but never block an entry. Anything in the configured subaccount that Settleview did not
create is "foreign" (R17): it blocks entries until the user adopts it (or, for resting orders, cancels them on Kalshi)
and never blocks exits.

Modes: "off" (nothing can be sent), "perp_demo" (Kalshi's demo exchange) and "perp_prod" (REAL MONEY: needs
`PerpConfig.prod_enabled`, a production margin client and the typed confirmation trading.PROD_CONFIRM). Stored as
trading_settings "perps.mode"; the kill switch is the shared "killed" key.

Gate, in order (the first failure refuses with its code; nothing is sent):
  1. mode on, configured and reconciled (after start, a mode switch, a stream resync or a trading pause)
  2. kill switch (entries and adds only; exits and trigger edits still go)
  3. presence (entries and adds only: the screen open and used within `presence_timeout_s`)
  4. Kalshi trading active (`/margin/exchange/status` trading_active and the market's status "active", KTD10)
  5. a fresh quote (the margin stream's cache; its own 2 s rule)
  6. one position and its sign (R2, R17, R18): an order of unknown outcome, foreign positions or orders, a diverged
     size, the opposite direction ("Close your long first"), an entry while open, an exit or trigger with nothing
     open; at approval the position's sign and size and the trigger's updated time must match the ticket (KTD9)
  7. limits on committed collateral (R9): per trade, and the one per-day total across event contracts and perps (KD7)
  8. the perps balance: known, read within BALANCE_MAX_AGE_MS, and at least the collateral (R10)
  9. Kalshi's notional risk limit headroom for the ticker
  10. the protective price within `max_band` of the touch (the band is a setting, `default_band` 0.5%, per ticket)
  11. a stop at or past the liquidation price (Kalshi's figure once the position exists, else the ticket's estimate)
      is refused `stop_past_liquidation`; Kalshi's own 400 `stop_loss_past_liquidation` maps to the same refusal
  12. a stop (or take profit) on the wrong side of the mark

Orders (KTD4, KTD5): every order is immediate-or-cancel at the protective price ("at most $P" for a buy, "at least $P"
for a sale), or fill-or-kill for all-or-nothing, or IOC at the user's limit price. Exits are reduce_only and sized from
the exchange position. The whole-position bracket (KTD6) is set right after an entry fills, always with both legs the
user chose; a failed or unknown trigger write leaves the position "unprotected" with a persistent warning and is never
retried here. A close says "Flat" only once the exchange shows zero (a fill's post_position first, else REST
positions; REST positions lag fills by one step, so for `position_lag_ms` after our own fill the fill wins).

Outcomes (R12) are built from Kalshi's recorded fills (`GET /margin/fills` since the position opened: count, price,
fees, realized_pnl) plus the funding ledger (perp_funding, one row per funding time): realized P&L = sum of Kalshi's
realized_pnl - fees + funding. What closed a position is read from its last fills: our order (client_order_id in
perp_orders) = you; a system order "etr:<trigger_id>:..." = the bracket (stop or take profit: the trigger's
`triggered_leg` when Kalshi reported it, else the leg nearest the fill price); any other system order in the configured
subaccount = a liquidation (auto-rebalance orders exist only in isolated subaccounts, per spec; REST order_reason, when
present, wins); a user order that is not ours = closed outside the app.

Locking (KTD11): the lock is held only around the gate and the send; network reads (monitor, fills, balance) happen
outside it and are applied under it. A send bumps `_writes`, so a monitor read taken across a send never overrules
what the send learnt.

Per spec only, not yet confirmed on demo (the demo account is not margin-enabled): the WebSocket fill's
client_order_id on a liquidation, `status=resting` on the order list, `order_reason` on a system order read, and
`triggered_leg` while a fired bracket is still listed. Confirmed in production (LEARNINGS 2026-10-30): the fired-leg
client id "etr:<trigger_id>:0", the whole-position bracket keeping its id and changing updated_time on an edit, the
trigger vanishing when flat, fees rounded up to $0.0001 per fill, IOC 0.5% through the touch filling at the touch,
400 stop_loss_past_liquidation, REST positions lagging fills by one step, REST fill_id equal to the stream's trade_id.

Lost answers (review fix plan 2026-10-31, P0 #2; the shared rule is domain/order_resolution.py): an order whose
answer was lost stays `unknown` until Kalshi proves its outcome. Each monitor pass looks it up by client_order_id in
the order lists (every page); a list miss is never proof of no fill. After FIND_GIVE_UP_MS, every PROOF_EVERY_MS the
fills since the send are read: a fill whose order, read by id, carries our client_order_id IS the order; with none,
the position must not have moved in the order's direction beyond what we hold, or it stays unknown (audited once
`order_unresolved`, the red "checking your order" banner stays). An entry found filled late gets the stop and take
profit the user approved with it (saved on the order row, perp_orders.stop_loss / take_profit, before the send); if
that write fails the position is `unprotected` with the no-stop warning, never plain "open". The approve holds the
lock for at most `send_deadline_s` (30 s) of venue calls: an order past it is left unknown and resolved as above, a
bracket write past it leaves the position unprotected. Nothing queues behind that send (B3#12): another approval, an
adopt or a mode change waits at most `lock_wait_s` (2 s) for the lock, then is refused `busy` with nothing sent, so
no request hangs for the length of someone else's send. (The lock still covers the gate and the send together: that
is what keeps two approvals from both passing the limits.) Stream fills count only with our subaccount on them; a fill
without one triggers a reconcile instead (its post_position is never applied).

Units: prices are dollars per contract (4 dp), counts whole contracts, money dollars, times UTC ms.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from typing import Any, Protocol

from btc_signal_engine.application.actor import actor
from btc_signal_engine.application.ports import Repository
from btc_signal_engine.application.settings_store import get_setting, put_setting
from btc_signal_engine.application.trading import (BALANCE_MAX_AGE_MS, OK, PROD_CONFIRM, SHARED_SETTINGS, GateResult,
                                                   _money, _Steps)
from btc_signal_engine.domain import days
from btc_signal_engine.domain.fmt import signed_usd, usd
from btc_signal_engine.domain import perps as pm
from btc_signal_engine.domain.markets import PERP_ENVS, PerpSpec, perp
from btc_signal_engine.domain.order_resolution import (FIND_GIVE_UP_MS, PROOF_EVERY_MS, UNKNOWN, candidate_orders,
                                                        decide)

log = logging.getLogger(__name__)

STRATEGY = "perps"
POSITIONS, ORDERS, TRIGGERS, FUNDING = "perp_positions", "perp_orders", "perp_triggers", "perp_funding"
AUDIT, SETTINGS = "trading_audit", "trading_settings"
MODES = ("off", *PERP_ENVS)
LIVE = ("sending", "unknown", "open", "protected", "unprotected", "closing", "diverged")
HELD = ("open", "protected", "unprotected", "diverged", "closing")  # states with contracts on the exchange
ENTRY_ACTIONS, EXIT_ACTIONS = ("entry", "add"), ("reduce", "close")
FILLS = ("market", "all_or_nothing", "limit")
EPS = 1e-9
VIEW_TTL_MS = 1_000  # view() reuses its kill switch and limits reads this long (~20 views a second, screen open)
AT_RISK = "Your whole perps balance ({}) backs this position: a big enough move can lose all of it."
LIMITS_TEXT = ("Per trade caps the collateral of one entry or add. Per day is one total across event-contract stakes "
               "and perps collateral (your day). The per-window limit does not apply to perps.")


class MarginGateway(Protocol):
    """What the service needs from Kalshi's margin API (adapters/kalshi/margin.py MarginClient implements it).

    Fixed-point fields come back as Decimal, date-times as "<name>_ts" epoch ms. Exceptions carry `definitive`
    (True: the write certainly did not happen), `status` (HTTP, 0 = no answer) and the response `body` (Kalshi's
    {"code", ...}); anything without `definitive = True` is an unknown outcome."""

    env: str
    subaccount: int

    async def exchange_status(self) -> dict: ...

    async def market(self, ticker: str) -> dict: ...

    async def risk_parameters(self) -> dict: ...

    async def fee_tiers(self) -> dict: ...

    async def notional_risk_limit(self) -> dict: ...

    async def balance(self, *, compute_available_balance: bool) -> dict: ...

    async def positions(self, *, subaccount: int | None = None, ticker: str | None = None) -> list[dict]: ...

    async def risk(self) -> dict: ...

    async def orders(self, *, ticker: str | None = None, subaccount: int | None = None, status: str | None = None,
                     min_ts: int | None = None, max_ts: int | None = None, limit: int | None = None,
                     cursor: str | None = None) -> dict: ...

    async def order(self, order_id: str) -> dict: ...

    async def fills(self, *, subaccount: int | None = None, min_ts: int | None = None, max_ts: int | None = None,
                    limit: int | None = None, cursor: str | None = None) -> dict: ...

    async def funding_estimate(self, ticker: str) -> dict: ...

    async def funding_history(self, *, start_date: Any, end_date: Any, ticker: str | None = None,
                              subaccount: int | None = None) -> list[dict]: ...

    async def create_order(self, *, ticker: str, side: str, count: Any, price: Any, client_order_id: str,
                           time_in_force: str, reduce_only: bool, purpose: str) -> dict: ...

    async def exit_triggers(self, ticker: str, *, kind: str | None = None) -> list[dict]: ...

    async def set_exit_trigger(self, ticker: str, *, stop_loss_price: Any = None,
                               take_profit_price: Any = None) -> dict: ...

    async def update_exit_trigger(self, ticker: str, trigger_id: str, *, stop_loss_price: Any = None,
                                  take_profit_price: Any = None) -> dict: ...

    async def delete_exit_triggers(self, ticker: str, *, kind: str | None = None) -> None: ...


class QuoteSource(Protocol):
    """The margin stream's PerpQuoteCache: quote() is None when stale (why_none() says why); attributes bid, ask,
    bid_size, ask_size, age_ms, last_price, reference_price, settlement_mark, liquidation_mark, funding_rate,
    next_funding_ms."""

    def quote(self) -> Any: ...

    def age_ms(self) -> int | None: ...

    def why_none(self) -> str | None: ...


@dataclass(frozen=True)
class PerpConfig:
    prod_enabled: bool = False  # master switch for perp_prod (real money)
    approval_ttl_s: int = 10
    presence_timeout_s: int = 180
    default_band: float = 0.005  # KTD4: protective price 0.5% through the touch
    max_band: float = 0.10  # the widest band (or limit distance) a ticket may use; Kalshi's own app sent ~8%
    amber: float = 0.10  # liquidation distance bands (fractions of the mark) for the position card
    red: float = 0.05
    position_lag_ms: int = 5_000  # our own fill beats REST positions for this long (positions lag one step)
    # the most an approval spends on venue calls while it holds the lock (the client's own retries and its 409 /
    # lost-reply list reads can add up to minutes); past it the order is left unknown and resolved by the monitor
    send_deadline_s: float = 30.0
    # how long another write (an approval, adopt, a mode change) waits for the lock held by a send before it is
    # refused `busy` (nothing sent); it never queues for the whole send
    lock_wait_s: float = 2.0


class SendDeadline(RuntimeError):
    """A venue write did not answer within the approval's deadline: it may have happened (not definitive)."""

    definitive = False
    status = 0


def _d(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def _usd4(x: float) -> str:
    return usd(abs(x), dp=4)


def _signed4(x: float) -> str:
    return signed_usd(x, dp=4)


def _px(p: float) -> str:
    return f"${p:,.4f}"


def _n(q: float) -> str:
    return f"{q:g}"


def _at(p: float | None) -> str:
    """' @ $price' for a fill, or that it is not known (an order found in Kalshi's lists carries no average)."""
    return f" @ {_px(p)}" if p is not None else " (fill price not yet read from Kalshi)"


def _fills_avg(fills: list[dict]) -> tuple[float | None, float | None]:
    """(average price, total fees) of an order's fills, rounded to $0.0001; (None, None) without fills."""
    n = sum(float(f["count"]) for f in fills)
    if n <= 0:
        return None, None
    return (round(sum(float(f["count"]) * float(f["price"]) for f in fills) / n, 4),
            round(sum(float(f["fees"]) for f in fills), 4))


async def _answer(value: Any) -> Any:
    return value


async def _gather(*reads: Awaitable[Any]) -> list[Any]:
    """Independent reads at once (one tick no longer waits for each in turn): each one's answer or the Exception it
    raised, in order. A cancellation is not an answer: it propagates."""
    got = await asyncio.gather(*reads, return_exceptions=True)
    for g in got:
        if isinstance(g, BaseException) and not isinstance(g, Exception):
            raise g
    return got


def _err_code(exc: Exception) -> str | None:
    """Kalshi's error code from a rejection ({"code": ...} body), or None."""
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    body = getattr(exc, "body", None)
    if isinstance(body, str):
        try:
            got = json.loads(body)
        except ValueError:
            return None
        if isinstance(got, dict) and got.get("code"):
            return str(got["code"])
    return None


BOUND = ("env", "action", "side", "count", "protective_price", "time_in_force", "collateral", "leverage",
         "stop_loss", "take_profit", "limit_price", "band")


def ticket_hash(token: str, ticket: dict) -> str:
    """Binds an approval token to the ticket fields the human saw, including the position and trigger it was
    prepared against (KTD9)."""
    canon: dict[str, Any] = {k: ticket.get(k) for k in BOUND}
    for k in ("count", "protective_price", "collateral", "leverage", "stop_loss", "take_profit", "limit_price",
              "band"):
        if canon[k] is not None:
            canon[k] = f"{float(canon[k]):.6f}"
    b = ticket.get("bound") or {}
    canon["bound"] = [b.get("position_sign"), None if b.get("position_size") is None
                      else f"{float(b['position_size']):.4f}", b.get("trigger_updated_time")]
    return hashlib.sha256((token + json.dumps(canon, sort_keys=True)).encode("utf-8")).hexdigest()


class PerpTradingService:
    day_tz: tzinfo = days.zone(None)  # the user's day zone (bootstrap sets the one it resolved; domain/days.py)

    def __init__(self, repo: Repository, gateways: dict[str, MarginGateway | None], config: PerpConfig, *,
                 quotes: dict[str, QuoteSource] | None = None, spec: PerpSpec | None = None,
                 limits_fn: Callable[[], dict] | None = None,
                 committed_today_fn: Callable[[str, int], float] | None = None,
                 notify_fn: Callable[[dict], None] | None = None,
                 access_check: Callable[[MarginGateway], Awaitable[str | None]] | None = None,
                 id_fn: Callable[[], str] = lambda: uuid.uuid4().hex,
                 token_fn: Callable[[], str] = lambda: secrets.token_urlsafe(24), now: int = 0) -> None:
        """`gateways` per env (perp_demo / perp_prod; None = not configured). `quotes` per env: the margin stream's
        quote cache. `limits_fn()` -> {"per_trade", "per_day", ...} ($, None = no limit; PlayLimits.for_trading); a
        ValueError refuses entries `limits_unreadable`; without it there are no app limits. `committed_today_fn(env,
        now)` -> KD7's one daily total (play_limits.CommittedToday.total, this service included); without it the
        per-day limit counts this service's collateral only. `access_check(gateway)` -> None when the account may
        trade perps there, else the plain-English reason it may not (U8: margin enabled and the configured subaccount
        listed by Kalshi); it runs first in every reconcile, and a reason keeps the service not ready with that
        reason."""
        self.repo = repo
        self.config = config
        self.spec = spec or perp("kalshi_btc_perp")
        self.gateways = {e: gateways.get(e) for e in PERP_ENVS}
        self.quotes = dict(quotes or {})
        self._limits_fn = limits_fn
        self._committed_today_fn = committed_today_fn
        self._notify = notify_fn
        self._access_check = access_check
        self._id = id_fn
        self._token = token_fn
        self._lock = asyncio.Lock()
        self._tickets: dict[str, dict] = {}
        self._last_seen: int | None = None
        self._writes = 0  # bumped by every send (a read taken across it must not overrule it)
        # client_order_id -> the stream's latest post_position for it (None: none yet), while its send is in flight
        # (from _send until approve's _enter / _exit returns, under the lock). The send path alone applies the fill
        # of an in-flight order: the stream and the monitor leave it alone, so every fill is counted exactly once.
        self._inflight: dict[str, float | None] = {}
        # view() only: the kill switch and the limits, each with the `now` it was read at (VIEW_TTL_MS); emptied by
        # every write of this service. The gate never reads these.
        self._view_memo: dict[str, tuple[int, Any]] = {}
        self._saved: dict[tuple[str, str], dict] = {}  # (table, id) -> the row as last written, without updated_ms
        self._deadline: float | None = None  # loop time by which approve's venue calls must be done (under the lock)
        self._proof_next: dict[str, int] = {}  # order id -> next lost-order proof check (order_resolution)
        self._reset_exchange()
        saved = self._setting("mode", {}).get("mode")
        self.mode = saved if saved in MODES else "off"
        if saved is not None and saved not in MODES:
            self._audit("mode_invalid", now, code="mode_invalid",
                        reason=f"The stored perps mode {saved!r} is not one of {', '.join(MODES)}; perps are off.")
        if self.mode == "perp_prod" and not self._prod_configured():
            self._set_mode("off", now, "Perps production is not configured; perps are off.")
        self._load()

    # -- state ------------------------------------------------------------------------------------------------------
    def _reset_exchange(self) -> None:
        self._ready = False
        self._ready_reason = "Not yet reconciled with Kalshi after start-up; nothing can be sent until it is."
        self._market: dict | None = None  # market row, risk parameters, fee rate
        self._status: dict | None = None  # {trading_active, reason, at_ms}
        self._was_paused = False
        self._balance: dict | None = None  # {equity, available, at_ms}
        self._balance_reason = "not read yet"
        self._risk_limit: dict | None = None  # {limit, usage}
        self._risk_limit_reason = "not read yet"
        self._ex_size = 0.0  # the configured subaccount's signed size from the last REST positions read
        self._ex_entry: float | None = None
        self._ex_unrealized: float | None = None
        # (signed size, at ms, source "order" | "post_position" | "rest"): our own order's fills or a stream fill's
        # post_position, trusted over REST positions for position_lag_ms
        self._hint: tuple[float, int, str] | None = None
        self._kalshi_liq: float | None = None
        self._mark: float | None = None
        self._others: list[dict] = []  # positions in other subaccounts (read-only, app_isolated)
        self._foreign_orders: list[dict] = []
        self._funding: dict | None = None  # {rate, next_ms}
        self._seen_fills: set[str] = set()
        self._outcome: dict | None = None
        self._no_stop_warned: str | None = None  # position id already warned (once)
        self._trigger_reason: str | None = None  # why the trigger is failed / unprotected
        self._market_row: dict | None = None  # the last GET /margin/markets/{ticker}

    def _load(self) -> None:
        env = self.env
        self._pos: dict | None = None
        self._trigger: dict | None = None
        if env is None:
            return
        rows = self.repo.select(POSITIONS, {"env": env}, filters=[("state", "in", LIVE)], order_by=["-opened_ms"])
        self._pos = rows[0] if rows else None
        if self._pos is not None:
            self._mark_saved(POSITIONS, self._pos)
            t = self.repo.select(TRIGGERS, {"position_id": self._pos["id"]}, order_by=["-updated_ms"], limit=1)
            self._trigger = t[0] if t else None
            if self._trigger is not None:
                self._mark_saved(TRIGGERS, self._trigger)

    @property
    def env(self) -> str | None:
        return None if self.mode == "off" else self.mode

    @property
    def ticker(self) -> str:
        return self.spec.ticker(self.env or PERP_ENVS[0])

    def _gw(self) -> MarginGateway | None:
        return None if self.env is None else self.gateways.get(self.env)

    def _quote_source(self) -> QuoteSource | None:
        return None if self.env is None else self.quotes.get(self.env)

    # -- persistence, settings, audit -------------------------------------------------------------------------------
    def _skey(self, key: str) -> str:
        return key if key in SHARED_SETTINGS else f"{STRATEGY}.{key}"

    def _setting(self, key: str, default: Any) -> Any:
        return get_setting(self.repo, {"key": self._skey(key)}, default, table=SETTINGS)

    def _put_setting(self, key: str, value: Any, now: int) -> None:
        put_setting(self.repo, {"key": self._skey(key)}, value, now, table=SETTINGS)
        self._view_memo.clear()

    def _for_view(self, key: str, now: int, read: Callable[[], Any]) -> Any:
        """view()'s database reads, reused for VIEW_TTL_MS of `now` until this service writes anything."""
        hit = self._view_memo.get(key)
        if hit is not None and 0 <= now - hit[0] < VIEW_TTL_MS:
            return hit[1]
        got = read()
        self._view_memo[key] = (now, got)
        return got

    def _audit(self, event: str, now: int, *, code: str | None = None, reason: str | None = None,
               detail: dict | None = None, position: dict | None = None) -> dict:
        row = {"ts": now, "event": event, "env": self.env or "perps", "bracket_id": (position or {}).get("id"),
               "session_id": None, "code": code, "reason": reason, "detail": detail, "actor": actor(),
               "strategy": STRATEGY}
        self.repo.insert(AUDIT, [row])
        self._view_memo.clear()
        log.info("perps %s %s %s", event, code or "", reason or "")
        if self._notify:
            try:
                self._notify(row)
            except Exception:
                log.warning("perps notify failed", exc_info=True)
        return row

    @contextlib.asynccontextmanager
    async def _write_lock(self):
        """The service lock for a write path, waited for at most `lock_wait_s`: yields True once held, False when a
        send still holds it (the caller refuses `busy`; B3#12)."""
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=float(self.config.lock_wait_s))
        except TimeoutError:
            yield False
            return
        try:
            yield True
        finally:
            self._lock.release()

    def _busy(self, now: int) -> dict:
        return self._refuse(GateResult(False, "busy", "Another perps order is still being sent; nothing was sent. "
                                                      "Try again in a moment."), now)

    def _refuse(self, r: GateResult, now: int, detail: dict | None = None) -> dict:
        self._audit("refused", now, code=r.code, reason=r.reason, detail=detail)
        return {"ok": False, "code": r.code, "reason": r.reason}

    def _mark_saved(self, table: str, row: dict) -> None:
        self._saved[(table, row["id"])] = {k: v for k, v in row.items() if k != "updated_ms"}

    def _unchanged(self, table: str, row: dict) -> bool:
        """True when `row` equals what was last written for its id (updated_ms aside): the monitor skips the write."""
        return self._saved.get((table, row["id"])) == {k: v for k, v in row.items() if k != "updated_ms"}

    def _save_pos(self, now: int, *, if_changed: bool = False) -> None:
        p = self._pos
        if if_changed and self._unchanged(POSITIONS, p):
            return
        p["updated_ms"] = now
        self.repo.insert(POSITIONS, [dict(p)], ignore_conflicts=False)
        self._mark_saved(POSITIONS, p)
        self._view_memo.clear()
        if p["state"] not in LIVE:
            self._saved.pop((POSITIONS, p["id"]), None)
            if self._trigger is not None:
                self._saved.pop((TRIGGERS, self._trigger["id"]), None)
            self._pos = None
            self._trigger = None

    def _save_order(self, o: dict, now: int) -> None:
        o["updated_ms"] = now
        self.repo.insert(ORDERS, [dict(o)], ignore_conflicts=False)
        self._view_memo.clear()

    def _save_trigger(self, now: int, *, if_changed: bool = False) -> None:
        t = self._trigger
        if if_changed and self._unchanged(TRIGGERS, t):
            return
        t["updated_ms"] = now
        self.repo.insert(TRIGGERS, [dict(t)], ignore_conflicts=False)
        self._mark_saved(TRIGGERS, t)
        self._view_memo.clear()

    def _outcome_set(self, kind: str, text: str, now: int) -> dict:
        self._outcome = {"kind": kind, "text": text, "at_ms": now}
        return self._outcome

    # -- mode, presence, kill switch (KTD16: the shared "killed" setting) ------------------------------------------
    def _prod_configured(self) -> bool:
        return bool(self.config.prod_enabled and self.gateways.get("perp_prod") is not None)

    def _set_mode(self, mode: str, now: int, reason: str) -> None:
        self.mode = mode
        self._put_setting("mode", {"mode": mode}, now)
        self._audit("mode", now, code=mode, reason=reason)

    async def set_mode(self, mode: str, now: int, confirm: str | None = None) -> dict:
        if mode not in MODES:
            return {"ok": False, "code": "invalid", "reason": f"mode must be {', '.join(MODES)}"}
        async with self._write_lock() as held:
            if not held:
                return self._busy(now)
            # Leaving production with a real position open (or an order still being resolved) would stop watching it
            # here: refused until it is closed (the user's call, 2026-10-30). Switching to production again is a no-op.
            if self.mode == "perp_prod" and mode != "perp_prod" and self._pos is not None:
                return self._refuse(GateResult(False, "position_open", "Close your production position first: perps "
                                               "stay in production while it is open."), now)
            if mode == "perp_prod":
                if (confirm or "").strip() != PROD_CONFIRM:
                    return self._refuse(GateResult(False, "confirm", f'Type exactly "{PROD_CONFIRM}" to switch '
                                                                     "perps to production."), now)
                if not self._prod_configured():
                    return self._refuse(GateResult(False, "not_configured", "Perps production is not configured: "
                                                   "turn it on and set the production trading key."), now)
            elif mode == "perp_demo" and self.gateways.get("perp_demo") is None:
                return self._refuse(GateResult(False, "not_configured", "No demo perps trading key is "
                                                                        "configured."), now)
            self._set_mode(mode, now, f"Perps switched to {mode}.")
            self._reset_exchange()
            self._load()
        if mode != "off":
            await self.reconcile(now)
        return {"ok": True, "mode": self.mode, "ready": self._ready}

    def heartbeat(self, now: int, visible: bool = True) -> dict:
        if visible:
            self._last_seen = now
        return {"present": self.present(now), "last_seen": self._last_seen,
                "timeout_s": self.config.presence_timeout_s}

    def present(self, now: int) -> bool:
        return self._last_seen is not None and 0 <= now - self._last_seen <= self.config.presence_timeout_s * 1000

    def killed(self) -> bool:
        return bool(self._setting("killed", {}).get("on"))

    async def kill(self, now: int) -> dict:
        """Kill switch (shared with the event services): no new entries or adds; nothing is sent, exits and the
        exchange-held trigger stay."""
        self._put_setting("killed", {"on": True}, now)
        self._audit("kill", now, reason="Kill switch on: no new perps entries or adds.")
        return {"killed": True}

    def resume(self, now: int) -> dict:
        self._put_setting("killed", {"on": False}, now)
        self._audit("resume", now, reason="Kill switch off.")
        return {"killed": self.killed()}

    # -- exchange truth -----------------------------------------------------------------------------------------------
    def exchange_size(self, now: int) -> float:
        """The configured subaccount's signed size: our own fill / post_position for `position_lag_ms`, then REST
        positions."""
        if self._hint is not None and now - self._hint[1] <= self.config.position_lag_ms:
            return self._hint[0]
        return self._ex_size

    def _sign(self) -> int:
        p = self._pos
        if p is None or p["state"] not in HELD or p["size"] <= 0:
            return 0
        return 1 if p["direction"] == "long" else -1

    def _ours_signed(self) -> float:
        return self._sign() * (self._pos["size"] if self._pos else 0.0)

    def _note_size(self, signed: float, now: int, source: str = "order") -> None:
        self._hint = (signed, now, source)

    # -- the gate ----------------------------------------------------------------------------------------------------
    def _mode_ready(self) -> GateResult:
        if self.env is None:
            return GateResult(False, "mode_off", "Perps trading is off: choose demo or production first.")
        if self.env == "perp_prod" and not self.config.prod_enabled:
            return GateResult(False, "disabled", "Perps production trading is turned off in settings.")
        if self._gw() is None:
            return GateResult(False, "not_configured", f"No {self.env} perps trading key is configured.")
        if not self._ready:
            return GateResult(False, "not_reconciled", self._ready_reason)
        return OK

    def _paused(self) -> GateResult:
        st = self._status
        if st is None:
            return GateResult(False, "exchange_paused", "Kalshi's perps trading status is not known yet; nothing "
                                                        "can be sent.")
        if not st["trading_active"]:
            return GateResult(False, "exchange_paused", f"Kalshi has paused perps trading ({st['reason']}); nothing "
                                                        "can be sent until it resumes.")
        return OK

    def _quote(self) -> tuple[Any, GateResult]:
        src = self._quote_source()
        q = src.quote() if src is not None else None
        if q is None:
            why = (src.why_none() if src is not None else None) or "no quote source"
            return None, GateResult(False, "quote_stale", f"No fresh Kalshi perps price ({why}); nothing was sent.")
        return q, OK

    def _position_check(self, action: str, side: str | None, now: int) -> GateResult:
        """Step 6: one position, its sign, unknown and foreign state (R2, R17, R18)."""
        p = self._pos
        s = self.exchange_size(now)
        if p is not None and p["state"] in ("sending", "unknown"):
            return GateResult(False, "unknown_order", "Checking whether your last order went through; nothing else "
                                                      "is sent until it is known.")
        if action in ENTRY_ACTIONS:
            if self._foreign_orders:
                return GateResult(False, "foreign_orders", "Your perps account has resting orders Settleview did "
                                                           "not place; cancel them on Kalshi first.")
            if p is None and abs(s) > EPS:
                return GateResult(False, "foreign_position", "Your perps account holds a position Settleview did not "
                                                             "open; adopt it first.")
            if p is not None and p["state"] == "diverged":
                return GateResult(False, "diverged", f"Kalshi shows {_n(s)} contracts where Settleview expects "
                                                     f"{_n(self._ours_signed())}; adopt Kalshi's size first.")
            held = p["direction"] if p is not None and p["state"] in HELD else None
            if held is not None and side != held:
                return GateResult(False, "close_first", f"Close your {held} first.")
            if action == "entry" and held is not None:
                return GateResult(False, "position_open", f"You already hold a {held}: use Add to add to it.")
            if action == "add" and held is None:
                return GateResult(False, "no_position", "There is no open position to add to.")
            if action == "add" and p["state"] == "closing":
                return GateResult(False, "closing", "Your position is closing; nothing can be added.")
            return OK
        if p is None or p["state"] not in HELD or abs(s) <= EPS:
            return GateResult(False, "no_position", "There is no open position.")
        if (s > 0) != (p["direction"] == "long"):
            return GateResult(False, "diverged", "Kalshi's position is on the other side of Settleview's; adopt it "
                                                 "first.")
        return OK

    def _bound_check(self, t: dict, now: int) -> GateResult:
        b = t["bound"]
        s = self.exchange_size(now)
        sign = 0 if abs(s) <= EPS else (1 if s > 0 else -1)
        if t["action"] != "trigger" and (sign != b["position_sign"] or abs(abs(s) - b["position_size"]) > EPS):
            return GateResult(False, "position_changed", "Your position changed since this ticket was shown "
                                                         f"(now {_n(s)} contracts); nothing was sent.")
        if t["action"] == "trigger" and self._trigger_time() != b["trigger_updated_time"]:
            return GateResult(False, "trigger_changed", "Your stop or take profit was changed since this ticket was "
                                                        "shown; nothing was sent.")
        return OK

    def limits(self) -> tuple[dict | None, GateResult]:
        if self._limits_fn is None:
            return {"per_trade": None, "per_day": None}, OK
        try:
            got = self._limits_fn()
            lim = {k: (None if got[k] is None else float(got[k])) for k in ("per_trade", "per_day")}
        except (ValueError, KeyError, TypeError) as e:
            return None, GateResult(False, "limits_unreadable", f"Trade limits setting unreadable: {e}. Save your "
                                                                "limits again; nothing was sent.")
        return lim, OK

    def _day_total(self, now: int) -> tuple[float | None, GateResult]:
        try:
            if self._committed_today_fn is not None:
                return float(self._committed_today_fn(self.env, now)), OK
            return self.committed_by_env(now).get(self.env, 0.0), OK
        except Exception as e:
            return None, GateResult(False, "limits_unreadable", f"Today's total across event contracts and perps "
                                                                f"could not be read ({type(e).__name__}); nothing "
                                                                "was sent.")

    def _limits_check(self, t: dict, now: int) -> GateResult:
        lim, r = self.limits()
        if not r.ok:
            return r
        c = t["collateral"]
        if lim["per_trade"] is not None and c > lim["per_trade"] + EPS:
            return GateResult(False, "over_trade_limit", f"{_money(c)} of collateral is over the "
                                                         f"{_money(lim['per_trade'])} per-trade limit.")
        if lim["per_day"] is not None:
            day, r = self._day_total(now)
            if not r.ok:
                return r
            if day + c > lim["per_day"] + EPS:
                return GateResult(False, "over_day_limit", f"Today already has {_money(day)} in across event "
                                  f"contracts and perps; {_money(c)} more would pass the {_money(lim['per_day'])} "
                                  "daily limit.")
        return OK

    def _balance_check(self, collateral: float, now: int) -> GateResult:
        b = self._balance
        if b is None or now - b["at_ms"] > BALANCE_MAX_AGE_MS:
            why = self._balance_reason if b is None else f"last read {(now - b['at_ms']) // 1000} s ago"
            return GateResult(False, "balance_unknown", f"Perps balance unknown ({why}): can't check the collateral "
                                                        "fits; nothing was sent.")
        if collateral > b["available"] + EPS:
            return GateResult(False, "balance_too_small", f"{_money(collateral)} of collateral is more than the "
                                                          f"{_money(b['available'])} available in your perps "
                                                          "balance.")
        return OK

    def _risk_check(self, t: dict) -> GateResult:
        rl = self._risk_limit
        if rl is None:
            return GateResult(False, "risk_limit_unknown", f"Kalshi's notional risk limit is unknown "
                                                           f"({self._risk_limit_reason}); nothing was sent.")
        mark = self._mark or t["protective_price"]
        ours = sum(abs(o["size"]) * mark for o in self._others) + abs(self._ex_size) * mark
        used = max(rl["usage"], ours)
        if used + t["notional_worst"] > rl["limit"] + EPS:
            return GateResult(False, "over_risk_limit", f"This adds {_money(t['notional_worst'])} of notional to "
                                                        f"{_money(used)} already held, over Kalshi's "
                                                        f"{_money(rl['limit'])} risk limit for {self.ticker}.")
        return OK

    def _band_check(self, t: dict, quote: Any) -> GateResult:
        band = t["band"]
        if band is None or not 0 <= band <= self.config.max_band + EPS:
            return GateResult(False, "price_out_of_band", f"The protection band must be from 0% to "
                                                          f"{self.config.max_band:.1%}.")
        touch = quote.ask if t["order_side"] == "bid" else quote.bid
        if touch is None:
            return GateResult(False, "no_offer", "Nobody is " + ("offering" if t["order_side"] == "bid" else
                                                                 "bidding") + " right now; nothing was sent.")
        if abs(t["protective_price"] / touch - 1) > self.config.max_band + EPS:
            return GateResult(False, "price_out_of_band", f"{_px(t['protective_price'])} is more than "
                                                          f"{self.config.max_band:.1%} from the market "
                                                          f"({_px(touch)}).")
        return OK

    def _legs_check(self, direction: str, sl: float | None, tp: float | None, liq: float | None,
                    label: str) -> GateResult:
        long = direction == "long"
        if sl is not None and liq is not None and (sl <= liq + EPS if long else sl >= liq - EPS):
            return GateResult(False, "stop_past_liquidation", f"A stop at {_px(sl)} is at or past the {label} "
                                                              f"liquidation price {_px(liq)}: it could never fire "
                                                              "before liquidation.")
        if (sl is not None or tp is not None) and self._mark is None:
            return GateResult(False, "mark_unknown", "Kalshi's mark price is not known right now, so a stop or take "
                                                     "profit cannot be checked; nothing was sent.")
        if sl is not None and (sl >= self._mark - EPS if long else sl <= self._mark + EPS):
            return GateResult(False, "stop_wrong_side", f"A stop at {_px(sl)} is on the wrong side of the mark "
                                                        f"{_px(self._mark)}: it would fire at once.")
        if tp is not None and (tp <= self._mark + EPS if long else tp >= self._mark - EPS):
            return GateResult(False, "take_profit_wrong_side", f"A take profit at {_px(tp)} is on the wrong side of "
                                                               f"the mark {_px(self._mark)}: it would fire at once.")
        return OK

    def _pre_gate(self, action: str, side: str | None, now: int, *,
                  killed: bool | None = None) -> tuple[Any, GateResult]:
        """Steps 1-6 (what does not need the ticket's numbers). `killed`: view() passes its kill switch reading; every
        other caller leaves it None and the switch is read fresh."""
        r = self._mode_ready()
        if not r.ok:
            return None, r
        entry = action in ENTRY_ACTIONS
        if entry and (self.killed() if killed is None else killed):
            return None, GateResult(False, "killed", "The kill switch is on: no new perps entries or adds.")
        if entry and not self.present(now):
            return None, GateResult(False, "not_present", "Nobody is at the screen: new positions need the Perps "
                                                          "screen open, visible and used within the last "
                                                          f"{self.config.presence_timeout_s // 60} minutes.")
        r = self._paused()
        if not r.ok:
            return None, r
        q, r = self._quote() if action != "trigger" else (None, OK)  # a trigger edit needs the mark, not the book
        if not r.ok:
            return None, r
        if self._market is None:
            return None, GateResult(False, "not_reconciled", "Kalshi's market details are not known yet.")
        return q, self._position_check("close" if action == "trigger" else action, side, now)

    def _gate(self, t: dict, now: int, *, approving: bool) -> tuple[Any, GateResult]:
        q, r = self._pre_gate(t["action"], t.get("side"), now)
        if not r.ok:
            return q, r
        if approving:
            r = self._bound_check(t, now)
            if not r.ok:
                return q, r
        a = t["action"]
        if a in ENTRY_ACTIONS:
            for r in (self._limits_check(t, now), self._balance_check(t["collateral"], now), self._risk_check(t),
                      self._band_check(t, q),
                      self._legs_check(t["side"], t["stop_loss"], t["take_profit"], t["liquidation_est"],
                                       "estimated" if t["liquidation_label"] == "estimate" else "Kalshi")):
                if not r.ok:
                    return q, r
        elif a in EXIT_ACTIONS:
            r = self._band_check(t, q)
            if not r.ok:
                return q, r
        else:
            liq, label = self._liquidation()
            r = self._legs_check(self._pos["direction"], t["stop_loss"], t["take_profit"], liq,
                                 "estimated" if label == "estimate" else "Kalshi")
            if not r.ok:
                return q, r
        return q, OK

    # -- tickets -----------------------------------------------------------------------------------------------------
    def _trigger_time(self) -> str | None:
        t = self._trigger
        return t["exchange_updated_time"] if t is not None and t["state"] in ("active", "pending") else None

    def _bound_now(self, now: int) -> dict:
        s = self.exchange_size(now)
        return {"position_sign": 0 if abs(s) <= EPS else (1 if s > 0 else -1), "position_size": abs(s),
                "trigger_updated_time": self._trigger_time()}

    def _issue(self, ticket: dict, now: int) -> dict:
        token = self._token()
        ticket["created_ms"] = now
        ticket["expires_ms"] = now + self.config.approval_ttl_s * 1000
        ticket["hash"] = ticket_hash(token, ticket)
        self._tickets[hashlib.sha256(token.encode()).hexdigest()] = ticket
        for k in [k for k, t in self._tickets.items() if now - t["created_ms"] > 60_000]:
            del self._tickets[k]
        shown = self._shown(ticket)
        self._audit("proposed", now, detail=shown)
        return {"ok": True, "token": token, "ticket": shown, "expires_ms": ticket["expires_ms"]}

    @staticmethod
    def _shown(ticket: dict) -> dict:
        return {k: v for k, v in ticket.items() if not k.startswith("_") and k not in ("hash", "created_ms")}

    @staticmethod
    def _num(req: dict, key: str, *, optional: bool = False) -> float | None:
        v = req.get(key)
        if v is None and optional:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            raise ValueError(f"{key} must be a number")
        return float(v)

    async def _read_balance(self, now: int) -> None:
        gw = self._gw()
        if gw is None:
            return
        try:
            got = await gw.balance(compute_available_balance=True)
        except Exception as exc:
            got = exc
        self._apply_balance(gw, got, now)

    def _apply_balance(self, gw: MarginGateway, got: dict | Exception, now: int) -> None:
        """A balance read's answer, or the exception the read raised."""
        if isinstance(got, Exception):
            self._balance_reason = f"read failed: {type(got).__name__}"
            log.warning("perps balance read failed: %s", got)
            return
        sub = next((b for b in got["subaccount_balances"] if b.get("subaccount") == gw.subaccount), None)
        if sub is None:
            self._balance, self._balance_reason = None, f"Kalshi lists no perps subaccount {gw.subaccount}"
            return
        self._balance = {"equity": float(sub["account_equity"]), "available": float(sub["available_balance"]),
                         "at_ms": now}

    def _side_estimates(self, side: str) -> dict:
        m = self._market["row"]
        return m.get("long_leverage_estimates" if side == "long" else "short_leverage_estimates") or {}

    def _liq_est(self, side: str, count: float, entry: float, fees: float) -> float | None:
        m = self._market
        equity = self._balance["equity"] if self._balance else None
        if equity is None or equity <= 0:
            return None
        return pm.liquidation_estimate(side, equity, count, entry, self._side_estimates(side), m["imm"], m["theta"],
                                       fees_paid=fees)

    def _funding_view(self, side: str, count: float) -> dict:
        f = self._funding or {}
        rate, nxt = f.get("rate"), f.get("next_ms")
        est = None if rate is None or self._mark is None else round(pm.funding_estimate(side, rate, count,
                                                                                        self._mark), 4)
        return {"rate": rate, "next_ms": nxt, "est_amount": est}

    async def prepare(self, req: dict, now: int) -> dict:
        """An entry or add ticket (contract: action, side, collateral, leverage, stop_loss, take_profit, fill,
        limit_price, band) and its single-use approval token. Reads the perps balance first (outside the lock)."""
        steps = _Steps()
        try:
            return await self._prepare(req, now, steps)
        finally:
            steps.log(STRATEGY, "prepare")

    async def _prepare(self, req: dict, now: int, steps: _Steps) -> dict:
        try:
            action, side, fill = req.get("action"), req.get("side"), req.get("fill") or "market"
            if action not in ENTRY_ACTIONS or side not in pm.SIDES or fill not in FILLS:
                raise ValueError("action must be entry or add, side long or short, fill market, all_or_nothing or "
                                 "limit")
            collateral = round(self._num(req, "collateral"), 2)
            leverage = self._num(req, "leverage")
            sl = self._num(req, "stop_loss", optional=True)
            tp = self._num(req, "take_profit", optional=True)
            limit = self._num(req, "limit_price", optional=True)
            band = self._num(req, "band", optional=True)
            if (fill == "limit") != (limit is not None):
                raise ValueError("a limit price goes with fill \"limit\" only, and that fill needs one")
        except ValueError as e:
            return {"ok": False, "code": "invalid", "reason": f"{e}."}
        if action == "add" and (sl is not None or tp is not None):
            return {"ok": False, "code": "invalid", "reason": "An add keeps the position's stop and take profit; "
                                                              "change them on the position."}
        await self._read_balance(now)
        steps.mark("balance")
        q, r = self._pre_gate(action, side, now)
        steps.mark("pre-gate")
        if not r.ok:
            return self._refuse(r, now)
        band = self.config.default_band if band is None else band
        try:
            t = self._entry_ticket(action, side, fill, collateral, leverage, sl, tp, limit, band, q, now)
        except pm.PerpRefused as e:
            return self._refuse(GateResult(False, "invalid", f"{e}."), now)
        _, r = self._gate(t, now, approving=False)
        steps.mark("gate")
        if not r.ok:
            return self._refuse(r, now)
        return self._issue(t, now)

    def _entry_ticket(self, action: str, side: str, fill: str, collateral: float, leverage: float,
                      sl: float | None, tp: float | None, limit: float | None, band: float, q: Any,
                      now: int) -> dict:
        tick, rate = self.spec.tick, self._market["taker"]
        touch = q.ask if side == "long" else q.bid
        if touch is None:
            raise pm.PerpRefused("nobody is " + ("offering" if side == "long" else "bidding") + " right now")
        if not 0 <= band < 1:
            raise pm.PerpRefused(f"protection band must be a fraction from 0 to under 1 (got {band!r})")
        sizing = pm.size_position(collateral, leverage, touch, self.spec.count_step, self._side_estimates(side))
        n = sizing.contracts
        if fill == "limit":
            protective = _d(pm.price_str(_d(limit), tick))
            band_used = abs(float(protective) / touch - 1)
        else:
            protective = pm.protective_price(side, touch, band, tick)
            band_used = band
        p = float(protective)
        equity = self._balance["equity"] if self._balance else None
        wc = pm.worst_case(equity, n, protective, rate) if equity and equity > 0 else None
        fee_est = round(pm.fee_estimate(n * p, rate), 4)
        chips = []
        if fill == "all_or_nothing":
            chips.append("All or nothing")
        if fill == "limit":
            chips.append(f"Limit {_px(p)}")
        if fill != "limit" and abs(band - self.config.default_band) > EPS:
            chips.append(f"Band {band:.1%}")
        t = {"action": action, "side": side, "env": self.env, "ticker": self.ticker, "count": n,
             "btc": round(n * self.spec.contract_btc, 8), "notional": round(sizing.notional, 4),
             "notional_worst": round(n * p, 4), "protective_price": p,
             "protective_text": f"{'at most' if side == 'long' else 'at least'} {_px(p)}",
             "time_in_force": "fill_or_kill" if fill == "all_or_nothing" else "immediate_or_cancel",
             "fill": fill, "limit_price": limit, "band": band_used if fill == "limit" else band,
             "fee_est": fee_est, "worst_case": None if wc is None else round(wc.total, 4),
             "at_risk_balance": equity, "at_risk_text": None if equity is None else AT_RISK.format(_money(equity)),
             "liquidation_est": None, "liquidation_label": "estimate",
             "funding": self._funding_view(side, n), "collateral": collateral, "leverage": leverage,
             "max_leverage": sizing.max_leverage, "stop_loss": sl, "take_profit": tp,
             "new_avg_entry": None, "new_size": None, "new_liquidation_est": None, "remaining": None,
             "est_realized_pnl": None, "chips": chips, "bound": self._bound_now(now),
             "order_side": "bid" if side == "long" else "ask"}
        if action == "entry":
            t["liquidation_est"] = self._liq_est(side, n, p, fee_est)
        else:
            held = self._pos
            equity_ = equity if equity and equity > 0 else None
            if equity_ is not None:
                add = pm.add_to_position(side, held["size"], held["avg_entry"], n, p, equity_,
                                         self._side_estimates(side), self._market["imm"], self._market["theta"],
                                         fees_paid=fee_est)
                t.update(new_avg_entry=round(add.average_entry, 4), new_size=add.contracts,
                         new_liquidation_est=add.liquidation, liquidation_est=add.liquidation)
        return t

    async def prepare_exit(self, req: dict, now: int) -> dict:
        """A reduce (count) or close (the whole exchange position) ticket: reduce-only IOC at "at least $P"."""
        steps = _Steps()
        try:
            action = req.get("action")
            if action not in EXIT_ACTIONS:
                return {"ok": False, "code": "invalid", "reason": "action must be reduce or close."}
            q, r = self._pre_gate(action, None, now)
            steps.mark("pre-gate")
            if not r.ok:
                return self._refuse(r, now)
            p = self._pos
            held = abs(self.exchange_size(now))
            if action == "close":
                count = held
            else:
                try:
                    count = self._num(req, "count")
                except ValueError as e:
                    return {"ok": False, "code": "invalid", "reason": f"{e}."}
                step = self.spec.count_step
                if count <= 0 or abs(count / step - round(count / step)) > EPS:
                    return {"ok": False, "code": "invalid", "reason": f"Reduce by a whole number of contracts "
                                                                      f"(steps of {_n(step)})."}
                if count >= held - EPS:
                    return {"ok": False, "code": "invalid", "reason": f"You hold {_n(held)}: to sell everything, "
                                                                      "use Close."}
            band = self.config.default_band
            sell_side = "short" if p["direction"] == "long" else "long"  # the order's own direction
            touch = q.bid if p["direction"] == "long" else q.ask
            if touch is None:
                return self._refuse(GateResult(False, "no_offer", "Nobody is " + (
                    "bidding" if p["direction"] == "long" else "offering") + " right now; nothing was sent."), now)
            protective = float(pm.protective_price(sell_side, touch, band, self.spec.tick))
            avg = p["avg_entry"] or 0.0
            est = (protective - avg) * count if p["direction"] == "long" else (avg - protective) * count
            t = {"action": action, "side": p["direction"], "env": self.env, "ticker": self.ticker, "count": count,
                 "btc": round(count * self.spec.contract_btc, 8), "notional": round(count * touch, 4),
                 "protective_price": protective,
                 "protective_text": f"{'at least' if p['direction'] == 'long' else 'at most'} {_px(protective)}",
                 "time_in_force": "immediate_or_cancel", "fill": "market", "limit_price": None, "band": band,
                 "fee_est": round(pm.fee_estimate(count * protective, self._market["taker"]), 4),
                 "worst_case": None, "at_risk_balance": None, "at_risk_text": None, "liquidation_est": None,
                 "liquidation_label": "estimate", "funding": self._funding_view(p["direction"], held - count),
                 "collateral": None, "leverage": None, "stop_loss": None, "take_profit": None,
                 "new_avg_entry": None, "new_size": None, "new_liquidation_est": None,
                 "remaining": round(held - count, 4), "est_realized_pnl": round(est, 4), "chips": [],
                 "bound": self._bound_now(now), "order_side": "ask" if p["direction"] == "long" else "bid"}
            _, r = self._gate(t, now, approving=False)
            steps.mark("gate")
            if not r.ok:
                return self._refuse(r, now)
            return self._issue(t, now)
        finally:
            steps.log(STRATEGY, "prepare_exit")

    async def prepare_trigger(self, req: dict, now: int) -> dict:
        """Set, change or remove the whole-position bracket. A key left out keeps the current leg; a key given as null
        clears it; both legs null (or absent with none set) = remove the trigger."""
        steps = _Steps()
        try:
            q, r = self._pre_gate("trigger", None, now)
            steps.mark("pre-gate")
            if not r.ok:
                return self._refuse(r, now)
            cur = self._trigger if self._trigger is not None and self._trigger["state"] in ("active", "pending") \
                else None
            legs = {}
            try:
                for key, col in (("stop_loss", "stop_loss_price"), ("take_profit", "take_profit_price")):
                    legs[key] = self._num(req, key, optional=True) if key in req else (cur or {}).get(col)
                    if legs[key] is not None:
                        pm.price_str(_d(legs[key]), self.spec.tick)
            except (ValueError, pm.PerpRefused) as e:
                return {"ok": False, "code": "invalid", "reason": f"{e}."}
            liq, label = self._liquidation()
            t = {"action": "trigger", "side": self._pos["direction"], "env": self.env, "ticker": self.ticker,
                 "count": None, "protective_price": None, "time_in_force": None, "collateral": None,
                 "leverage": None, "limit_price": None, "band": None, "stop_loss": legs["stop_loss"],
                 "take_profit": legs["take_profit"], "remove": legs["stop_loss"] is None and
                 legs["take_profit"] is None, "liquidation_est": liq, "liquidation_label": label,
                 "chips": [], "bound": self._bound_now(now)}
            _, r = self._gate(t, now, approving=False)
            steps.mark("gate")
            if not r.ok:
                return self._refuse(r, now)
            return self._issue(t, now)
        finally:
            steps.log(STRATEGY, "prepare_trigger")

    @staticmethod
    def _bound(token: str, ticket: dict, shown: dict | None) -> bool:
        shown = shown or {}
        if any(k not in shown for k in (*BOUND, "bound") if ticket.get(k) is not None):
            return False
        try:
            echo = {**ticket, **{k: shown[k] for k in (*BOUND, "bound") if k in shown}}
            return ticket_hash(token, echo) == ticket["hash"]
        except (TypeError, ValueError, KeyError, AttributeError):
            return False

    # -- approval and sends ------------------------------------------------------------------------------------------
    async def approve(self, token: str, shown: dict, now: int) -> dict:
        """The slide's end. Re-checks the binding and the whole gate under the lock, then sends."""
        steps = _Steps()
        try:
            ticket = self._tickets.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)
            if ticket is None:
                return self._refuse(GateResult(False, "bad_token", "This approval was already used or never existed; "
                                                                   "nothing was sent."), now)
            if now - ticket["created_ms"] > self.config.approval_ttl_s * 1000:
                return self._refuse(GateResult(False, "expired", f"The approval expired after "
                                                                 f"{self.config.approval_ttl_s} seconds; nothing was "
                                                                 "sent."), now)
            if not self._bound(token, ticket, shown):
                return self._refuse(GateResult(False, "altered", "The ticket was changed after it was prepared; "
                                                                 "nothing was sent."), now)
            if ticket["env"] != self.env:
                return self._refuse(GateResult(False, "mode_changed", "The perps mode changed since this ticket was "
                                                                      "prepared; nothing was sent."), now)
            if ticket["action"] == "trigger":
                await self._read_triggers(now)  # a newer edit made elsewhere must refuse (KTD9); outside the lock
                steps.mark("trigger read")
            async with self._write_lock() as held:
                steps.mark("lock")
                if not held:
                    return self._busy(now)
                _, r = self._gate(ticket, now, approving=True)
                steps.mark("gate")
                if not r.ok:
                    return self._refuse(r, now)
                self._audit("approved", now, detail={"action": ticket["action"], "count": ticket["count"]})
                # B3#12: one deadline for every venue call made under the lock (the send, the bracket, the fill read)
                self._deadline = asyncio.get_running_loop().time() + float(self.config.send_deadline_s)
                try:
                    if ticket["action"] in ENTRY_ACTIONS:
                        out = await self._enter(ticket, now)
                    elif ticket["action"] in EXIT_ACTIONS:
                        out = await self._exit(ticket, now)
                    else:
                        out = await self._write_trigger_ticket(ticket, now)
                finally:
                    self._deadline = None
                    self._inflight.clear()  # the send and its bookkeeping are done (or failed): no longer in flight
                steps.mark("send")
            if self._pos is not None and self._pos["state"] == "closing":
                await self._confirm_flat(now)  # a REST read, outside the lock
                out["outcome"] = self._outcome
            out["view"] = self.view(now)
            return out
        finally:
            steps.log(STRATEGY, "approve")

    def _new_order(self, role: str, t: dict, now: int) -> dict:
        oid = self._id()
        o = {"id": oid, "env": self.env, "position_id": self._pos["id"], "client_order_id": f"svperp-{oid}",
             "order_id": None, "role": role, "side": t["order_side"], "price": t["protective_price"],
             "count": t["count"], "collateral": t.get("collateral") if role in ENTRY_ACTIONS else None,
             "time_in_force": t["time_in_force"], "reduce_only": role in EXIT_ACTIONS,
             "filled_count": 0.0, "avg_fill_price": None, "fees": None, "outcome": "sending", "reason": None,
             # an entry's approved bracket, saved with it: placed even if its fill is only found later (B3#1)
             "stop_loss": t.get("stop_loss") if role == "entry" else None,
             "take_profit": t.get("take_profit") if role == "entry" else None,
             "created_ms": now, "updated_ms": now}
        self._save_order(o, now)  # persisted BEFORE sending: a crash mid-send is resolved by client_order_id
        return o

    async def _bounded(self, aw: Awaitable[Any], what: str) -> Any:
        """Await a venue call within approve's deadline (no deadline outside approve). Past it: SendDeadline (not
        definitive: the write may have happened; the caller treats it like a lost answer)."""
        if self._deadline is None:
            return await aw
        left = self._deadline - asyncio.get_running_loop().time()
        try:
            return await asyncio.wait_for(aw, timeout=max(left, 0.05))
        except TimeoutError:
            log.warning("perps %s passed the %.0f s approval deadline", what, self.config.send_deadline_s)
            raise SendDeadline(f"Kalshi did not answer the {what} within {self.config.send_deadline_s:g} s") from None

    async def _send(self, o: dict, purpose: str) -> dict:
        self._writes += 1
        self._inflight[o["client_order_id"]] = None
        return await self._bounded(self._gw().create_order(
            ticker=self.ticker, side=o["side"], count=_d(int(round(o["count"]))) if self.spec.count_step >= 1
            else _d(o["count"]), price=_d(pm.price_str(_d(o["price"]), self.spec.tick)),
            client_order_id=o["client_order_id"], time_in_force=o["time_in_force"], reduce_only=o["reduce_only"],
            purpose=purpose), "order")

    def _record(self, o: dict, res: dict, now: int) -> float:
        filled = float(res.get("fill_count") or 0)
        avg = _f(res.get("average_fill_price"))
        per = _f(res.get("average_fee_paid"))
        o.update(order_id=res.get("order_id"), filled_count=filled, avg_fill_price=avg,
                 fees=None if per is None else round(per * filled, 4),
                 outcome="nothing_filled" if filled <= 0 else ("filled" if filled >= o["count"] - EPS else "partial"))
        self._save_order(o, now)
        return filled

    async def _read_fill_price(self, o: dict, now: int) -> bool:
        """An order found in Kalshi's lists (a 409 / lost reply) has no averages: read them from its fills."""
        if o["order_id"] is None:
            return False
        try:
            avg, fee = _fills_avg(await self._bounded(self._order_fills(o["order_id"], o["created_ms"]), "fills read"))
        except Exception as exc:
            log.warning("perps fills read for order %s failed: %s", o["order_id"], exc)
            return False
        if avg is None:
            return False
        o.update(avg_fill_price=avg, fees=fee)
        self._save_order(o, now)
        return True

    def _send_failed(self, o: dict, exc: Exception, now: int, *, new_position: bool) -> dict:
        if getattr(exc, "definitive", False):
            o.update(outcome="rejected", reason=str(exc)[:500])
            self._save_order(o, now)
            if new_position:
                self._pos.update(state="closed", close_reason="rejected", closed_ms=now)
                self._save_pos(now)
            self._audit("error", now, code="rejected", reason=str(exc)[:500])
            text = "Kalshi refused the order; nothing was sent through" + ("; nothing is open." if new_position
                                                                            else "; your position is unchanged.")
            self._outcome_set("rejected", text, now)
            return {"ok": False, "code": "rejected", "reason": text, "outcome": self._outcome}
        o.update(outcome="unknown", reason=f"{type(exc).__name__}: outcome unknown")
        self._save_order(o, now)
        self._pos["state"] = "unknown"
        self._save_pos(now)
        self._audit("error", now, code="unknown", reason=o["reason"], position=self._pos)
        text = "Checking whether your order went through; nothing else is sent until it is known."
        self._outcome_set("unknown", text, now)
        return {"ok": False, "code": "unknown", "reason": text, "outcome": self._outcome}

    async def _enter(self, t: dict, now: int) -> dict:
        new = t["action"] == "entry"
        if new:
            self._pos = {"id": self._id(), "env": self.env, "ticker": self.ticker, "subaccount": self._gw().subaccount,
                         "direction": t["side"], "size": 0.0, "avg_entry": None, "committed_collateral": 0.0,
                         "leverage_chosen": t["leverage"], "state": "sending", "close_reason": None,
                         "realized_pnl": 0.0, "fees": 0.0, "funding_total": 0.0, "opened_ms": now,
                         "closed_ms": None, "updated_ms": now}
            self._save_pos(now)
            self._trigger = None
        prev_state = self._pos["state"]
        o = self._new_order(t["action"], t, now)
        if not new:
            self._pos["state"] = "sending"
            self._save_pos(now)
        self._audit("sent", now, position=self._pos, detail={
            "client_order_id": o["client_order_id"], "role": t["action"], "collateral": t["collateral"],
            "count": t["count"], "price": t["protective_price"], "time_in_force": t["time_in_force"]})
        try:
            res = await self._send(o, "entry")
        except Exception as exc:
            if not new and getattr(exc, "definitive", False):
                self._pos["state"] = prev_state
                self._save_pos(now)
            return self._send_failed(o, exc, now, new_position=new)
        filled = self._record(o, res, now)
        p = self._pos
        if filled <= 0:
            text = f"Nothing filled {t['protective_text']}; " + ("nothing is open." if new else
                                                                  "your position is unchanged.")
            if new:
                p.update(state="closed", close_reason="nothing_filled", closed_ms=now)
            else:
                p["state"] = prev_state
            self._save_pos(now)
            self._audit("cancelled", now, code="nothing_filled", reason=text)
            self._outcome_set("nothing_filled", text, now)
            return {"ok": True, "outcome": self._outcome}
        want_trigger = new and (t["stop_loss"] is not None or t["take_profit"] is not None)
        q0, a0 = p["size"], p["avg_entry"]
        ok = None
        try:
            self._apply_entry_fill(o, filled, t, now, prev_state)
        finally:  # the bracket goes on right after the fill, before any read or wording that could fail
            if want_trigger:
                ok = await self._set_trigger(t["stop_loss"], t["take_profit"], now)
        if o["avg_fill_price"] is None and await self._read_fill_price(o, now) and self._pos is p:
            p["avg_entry"] = round((q0 * (a0 or 0.0) + filled * o["avg_fill_price"]) / (q0 + filled), 4)
            p["fees"] = round(p["fees"] + o["fees"], 4)
            self._save_pos(now)
        partial = filled < t["count"] - EPS
        if new:
            head = f"{'Long' if p['direction'] == 'long' else 'Short'} {_n(filled)}{_at(o['avg_fill_price'])}"
        elif o["avg_fill_price"] is not None:
            head = f"Added {_n(filled)}{_at(o['avg_fill_price'])}; now {_n(p['size'])} @ {_px(p['avg_entry'])}"
        else:
            head = f"Added {_n(filled)}{_at(None)}; now {_n(p['size'])}"
        text = head + (f" (fee {_usd4(o['fees'])})" if o["fees"] is not None else "")
        if partial:
            text += f". Filled {_n(filled)} of {_n(t['count'])}; the rest was cancelled"
        if want_trigger:
            text += "; " + (self._legs_text() + " on Kalshi" if ok else "no stop on Kalshi")
        text += "."
        kind = ("partial" if partial else "opened") if new else "added"
        self._audit("filled", now, code=kind, reason=text, position=p)
        self._outcome_set(kind, text, now)
        return {"ok": True, "outcome": self._outcome}

    def _apply_entry_fill(self, o: dict, filled: float, t: dict | None, now: int, prev_state: str) -> None:
        p = self._pos
        avg = o["avg_fill_price"]
        if avg is None:  # found in the order lists (no averages) and no fills read: the order's limit
            avg = p["avg_entry"] or o["price"]
        q0 = p["size"]
        p["avg_entry"] = round((q0 * (p["avg_entry"] or 0.0) + filled * avg) / (q0 + filled), 4)
        p["size"] = q0 + filled
        p["fees"] = round(p["fees"] + (o["fees"] or 0.0), 4)
        if t is not None and t.get("collateral") is not None:
            p["committed_collateral"] = round(p["committed_collateral"] + t["collateral"] * filled / t["count"], 4)
        p["state"] = prev_state if prev_state in HELD and prev_state != "closing" else "open"
        self._restate()
        self._note_size(self._ours_signed(), now)
        self._save_pos(now)

    def _restate(self) -> None:
        """open / protected / unprotected from the trigger (diverged and closing are kept)."""
        p = self._pos
        if p is None or p["state"] in ("diverged", "closing", "sending", "unknown", "closed"):
            return
        t = self._trigger
        p["state"] = "protected" if t is not None and t["state"] == "active" else (
            "unprotected" if t is not None and t["state"] == "failed" else "open")

    async def _exit(self, t: dict, now: int) -> dict:
        p = self._pos
        # the size held before the send (the gate checked it against the ticket under this lock): the stream's fill
        # for this very order may move the post_position hint while the send is in flight
        held = abs(self.exchange_size(now))
        o = self._new_order(t["action"], t, now)
        self._audit("sent", now, position=p, detail={"client_order_id": o["client_order_id"], "role": t["action"],
                                                     "count": t["count"], "price": t["protective_price"]})
        try:
            res = await self._send(o, "exit")
        except Exception as exc:
            prev = p["state"]
            out = self._send_failed(o, exc, now, new_position=False)
            if getattr(exc, "definitive", False):
                p["state"] = prev
                self._save_pos(now)
            return out
        filled = self._record(o, res, now)
        if filled <= 0:
            text = f"Nothing sold {t['protective_text']}; your position is unchanged."
            self._outcome_set("nothing_filled", text, now)
            return {"ok": True, "outcome": self._outcome}
        if o["avg_fill_price"] is None:
            await self._read_fill_price(o, now)
        if self._pos is not p or p["state"] not in HELD:  # closed meanwhile (a stop or liquidation fill took it flat)
            return {"ok": True, "outcome": self._outcome}
        left = round(held - filled, 8)
        p["size"] = max(left, 0.0)
        post = self._inflight.get(o["client_order_id"])
        if left <= EPS and post is not None and abs(post) <= EPS:
            self._note_size(0.0, now, "post_position")  # the stream already said flat: Kalshi's word, kept
        else:
            self._note_size(self._sign_of(p) * p["size"], now)
        verb = "Bought back" if p["direction"] == "short" else "Sold"
        if left <= EPS:
            p["state"] = "closing"
            self._save_pos(now)
            text = f"{verb} {_n(filled)}{_at(o['avg_fill_price'])}; waiting for Kalshi to confirm you are flat."
            self._outcome_set("closing", text, now)
        else:
            if p["state"] == "diverged" and abs(abs(self._ex_size) - p["size"]) <= EPS:
                p["state"] = "open"
            self._restate()
            self._save_pos(now)
            text = f"{verb} {_n(filled)}{_at(o['avg_fill_price'])}; {_n(left)} still open."
            if filled < t["count"] - EPS:
                text += f" Filled {_n(filled)} of {_n(t['count'])}; the rest was cancelled."
            self._outcome_set("reduced", text, now)
        self._audit("filled", now, code=self._outcome["kind"], reason=text, position=p)
        return {"ok": True, "outcome": self._outcome}

    @staticmethod
    def _sign_of(p: dict) -> int:
        return 1 if p["direction"] == "long" else -1

    # -- triggers ----------------------------------------------------------------------------------------------------
    def _legs_text(self) -> str:
        t = self._trigger or {}
        parts = []
        if t.get("stop_loss_price") is not None:
            parts.append(f"stop {_px(t['stop_loss_price'])}")
        if t.get("take_profit_price") is not None:
            parts.append(f"take profit {_px(t['take_profit_price'])}")
        return " and ".join(parts) or "no stop or take profit"

    def _trigger_from(self, got: dict, now: int, *, if_changed: bool = False) -> None:
        """Our trigger row from Kalshi's ExitTrigger (updated_time kept as its epoch-ms text, compared verbatim);
        `if_changed` (the monitor) writes it only when it differs from the row last written."""
        row = self._trigger if self._trigger is not None and self._trigger["position_id"] == self._pos["id"] else {
            "id": self._id(), "env": self.env, "position_id": self._pos["id"], "fired_leg": None,
            "fired_order_id": None, "created_ms": now}
        status = got.get("status")
        row.update(exchange_trigger_id=got.get("id"), stop_loss_price=_f(got.get("stop_loss_price")),
                   take_profit_price=_f(got.get("take_profit_price")),
                   state={"active": "active", "pending_on_entry": "pending", "failed": "failed",
                          "filled": "filled", "canceled": "canceled", "unknown": "pending"}.get(status, "active"),
                   exchange_updated_time=None if got.get("updated_ts") is None else str(got["updated_ts"]))
        if got.get("triggered_leg"):
            row["fired_leg"] = got["triggered_leg"]
        if got.get("triggered_order_id"):
            row["fired_order_id"] = got["triggered_order_id"]
        self._trigger = row
        self._save_trigger(now, if_changed=if_changed)

    def _trigger_failed(self, reason: str, now: int, sl: float | None, tp: float | None) -> None:
        row = self._trigger if self._trigger is not None and self._trigger["position_id"] == self._pos["id"] else {
            "id": self._id(), "env": self.env, "position_id": self._pos["id"], "exchange_trigger_id": None,
            "exchange_updated_time": None, "fired_leg": None, "fired_order_id": None, "created_ms": now}
        row.update(state="failed", stop_loss_price=sl, take_profit_price=tp)
        self._trigger = row
        self._trigger_reason = reason
        self._save_trigger(now)
        self._restate()
        self._save_pos(now)
        self._warn_no_stop(reason, now)

    def _warn_no_stop(self, reason: str, now: int) -> None:
        if self._no_stop_warned == self._pos["id"]:
            return
        self._no_stop_warned = self._pos["id"]
        self._audit("no_stop", now, code="no_stop", reason=f"Position open, no stop on Kalshi: {reason}",
                    position=self._pos)

    async def _set_trigger(self, sl: float | None, tp: float | None, now: int) -> bool:
        """The whole-position bracket right after an entry fills (KTD6): both legs; no retry beyond the client's."""
        self._writes += 1
        try:
            got = await self._bounded(self._gw().set_exit_trigger(
                self.ticker, stop_loss_price=None if sl is None else pm.price_str(_d(sl), self.spec.tick),
                take_profit_price=None if tp is None else pm.price_str(_d(tp), self.spec.tick)), "stop write")
        except Exception as exc:
            why = ("Kalshi says the stop is past the liquidation price" if _err_code(exc) ==
                   "stop_loss_past_liquidation" else
                   ("Kalshi refused it" if getattr(exc, "definitive", False) else "Kalshi did not confirm it"))
            self._trigger_failed(f"{why} ({type(exc).__name__}).", now, sl, tp)
            return False
        self._trigger_reason = None
        self._trigger_from(got, now)
        self._restate()
        self._save_pos(now)
        return True

    async def _write_trigger_ticket(self, t: dict, now: int) -> dict:
        gw, cur = self._gw(), self._trigger
        live = cur is not None and cur["state"] in ("active", "pending") and cur.get("exchange_trigger_id")
        sl, tp = t["stop_loss"], t["take_profit"]
        self._writes += 1
        try:
            if t["remove"]:
                await self._bounded(gw.delete_exit_triggers(self.ticker), "trigger removal")
            else:
                args = {"stop_loss_price": None if sl is None else pm.price_str(_d(sl), self.spec.tick),
                        "take_profit_price": None if tp is None else pm.price_str(_d(tp), self.spec.tick)}
                if live:
                    got = await self._bounded(gw.update_exit_trigger(self.ticker, cur["exchange_trigger_id"], **args),
                                              "trigger change")
                else:
                    got = await self._bounded(gw.set_exit_trigger(self.ticker, **args), "trigger write")
        except Exception as exc:
            if getattr(exc, "definitive", False):
                if _err_code(exc) == "stop_loss_past_liquidation":
                    r = GateResult(False, "stop_past_liquidation", f"Kalshi says a stop at {_px(sl)} is at or past "
                                                                   "your liquidation price; nothing changed.")
                else:
                    r = GateResult(False, "trigger_rejected", f"Kalshi refused the change ({str(exc)[:200]}); your "
                                                              "stop and take profit are unchanged.")
                return self._refuse(r, now)
            self._trigger_failed("Kalshi did not confirm the change; re-checking.", now, sl, tp)
            text = "Kalshi did not confirm the change to your stop or take profit; re-checking."
            self._outcome_set("trigger_unknown", text, now)
            return {"ok": False, "code": "unknown", "reason": text, "outcome": self._outcome}
        if t["remove"]:
            if cur is not None:
                cur["state"] = "canceled"
                self._save_trigger(now)
            self._trigger = None
            text = "Stop and take profit removed: Kalshi holds nothing for this position."
        else:
            self._trigger_reason = None
            self._trigger_from(got, now)
            text = f"{self._legs_text()[0].upper()}{self._legs_text()[1:]} set on Kalshi."
        self._restate()
        self._save_pos(now)
        self._audit("trigger", now, code="removed" if t["remove"] else "set", reason=text, position=self._pos)
        self._outcome_set("trigger_removed" if t["remove"] else "trigger_set", text, now)
        return {"ok": True, "outcome": self._outcome}

    async def _read_triggers(self, now: int) -> None:
        gw = self._gw()
        if gw is None or self._pos is None:
            return
        try:
            got = await gw.exit_triggers(self.ticker)
        except Exception as exc:
            log.warning("perps trigger read failed: %s", exc)
            return
        self._apply_triggers(got, now)

    def _apply_triggers(self, got: list[dict], now: int) -> None:
        if self._pos is None or self._pos["state"] not in HELD:
            return
        bracket = next((t for t in got if t.get("kind") in (None, "bracket")), None)
        cur = self._trigger
        if bracket is not None:
            if bracket.get("status") == "failed":
                self._trigger_from(bracket, now, if_changed=True)
                why = bracket.get("status_reason") or "no reason given"
                self._trigger_reason = f"Kalshi reports your stop failed ({why})."
                self._warn_no_stop(self._trigger_reason, now)
            else:
                self._trigger_from(bracket, now, if_changed=True)
                self._trigger_reason = None
        elif cur is not None and cur["state"] in ("active", "pending"):
            self._trigger_failed("Kalshi no longer lists your stop while the position is open.", now,
                                 cur.get("stop_loss_price"), cur.get("take_profit_price"))
        self._restate()
        self._save_pos(now, if_changed=True)  # a tick that changed nothing writes nothing

    # -- adopt --------------------------------------------------------------------------------------------------------
    async def adopt(self, size_shown: float, now: int) -> dict:
        """R17: take Kalshi's position in the configured subaccount as Settleview's (size_shown: the signed size the
        screen showed)."""
        async with self._write_lock() as held:
            if not held:
                return self._busy(now)
            r = self._mode_ready()
            if not r.ok:
                return self._refuse(r, now)
            s = self.exchange_size(now)
            try:
                shown = float(size_shown)
            except (TypeError, ValueError):
                return {"ok": False, "code": "invalid", "reason": "size_shown must be a number."}
            if abs(shown - s) > EPS:
                return self._refuse(GateResult(False, "position_changed", f"Kalshi now shows {_n(s)} contracts, not "
                                                                          f"{_n(shown)}; nothing was adopted."), now)
            p = self._pos
            if abs(s) <= EPS:
                return self._refuse(GateResult(False, "no_position", "There is nothing to adopt."), now)
            direction = "long" if s > 0 else "short"
            if p is None:
                self._pos = p = {"id": self._id(), "env": self.env, "ticker": self.ticker,
                                 "subaccount": self._gw().subaccount, "direction": direction, "size": abs(s),
                                 "avg_entry": self._ex_entry, "committed_collateral": 0.0, "leverage_chosen": 0.0,
                                 "state": "open", "close_reason": None, "realized_pnl": 0.0, "fees": 0.0,
                                 "funding_total": 0.0, "opened_ms": now, "closed_ms": None, "updated_ms": now}
            elif p["state"] in ("sending", "unknown"):
                return self._refuse(GateResult(False, "unknown_order", "Checking whether your last order went "
                                                                       "through; adopt after it is known."), now)
            else:
                p.update(direction=direction, size=abs(s), avg_entry=self._ex_entry or p["avg_entry"], state="open")
            self._restate()
            self._save_pos(now)
            text = f"Adopted Kalshi's {direction} of {_n(abs(s))} @ {_px(p['avg_entry'] or 0.0)}."
            self._audit("adopted", now, code="adopted", reason=text, position=p)
            self._outcome_set("adopted", text, now)
        return {"ok": True, "outcome": self._outcome, "view": self.view(now)}

    # -- monitor and reconcile -----------------------------------------------------------------------------------------
    async def refresh_status(self, now: int) -> None:
        """Kalshi's trading status (KTD10). When a pause ends, the service is not ready until a reconcile completes."""
        gw = self._gw()
        if gw is None:
            return
        try:
            st, m = await _gather(gw.exchange_status(), gw.market(self.ticker))
            for got in (st, m):
                if isinstance(got, Exception):
                    raise got
        except Exception as exc:
            self._status = {"trading_active": False, "reason": f"status unreadable: {type(exc).__name__}",
                            "at_ms": now}
            self._was_paused = True
            return
        active = bool(st.get("exchange_active", True)) and bool(st.get("trading_active")) and \
            m.get("status") == "active"
        reason = None
        if not active:
            reason = ("exchange under maintenance" if not st.get("exchange_active", True) else
                      "trading paused" if not st.get("trading_active") else f"market {m.get('status')}")
        self._status = {"trading_active": active, "reason": reason, "at_ms": now,
                        "resume_ms": st.get("exchange_estimated_resume_ts")}
        self._market_row = m
        if self._market is not None:
            self._market["row"] = m
        if not active:
            if not self._was_paused:
                self._audit("paused", now, code="exchange_paused", reason=f"Kalshi perps: {reason}.")
            self._was_paused = True
        elif self._was_paused:
            self._was_paused = False
            self._ready = False
            self._ready_reason = "Kalshi trading resumed; Settleview is re-checking your account before anything " \
                                 "is sent."
            self._audit("resumed", now, code="exchange_resumed", reason=self._ready_reason)

    async def reconcile(self, now: int) -> dict:
        """Read everything from Kalshi (outside the lock), resolve unknown orders, compare positions, then ready."""
        gw = self._gw()
        if gw is None:
            return {"ok": False, "reason": "not configured"}
        if self._access_check is not None:
            try:
                why = await self._access_check(gw)
            except Exception as exc:
                why = f"Kalshi's perps account could not be checked ({type(exc).__name__}); nothing can be sent "                       "until it is."
            if why:
                self._ready, self._ready_reason = False, why
                self._audit("error", now, code="no_access", reason=why)
                return {"ok": False, "reason": why}
        try:
            await self.refresh_status(now)
            m = self._market_row if self._market_row is not None else await gw.market(self.ticker)
            rp = await gw.risk_parameters()
            fees = await gw.fee_tiers()
            self._market = {"row": m, "theta": float(rp["liquidation_margin_ratio_threshold"]),
                            "imm": float(rp["initial_margin_multiplier"][self.ticker]),
                            "taker": float(fees["taker_fee_rates"][self.ticker]),
                            "maker": float((fees.get("maker_fee_rates") or {}).get(self.ticker) or 0.0)}
            await self._resolve_unknown(now)
            await self._monitor_reads(now, reconciling=True)
        except Exception as exc:
            self._ready = False
            self._ready_reason = f"Reconciliation with Kalshi failed ({type(exc).__name__}); nothing can be sent " \
                                 "until it succeeds."
            self._audit("error", now, code="reconcile", reason=self._ready_reason)
            log.warning("perps reconcile failed", exc_info=True)
            return {"ok": False, "reason": type(exc).__name__}
        self._ready = True
        self._ready_reason = None
        self._audit("reconciled", now, detail={"size": self.exchange_size(now), "others": len(self._others),
                                                "foreign_orders": len(self._foreign_orders)})
        return {"ok": True}

    async def tick(self, now: int) -> None:
        """The monitor (U8 runs it): status; a reconcile when not ready; else positions, risk, triggers, funding,
        balance and unknown orders."""
        if self._gw() is None:
            return
        await self.refresh_status(now)
        if not self._ready:
            if self._status and self._status["trading_active"]:
                await self.reconcile(now)
            return
        try:
            await self._resolve_unknown(now)
            await self._monitor_reads(now, reconciling=False)  # the balance too
        except Exception as exc:
            log.warning("perps monitor read failed: %s", exc)

    async def _monitor_reads(self, now: int, *, reconciling: bool) -> None:
        """Positions, risk, triggers (with a position), resting orders, the funding estimate, the notional risk limit
        and the balance, read at once. A failed positions / risk / triggers / orders read aborts (raised, in that
        order), applying nothing; the other three are each logged or recorded on their own. The balance is applied
        before the monitor while reconciling, after it on a tick."""
        gw = self._gw()
        writes = self._writes
        positions, risk, triggers, resting, est, rl, bal = await _gather(
            gw.positions(ticker=self.ticker), gw.risk(),
            gw.exit_triggers(self.ticker) if self._pos is not None else _answer([]),
            gw.orders(ticker=self.ticker, subaccount=gw.subaccount, status="resting"),
            gw.funding_estimate(self.ticker), gw.notional_risk_limit(), gw.balance(compute_available_balance=True))
        for got in (positions, risk, triggers, resting):
            if isinstance(got, Exception):
                raise got
        try:
            if isinstance(est, Exception):
                raise est
            self._funding = {"rate": est.get("funding_rate"), "next_ms": est.get("next_funding_ts")}
            if est.get("mark_price") is not None and self._mark is None:
                self._mark = float(est["mark_price"])
        except Exception as exc:
            log.warning("perps funding estimate failed: %s", exc)
        try:
            if isinstance(rl, Exception):
                raise rl
            lim = (rl.get("notional_value_risk_limits_by_market_ticker") or {}).get(self.ticker)
            if lim is None:  # Kalshi: a market's own limit overrides the account default
                lim = rl.get("default_notional_value_risk_limit")
            eff = rl.get("effective_account_notional_value_risk_limit")
            cands = [float(x) for x in (lim, eff) if x is not None]
            if not cands:
                raise ValueError(f"Kalshi gives no notional risk limit for {self.ticker}")
            self._risk_limit = {"limit": min(cands), "usage": float(
                (rl.get("current_usage_by_market_ticker") or {}).get(self.ticker) or 0)}
            self._risk_limit_reason = None
        except Exception as exc:
            self._risk_limit, self._risk_limit_reason = None, f"read failed: {type(exc).__name__}"
        if reconciling:
            self._apply_balance(gw, bal, now)
        # stale: a send started while reading, or one is still in flight: its own result is newer than these reads
        await self._apply_monitor(positions, risk, triggers, resting, now,
                                  stale=writes != self._writes or bool(self._inflight))
        if not reconciling:
            self._apply_balance(gw, bal, now)

    async def _apply_monitor(self, positions: list[dict], risk: dict, triggers: list[dict], resting: dict,
                             now: int, *, stale: bool) -> None:
        sub = self._gw().subaccount
        mine = [p for p in positions if p.get("subaccount") == sub and p.get("market_ticker") == self.ticker]
        self._others = [{"subaccount": p["subaccount"], "kind": "app_isolated", "size": float(p["position"]),
                         "avg_entry": _f(p.get("entry_price")), "unrealized_pnl": _f(p.get("unrealized_pnl"))}
                        for p in positions if p.get("subaccount") != sub and p.get("market_ticker") == self.ticker
                        and abs(float(p["position"])) > EPS]
        coids = [o["client_order_id"] for o in resting.get("orders", []) if o.get("client_order_id") is not None]
        known = {o["client_order_id"] for o in self.repo.select(ORDERS, {"env": self.env},
                                                                 filters=[("client_order_id", "in", coids)])}             if coids else set()
        self._foreign_orders = [o for o in resting.get("orders", []) if o.get("client_order_id") not in known
                                and float(o.get("remaining_count") or 0) > 0]
        rk = next((r for r in risk.get("positions", []) if r.get("subaccount") == sub
                   and r.get("market_ticker") == self.ticker), None)
        self._kalshi_liq = _f(rk.get("estimated_liquidation_price")) if rk else None
        if rk and rk.get("mark_price") is not None:
            self._mark = float(rk["mark_price"])
        src = self._quote_source()
        q = src.quote() if src is not None else None
        if q is not None and q.liquidation_mark is not None:
            self._mark = float(q.liquidation_mark)
        if stale:  # a send happened while reading: its own result is newer than these reads
            return
        self._ex_size = float(mine[0]["position"]) if mine else 0.0
        self._ex_entry = _f(mine[0].get("entry_price")) if mine else None
        self._ex_unrealized = _f(mine[0].get("unrealized_pnl")) if mine else None
        if self._pos is not None:
            self._apply_triggers(triggers, now)
        await self._compare(now)
        await self._read_funding(now)

    async def _compare(self, now: int) -> None:
        """Exchange size vs ours: flat -> close from fills; different -> diverged (R17)."""
        p = self._pos
        if p is None or p["state"] not in HELD:
            return
        if p["state"] == "closing":  # "Flat" only on Kalshi's word: a post_position of 0 or REST showing zero
            h = self._hint
            if abs(self._ex_size) <= EPS or (h is not None and h[2] == "post_position" and abs(h[0]) <= EPS):
                await self._finish(now)
            return
        s = self.exchange_size(now)
        if abs(s) <= EPS:
            await self._finish(now)
            return
        if abs(s - self._ours_signed()) > EPS:
            if p["state"] != "diverged":
                p["state"] = "diverged"
                self._save_pos(now)
                self._audit("diverged", now, code="diverged", reason=f"Kalshi shows {_n(s)} contracts where "
                                                                     f"Settleview expects {_n(self._ours_signed())}.",
                            position=p)
        elif p["state"] == "diverged":
            p["state"] = "open"
            self._restate()
            self._save_pos(now)

    async def _confirm_flat(self, now: int) -> None:
        h = self._hint
        if h is not None and h[2] == "post_position" and abs(h[0]) <= EPS:
            await self._finish(now)  # the stream's post_position 0 for our own close: Kalshi's word, no REST read
            return
        gw = self._gw()
        try:
            positions = await gw.positions(ticker=self.ticker)
        except Exception as exc:
            log.warning("perps flat check failed: %s", exc)
            return
        mine = [x for x in positions if x.get("subaccount") == gw.subaccount and x.get("market_ticker") == self.ticker]
        if not mine or abs(float(mine[0]["position"])) <= EPS:
            self._ex_size = 0.0
            self._note_size(0.0, now, "rest")
            await self._finish(now)

    async def _resolve_unknown(self, now: int) -> None:
        """R18: an order saved 'sending' / 'unknown' is found by its client_order_id in Kalshi's order list."""
        p = self._pos
        if p is None or p["state"] not in ("sending", "unknown"):
            return
        if self._inflight:  # its send is still in flight: the send applies its result, not this read (nor a give-up)
            return
        rows = self.repo.select(ORDERS, {"position_id": p["id"]}, filters=[("outcome", "in", ("sending", "unknown"))])
        if not rows:
            p["state"] = "open" if p["size"] > 0 else "closed"
            self._save_pos(now)
            return
        o = rows[0]
        found = await self._listed(o["client_order_id"])
        if found is None:
            # A list miss proves nothing (domain/order_resolution.py): after FIND_GIVE_UP_MS the fills and the
            # position are checked every PROOF_EVERY_MS; only "no fill" and "no move" close it as never sent.
            if now - o["created_ms"] <= FIND_GIVE_UP_MS or now < self._proof_next.get(o["id"], 0):
                return
            self._proof_next[o["id"]] = now + PROOF_EVERY_MS
            found = await self._prove_lost(o, now)
            if found is None:
                return  # unprovable: stays unknown, the "checking your order" banner stays
            if found.get("not_found"):
                self._proof_next.pop(o["id"], None)
                o.update(outcome="rejected", reason="not on Kalshi: no fill and no position change since it was sent")
                self._save_order(o, now)
                text = "Your order never reached Kalshi; " + (
                    "nothing is open." if p["size"] <= 0 else "your position is unchanged.")
                if p["size"] <= 0:
                    p.update(state="closed", close_reason="not_found", closed_ms=now)
                else:
                    p["state"] = "open"
                    self._restate()
                self._save_pos(now)
                self._audit("cancelled", now, code="not_found", reason=text)
                self._outcome_set("not_found", text, now)
                return
        self._proof_next.pop(o["id"], None)
        fills = await self._order_fills(found["order_id"], o["created_ms"])
        filled = float(found.get("fill_count") or 0)
        avg, fee = _fills_avg(fills) if filled > 0 else (None, None)
        o.update(order_id=found["order_id"], filled_count=filled, avg_fill_price=avg, fees=fee,
                 outcome="nothing_filled" if filled <= 0 else ("filled" if filled >= o["count"] - EPS else "partial"))
        self._save_order(o, now)
        if o["role"] in EXIT_ACTIONS:
            p["size"] = max(p["size"] - filled, 0.0)
            p["state"] = "closing" if p["size"] <= EPS else "open"
            self._restate()
            self._save_pos(now)
            self._outcome_set("resolved", f"Your {o['role']} order went through: {_n(filled)} filled.", now)
            return
        if filled <= 0:
            if p["size"] <= 0:
                p.update(state="closed", close_reason="nothing_filled", closed_ms=now)
            else:
                p["state"] = "open"
                self._restate()
            self._save_pos(now)
            text = f"Your order went through but nothing filled at or better than {_px(o['price'])}; " + (
                "nothing is open." if p["state"] == "closed" else "your position is unchanged.")
            self._outcome_set("nothing_filled", text, now)
            return
        collateral = self._sent_collateral(o["client_order_id"])
        self._apply_entry_fill(o, filled, {"collateral": collateral, "count": o["count"]}
                               if collateral is not None else None, now, "open")
        # B3#1: a late fill gets the bracket the user approved with the entry (saved on its order row); a failed
        # write leaves the position "unprotected" with the no-stop warning (_set_trigger -> _trigger_failed)
        sl, tp = o.get("stop_loss"), o.get("take_profit")
        legs = None
        if o["role"] == "entry" and (sl is not None or tp is not None):
            legs = await self._set_trigger(sl, tp, now)
        text = (f"Your order went through: {'long' if p['direction'] == 'long' else 'short'} {_n(p['size'])} @ "
                f"{_px(p['avg_entry'])}.")
        if filled < o["count"] - EPS:
            text += f" Filled {_n(filled)} of {_n(o['count'])}; the rest was cancelled."
        if legs is not None:
            text += " " + (self._legs_text()[0].upper() + self._legs_text()[1:] + " set on Kalshi." if legs
                           else "No stop on Kalshi: " + (self._trigger_reason or "it could not be set."))
        self._audit("resolved", now, code="filled", reason=text, position=p)
        self._outcome_set("resolved", text, now)

    async def _listed(self, coid: str) -> dict | None:
        """The order with this client_order_id from our subaccount's order lists (every page), or None."""
        gw = self._gw()
        cursor = None
        for _ in range(20):
            got = await gw.orders(ticker=self.ticker, subaccount=gw.subaccount, cursor=cursor)
            found = next((x for x in got.get("orders", []) if x.get("client_order_id") == coid), None)
            if found is not None:
                return found
            cursor = got.get("cursor")
            if not cursor:
                return None
        return None

    async def _prove_lost(self, o: dict, now: int) -> dict | None:
        """The fills' and the position's word on an order the lists do not show (domain/order_resolution.py): the
        order (read by the id of a fill since the send, carrying our client_order_id), {"not_found": True} when no
        fill could be it and the position has not moved in its direction beyond ours, or None (unknown, noted)."""
        gw = self._gw()
        p = self._pos
        why = "Kalshi's fills could not be read"
        try:
            fills = await self._fills_since(o["created_ms"])
            ids = sorted({f["order_id"] for f in fills if f.get("order_id")})  # only the fills' orders, not every one
            known = {r["order_id"] for r in self.repo.select(ORDERS, {"env": self.env},
                                                             filters=[("order_id", "in", ids)])} if ids else set()
            cands = candidate_orders(((f.get("order_id"), f.get("created_ts"), f.get("side")) for f in fills),
                                     since_ms=o["created_ms"], book_side=o["side"], known=known)
            for oid in cands:
                got = await gw.order(oid)
                if got.get("client_order_id") == o["client_order_id"]:
                    return got
            evidence = {"filled": 0.0}
            why = "Kalshi's position moved in this order's direction and no fill read explains it"
            positions = await gw.positions(ticker=self.ticker)
            mine = [x for x in positions if x.get("subaccount") == gw.subaccount
                    and x.get("market_ticker") == self.ticker]
            ex = float(mine[0]["position"]) if mine else 0.0
            sign = 1 if o["side"] == "bid" else -1
            ours = (1 if p["direction"] == "long" else -1) * float(p["size"]) if p is not None else 0.0
            # how far Kalshi's position is past ours in this order's direction (a buy: more long / less short)
            excess = round(sign * (ex - ours), 8)
        except Exception as exc:
            evidence, excess, why = None, None, f"Kalshi's fills or positions could not be read ({type(exc).__name__})"
        if decide(evidence, excess) == UNKNOWN:
            seen = self.repo.select(AUDIT, {"strategy": STRATEGY, "event": "order_unresolved",
                                            "bracket_id": (p or {}).get("id")})
            if not any((r.get("detail") or {}).get("client_order_id") == o["client_order_id"] for r in seen):
                self._audit("order_unresolved", now, code="order_unresolved", position=p,
                            reason=(f"Could not confirm whether your {o['role']} order filled: {why}. Nothing else is "
                                    "sent until it is known; check Kalshi.")[:300],
                            detail={"client_order_id": o["client_order_id"], "excess": excess})
            return None
        return {"not_found": True}

    def _sent_collateral(self, coid: str) -> float | None:
        rows = self.repo.select(ORDERS, {"client_order_id": coid})
        return rows[0].get("collateral") if rows else None

    async def _order_fills(self, order_id: str, since_ms: int) -> list[dict]:
        return [f for f in await self._fills_since(since_ms) if f.get("order_id") == order_id]

    async def _fills_since(self, since_ms: int) -> list[dict]:
        gw = self._gw()
        out, cursor = [], None
        for _ in range(20):
            got = await gw.fills(subaccount=gw.subaccount, min_ts=max(since_ms // 1000 - 1, 0), cursor=cursor)
            out += [f for f in got.get("fills", []) if f.get("ticker") == self.ticker]
            cursor = got.get("cursor")
            if not cursor:
                break
        return out

    # -- funding ledger ----------------------------------------------------------------------------------------------
    async def _read_funding(self, now: int) -> None:
        p = self._pos
        if p is None or p["state"] not in HELD:
            return
        gw = self._gw()
        day = lambda ms: datetime.fromtimestamp(ms / 1000, tz=UTC).date()  # noqa: E731
        try:
            rows = await gw.funding_history(start_date=day(p["opened_ms"]), end_date=day(now), ticker=self.ticker,
                                            subaccount=gw.subaccount)
        except Exception as exc:
            log.warning("perps funding history failed: %s", exc)
            return
        new = [{"env": self.env, "ticker": self.ticker, "funding_ms": int(r["funding_ts"]),
                "funding_rate": float(r["funding_rate"]), "amount": float(r["funding_amount"]), "position_id": p["id"]}
               for r in rows if r.get("market_ticker", self.ticker) == self.ticker
               and int(r["funding_ts"]) >= p["opened_ms"]]
        if new:
            self.repo.insert(FUNDING, new)  # one row per (env, ticker, funding time): repeats are ignored
        total = round(sum(r["amount"] for r in self.repo.select(FUNDING, {"position_id": p["id"]})), 4)
        if abs(total - p["funding_total"]) > EPS:
            p["funding_total"] = total
            self._save_pos(now)

    # -- outcomes from fills (R12) -------------------------------------------------------------------------------------
    async def _classify(self, f: dict) -> str:
        """What a closing fill was: user_close | stop | take_profit | liquidated | closed_outside."""
        ours = {o["order_id"] for o in self.repo.select(ORDERS, {"position_id": self._pos["id"]})
                if o.get("order_id")}
        if f.get("order_id") in ours:
            return "user_close"
        coid, reason = f.get("client_order_id"), None
        if coid is None or f.get("order_source") == "system":
            try:
                o = await self._gw().order(f["order_id"])
                coid, reason = o.get("client_order_id"), o.get("order_reason")
            except Exception as exc:
                log.warning("perps order read for a fill failed: %s", exc)
        if coid and str(coid).startswith("etr:"):
            return self._leg_of(f)
        if f.get("order_source") == "system":
            return "liquidated" if reason in (None, "liquidation") else "exchange_reduced"
        return "closed_outside"

    def _leg_of(self, f: dict) -> str:
        t = self._trigger or {}
        if t.get("fired_leg") in ("stop_loss", "take_profit"):
            return "stop" if t["fired_leg"] == "stop_loss" else "take_profit"
        px = float(f["price"])
        sl, tp = t.get("stop_loss_price"), t.get("take_profit_price")
        if sl is None or tp is None:
            return "stop" if sl is not None else "take_profit"
        return "stop" if abs(px - sl) <= abs(px - tp) else "take_profit"

    async def _finish(self, now: int, reason: str | None = None) -> None:
        """The exchange is flat: close the position with its outcome from Kalshi's fills and the funding ledger."""
        p = self._pos
        if p is None or p["state"] not in HELD:
            return
        try:
            fills = await self._fills_since(p["opened_ms"])
            await self._read_funding(now)
            exit_side = "ask" if p["direction"] == "long" else "bid"
            closing = [f for f in fills if f.get("side") == exit_side]
            if reason is None and closing:
                newest = max(closing, key=lambda f: f.get("created_ts") or 0)
                reason = await self._classify(newest)
        except Exception as exc:
            log.warning("perps fills read failed: %s", exc)
            return  # retried on the next tick; the position stays as it is
        if self._pos is not p or p["state"] not in HELD:
            return
        reason = reason or "closed_outside"
        fees = round(sum(float(f["fees"]) for f in fills), 4)
        gross = sum(float(f["realized_pnl"]) for f in fills)
        net = round(gross - fees + p["funding_total"], 4)
        n = sum(float(f["count"]) for f in closing)
        avg = sum(float(f["count"]) * float(f["price"]) for f in closing) / n if n > 0 else None
        p.update(state="closed", close_reason=reason, realized_pnl=net, fees=fees, closed_ms=now, size=0.0)
        if self._trigger is not None:
            self._trigger["state"] = "filled" if reason in ("stop", "take_profit") else "canceled"
            self._save_trigger(now)
        what = f"{_n(n)} {p['direction']}" + (f" @ {_px(avg)} avg" if avg is not None else "")
        head = {"user_close": f"Flat. You closed {what}",
                "stop": f"Flat. Your stop closed {what}",
                "take_profit": f"Flat. Your take profit closed {what}",
                "liquidated": f"Flat. Kalshi liquidated {what}",
                "exchange_reduced": f"Flat. Kalshi reduced {what}",
                "closed_outside": f"Flat. Your position was closed outside Settleview ({what})"}[reason]
        text = (f"{head}. Realized P&L {_signed4(net)} (fees {_usd4(fees)}, funding "
                f"{_signed4(p['funding_total'])}).")
        self._save_pos(now)
        self._hint = None
        self._audit("closed", now, code=reason, reason=text, position=p,
                    detail={"realized_pnl": net, "fees": fees, "funding": p["funding_total"]})
        self._outcome_set("closed" if reason == "user_close" else reason, text, now)

    # -- the stream ---------------------------------------------------------------------------------------------------
    async def on_stream_event(self, ev: dict, now: int) -> None:
        """Margin stream events: `resync` reconciles over REST before any later fill is trusted (fills carry no
        sequence numbers); a `fill` updates the position at once from post_position."""
        if ev.get("env") != self.env:
            return
        kind = ev.get("type")
        if kind == "resync":
            self._ready = False
            self._ready_reason = "The Kalshi stream reconnected; re-checking your account before anything is sent."
            await self.reconcile(now)
        elif kind == "fill":
            await self._on_fill(ev, now)

    async def _on_fill(self, ev: dict, now: int) -> None:
        if ev.get("ticker") != self.ticker:
            return
        if ev.get("subaccount") is None:
            # B3#4: whose fill it is cannot be told (production fills carry `subaccount`): its post_position is never
            # applied; the account is re-read over REST instead, as after a stream resync
            log.warning("perps stream fill %s without a subaccount; reconciling instead", ev.get("trade_id"))
            self._ready = False
            self._ready_reason = "A Kalshi fill arrived without its subaccount; re-checking your account before " \
                                 "anything is sent."
            await self.reconcile(now)
            return
        if ev.get("subaccount") != self._gw().subaccount:
            return  # another subaccount's fill (Kalshi's app, isolated): never ours to apply
        tid = ev.get("trade_id")
        if tid in self._seen_fills:
            return
        self._seen_fills.add(tid)
        post = float(ev["post_position"])
        p = self._pos
        coid = ev.get("client_order_id") or ""
        ours = bool(coid) and bool(self.repo.select(ORDERS, {"client_order_id": coid}))
        self._note_size(post, now, "post_position")
        if coid in self._inflight:  # our own order, its send still in flight: the send applies this fill
            self._inflight[coid] = post
            return
        if p is None or p["state"] not in HELD:
            return  # ours are recorded by the send itself; anything else is foreign (the monitor shows it)
        if abs(post) <= EPS:
            reason = None
            if ours:
                reason = "user_close"
            elif coid.startswith("etr:"):
                reason = self._leg_of(ev)
            elif ev.get("order_source") == "system":
                reason = "liquidated"
            await self._finish(now, reason)
            return
        if ours:
            return
        if ev.get("order_source") == "system" and (post > 0) == (p["direction"] == "long") and \
                abs(post) < p["size"] - EPS:
            gone = p["size"] - abs(post)
            p["size"] = abs(post)
            self._save_pos(now)
            label = "Your stop or take profit" if coid.startswith("etr:") else "Kalshi liquidated"
            realized = await self._realized_of(tid, p["opened_ms"])
            kind = "trigger_partial" if coid.startswith("etr:") else "liquidated_partial"
            text = (f"{label} {_n(gone)} of your {_n(gone + abs(post))} {p['direction']} @ {_px(float(ev['price']))};"
                    f" {_n(abs(post))} remain open.")
            if realized is not None:
                text += f" Realized on it {_signed4(realized)} incl. its fee."
            self._audit("exchange_fill", now, code=kind, reason=text, position=p)
            self._outcome_set(kind, text, now)
            return
        await self._compare(now)

    async def _realized_of(self, trade_id: str, since_ms: int) -> float | None:
        try:
            for f in await self._fills_since(since_ms):
                if f.get("fill_id") == trade_id:
                    return round(float(f["realized_pnl"]) - float(f["fees"]), 4)
        except Exception as exc:
            log.warning("perps fill read failed: %s", exc)
        return None

    # -- limits figure (KD7) ------------------------------------------------------------------------------------------
    def committed_by_env(self, now: int) -> dict[str, float]:
        """Collateral committed today (the user's day, `day_tz`) per env: each entry or add's collateral, in full while its outcome is
        unknown, in proportion to what filled once known, nothing when nothing filled. A CommittedToday source."""
        day0 = days.day_start(now, self.day_tz)
        out = {e: 0.0 for e in PERP_ENVS}
        for o in self.repo.select(ORDERS, filters=[("created_ms", ">=", day0)]):
            if o["role"] not in ENTRY_ACTIONS or o.get("collateral") is None or o["env"] not in out:
                continue
            if o["outcome"] in ("sending", "unknown"):
                share = 1.0
            elif o["outcome"] in ("nothing_filled", "rejected"):
                share = 0.0
            else:
                share = min(o["filled_count"] / o["count"], 1.0) if o["count"] else 0.0
            out[o["env"]] += float(o["collateral"]) * share
        return {e: round(v, 4) for e, v in out.items()}

    # -- read model ---------------------------------------------------------------------------------------------------
    def market_state(self) -> dict:
        """What the read model (application/perp_view.py) adds to view(): Kalshi's trading status {trading_active,
        reason, at_ms, resume_ms} or None, the market {row, theta, imm, taker, maker} or None (both None until read),
        the monitor's REST funding estimate {rate, next_ms} or None, and the active env's quote source (None when
        off)."""
        return {"status": self._status, "market": self._market, "market_row": self._market_row,
                "funding": self._funding, "quote_source": self._quote_source()}

    def record(self, last: int) -> dict:
        """Your record (Play's "Your record" card, for perps): the active env's last `last` closed positions, newest
        first, with their realized P&L from recorded fills (fees and funding included), plus wins / losses / total.
        Off (no env): an empty record with the reason."""
        env = self.env
        if env is None:
            return {"env": None, "reason": "Perps trading is off.", "rows": [], "wins": 0, "losses": 0, "pnl": 0.0}
        rows = self.repo.select(POSITIONS, {"env": env, "state": "closed"}, order_by=["-closed_ms"], limit=last)
        out = [{k: r.get(k) for k in ("id", "direction", "avg_entry", "committed_collateral", "leverage_chosen",
                                      "close_reason", "realized_pnl", "fees", "funding_total", "opened_ms", "closed_ms")}
               for r in rows]
        pnl = [float(r["realized_pnl"] or 0.0) for r in out]
        return {"env": env, "reason": None, "rows": out, "wins": sum(1 for x in pnl if x > 0),
                "losses": sum(1 for x in pnl if x < 0), "pnl": round(sum(pnl), 4)}

    def record_failure(self, what: str, now: int, exc: Exception) -> None:
        """KTD16: a shared control (kill, resume, heartbeat) that failed on this service, logged and audited by the
        caller that fanned it out; it never stops the other services."""
        log.error("perps %s failed: %s", what, exc, exc_info=exc)
        self._audit("error", now, code=f"{what}_failed", reason=f"The perps service's {what} failed "
                                                                 f"({type(exc).__name__}: {str(exc)[:200]}).")

    def _liquidation(self) -> tuple[float | None, str]:
        if self._kalshi_liq is not None and self._pos is not None:
            return self._kalshi_liq, "kalshi"
        p = self._pos
        if p is None or not p["size"] or p["avg_entry"] is None or self._market is None:
            return None, "estimate"
        try:
            return self._liq_est(p["direction"], p["size"], p["avg_entry"], 0.0), "estimate"
        except pm.PerpRefused:
            return None, "estimate"

    def _position_view(self, now: int) -> dict | None:
        p = self._pos
        if p is None:
            return None
        liq, src = self._liquidation()
        mark = self._mark
        dist = band = None
        if liq is not None and mark is not None and p["state"] in HELD:
            dist = round(pm.liquidation_distance(p["direction"], mark, liq), 4)
            band = pm.liquidation_band(dist, amber=self.config.amber, red=self.config.red)
        adopted = p["leverage_chosen"] == 0.0
        return {"id": p["id"], "direction": p["direction"], "size": p["size"], "avg_entry": p["avg_entry"],
                "state": p["state"], "collateral": None if adopted else p["committed_collateral"],
                "leverage": None if adopted else p["leverage_chosen"], "mark": mark,
                "unrealized_pnl": self._ex_unrealized, "realized_pnl": p["realized_pnl"], "fees": p["fees"],
                "funding_total": p["funding_total"], "liquidation_price": liq, "liquidation_source": src,
                "distance": dist, "band": band, "opened_ms": p["opened_ms"]}

    def _trigger_view(self) -> dict:
        t = self._trigger
        if t is None or t["state"] in ("canceled", "filled"):
            return {"state": "none", "stop_loss": None, "take_profit": None, "updated_time": None, "reason": None}
        return {"state": t["state"], "stop_loss": t["stop_loss_price"], "take_profit": t["take_profit_price"],
                "updated_time": t["exchange_updated_time"],
                "reason": self._trigger_reason if t["state"] == "failed" else None}

    def _can(self, action: str, now: int, *, killed: bool | None = None,
             limits: tuple[dict | None, GateResult] | None = None) -> tuple[bool, str | None]:
        """view()'s "can" flags; `killed` and `limits` are view()'s own readings (None = read them here)."""
        _, r = self._pre_gate(action, self._pos["direction"] if self._pos and action == "add" else
                              (None if action != "entry" else "long"), now, killed=killed)
        if action == "entry" and not r.ok and r.code == "close_first":
            r = GateResult(False, "position_open", "You already hold a position: add to it, reduce it or close it.")
        if action == "entry" and r.ok:
            lim, r2 = self.limits() if limits is None else limits
            if r2.ok:
                r2 = self._balance_check(0.0, now)
            r = r2
        return r.ok, None if r.ok else r.reason

    def _banners(self, now: int) -> list[dict]:
        out = []
        p = self._pos
        if p is not None and p["state"] in ("sending", "unknown"):
            out.append({"code": "unknown_order", "severity": "red",
                        "text": "Checking whether your order went through. New entries wait until it is known."})
        if p is not None and p["state"] == "unprotected":
            why = self._trigger_reason or "setting it failed"
            out.append({"code": "no_stop", "severity": "red",
                        "text": f"Position open, no stop on Kalshi: {why} Close it, or set the stop again."})
        s = self.exchange_size(now)
        if p is not None and p["state"] == "diverged":
            out.append({"code": "diverged", "severity": "amber",
                        "text": f"Kalshi shows {_n(s)} contracts where Settleview expects {_n(self._ours_signed())}. "
                                "Adopt Kalshi's size to trade again; exits still work."})
        elif p is None and abs(s) > EPS:
            out.append({"code": "diverged", "severity": "amber",
                        "text": f"Your perps account holds {_n(s)} contracts Settleview did not open. Adopt them to "
                                "trade here; new entries wait until then."})
        elif self._foreign_orders:
            out.append({"code": "diverged", "severity": "amber",
                        "text": f"{len(self._foreign_orders)} resting order(s) in your perps account were not placed "
                                "by Settleview. Cancel them on Kalshi; new entries wait until then."})
        st = self._status
        if st is not None and not st["trading_active"]:
            out.append({"code": "paused", "severity": "amber",
                        "text": f"Kalshi has paused perps trading ({st['reason']}). Nothing can be sent until it "
                                "resumes."})
        elif not self._ready and self.env is not None and st is not None:
            out.append({"code": "paused", "severity": "info", "text": self._ready_reason})
        src = self._quote_source()
        if self.env is not None and src is not None and src.quote() is None:
            out.append({"code": "stale", "severity": "amber",
                        "text": f"Kalshi's perps price is not fresh ({src.why_none()}); nothing can be sent."})
        pv = self._position_view(now)
        if pv is not None and pv["band"] in ("amber", "red"):
            out.append({"code": "near_liquidation", "severity": pv["band"],
                        "text": f"The mark {_px(pv['mark'])} is {pv['distance']:.1%} from your liquidation price "
                                f"{_px(pv['liquidation_price'])} ({pv['liquidation_source']})."})
        return out

    def readiness(self) -> dict:
        """view()'s mode, ready and ready_reason, without its database reads (/api/health)."""
        ready = self._ready and self.env is not None
        return {"mode": self.mode, "ready": ready,
                "ready_reason": None if ready else ("Perps trading is off." if self.env is None else self._ready_reason)}

    def view(self, now: int) -> dict:
        """The read model fields this service owns (contract View); U7 adds market, quote, exchange and settings.
        The kill switch and the limits are reused for VIEW_TTL_MS until this service writes; the gate reads them fresh.
        Today's total is read every time."""
        killed = self._for_view("killed", now, self.killed)
        lim, r = self._for_view("limits", now, self.limits)
        ok_e, why_e = self._can("entry", now, killed=killed, limits=(lim, r))
        ok_x, why_x = self._can("close", now)
        ok_t, why_t = self._can("trigger", now)
        day, r2 = self._day_total(now)  # fresh: other services' stakes and orders count too
        foreign = list(self._others)
        if abs(self.exchange_size(now) - self._ours_signed()) > EPS and (self._pos is None or
                                                                       self._pos["state"] in HELD):
            gw = self._gw()
            foreign.insert(0, {"subaccount": gw.subaccount if gw else 0, "kind": "foreign",
                               "size": self.exchange_size(now), "avg_entry": self._ex_entry,
                               "unrealized_pnl": self._ex_unrealized})
        b = self._balance
        return {"server_ms": now, **self.readiness(), "killed": killed,
                "balance": None if b is None else dict(b), "balance_reason": None if b else self._balance_reason,
                "position": self._position_view(now), "trigger": self._trigger_view(),
                "unknown_order": self._pos is not None and self._pos["state"] in ("sending", "unknown"),
                "foreign": foreign, "banners": self._banners(now),
                "can": {"entry": ok_e, "entry_reason": why_e, "exit": ok_x, "exit_reason": why_x,
                        "trigger": ok_t, "trigger_reason": why_t},
                "outcome": self._outcome,
                "limits": {"per_trade": lim["per_trade"] if r.ok else None, "per_day": lim["per_day"] if r.ok
                           else None, "committed_today": day if r2.ok else None,
                           "text": LIMITS_TEXT if r.ok else r.reason}}
