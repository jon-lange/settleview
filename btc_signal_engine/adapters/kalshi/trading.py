"""Kalshi TRADING client (real money). Separate from the read-only account link (KTD1, R8).

Verified against Kalshi's published OpenAPI spec (https://docs.kalshi.com/openapi.yaml, read 2026-10-28):

* Create:  POST /portfolio/events/orders (CreateOrderV2). The legacy POST /portfolio/orders is deprecated and no
  longer in the spec. Body: ticker, side ("bid" = buy YES, "ask" = sell YES, YES leg only), count (fixed-point
  string, fractional to 0.01), price (YES price, fixed-point dollars), time_in_force (fill_or_kill |
  good_till_canceled | immediate_or_cancel), self_trade_prevention_type (taker_at_cross | maker), client_order_id,
  post_only, reduce_only (rejected unless immediate_or_cancel). Every V2 order carries a price: a limit order.
  Response 201: order_id, client_order_id, fill_count, remaining_count (IOC: final state), average_fill_price,
  average_fee_paid, ts_ms. A duplicate client_order_id is rejected
  (https://docs.kalshi.com/getting_started/quick_start_create_order.md): HTTP 409 order_already_exists, after which
  the existing order is looked up by client_order_id (re-read ~5 s); not found yet -> OrderUnknown (not definitive).
* Cancel:  DELETE /portfolio/events/orders/{order_id}?market_ticker=... -> {order_id, client_order_id, reduced_by}.
* Get:     GET /portfolio/orders/{order_id} -> {"order": Order}; GET /portfolio/orders?ticker=&status= lists.
* Fills/positions: GET /portfolio/fills?order_id=, GET /portfolio/positions (market_positions[].position_fp).
* Result:  GET /markets/{ticker} `result` ("yes" | "no"; "" until determined) with `status`; `market_result` reads it
  uncached and calls it final once the status is determined, finalized or settled (the payout is $1 a contract to the
  winning side; Kalshi charges no settlement fee).
* Hosts:   prod https://external-api.kalshi.com/trade-api/v2, demo https://external-api.demo.kalshi.co/trade-api/v2
  (https://docs.kalshi.com/getting_started/api_environments.md). Demo keys only work on demo and vice versa.

The application speaks outcome terms (side yes|no, action buy|sell, price on that side); this adapter maps them
to the V2 book: buy yes -> bid @p, sell yes -> ask @p, buy no @q -> ask @(1-q), sell no @q -> bid @(1-q)
(https://docs.kalshi.com/getting_started/order_direction.md).

Transport (adapters/kalshi/backend.py): prod and demo go through Kalshi's official SDK (`kalshi_python_async`,
retries off, signed by `TradingAuth`, not the SDK's own auth); the shadow environment keeps httpx on the simulated exchange.
Every guard here runs before the backend is called, and our own retry loop is the only one.

Safety: `TradingAuth` signs only an allow-list of (method, path) pairs; anything else raises before a byte is
sent. Every order is refused unless its ticker is in a tradable Kalshi series of the market table
(domain/markets.py; today KXBTC15M), its count is positive and its price is strictly between 0 and 1; buys whose
worst-case notional exceeds `max_order_cost` are refused. Key material is never in repr or logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Generator, Iterable
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import httpx

from btc_signal_engine.adapters.kalshi.auth import PrivateKey, load_private_key, sign
from btc_signal_engine.adapters.kalshi.backend import Backend, HttpxBackend, SdkBackend, TransportFailure
from btc_signal_engine.adapters.kalshi.client import book_top, iso_to_ms
from btc_signal_engine.adapters.retry import header, retry_after_s, retry_delay
from btc_signal_engine.domain.markets import allowed_series
from btc_signal_engine.domain.order_resolution import TS_SLACK_MS, candidate_orders, definitive_status

log = logging.getLogger(__name__)

PREFIX = "/trade-api/v2"
BASE_URLS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}
SERIES = allowed_series("kalshi")  # the market table's tradable Kalshi series (today just KXBTC15M)
_ID = r"[A-Za-z0-9\-_]+"
ALLOWED = (
    ("POST", re.compile(rf"^{PREFIX}/portfolio/events/orders$")),
    ("DELETE", re.compile(rf"^{PREFIX}/portfolio/events/orders/{_ID}$")),
    ("GET", re.compile(rf"^{PREFIX}/portfolio/orders/{_ID}$")),
    ("GET", re.compile(rf"^{PREFIX}/portfolio/orders$")),
    ("GET", re.compile(rf"^{PREFIX}/portfolio/fills$")),
    ("GET", re.compile(rf"^{PREFIX}/portfolio/positions$")),
    ("GET", re.compile(rf"^{PREFIX}/portfolio/balance$")),
    ("GET", re.compile(rf"^{PREFIX}/markets/{_ID}$")),  # metadata only (status, open/close), never prices
    ("GET", re.compile(rf"^{PREFIX}/markets/{_ID}/orderbook$")),  # live prices
    # The WebSocket handshake (adapters/kalshi/stream.py): Kalshi signs it like a REST GET on this path
    # (https://docs.kalshi.com/getting_started/quick_start_websockets.md). It carries no order commands.
    ("GET", re.compile(r"^/trade-api/ws/v2$")),
)
_RETRY = 3
# Order-list reads to find the order behind a 409 on create (waits 0.5, 1.0, 1.5, 2.0 s between: ~5 s). The demo
# exchange's lists were seen missing an order for several seconds after it was placed (2026-10-29 SDK demo check).
_FIND_TRIES = 5
_MAX_PAGES = 20  # cursor pages read per list (200 rows each): a bound, never an endless loop
# A /markets/{ticker} status that means the result is decided (Kalshi's market lifecycle: ... closed -> determined ->
# finalized; "settled" is the older name the list filter still uses). "disputed" / "amended" are not final.
RESULT_FINAL_STATUSES = frozenset({"determined", "finalized", "settled"})
META_TTL_S = 30.0  # a market's status is re-read at most this often; open/close times never change


class TradingViolation(RuntimeError):
    """A request the trading client must never make (wrong method/path, series, size or price). Nothing is sent."""

    definitive = True  # the application reads this: the order certainly does not exist
    status = 0


class OrderRejected(RuntimeError):
    """Kalshi refused the order (4xx other than a duplicate). Nothing was placed."""

    definitive = True

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Kalshi rejected the order (HTTP {status}): {body[:300]}")
        self.status = status
        self.body = body


class OrderUnknown(RuntimeError):
    """A create answered 409 (the client_order_id is already taken, so an order with it may well exist) whose order
    the order lists do not show yet, or a 4xx that does not prove the order was refused (408, 425, 499, ...: only
    400/401/403/404/422 do, domain/order_resolution.DEFINITIVE_4XX). Not definitive: the order must be reconciled by
    client_order_id, never treated as rejected (that would invite a second order)."""

    definitive = False

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Kalshi answered HTTP {status} and the order is not in the order lists yet: {body[:300]}")
        self.status = status
        self.body = body


class TradingAuth(httpx.Auth):
    def __init__(self, key_id: str, private_key: PrivateKey,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self._key_id = key_id
        self._key = private_key
        self._clock_ms = clock_ms

    def __repr__(self) -> str:
        return "TradingAuth(<redacted>)"

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Signed KALSHI-ACCESS-* headers for one allow-listed (method, path); anything else raises."""
        if not any(method == m and rx.match(path) for m, rx in ALLOWED):
            raise TradingViolation(f"trading client refuses {method} {path}")
        ts = str(self._clock_ms())
        return {"KALSHI-ACCESS-KEY": self._key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self._key, ts + method + path)}

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers.update(self.headers(request.method, request.url.path))
        yield request


def _count(c: float) -> str:
    return str(Decimal(repr(c)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _price(p: Decimal) -> str:
    return str(p.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _f(x: Any) -> float | None:
    if x in (None, ""):
        return None
    return float(x)


def book_order(side: str, action: str, price: float) -> tuple[str, str]:
    """(V2 book side, YES price string) for an outcome-side order."""
    p = Decimal(repr(price))
    if side == "yes":
        return ("bid" if action == "buy" else "ask"), _price(p)
    return ("ask" if action == "buy" else "bid"), _price(Decimal(1) - p)


def normalize_order(o: dict, side: str | None = None) -> dict:
    """A documented Order object -> the fields the application uses. `side` converts prices to that side."""
    yes = _f(o.get("yes_price_dollars"))
    filled = _f(o.get("fill_count_fp")) or 0.0
    fees = (_f(o.get("taker_fees_dollars")) or 0.0) + (_f(o.get("maker_fees_dollars")) or 0.0)
    cost = (_f(o.get("taker_fill_cost_dollars")) or 0.0) + (_f(o.get("maker_fill_cost_dollars")) or 0.0)
    return {"order_id": o.get("order_id"), "client_order_id": o.get("client_order_id"), "ticker": o.get("ticker"),
            "status": o.get("status"), "filled": filled, "remaining": _f(o.get("remaining_count_fp")) or 0.0,
            "initial": _f(o.get("initial_count_fp")), "book_side": o.get("book_side"),
            "price": (yes if side in (None, "yes") or yes is None else round(1 - yes, 4)),
            "avg_price": round(cost / filled, 4) if filled and cost else None, "fees": round(fees, 4)}


class KalshiTrading:
    """Real-order client. Implements the application's `TradingGateway` protocol."""

    def __init__(self, auth: TradingAuth, *, env: str, base_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, max_order_cost: float = 25.0,
                 series: str | Iterable[str] = SERIES, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock_s: Callable[[], float] = time.monotonic, backend: Backend | None = None) -> None:
        """The REST backend: `backend` if given (tests), else httpx over `transport` (the shadow exchange, or a test's
        mock), else Kalshi's SDK on this environment's host (prod, demo)."""
        if env not in ("demo", "prod", "shadow"):
            raise ValueError("trading env must be 'demo', 'prod' or 'shadow'")
        if env == "shadow" and transport is None:
            raise ValueError("shadow trading needs the simulated exchange transport")
        self.env = env
        self.base_url = base_url or BASE_URLS["prod" if env == "shadow" else env]
        if backend is not None:
            self._backend = backend
        elif transport is not None:
            self._backend = HttpxBackend(httpx.AsyncClient(base_url=self.base_url, auth=auth, transport=transport,
                                                           timeout=10.0))
        else:
            self._backend = SdkBackend(self.base_url, signer=auth, timeout=10.0)
        self._max_cost = max_order_cost
        # Ticker prefixes this client may trade (one string, e.g. a Polymarket slug family on the shadow exchange, or
        # a set of series); a ticker must start with one of them plus "-".
        self._series = (series,) if isinstance(series, str) else tuple(sorted(series))
        if not self._series:
            raise ValueError("trading needs at least one series")
        self._sleep = sleep
        self._clock_s = clock_s
        self._meta: dict[str, tuple[float, dict]] = {}  # ticker -> (read at, {status, open_ts, close_ts})

    def __repr__(self) -> str:
        return f"KalshiTrading(env={self.env!r})"

    @property
    def max_order_cost(self) -> float:
        """The cap on a buy's notional (count x limit, $) this gateway enforces (the application pre-checks it)."""
        return float(self._max_cost)

    # -- guards -----------------------------------------------------------------------------------
    def _series_only(self, ticker: str) -> None:
        if not isinstance(ticker, str) or not ticker.startswith(tuple(f"{x}-" for x in self._series)):
            raise TradingViolation(f"only {'/'.join(self._series)} markets may be traded (got {ticker!r})")

    def _check(self, ticker: str, side: str, action: str, count: float, price: float) -> None:
        self._series_only(ticker)
        if side not in ("yes", "no") or action not in ("buy", "sell"):
            raise TradingViolation("side must be yes|no and action buy|sell")
        if not (count and count >= 0.01):
            raise TradingViolation("count must be at least 0.01 contracts")
        if not (0.0 < price < 1.0):
            raise TradingViolation("limit price must be strictly between $0 and $1")
        if action == "buy" and count * price > self._max_cost + 1e-9:
            raise TradingViolation(f"order notional ${count * price:.2f} exceeds the ${self._max_cost:.2f} cap")

    # -- requests ---------------------------------------------------------------------------------
    async def _send(self, op: str, *args: Any, **kw: Any) -> Any:
        """One backend operation (e.g. "create_order"), retried on a transport error, 429 or 5xx with the SAME
        arguments. Returns the response (status_code, text, json(), raise_for_status())."""
        last: Exception | None = None
        call = getattr(self._backend, op)
        for attempt in range(_RETRY):
            try:
                resp = await call(*args, **kw)
            except (httpx.TransportError, TransportFailure) as exc:  # same body (same client_order_id): no double order
                last = exc
                cause = exc.__cause__ or exc  # TransportFailure wraps aiohttp's error: name the real one
                log.warning("%s transport error (%s: %s: %s); retry %d", op, type(exc).__name__,
                            type(cause).__name__, str(cause)[:200], attempt + 1)
                if attempt + 1 < _RETRY:
                    await self._sleep(retry_delay(attempt))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                try:
                    resp.raise_for_status()
                except Exception as exc:
                    last = exc
                if attempt + 1 < _RETRY:  # full jitter; a 429 waits at least its Retry-After (adapters/retry.py)
                    wait = retry_delay(attempt, status=resp.status_code,
                                       retry_after=retry_after_s(header(getattr(resp, "headers", None))))
                    log.warning("%s HTTP %d; retry %d in %.2f s", op, resp.status_code, attempt + 1, wait)
                    await self._sleep(wait)
                continue
            return resp
        raise last if last else RuntimeError("request failed")

    async def place_limit(self, *, ticker: str, side: str, action: str, count: float, price: float,
                          client_order_id: str, tif: str = "immediate_or_cancel", post_only: bool = False,
                          reduce_only: bool = False) -> dict:
        """Place one limit order. Returns {order_id, client_order_id, filled, remaining, avg_price, fees, status}.

        A retry after a lost response reuses `client_order_id`; if Kalshi reports it as a duplicate, the existing
        order is looked up and returned instead of placing another (if the lists do not show it within ~5 s,
        OrderUnknown: not definitive, reconcile; never a rejection).
        """
        self._check(ticker, side, action, count, price)
        if tif not in ("immediate_or_cancel", "good_till_canceled", "fill_or_kill"):
            raise TradingViolation(f"unsupported time_in_force {tif!r}")
        book, yes_price = book_order(side, action, price)
        body = {"ticker": ticker, "client_order_id": client_order_id, "side": book, "count": _count(count),
                "price": yes_price, "time_in_force": tif, "self_trade_prevention_type": "taker_at_cross",
                "post_only": bool(post_only), "reduce_only": bool(reduce_only), "cancel_order_on_pause": False}
        resp = await self._send("create_order", body)
        if resp.status_code == 409:
            found = await self._find_after_conflict(ticker, client_order_id, side)
            if found is None:
                raise OrderUnknown(409, resp.text)
            return {**found, "duplicate": True}
        if resp.status_code >= 400:
            if definitive_status(resp.status_code):
                raise OrderRejected(resp.status_code, resp.text)
            raise OrderUnknown(resp.status_code, resp.text)  # e.g. 408 / 499: it may have been placed
        d = resp.json()
        filled = _f(d.get("fill_count")) or 0.0
        remaining = _f(d.get("remaining_count")) or 0.0
        avg = _f(d.get("average_fill_price"))
        if avg is not None and side == "no":
            avg = round(1 - avg, 4)
        fee_each = _f(d.get("average_fee_paid")) or 0.0
        status = "resting" if remaining > 0 and tif == "good_till_canceled" else (
            "executed" if filled > 0 and remaining == 0 else "canceled")
        return {"order_id": d.get("order_id"), "client_order_id": d.get("client_order_id", client_order_id),
                "filled": filled, "remaining": remaining, "avg_price": avg, "fees": round(fee_each * filled, 4),
                "status": status, "ts_ms": d.get("ts_ms")}

    async def cancel(self, order_id: str, ticker: str) -> dict:
        """Cancel a resting order. A 404 means it is no longer resting (filled or already canceled)."""
        self._series_only(ticker)
        resp = await self._send("cancel_order", order_id, ticker)
        if resp.status_code == 404:
            return {"order_id": order_id, "reduced_by": 0.0, "not_found": True}
        resp.raise_for_status()
        d = resp.json()
        return {"order_id": d.get("order_id", order_id), "reduced_by": _f(d.get("reduced_by")) or 0.0}

    async def get_order(self, order_id: str, side: str | None = None) -> dict:
        # Production briefly answers 404 for an order just written (seen right after a cancel); retry before
        # treating the order as missing.
        for attempt in range(_RETRY):
            resp = await self._send("get_order", order_id)
            if resp.status_code != 404:
                break
            await self._sleep(0.5 * (attempt + 1))
        resp.raise_for_status()
        return normalize_order(resp.json()["order"], side)

    async def _pages(self, op: str, key: str, **params: Any) -> list[dict]:
        """Every row of a cursor-paged list (`key` in each page), up to _MAX_PAGES pages of 200."""
        out: list[dict] = []
        cursor = None
        for _ in range(_MAX_PAGES):
            resp = await self._send(op, **params, limit=200, **({"cursor": cursor} if cursor else {}))
            resp.raise_for_status()
            d = resp.json()
            out += d.get(key) or []
            cursor = d.get("cursor")
            if not cursor:
                break
        else:
            log.warning("%s: more than %d pages; the rest is not read", op, _MAX_PAGES)
        return out

    async def orders(self, *, ticker: str | None = None, status: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return [normalize_order(o) for o in await self._pages("get_orders", "orders", **params)]

    async def _find_after_conflict(self, ticker: str, client_order_id: str, side: str | None) -> dict | None:
        """find_order, re-read for ~5 s: the lists can lag a placement by seconds (demo)."""
        for attempt in range(_FIND_TRIES):
            found = await self.find_order(ticker, client_order_id, side)
            if found is not None:
                return found
            if attempt + 1 < _FIND_TRIES:
                await self._sleep(0.5 * (attempt + 1))
        return None

    async def find_order(self, ticker: str, client_order_id: str, side: str | None = None) -> dict | None:
        """The order with this client_order_id, from the order list. The market's list (?ticker=) was seen on the
        Kalshi demo exchange briefly missing an order just placed while the account-wide list already showed it
        (2026-10-28), so a miss there is checked against the account-wide list before answering None."""
        for params in ({"ticker": ticker}, {}):
            for o in await self._pages("get_orders", "orders", **params):
                if o.get("client_order_id") == client_order_id and o.get("ticker") in (None, ticker):
                    return normalize_order(o, side)
        return None

    async def lost_order(self, ticker: str, client_order_id: str, *, side: str, action: str, since_ms: int,
                         count: float | None = None, price: float | None = None) -> dict:
        """The fills' word on an order whose answer was lost and that the order lists still do not show
        (domain/order_resolution.py). Every fill of the account in `ticker` since `since_ms` on the order's book side
        names an order id; each is read by id and the one carrying `client_order_id` IS the order (returned like
        find_order, with `proof` "fills"). None of them -> {filled: 0, status: "not_found", proof: "fills"}: the fills
        show nothing that could be it (the caller still checks the position before calling it unfilled). A failed
        read raises (unprovable: the order stays unknown)."""
        self._series_only(ticker)
        want = book_order(side, action, 0.5)[0]
        rows = await self.market_fills(ticker, max(int(since_ms) - TS_SLACK_MS, 0)) or []
        fills = [(f.get("order_id"), f.get("ts_ms"), "bid" if (f.get("yes_delta") or 0) > 0 else "ask") for f in rows]
        for oid in candidate_orders(fills, since_ms=since_ms, book_side=want):
            o = await self.get_order(oid, side)
            if o.get("client_order_id") == client_order_id:
                return {**o, "proof": "fills"}
        return {"order_id": None, "client_order_id": client_order_id, "ticker": ticker, "status": "not_found",
                "filled": 0.0, "remaining": 0.0, "avg_price": None, "fees": 0.0, "proof": "fills"}

    async def positions(self) -> list[dict]:
        """Open market positions: [{ticker, position}] (>0 yes contracts, <0 no contracts)."""
        out = []
        for p in await self._pages("get_positions", "market_positions"):
            pos = _f(p.get("position_fp", p.get("position"))) or 0.0
            if pos:
                out.append({"ticker": p.get("ticker"), "position": pos})
        return out

    async def market_fills(self, ticker: str, since_ms: int) -> list[dict] | None:
        """The account's fills in `ticker` since `since_ms`, oldest first, for the P&L of an exit made by hand:
        [{fill_id, order_id, ts_ms, count, yes_price, yes_delta, fee}]. `yes_delta` is the change in the net YES
        position: V2 fills report the book side (bid = bought YES, + count; ask = sold YES, - count; a NO buy is an
        "ask" fill, e.g. 2026-10-29 17:06:00 entry `book_side: ask, outcome_side: no`; the user's NO sale at 84c was
        `book_side: bid, outcome_side: yes, no_price_dollars: 0.8400`). Legacy side/action is the fallback."""
        self._series_only(ticker)
        out = []
        for f in await self._pages("get_fills", "fills", ticker=ticker, min_ts=int(since_ms) // 1000):
            if (f.get("market_ticker") or f.get("ticker")) not in (None, ticker):
                continue
            n = _f(f.get("count_fp", f.get("count"))) or 0.0
            yes = _f(f.get("yes_price_dollars"))
            if yes is None and _f(f.get("no_price_dollars")) is not None:
                yes = round(1 - _f(f.get("no_price_dollars")), 4)
            book = f.get("book_side")
            if book in ("bid", "ask"):
                delta = n if book == "bid" else -n
            else:
                buy_yes = (f.get("side"), f.get("action")) in (("yes", "buy"), ("no", "sell"))
                delta = n if buy_yes else -n
            ts = iso_to_ms(f["created_time"]) if f.get("created_time") else f.get("ts_ms")
            if not n or ts is None or ts < since_ms:
                continue
            out.append({"fill_id": f.get("fill_id") or f.get("trade_id"), "order_id": f.get("order_id"), "ts_ms": ts,
                        "count": n, "yes_price": yes, "yes_delta": delta, "fee": _f(f.get("fee_cost"))})
        return sorted(out, key=lambda x: x["ts_ms"])

    async def quote(self, ticker: str) -> dict:
        """The market's live top of book from this environment's exchange (dollars per contract).

        Prices come from GET /markets/{ticker}/orderbook, which is current. GET /markets/{ticker} was measured
        lagging the book by a median 0.8 s and up to 11 s (2026-10-28), so it supplies only metadata (status,
        open/close), cached per ticker and re-read at most every META_TTL_S."""
        self._series_only(ticker)
        meta = await self._market_meta(ticker)
        resp = await self._send("get_orderbook", ticker)  # last, so the prices are as fresh as possible
        resp.raise_for_status()
        return {**meta, **book_top(ticker, resp.json().get("orderbook_fp") or {})}

    async def market_result(self, ticker: str) -> dict:
        """The market's official result for settlement, read fresh (never the metadata cache): {ticker, status,
        result: "yes" | "no" | None, final, source}. `final` only once the status says the outcome is determined
        (RESULT_FINAL_STATUSES) and the result is yes or no. Raises on a failed read (the caller retries)."""
        self._series_only(ticker)
        resp = await self._send("get_market", ticker)
        resp.raise_for_status()
        m = resp.json()["market"]
        result = m.get("result") if m.get("result") in ("yes", "no") else None
        status = m.get("status")
        return {"ticker": m.get("ticker") or ticker, "status": status, "result": result,
                "final": result is not None and status in RESULT_FINAL_STATUSES,
                "source": "shadow_market_result" if self.env == "shadow" else "kalshi_market_result"}

    async def market_meta(self, ticker: str) -> dict:
        """The market's metadata only ({ticker, status, open_ts, close_ts}), cached per ticker for META_TTL_S: what
        the application merges with a live WebSocket quote (prices never come from here)."""
        self._series_only(ticker)
        return await self._market_meta(ticker)

    async def _market_meta(self, ticker: str) -> dict:
        now = self._clock_s()
        got = self._meta.get(ticker)
        if got is not None and now - got[0] < META_TTL_S:
            return got[1]
        resp = await self._send("get_market", ticker)
        resp.raise_for_status()
        meta = market_meta(resp.json()["market"])
        self._meta[ticker] = (now, meta)
        if len(self._meta) > 32:
            self._meta.pop(next(iter(self._meta)))
        return meta

    async def aclose(self) -> None:
        await self._backend.aclose()


def market_meta(m: dict) -> dict:
    """A /markets/{ticker} Market -> the metadata the gate uses. Its prices are deliberately ignored (they lag)."""
    return {"ticker": m.get("ticker"), "status": m.get("status"),
            "open_ts": iso_to_ms(m["open_time"]) if m.get("open_time") else None,
            "close_ts": iso_to_ms(m["close_time"]) if m.get("close_time") else None}


def trading_from_settings(env: str, key_id: str | None, key_path: str | Path | None, *,
                          max_order_cost: float) -> KalshiTrading | None:
    """The trading client for `env` (demo|prod), or None unless that environment's own key is configured.

    A key that cannot be loaded also gives None (the environment then counts as not configured)."""
    if not key_id or not key_path:
        return None
    try:
        key = load_private_key(key_path)
    except Exception as exc:  # never log the path contents
        log.warning("%s trading key could not be loaded (%s); %s trading stays off", env, type(exc).__name__, env)
        return None
    return KalshiTrading(TradingAuth(key_id, key), env=env, max_order_cost=max_order_cost)
