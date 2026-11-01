"""Polymarket US TRADING gateway (real money). Implements the application's `TradingGateway` protocol with the
official `polymarket_us` SDK (`AsyncPolymarketUS`); nothing here signs or sends HTTP by hand.

Orders (docs.polymarket.us/api-reference/orders/create-order.md, concepts/orders.md, read 2026-10-29):
* `client.orders.create({marketSlug, intent, type: ORDER_TYPE_LIMIT, price: {value, currency: USD}, quantity, tif,
  participateDontInitiate, synchronousExecution})` -> {id, executions: [{type, order{state, cumQuantity,
  leavesQuantity, avgPx, commissionNotionalTotalCollected}, lastPx, lastShares, commissionNotionalCollected}]}.
  "The price always refers to the YES side." Intents: BUY_LONG buys YES, SELL_LONG sells YES, BUY_SHORT buys NO,
  SELL_SHORT sells NO. The application's outcome terms map as: buy yes @p -> BUY_LONG @p; sell yes @p -> SELL_LONG
  @p; buy no @q -> BUY_SHORT @(1-q); sell no @q -> SELL_SHORT @(1-q). The DOWN (NO) mapping and the sign of
  `netPosition` for NO holdings (taken as negative, like Kalshi) follow the docs but are NOT yet verified with real
  orders: penny-test DOWN entries and exits before any live DOWN trade.
* IOC / FOK orders use `synchronousExecution` (the answer is meant to be the final state). An IOC/FOK answer whose
  order is still in an open state (PENDING_NEW, PENDING_RISK, ...) or that carries no executions at all is NOT final:
  the order is retrieved (about 2 s) until it is terminal, else the create raises OrderOutcomeUnknown with the order
  id remembered, so `find_order` keeps reading it (review fix plan 2026-10-31, B3#5). GTC orders do not use
  synchronous execution (they would block); their state comes from the private order stream and `retrieve`.
* There is NO client order id and NO idempotency key, and the SDK never retries a POST (create / cancel). So this
  gateway keeps the client_order_id -> order id map itself (plus `coid_lookup` / `oid_lookup` from the app's own
  trading_orders rows, so the map survives a restart) and NEVER resends a create. A create whose answer is lost
  (timeout, connection error, 5xx, 429, or a 4xx other than 400/401/403/404/422, e.g. 408 / 499) raises a
  non-definitive error; `find_order(coid)` then resolves it from the account: an unknown open order of the same
  market, intent, price and size placed after the send, else (after `settle_ms`) the account's activity TRADES in that
  market since the send that no other order of ours explains (`_own_trades`), at prices the order could fill at. It
  is NEVER resolved from the positions list (it lags our own fills, and the "before" reading may itself be stale: a
  filled buy was read as "canceled, 0 filled", B3#2); no such trade -> still None (unknown). `lost_order` gives the
  application the activities' "nothing there" answer; the application then checks the position before it closes the
  order as unfilled (domain/order_resolution.py).
* Cancel: `client.orders.cancel(id, {marketSlug})`, answer empty. A 404, or a refusal for an order the exchange
  reports finished, counts as "no longer resting" (authoritative); any other refusal raises (the caller retries
  before selling). `retrieve` right after a cancel may be stale (Kalshi lesson); the application reads fills
  through `find_order` and positions.
* Positions: `client.portfolio.positions()` -> {"positions": {slug: {netPosition, ...}}} (KEYED BY SLUG, paginated
  with nextCursor / eof). Balance: `client.account.balances()["balances"][0]`.

Held quantities (production penny test 2026-10-29: GET /v1/portfolio/positions lagged our own fills: still +1 right
after a sale, empty right after a Down buy, so a sale cap taken from it SKIPPED a take profit and would have skipped a
stop). The exchange's direct answers are authoritative and the positions list is only a backup, as on Kalshi:
* A LOCAL book of the net position per market is kept from authoritative sources only: the executions in our own
  create answers, orders we read back (`retrieve`, the open-order list) and private-WebSocket order updates. Each
  order's cumulative fill count is applied once (a repeat or an older read never moves it), so the create answer and
  the stream's copy of the same fill never double count.
* The position used for an order (and returned by `positions()`) is the local one whenever it disagrees with the REST
  read and our last fill on that market is under `recheck_ms` (10 s) old: the list lags OUR fills by 1-3 s, so a stale
  list can never shrink or skip a stop or exit right after a fill, and can never make a sale bigger than what we
  bought (no double sale). Once our last fill is older than that, the list has had time to show it, so a remaining
  disagreement is something we did not do (a manual sale or purchase in the Polymarket app): the LIST wins and the
  local book is re-synced to it, so a position the user sold is never resurrected and a stop never sells contracts we
  no longer hold (2026-10-29: the user sold a pnw_poly position by hand). A sale's size is also capped by the
  application's own held quantity, so contracts the user added are never sold by us. Every disagreement is logged and
  reported through `note_fn` (the app audits it) and re-read once after `recheck_ms`.
* On a restart the local book is seeded from the app's live brackets (`seed_fn`; closed brackets hold 0) and the first
  positions read, taking the larger holding per market for `recheck_ms` (the list may lag a fill made just before the
  restart), after which the list wins as above; a note is made when they differ.
* `exchange_positions()` is the RAW list (what the exchange reports, for the application's manual-exit detector),
  `note_external_position(slug, net)` re-syncs the local book after the application has decided that the user
  traded by hand, and `market_fills(slug, since_ms)` lists the account's trades in a market (portfolio activities)
  that are not ours, for the P&L of a manual exit. Polymarket's activity trades carry no side, so their price is
  given in YES terms with the direction unknown (`yes_delta` None); the application infers which side it was.
* A create whose answer was lost is resolved from the open-order list, a stream order update, or the activity trades
  (see above), never from the positions list; the resolved fill then enters the local book and its trades become
  ours (`_own_trades`: a bounded deque + set, oldest evicted first).

Post-only: Polymarket documents `participateDontInitiate` as "rejected if it would immediately match", but a penny
test (2026-10-29) saw a post-only GTC buy at ask + 2c REST instead (the quote may have moved, or the exchange rests /
reprices such orders). So the gateway does not rely on the exchange: a post-only order is refused before sending
(definitive) unless the live WebSocket quote shows it would rest (a buy below the ask, a sale above the bid, on the
order's own side); `participateDontInitiate` is still sent. The app's only post-only order is the 99c take profit.

Safety (every order, before anything is sent): the market is a BTC 15-minute or 5-minute Up/Down market; side yes|no and action
buy|sell; a whole number of contracts >= 1; price strictly between 0 and 1 on the cent grid; a buy's notional
<= `max_order_cost`; a SELL is capped at the contracts held on that side (local book, see above; Polymarket has no
reduce-only flag, so this is what stops a sale from ever opening a short) and is not sent at all when nothing is
held; a BUY is refused while the account holds the OTHER side of that market. All REST
calls share a <= `max_rps` request budget (Polymarket allows 25/s per IP) and pause after a 429. Key material is
never in repr, logs, errors or return values (errors carry the API's message only, capped).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from polymarket_us import (APIConnectionError, APIStatusError, AuthenticationError, BadRequestError, NotFoundError,
                           PermissionDeniedError, RateLimitError)

from btc_signal_engine.adapters.polymarket_us.markets import (LIVE_SPANS, is_series, num, result_reader, span_of,
                                                              window_of)
from btc_signal_engine.adapters.retry import header, retry_after_s, retry_delay
from btc_signal_engine.domain.order_resolution import TS_SLACK_MS, definitive_status
from btc_signal_engine.domain.timestamps import iso_to_ms_or_none

log = logging.getLogger(__name__)

INTENTS = {("yes", "buy"): "ORDER_INTENT_BUY_LONG", ("yes", "sell"): "ORDER_INTENT_SELL_LONG",
           ("no", "buy"): "ORDER_INTENT_BUY_SHORT", ("no", "sell"): "ORDER_INTENT_SELL_SHORT"}
TIFS = {"immediate_or_cancel": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "good_till_canceled": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        "fill_or_kill": "TIME_IN_FORCE_FILL_OR_KILL"}
OPEN_STATES = {"ORDER_STATE_NEW", "ORDER_STATE_PENDING_NEW", "ORDER_STATE_PARTIALLY_FILLED",
               "ORDER_STATE_PENDING_REPLACE", "ORDER_STATE_PENDING_CANCEL", "ORDER_STATE_PENDING_RISK"}
FILL_TYPES = {"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"}
LOST = "pm-lost-"  # prefix of the synthetic order id for a create whose answer was lost
RECHECK_MS = 10_000  # a REST position read that disagrees with our own fills younger than this is stale
_SIGN = {("yes", "buy"): 1, ("no", "sell"): 1, ("yes", "sell"): -1, ("no", "buy"): -1}  # effect on net YES
EPS = 1e-9


class TradingViolation(RuntimeError):
    """A request this gateway must never make. Nothing is sent."""

    definitive = True
    status = 0


class OrderRejected(RuntimeError):
    """Polymarket refused the order (or the credentials). Nothing was placed."""

    definitive = True

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Polymarket rejected the order (HTTP {status}): {str(message)[:300]}")
        self.status = status


class OrderOutcomeUnknown(RuntimeError):
    """The create's answer was lost (timeout, connection, 5xx, 429). It may exist: resolve with find_order."""

    definitive = False

    def __init__(self, why: str, status: int | None = None) -> None:
        super().__init__(f"Polymarket order outcome unknown ({why})")
        self.status = status


class RateLimiter:
    """At most `rps` calls per second (token bucket); after a 429 every caller waits `pause_s`."""

    def __init__(self, rps: float, *, clock_s: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._rps, self._clock, self._sleep = float(rps), clock_s, sleep
        self._tokens, self._at, self._until = float(rps), clock_s(), 0.0

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            if now < self._until:
                await self._sleep(self._until - now)
                continue
            self._tokens = min(self._rps, self._tokens + (now - self._at) * self._rps)
            self._at = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await self._sleep((1 - self._tokens) / self._rps)

    def backoff(self, pause_s: float = 1.0) -> None:
        self._until = max(self._until, self._clock() + pause_s)


def _iso_ms(s: Any) -> int | None:
    """A Polymarket time (UTC; "Z" may be missing) -> epoch ms, None when absent or unreadable (domain/timestamps.py)."""
    return iso_to_ms_or_none(s, naive="utc")


def _whole(c: float) -> float:
    return float(int(c + EPS))


def _on_side(yes_price: float | None, side: str | None) -> float | None:
    if yes_price is None:
        return None
    return round(yes_price if side in (None, "yes") else 1 - yes_price, 4)


def side_action_of_intent(intent: str | None) -> tuple[str, str] | None:
    return {v: k for k, v in INTENTS.items()}.get(intent or "")


def side_of_intent(intent: str | None) -> str | None:
    return {"ORDER_INTENT_BUY_LONG": "yes", "ORDER_INTENT_SELL_LONG": "yes", "ORDER_INTENT_BUY_SHORT": "no",
            "ORDER_INTENT_SELL_SHORT": "no"}.get(intent or "")


def normalize_order(o: dict, side: str | None = None, coid: str | None = None) -> dict:
    """A Polymarket Order -> the fields the application uses (prices on `side`, default the order's own side)."""
    side = side or side_of_intent(o.get("intent"))
    state = o.get("state")
    filled = num(o.get("cumQuantity")) or 0.0
    remaining = num(o.get("leavesQuantity")) or 0.0
    if state in OPEN_STATES:
        status = "resting"
    else:
        status = "executed" if filled > 0 and state == "ORDER_STATE_FILLED" else "canceled"
    fees = num(o.get("commissionNotionalTotalCollected"))
    return {"order_id": o.get("id"), "client_order_id": coid, "ticker": o.get("marketSlug"), "status": status,
            "state": state, "filled": filled, "remaining": 0.0 if status != "resting" else remaining,
            "initial": num(o.get("quantity")), "intent": o.get("intent"), "price": _on_side(num(o.get("price")), side),
            "avg_price": _on_side(num(o.get("avgPx")), side) if filled else None,
            "fees": round(fees, 4) if fees is not None else None}


def result_of_create(resp: dict, side: str, coid: str, tif: str) -> dict:
    """A create answer -> {order_id, client_order_id, filled, remaining, avg_price, fees, status, ts_ms}.
    Raises OrderRejected when the only outcome is a rejection. An IOC/FOK answer with no executions, or whose order is
    still in an open state, has status "pending" (not final: place_limit retrieves it until it is)."""
    execs = [e for e in (resp or {}).get("executions") or [] if isinstance(e, dict)]
    orders = [e["order"] for e in execs if isinstance(e.get("order"), dict)]
    last = orders[-1] if orders else {}
    fills = [(num(e.get("lastPx")), num(e.get("lastShares"))) for e in execs if e.get("type") in FILL_TYPES]
    fills = [(p, q) for p, q in fills if p is not None and q]
    filled = round(sum(q for _, q in fills), 2)
    cum = num(last.get("cumQuantity"))
    if cum is not None and cum > filled:
        filled = cum
    rejected = [e for e in execs if e.get("type") == "EXECUTION_TYPE_REJECTED"]
    if rejected and filled <= 0:
        e = rejected[-1]
        raise OrderRejected(400, e.get("orderRejectReason") or e.get("text") or "rejected")
    if fills:
        avg_yes = sum(p * q for p, q in fills) / sum(q for _, q in fills)
    else:
        avg_yes = num(last.get("avgPx")) if filled else None
    fee_parts = [num(e.get("commissionNotionalCollected")) for e in execs if e.get("type") in FILL_TYPES]
    fee_parts = [f for f in fee_parts if f is not None]
    if fee_parts:
        fees = round(sum(fee_parts), 4)
    else:
        total = num(last.get("commissionNotionalTotalCollected"))
        fees = round(total, 4) if total is not None else None  # None: the application estimates it
    state = last.get("state")
    leaves = num(last.get("leavesQuantity")) or 0.0
    if tif == "good_till_canceled" and (state in OPEN_STATES or (state is None and not filled)):
        status, remaining = "resting", leaves if state is not None else None
    elif not execs or state in OPEN_STATES:  # an IOC/FOK not finished yet (PENDING_NEW / PENDING_RISK / ...): not final
        status, remaining = "pending", leaves
    else:
        status, remaining = ("executed" if filled > 0 else "canceled"), 0.0
    return {"order_id": resp.get("id"), "client_order_id": coid, "filled": filled, "remaining": remaining,
            "avg_price": _on_side(avg_yes, side) if filled else None, "fees": fees, "status": status,
            "ts_ms": int(time.time() * 1000)}


class PolymarketTrading:
    """Real-order client for Polymarket US. `client` is an `AsyncPolymarketUS` (tests: a fake with the same
    `orders` / `portfolio` / `account` surface); `quote_fn(slug)` -> the live WebSocket quote dict or None."""

    def __init__(self, client: Any, *, quote_fn: Callable[[str], dict | None], env: str = "pm_prod",
                 max_order_cost: float = 25.0, max_rps: float = 20.0,
                 coid_lookup: Callable[[str], str | None] | None = None,
                 oid_lookup: Callable[[str], str | None] | None = None,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000), settle_ms: int = 15_000,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 seed_fn: Callable[[], dict[str, float]] | None = None,
                 note_fn: Callable[[dict], None] | None = None, recheck_ms: int = RECHECK_MS,
                 markets_fn: Callable[[str], Awaitable[dict]] | None = None) -> None:
        """`seed_fn()` -> {slug: net YES contracts} the app's live brackets hold (restart seeding); `note_fn(dict)`
        receives every position disagreement (the app audits it); `markets_fn(slug)` = the SDK's public
        `markets.retrieve_by_slug` for settlement results (default: this client's own)."""
        self._markets_fn = markets_fn
        self.env = env
        self._c = client
        self._quote_fn = quote_fn
        self._max_cost = float(max_order_cost)
        self._limiter = RateLimiter(max_rps, sleep=sleep)
        self._coid_lookup = coid_lookup
        self._oid_lookup = oid_lookup
        self._clock = clock_ms
        self._settle_ms = settle_ms
        self._sleep = sleep
        self._oid: dict[str, str] = {}  # client_order_id -> order id
        self._coid: dict[str, str] = {}  # order id -> client_order_id
        self._pending: dict[str, dict] = {}  # client_order_id -> what was sent when its answer was lost
        self._lost: dict[str, dict] = {}  # client_order_id -> the resolved outcome of a lost create
        self._lost_info: dict[str, dict] = {}  # client_order_id -> what that lost create sent (to match it later)
        self.positions_cache: dict[str, float] = {}  # slug -> netPosition as the last RAW REST read gave it
        self.positions_ms: int | None = None
        self.stream_positions: dict[str, float] = {}  # slug -> netPosition pushed by the private stream (info)
        self._local: dict[str, float] = {}  # slug -> net YES from authoritative sources (our fills)
        self._local_ms: dict[str, int] = {}  # slug -> when the local book last changed from a fill
        self._cum: dict[str, float] = {}  # order id -> cumulative fills already applied to the local book
        self._seed_fn = seed_fn
        self._seeded = False
        self._note_fn = note_fn
        self._recheck_ms = recheck_ms
        self._rechecking: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._last_note: dict[str, tuple] = {}
        self.notes: deque = deque(maxlen=50)
        self._own_trades: set[str] = set()  # trade ids of OUR fills (membership)
        self._own_order: deque[str] = deque()  # the same ids, oldest first (bounded eviction)
        self.balance: tuple[float | None, int | None] = (None, None)  # (current balance $, read at ms)

    def __repr__(self) -> str:
        return f"PolymarketTrading(env={self.env!r})"

    @property
    def max_order_cost(self) -> float:
        """The cap on a buy's notional (count x limit, $) this gateway enforces (the application pre-checks it)."""
        return float(self._max_cost)

    # -- ids --------------------------------------------------------------------------------------
    def remember(self, coid: str, order_id: str) -> None:
        if coid and order_id:
            self._oid[coid], self._coid[order_id] = order_id, coid
            for d in (self._oid, self._coid):
                while len(d) > 2000:
                    d.pop(next(iter(d)))

    def order_id_for(self, coid: str) -> str | None:
        oid = self._oid.get(coid)
        if oid is None and self._coid_lookup is not None:
            try:
                oid = self._coid_lookup(coid)
            except Exception:
                oid = None
            if oid:
                self.remember(coid, oid)
        return oid

    def coid_for(self, order_id: str | None) -> str | None:
        if not order_id:
            return None
        coid = self._coid.get(order_id)
        if coid is None and self._oid_lookup is not None:
            try:
                coid = self._oid_lookup(order_id)
            except Exception:
                coid = None
            if coid:
                self.remember(coid, order_id)
        return coid

    # -- guards -----------------------------------------------------------------------------------
    def _series(self, ticker: str) -> None:
        if span_of(ticker) not in LIVE_SPANS:  # the table's tradable spans only (1h is recognised, never traded yet)
            raise TradingViolation(f"only BTC Up/Down {'/'.join(LIVE_SPANS)} markets may be traded (got {ticker!r})")

    def _check(self, ticker: str, side: str, action: str, count: float, price: float) -> None:
        self._series(ticker)
        if (side, action) not in INTENTS:
            raise TradingViolation("side must be yes|no and action buy|sell")
        if not (count and count >= 1 - EPS and abs(count - round(count)) < 1e-6):
            raise TradingViolation("Polymarket orders are whole contracts, at least 1")
        if not (0.0 < price < 1.0) or abs(price * 100 - round(price * 100)) > 1e-6:
            raise TradingViolation("limit price must be on the cent grid strictly between $0 and $1")
        if action == "buy" and count * price > self._max_cost + 1e-9:
            raise TradingViolation(f"order notional ${count * price:.2f} exceeds the ${self._max_cost:.2f} cap")

    async def _call(self, fn: Callable[..., Awaitable[Any]], *a: Any) -> Any:
        await self._limiter.acquire()
        try:
            return await fn(*a)
        except RateLimitError as exc:  # every caller pauses: the Retry-After (1 s without one) plus jitter
            wait = retry_after_s(header(getattr(getattr(exc, "response", None), "headers", None)))
            self._limiter.backoff(retry_delay(0, status=429, retry_after=1.0 if wait is None else wait))
            raise

    # -- orders -----------------------------------------------------------------------------------
    async def place_limit(self, *, ticker: str, side: str, action: str, count: float, price: float,
                          client_order_id: str, tif: str = "immediate_or_cancel", post_only: bool = False,
                          reduce_only: bool = False) -> dict:
        """Place one limit order (never retried). Returns {order_id, client_order_id, filled, remaining, avg_price,
        fees, status}. Sells are capped at what is held on that side (`reduce_only` is implied for every sale)."""
        self._check(ticker, side, action, count, price)
        if tif not in TIFS:
            raise TradingViolation(f"unsupported time_in_force {tif!r}")
        if tif != "good_till_canceled" and post_only:
            raise TradingViolation("post_only needs good_till_canceled")
        if client_order_id in self._oid or client_order_id in self._pending:
            found = await self.find_order(ticker, client_order_id, side)  # never send the same order twice
            if found is None:
                raise TradingViolation("this client_order_id was already sent; resolve it before sending again")
            return {**found, "duplicate": True}
        if post_only:
            self._post_only_rests(ticker, side, action, price)
        try:
            pos = await self._position(ticker)  # local fills first; the REST list only as the backup
        except Exception as exc:
            raise TradingViolation(f"position could not be read before the order ({type(exc).__name__}); "
                                   "nothing was sent") from None
        self._local.setdefault(ticker, pos)
        held_yes, held_no = max(pos, 0.0), max(-pos, 0.0)
        qty = _whole(count)
        if action == "sell":
            qty = min(qty, _whole(held_yes if side == "yes" else held_no))
            if qty < 1:
                return {"order_id": None, "client_order_id": client_order_id, "filled": 0.0, "remaining": 0.0,
                        "avg_price": None, "fees": 0.0, "status": "canceled", "not_sent": True}
        elif (held_no if side == "yes" else held_yes) > EPS:
            raise TradingViolation("the account holds the other side of this market; a buy would net against it")
        yes_price = round(price if side == "yes" else 1 - price, 2)
        body = {"marketSlug": ticker, "intent": INTENTS[(side, action)], "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{yes_price:.2f}", "currency": "USD"}, "quantity": int(qty), "tif": TIFS[tif],
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}
        if post_only:
            body["participateDontInitiate"] = True
        if tif != "good_till_canceled":
            body["synchronousExecution"] = True
        self._pending[client_order_id] = {"slug": ticker, "intent": body["intent"], "yes_price": yes_price,
                                          "qty": qty, "side": side, "action": action, "pos_before": pos,
                                          "sent_ms": self._clock()}
        try:
            resp = await self._call(self._c.orders.create, body)
        except (BadRequestError, AuthenticationError, PermissionDeniedError, NotFoundError) as exc:
            self._pending.pop(client_order_id, None)
            raise OrderRejected(exc.status_code, exc.message) from None
        except RateLimitError:
            raise OrderOutcomeUnknown("rate limited", 429) from None
        except APIStatusError as exc:
            if definitive_status(exc.status_code):  # 400/401/403/404/422 only: e.g. a 408 or 499 may have been placed
                self._pending.pop(client_order_id, None)
                raise OrderRejected(exc.status_code, exc.message) from None
            raise OrderOutcomeUnknown(f"HTTP {exc.status_code}", exc.status_code) from None
        except APIConnectionError as exc:
            raise OrderOutcomeUnknown(type(exc).__name__) from None
        except Exception as exc:  # anything else: the order may exist
            raise OrderOutcomeUnknown(type(exc).__name__) from None
        try:
            res = result_of_create(resp, side, client_order_id, tif)
        except OrderRejected:
            self._pending.pop(client_order_id, None)
            if (resp or {}).get("id"):
                self.remember(client_order_id, resp["id"])
            raise
        for e in (resp or {}).get("executions") or []:
            if isinstance(e, dict):
                self.observe_trade_id(e.get("tradeId"))
        if res["status"] == "pending":  # an IOC/FOK answer that is not final: read the order until it is
            return await self._until_final(ticker, side, action, client_order_id, res)
        self._pending.pop(client_order_id, None)
        if res["order_id"]:
            self.remember(client_order_id, res["order_id"])
            self._apply_fill(res["order_id"], ticker, side, action, res["filled"])  # the authoritative answer
        return res

    async def _until_final(self, ticker: str, side: str, action: str, coid: str, res: dict) -> dict:
        """An IOC/FOK create answered before it finished (B3#5): retrieve it (0.25 + 0.5 + 1 s) until it is terminal.
        Still open, or no order id to read -> OrderOutcomeUnknown (not definitive; the id is remembered so find_order
        reads it, never treated as canceled)."""
        oid = res.get("order_id")
        if not oid:
            raise OrderOutcomeUnknown("IOC answer without an order or executions")
        self.remember(coid, oid)
        self._pending.pop(coid, None)
        for wait in (0.25, 0.5, 1.0):
            await self._sleep(wait)
            try:
                o = (await self._call(self._c.orders.retrieve, oid)).get("order") or {}
            except Exception as exc:
                log.warning("polymarket IOC %s read failed (%s)", oid, type(exc).__name__)
                continue
            self.observe_order(o)
            got = normalize_order(o, side, coid)
            if got["status"] != "resting":
                return {**got, "ts_ms": self._clock()}
        raise OrderOutcomeUnknown(f"IOC order {oid} still pending")

    def _post_only_rests(self, ticker: str, side: str, action: str, price: float) -> None:
        """Refuse (definitive) a post-only order unless the live quote shows it would rest on the book."""
        q = self._quote_fn(ticker)
        if not q:
            raise TradingViolation("no live quote to check that a post-only order would rest; nothing was sent")
        if action == "buy":
            ask = q.get(f"{side}_ask")
            if ask is not None and price >= ask - EPS:
                raise TradingViolation(f"post-only buy at {price:.2f} would cross the {ask:.2f} ask; nothing was sent")
        else:
            bid = q.get(f"{side}_bid")
            if bid is not None and price <= bid + EPS:
                raise TradingViolation(f"post-only sale at {price:.2f} would cross the {bid:.2f} bid; nothing was sent")

    async def cancel(self, order_id: str, ticker: str) -> dict:
        """Cancel a resting order. Answered or 404 / already finished -> no longer resting (authoritative)."""
        self._series(ticker)
        if not order_id or order_id.startswith(LOST):
            return {"order_id": order_id, "reduced_by": None, "not_found": True}
        try:
            await self._call(self._c.orders.cancel, order_id, {"marketSlug": ticker})
        except NotFoundError:
            return {"order_id": order_id, "reduced_by": None, "not_found": True}
        except APIStatusError as exc:
            if exc.status_code in (401, 403):
                raise
            try:  # refused: only "it is already finished" makes that authoritative
                o = normalize_order((await self._call(self._c.orders.retrieve, order_id)).get("order") or {})
            except Exception:
                raise exc from None
            if o["status"] != "resting":
                return {"order_id": order_id, "reduced_by": None, "not_found": True}
            raise
        return {"order_id": order_id, "reduced_by": None}

    async def get_order(self, order_id: str, side: str | None = None) -> dict:
        coid = self.coid_for(order_id)
        if order_id.startswith(LOST) and coid in self._lost:
            return dict(self._lost[coid])
        for attempt in range(3):
            try:
                got = await self._call(self._c.orders.retrieve, order_id)
                self.observe_order(got.get("order") or {})
                return normalize_order(got.get("order") or {}, side, coid)
            except NotFoundError:
                if attempt == 2:
                    raise
                await self._sleep(0.5 * (attempt + 1))
        raise RuntimeError("unreachable")

    async def orders(self, *, ticker: str | None = None, status: str | None = None) -> list[dict]:
        """Open (resting) orders, account-wide or for one market. Polymarket lists open orders only."""
        if status not in (None, "resting"):
            return []
        got = await self._call(self._c.orders.list, {"slugs": [ticker]} if ticker else None)
        rows = (got or {}).get("orders") or []
        for o in rows:
            self.observe_order(o)
        return [normalize_order(o, None, self.coid_for(o.get("id"))) for o in rows]

    async def find_order(self, ticker: str, client_order_id: str, side: str | None = None) -> dict | None:
        """The order sent with this client_order_id, or None when it is not (yet) known to exist."""
        coid = client_order_id
        if coid in self._lost:
            return dict(self._lost[coid])
        oid = self.order_id_for(coid)
        if oid:
            try:
                got = await self._call(self._c.orders.retrieve, oid)
            except NotFoundError:
                return None
            self.observe_order(got.get("order") or {})
            return normalize_order(got.get("order") or {}, side, coid)
        p = self._pending.get(coid)
        if p is None or p["slug"] != ticker:
            return None
        known = set(self._coid)
        for o in (await self._call(self._c.orders.list, {"slugs": [ticker]}) or {}).get("orders") or []:
            if (o.get("id") not in known and o.get("intent") == p["intent"]
                    and abs((num(o.get("price")) or -1) - p["yes_price"]) < 1e-6
                    and abs((num(o.get("quantity")) or -1) - p["qty"]) < 1e-6):
                self.remember(coid, o["id"])
                self._pending.pop(coid, None)
                self.observe_order(o)
                return normalize_order(o, side, coid)
        if self._clock() - p["sent_ms"] < self._settle_ms:
            return None  # may still be in flight (and the activities may lag our own fills too)
        return await self._from_activities(coid, p)  # None: no trade proves it yet (never "canceled" on a miss)

    async def lost_order(self, ticker: str, client_order_id: str, *, side: str, action: str, since_ms: int,
                         count: float, price: float) -> dict:
        """The activities' word on an order whose answer was lost and that find_order cannot show (also after a
        restart, when the in-memory send record is gone): the order, filled, from unclaimed activity trades since
        `since_ms` (as find_order), or {filled: 0, status: "not_found", proof: "activities"} when no trade could be it.
        The application checks the position before it calls the order unfilled. A failed read raises (unprovable)."""
        self._series(ticker)
        coid = client_order_id
        if coid in self._lost:
            return dict(self._lost[coid])
        p = self._pending.get(coid)
        if p is None or p["slug"] != ticker:
            yes_price = round(price if side == "yes" else 1 - price, 2)
            p = {"slug": ticker, "intent": INTENTS[(side, action)], "yes_price": yes_price, "qty": _whole(count),
                 "side": side, "action": action, "sent_ms": int(since_ms)}
        got = await self._from_activities(coid, p)
        if got is not None:
            return got
        return {"order_id": None, "client_order_id": coid, "ticker": ticker, "status": "not_found", "filled": 0.0,
                "remaining": 0.0, "avg_price": None, "fees": None, "proof": "activities"}

    def _fills_at(self, p: dict, yes_px: float | None) -> bool:
        """Could the order `p` (a lost create) have traded at this YES price? A buy of YES at most its limit, a sale
        of YES at least it; a NO buy (BUY_SHORT at YES price y) trades at YES >= y, a NO sale at YES <= y."""
        if yes_px is None:
            return False
        sign = _SIGN[(p["side"], p["action"])]  # +1: it adds YES (buy YES / sell NO) -> pays at most the limit
        return yes_px <= p["yes_price"] + 1e-6 if sign > 0 else yes_px >= p["yes_price"] - 1e-6

    async def _from_activities(self, coid: str, p: dict) -> dict | None:
        """The lost create's fill from the account's activity trades in its market since it was sent (oldest first,
        trades not already ours, at prices it could fill at, up to its size): the resolved order, or None when there
        is no such trade. Polymarket's trades carry no order id or side (to verify live): a trade of the user's own in
        the same market, second and price range is the residual risk; it is noted when used."""
        slug = p["slug"]
        got = await self._call(self._c.portfolio.activities, {"marketSlug": slug, "types": ["ACTIVITY_TYPE_TRADE"],
                                                             "limit": 100, "sortOrder": "SORT_ORDER_DESCENDING"})
        trades = []
        for a in (got or {}).get("activities") or []:
            t = a.get("trade") if isinstance(a, dict) else None
            if not isinstance(t, dict) or t.get("marketSlug") not in (None, slug) or not t.get("id") \
                    or str(t["id"]) in self._own_trades:
                continue
            ts = _iso_ms(t.get("createTime") or t.get("updateTime"))
            qty, px = num(t.get("qtyDecimal") or t.get("qty")), num(t.get("price"))
            if ts is None or ts < p["sent_ms"] - TS_SLACK_MS or not qty or not self._fills_at(p, px):
                continue
            trades.append((ts, str(t["id"]), abs(qty), px))
        filled, cost, ids = 0.0, 0.0, []
        for _, tid, qty, px in sorted(trades):
            take = min(qty, p["qty"] - filled)
            if take <= 0:
                break
            filled, cost = filled + take, cost + take * px
            ids.append(tid)
        filled = _whole(filled)
        if filled < 1:
            return None
        side = p["side"]
        res = {"order_id": LOST + coid, "client_order_id": coid, "ticker": slug, "status": "executed",
               "filled": filled, "remaining": 0.0, "price": _on_side(p["yes_price"], side),
               "avg_price": _on_side(round(cost / filled, 4), side), "fees": None, "from": "activities",
               "proof": "activities", "trade_ids": ids}
        for tid in ids:
            self.observe_trade_id(tid)  # ours from now on: never claimed twice, never a "manual" trade
        self._lost[coid] = res
        self._lost_info[coid] = p
        self.remember(coid, res["order_id"])
        self._pending.pop(coid, None)
        self._apply_fill(res["order_id"], slug, side, p["action"], filled)
        self._note(slug, "lost_create", f"a create whose answer was lost filled {filled:g} (from the activity trades "
                                        f"{', '.join(ids)})")
        return dict(res)

    # -- account ----------------------------------------------------------------------------------
    # -- the local position book -----------------------------------------------------------------
    def _apply_fill(self, order_id: str | None, slug: str | None, side: str, action: str, cum: float | None) -> None:
        """Apply an order's CUMULATIVE fill count to the local book once (repeats and older reads are no-ops)."""
        if not order_id or not is_series(slug) or (side, action) not in _SIGN or cum is None:
            return
        delta = round(float(cum) - self._cum.get(order_id, 0.0), 2)
        if delta <= 0:
            return
        self._cum[order_id] = float(cum)
        while len(self._cum) > 4000:
            self._cum.pop(next(iter(self._cum)))
        base = self._local.get(slug, self.positions_cache.get(slug, 0.0))
        self._local[slug] = round(base + _SIGN[(side, action)] * delta, 2)
        self._local_ms[slug] = self._clock()

    def observe_order(self, o: dict) -> None:
        """An order as the exchange reports it (create answer, retrieve, open list, private stream)."""
        if not isinstance(o, dict) or not o.get("id"):
            return
        sa = side_action_of_intent(o.get("intent"))
        if sa is None:
            return
        if o["id"] not in self._coid:  # the real order behind a create whose answer was lost?
            same = lambda p: (p["slug"] == o.get("marketSlug") and p["intent"] == o.get("intent")  # noqa: E731
                              and abs((num(o.get("price")) or -1) - p["yes_price"]) < 1e-6
                              and abs((num(o.get("quantity")) or -1) - p["qty"]) < 1e-6)
            for coid, p in list(self._pending.items()):
                if same(p):
                    self.remember(coid, o["id"])
                    self._pending.pop(coid, None)
                    break
            else:
                for coid, p in list(self._lost_info.items()):
                    if same(p):  # already resolved from the positions list: its fills are in the book already
                        self._cum[o["id"]] = max(self._cum.get(o["id"], 0.0), self._cum.get(LOST + coid, 0.0))
                        self._lost_info.pop(coid, None)
                        self.remember(coid, o["id"])
                        break
        self._apply_fill(o["id"], o.get("marketSlug"), sa[0], sa[1], num(o.get("cumQuantity")))

    def local_position(self, slug: str) -> float | None:
        return self._local.get(slug)

    def _note(self, slug: str, code: str, reason: str, detail: dict | None = None) -> None:
        row = {"ts": self._clock(), "env": self.env, "slug": slug, "code": code, "reason": reason,
               "detail": detail or {}}
        self.notes.appendleft(row)
        log.warning("polymarket position %s on %s: %s", code, slug, reason)
        if self._note_fn is not None:
            try:
                self._note_fn(row)
            except Exception:
                log.warning("position note consumer failed", exc_info=True)

    def _effective(self, slug: str, rest: float) -> float:
        """The net position to act on: the local book while our last fill on the market is under `recheck_ms` old
        (the list lags our own fills), otherwise the list (a remaining difference is a trade we did not make); the
        local book is then re-synced to the list."""
        local = self._local.get(slug)
        if local is None or abs(local - rest) < EPS:
            return rest if local is None else local
        recent = self._clock() - self._local_ms.get(slug, -10**15) < self._recheck_ms
        use = local if recent else rest
        key = (local, rest, use)
        if self._last_note.get(slug) != key:
            self._last_note[slug] = key
            self._note(slug, "position_mismatch",
                       f"our fills say {local:g}, the positions list says {rest:g}; using {use:g} "
                       f"({'the list lags our last fill' if recent else 'a trade we did not make: the list wins'})",
                       {"local": local, "rest": rest, "used": use})
            if recent:
                self._schedule_recheck(slug)
        if not recent:
            self._local[slug] = rest
        return use

    def note_external_position(self, slug: str, net: float) -> None:
        """The application decided the user traded by hand: the exchange's `net` is the truth from now on."""
        if not is_series(slug):
            return
        before = self._local.get(slug)
        self._local[slug] = float(net)
        self._local_ms[slug] = -10**15  # not one of our fills: never shields a later list reading
        self._note(slug, "external_position", f"set to the exchange's {net:g} after a trade we did not make "
                                              f"(our book had {before})", {"before": before, "net": net})

    async def exchange_positions(self) -> list[dict]:
        """The RAW positions list, exactly as the exchange reports it (no local book)."""
        return [{"ticker": k, "position": v} for k, v in (await self._rest_positions()).items()]

    def observe_trade_id(self, trade_id: str | None) -> None:
        """A trade id of one of OUR fills (create answers, the private stream): excluded from `market_fills`."""
        if trade_id and str(trade_id) not in self._own_trades:
            self._own_trades.add(str(trade_id))
            self._own_order.append(str(trade_id))
            while len(self._own_order) > 4000:  # the OLDEST goes (a set's pop() dropped an arbitrary, maybe recent one)
                self._own_trades.discard(self._own_order.popleft())

    async def market_fills(self, slug: str, since_ms: int) -> list[dict] | None:
        """The account's trades in `slug` since `since_ms` that are not ours (oldest first), from the portfolio
        activities. {fill_id, ts_ms, count, yes_price, yes_delta: None (Polymarket gives no side), fee: None}."""
        if not is_series(slug):
            return None
        try:
            got = await self._call(self._c.portfolio.activities, {"marketSlug": slug, "types": ["ACTIVITY_TYPE_TRADE"],
                                                                 "limit": 100, "sortOrder": "SORT_ORDER_DESCENDING"})
        except Exception as exc:
            log.warning("polymarket activities for %s failed (%s)", slug, type(exc).__name__)
            return None
        out = []
        for a in (got or {}).get("activities") or []:
            t = a.get("trade") if isinstance(a, dict) else None
            if not isinstance(t, dict) or t.get("marketSlug") not in (None, slug) or str(t.get("id")) in self._own_trades:
                continue
            ts = _iso_ms(t.get("createTime") or t.get("updateTime"))
            qty, px = num(t.get("qtyDecimal") or t.get("qty")), num(t.get("price"))
            if ts is None or ts < since_ms or not qty:
                continue
            out.append({"fill_id": t.get("id"), "order_id": None, "ts_ms": ts, "count": abs(qty), "yes_price": px,
                        "yes_delta": None, "fee": None})
        return sorted(out, key=lambda f: f["ts_ms"])

    def _schedule_recheck(self, slug: str) -> None:
        if slug in self._rechecking:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._rechecking.add(slug)
        task = loop.create_task(self._recheck(slug))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _recheck(self, slug: str) -> None:
        try:
            await self._sleep(self._recheck_ms / 1000)
            await self._rest_positions()
            rest, local = self.positions_cache.get(slug, 0.0), self._local.get(slug)
            if local is not None and abs(local - rest) < EPS:
                self._note(slug, "position_agrees", f"the positions list caught up ({rest:g})")
            else:
                self._note(slug, "position_still_differs", f"our fills say {local}, the list says {rest:g}",
                           {"local": local, "rest": rest})
        except Exception as exc:
            log.warning("position re-read for %s failed (%s)", slug, type(exc).__name__)
        finally:
            self._rechecking.discard(slug)

    def _seed(self, rest: dict[str, float]) -> None:
        """Restart: the larger of what the app's live brackets hold and the positions list, per market."""
        self._seeded = True
        try:
            app = dict(self._seed_fn()) if self._seed_fn is not None else {}
        except Exception:
            log.warning("position seed failed", exc_info=True)
            app = {}
        for slug in set(app) | set(rest):
            a, r = float(app.get(slug, 0.0)), float(rest.get(slug, 0.0))
            use = a if abs(a) >= abs(r) else r
            if slug in app or use:
                self._local[slug] = use
                self._local_ms[slug] = self._clock()  # shields a just-made fill for recheck_ms, then the list wins
            if abs(a - r) > EPS and slug in app:
                self._note(slug, "seed_mismatch", f"after a restart the app's brackets hold {a:g} and the positions "
                                                  f"list says {r:g}; starting from {use:g}",
                           {"brackets": a, "rest": r, "used": use})

    async def _position(self, slug: str) -> float:
        """Net YES position for one market to act on (+ YES, - NO): the local book, the REST list as the backup."""
        return {p["ticker"]: p["position"] for p in await self.positions()}.get(slug, 0.0)

    async def positions(self) -> list[dict]:
        """Open positions in any market, [{ticker, position}] (> 0 YES contracts, < 0 NO contracts): the REST list
        merged with the local book (see the module docstring). Markets closed over 10 minutes ago leave the local
        book (they settle; the list is then the truth)."""
        rest = await self._rest_positions()
        if not self._seeded:
            self._seed(rest)
        now = self._clock()
        for slug in [s for s in self._local if (window_of(s) or (0, 0))[1] + 600_000 < now]:
            self._local.pop(slug, None)
            self._local_ms.pop(slug, None)
        out = {}
        for slug in set(rest) | set(self._local):
            v = self._effective(slug, rest.get(slug, 0.0))
            if v:
                out[slug] = v
        return [{"ticker": k, "position": v} for k, v in out.items()]

    async def _rest_positions(self) -> dict[str, float]:
        """The RAW positions list (paged). It lags our own fills by seconds: never act on it alone."""
        out: dict[str, float] = {}
        cursor = None
        for _ in range(20):
            got = await self._call(self._c.portfolio.positions, {"cursor": cursor} if cursor else None) or {}
            for slug, p in (got.get("positions") or {}).items():
                net = num((p or {}).get("netPosition")) or 0.0
                if net:
                    out[slug] = net
            cursor = got.get("nextCursor")
            if got.get("eof", True) or not cursor:
                break
        self.positions_cache = dict(out)
        self.positions_ms = self._clock()
        return out

    async def read_balance(self) -> tuple[float | None, int | None]:
        got = await self._call(self._c.account.balances)
        rows = (got or {}).get("balances") or []
        v = num(rows[0].get("currentBalance")) if rows else None
        self.balance = (round(v, 2) if v is not None else None, self._clock())
        return self.balance

    def on_position(self, slug: str, net: float | None) -> None:
        """A position change pushed by the private stream (kept for display; fills come from order updates)."""
        if slug and net is not None:
            self.stream_positions[slug] = float(net)

    def on_balance(self, value: float | None) -> None:
        if value is not None:
            self.balance = (round(float(value), 2), self._clock())

    async def market_result(self, ticker: str) -> dict:
        """The market's official resolution for settlement: {ticker, status, result: "yes" | "no" | None, final,
        source} (markets.resolution). Raises on a failed read (the caller retries)."""
        self._series(ticker)
        fn = self._markets_fn or self._c.markets.retrieve_by_slug
        await self._limiter.acquire()
        return await result_reader(fn)(ticker)

    async def quote(self, ticker: str) -> dict:
        """The live WebSocket top of book (never the cached REST BBO); raises when it is missing or stale."""
        self._series(ticker)
        q = self._quote_fn(ticker)
        if not q:
            raise RuntimeError(f"no servable live Polymarket quote for {ticker} (WebSocket down, or its book fails the "
                               "freshness rule in market_data.py)")
        return q

    async def aclose(self) -> None:
        close = getattr(self._c, "close", None)
        if close is not None:
            await close()
