"""Kalshi perps (margin) WebSocket stream and the perp quote cache (perps plan U4; KTD8, KTD10, KTD11, KTD14).

Facts, from Kalshi's perps asyncapi spec (`perps_asyncapi.yaml`, downloaded 2026-10-30) and a read-only demo capture
(tests/fixtures/kalshi_margin_ws/public_capture.json, 2026-10-30):

* Endpoint: prod wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin, demo
  wss://external-api-margin-ws.demo.kalshi.co/trade-api/ws/v2/margin. The handshake carries Kalshi's three signed
  headers over GET /trade-api/ws/v2/margin (demo: opened in ~400 ms). The signer is any object with
  `headers(method, path)`; its own allow-list decides whether it signs. Headers are never logged.
* One `subscribe` with channels [orderbook_delta, ticker, trade] answers one `subscribed` per channel, each with its own
  sid (seen in the capture); a second one subscribes the private channels fill and user_orders.
* `orderbook_delta`: `orderbook_snapshot` first (levels under msg.bid / msg.ask as ["price", "count"] strings, e.g.
  ["7.3998", "1.00"]; the captured ask list is in descending price order), then `orderbook_delta`
  {market_ticker, price, delta, side: bid|ask}. Every message of a subscription carries the next `seq`; the spec says
  to apply deltas in seq order and recover a gap with update_subscription action get_snapshot for every subscribed
  market, replacing the book with the returned snapshot. Real-time mode (no update_interval_ms) is used, so our own
  orders and the public levels are never held apart.
* `ticker`: no seq, coalesced to at most one message per market per second (seen: exactly one a second, even when
  nothing changed). It carries the last trade price, reference_price (CF Benchmarks index scaled to one contract),
  settlement_mark_price, liquidation_mark_price ({price, ts_ms}) and funding_rate {rate, next_funding_time_ms, ts_ms}.
  Its bid/ask are not used: the book is the price source (KTD11).
* `trade`: {trade_id, market_ticker, price, count, taker_side: bid|ask, ts_ms} with a seq.
* `fill` and `user_order` (channel user_orders) carry NO seq, so a reconnect cannot tell what was missed: the service
  reconciles orders and fills over REST after every `resync` event (KTD8). Their fields are built from the spec's
  marginFillPayload / marginUserOrderPayload, NOT YET CONFIRMED ON DEMO (the demo account is not margin-enabled):
  fill {trade_id, order_id, client_order_id?, market_ticker, is_taker, side, ts_ms, price, count, fee_cost,
  post_position, subaccount?, order_source: user|system}; user_order {order_id, user_id, client_order_id, ticker, side,
  price, fill_count, remaining_count, created_ts_ms (nullable), last_updated_ts_ms?, last_update_reason?,
  expiration_ts_ms?, order_group_id?, subaccount_number?, order_source}. `order_source` "system" marks liquidations,
  margin exit / trailing-stop trigger fires and auto-rebalance orders. Whether a market filter on the private
  subscribe is honoured is per spec only.
* Keep-alive: Kalshi pings every 10 s; the `websockets` library answers and pings the server itself (stream.py's
  connect, compression off, is reused).

Emitted events (one callback, plain dicts, `env` on each):
  {"type": "resync", "reason"}          on EVERY connection, once all five channels are acknowledged, and before any
                                         fill or order from that connection (earlier ones are held until then)
  {"type": "health", "connected", "book_live", "reason", "last_msg_ms", "last_seq"}   on each change
  {"type": "fill", ...}, {"type": "order", ...}   see fill_event / order_event
  {"type": "trade", "ticker", "trade_id", "price", "count", "taker_side", "ts_ms"}
  {"type": "book_top", "ticker", "bid", "ask", "ts_ms"}   whenever the best bid or ask changes (the push trigger)
  {"type": "ticker", "ticker", "ts_ms"}                    on every ticker message (mark, funding: read the cache)

`PerpQuoteCache` (stream.quotes) judges freshness by BOOK updates (the snapshot and each delta), never by any message:
the ticker channel sends one message a second even when the book has stopped, so a frozen `orderbook_delta`
subscription used to be served as fresh (review fix plan 2026-10-31, B3#8; the 2026-10-31 13:59 Polymarket outage was
the same failure). The rule is the one the Polymarket book uses (domain/book_freshness.py): a book updated within 2 s
is served; an older, quiet book is served (at most 30 s) only while the connection is alive (any message within
15 s), the `trade` subscription has been confirmed since before the book's last update, and no print for the market
has arrived after it (a print without a book update within 2 s is a broken book feed). It answers None while the book
is resyncing or while disconnected; `age_ms()` (the book's age) stays readable so the view can say how old it is.
Per message the work is a dict update and, only when the best level empties, one scan of that side.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from btc_signal_engine.adapters.kalshi.stream import _ws_connect
from btc_signal_engine.adapters.backoff import failure, jittered, reconnecting, start_over
from btc_signal_engine.domain.book_freshness import unservable_reason
from btc_signal_engine.domain.markets import PerpSpec

log = logging.getLogger(__name__)

MARGIN_WS_PATH = "/trade-api/ws/v2/margin"
MARGIN_WS_URLS = {
    "perp_demo": "wss://external-api-margin-ws.demo.kalshi.co" + MARGIN_WS_PATH,
    "perp_prod": "wss://external-api-margin-ws.kalshi.com" + MARGIN_WS_PATH,
}
PUBLIC = ("orderbook_delta", "ticker", "trade")
PRIVATE = ("fill", "user_orders")
MAX_AGE_MS = 2_000  # older than this on the wall clock, the quote is not served
RESET_AFTER_MS = 60_000  # connected this long: the backoff starts over
SIDES = ("bid", "ask")
SOURCES = ("user", "system")

Emit = Callable[[dict], None]


class Signer(Protocol):
    def headers(self, method: str, path: str) -> dict[str, str]: ...


class MarginMessageError(ValueError):
    """A message that does not match Kalshi's schema (a required field missing or malformed)."""


class MarginStreamError(RuntimeError):
    """Kalshi refused one of our commands: the session is dropped and reopened with backoff."""


# -- parsing ------------------------------------------------------------------------------------------
def _req(msg: dict, key: str, kind: str) -> Any:
    if key not in msg or msg[key] is None:
        raise MarginMessageError(f"{kind} without {key}")
    return msg[key]


def _num(msg: dict, key: str, kind: str) -> float:
    v = _req(msg, key, kind)
    try:
        return float(v)
    except (TypeError, ValueError):
        raise MarginMessageError(f"{kind} {key} is not a number") from None


def _enum(msg: dict, key: str, allowed: tuple[str, ...], kind: str) -> str:
    v = _req(msg, key, kind)
    if v not in allowed:
        raise MarginMessageError(f"{kind} {key} {v!r} is not one of {allowed}")
    return v


def fill_event(msg: dict) -> dict:
    """A `fill` message's msg -> the stream's `fill` event (side bid = bought, ask = sold; dollars per contract)."""
    k = "fill"
    return {"type": "fill", "trade_id": _req(msg, "trade_id", k), "order_id": _req(msg, "order_id", k),
            "client_order_id": msg.get("client_order_id"), "ticker": _req(msg, "market_ticker", k),
            "is_taker": bool(_req(msg, "is_taker", k)), "side": _enum(msg, "side", SIDES, k),
            "price": _num(msg, "price", k), "count": _num(msg, "count", k), "fee": _num(msg, "fee_cost", k),
            "post_position": _num(msg, "post_position", k), "subaccount": msg.get("subaccount"),
            "order_source": _enum(msg, "order_source", SOURCES, k), "ts_ms": _req(msg, "ts_ms", k)}


def order_event(msg: dict) -> dict:
    """A `user_order` message's msg -> the stream's `order` event."""
    k = "user_order"
    if "created_ts_ms" not in msg:  # required, but may be null
        raise MarginMessageError("user_order without created_ts_ms")
    return {"type": "order", "order_id": _req(msg, "order_id", k), "client_order_id": _req(msg, "client_order_id", k),
            "ticker": _req(msg, "ticker", k), "side": _enum(msg, "side", SIDES, k), "price": _num(msg, "price", k),
            "fill_count": _num(msg, "fill_count", k), "remaining_count": _num(msg, "remaining_count", k),
            "created_ts_ms": msg["created_ts_ms"], "last_updated_ts_ms": msg.get("last_updated_ts_ms"),
            "last_update_reason": msg.get("last_update_reason"), "expiration_ts_ms": msg.get("expiration_ts_ms"),
            "order_group_id": msg.get("order_group_id"), "subaccount": msg.get("subaccount_number"),
            "order_source": _enum(msg, "order_source", SOURCES, k)}


def trade_event(msg: dict) -> dict:
    k = "trade"
    return {"type": "trade", "ticker": _req(msg, "market_ticker", k), "trade_id": _req(msg, "trade_id", k),
            "price": _num(msg, "price", k), "count": _num(msg, "count", k),
            "taker_side": _enum(msg, "taker_side", SIDES, k), "ts_ms": _req(msg, "ts_ms", k)}


# -- book ---------------------------------------------------------------------------------------------
class _Side:
    """One side's levels keyed by integer ticks (exact), with the best level cached."""

    def __init__(self, levels: Any, scale: int, bid: bool) -> None:
        self._scale, self._bid = scale, bid
        self.levels: dict[int, float] = {}
        for lv in levels or ():
            if not isinstance(lv, (list, tuple)) or len(lv) != 2:
                raise MarginMessageError("orderbook_snapshot level is not [price, count]")
            p, q = self._tick(lv[0]), self._count(lv[1])
            if q > 0:
                self.levels[p] = q
        self.best = self._scan()

    def _tick(self, price: Any) -> int:
        try:
            return round(float(price) * self._scale)
        except (TypeError, ValueError):
            raise MarginMessageError("orderbook price is not a number") from None

    @staticmethod
    def _count(count: Any) -> float:
        try:
            return float(count)
        except (TypeError, ValueError):
            raise MarginMessageError("orderbook count is not a number") from None

    def _scan(self) -> int | None:
        if not self.levels:
            return None
        return max(self.levels) if self._bid else min(self.levels)

    def apply(self, price: Any, delta: Any) -> None:
        p = self._tick(price)
        size = round(self.levels.get(p, 0.0) + self._count(delta), 2)
        if size < 0:
            raise MarginMessageError("orderbook delta takes a level below zero")
        if size > 0:
            self.levels[p] = size
            if self.best is None or (p > self.best if self._bid else p < self.best):
                self.best = p
        else:
            self.levels.pop(p, None)
            if p == self.best:
                self.best = self._scan()

    def top(self) -> tuple[float | None, float | None]:
        if self.best is None:
            return None, None
        return round(self.best / self._scale, 4), self.levels[self.best]


class PerpBook:
    """The market's book from one snapshot and the deltas after it (prices in dollars per contract)."""

    def __init__(self, snapshot: dict, tick: float) -> None:
        scale = round(1 / tick)
        self.bids = _Side(snapshot.get("bid"), scale, bid=True)
        self.asks = _Side(snapshot.get("ask"), scale, bid=False)

    def apply(self, msg: dict) -> None:
        side = _enum(msg, "side", SIDES, "orderbook_delta")
        (self.bids if side == "bid" else self.asks).apply(_req(msg, "price", "orderbook_delta"),
                                                          _req(msg, "delta", "orderbook_delta"))

    def top(self) -> tuple[float | None, float | None, float | None, float | None]:
        """(best bid, best ask, bid size, ask size); None on an empty side."""
        (b, bq), (a, aq) = self.bids.top(), self.asks.top()
        return b, a, bq, aq


# -- quote cache --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PerpQuote:
    ticker: str
    bid: float | None  # from the book; None when that side is empty
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    age_ms: int  # wall-clock age of the book's newest update (snapshot or delta)
    # from the ticker channel, None when its last message is older than the max age
    last_price: float | None
    reference_price: float | None
    settlement_mark: float | None
    liquidation_mark: float | None
    funding_rate: float | None  # a decimal per funding interval (0.0152 = 1.52%)
    next_funding_ms: int | None
    ticker_age_ms: int | None


def _price_of(obj: Any, key: str) -> float | None:
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise MarginMessageError(f"ticker {key} is not an object")
    return _num(obj, "price", f"ticker {key}")


class PerpQuoteCache:
    """The freshest book top and ticker for one perp market; answers nothing when older than `max_age_ms`."""

    def __init__(self, ticker: str, clock_ms: Callable[[], int], max_age_ms: int = MAX_AGE_MS) -> None:
        self.ticker = ticker
        self._clock = clock_ms
        self.max_age_ms = max_age_ms
        self._book: PerpBook | None = None
        self._why = "not connected yet"
        self._last_ms: int | None = None
        self._tick: dict | None = None
        self._tick_ms: int | None = None
        self._n = 0  # every message gets the next number: "a print after this book update" is exact
        self._book_ms: int | None = None  # the book's newest update (snapshot or delta): local ms, message number
        self._book_seq = 0
        self._trade_seq: int | None = None  # the newest print's message number
        self._unbooked_ms: int | None = None  # the first print since the book's last update (None: one followed)
        self._covered_ms: int | None = None  # every print since this local time was received (trade sub confirmed)

    # fed by the stream
    def heard(self, now_ms: int) -> None:
        """Any message on the connection (keeps it alive; never makes the book fresh)."""
        self._last_ms = now_ms
        self._n += 1

    def set_book(self, book: PerpBook | None, why: str = "") -> None:
        self._book = book
        self._why = why
        if book is None:
            self._book_ms = None

    def book_updated(self, now_ms: int) -> None:
        """A snapshot or a delta for the market was applied (after `heard` for the same message)."""
        self._book_ms, self._book_seq, self._unbooked_ms = now_ms, self._n, None

    def traded(self, now_ms: int) -> None:
        """A print for the market arrived (after `heard` for the same message)."""
        self._trade_seq = self._n
        if self._unbooked_ms is None:
            self._unbooked_ms = now_ms

    def cover_trades(self, since_ms: int | None) -> None:
        """The trade subscription is confirmed and every print since `since_ms` was received (None: not covered)."""
        self._covered_ms = since_ms

    def on_ticker(self, msg: dict, now_ms: int) -> None:
        """Parse a ticker msg; a malformed one raises MarginMessageError and leaves the previous one in place."""
        fr = msg.get("funding_rate")
        if fr is not None and not isinstance(fr, dict):
            raise MarginMessageError("ticker funding_rate is not an object")
        self._tick = {"last_price": _num(msg, "price", "ticker"),
                      "reference_price": _price_of(msg.get("reference_price"), "reference_price"),
                      "settlement_mark": _price_of(msg.get("settlement_mark_price"), "settlement_mark_price"),
                      "liquidation_mark": _price_of(msg.get("liquidation_mark_price"), "liquidation_mark_price"),
                      "funding_rate": None if fr is None else _num(fr, "rate", "ticker funding_rate"),
                      "next_funding_ms": None if fr is None else _req(fr, "next_funding_time_ms",
                                                                      "ticker funding_rate")}
        self._tick_ms = now_ms

    # reads
    @property
    def last_msg_ms(self) -> int | None:
        """Local wall-clock time of the newest message on the connection (any kind)."""
        return self._last_ms

    def age_ms(self) -> int | None:
        """Wall-clock age of the book's newest update (None before any)."""
        return None if self._book_ms is None else self._clock() - self._book_ms

    def why_none(self) -> str | None:
        """Why quote() answers None, in plain English (None while a quote is served): the module docstring."""
        if self._book is None:
            return self._why
        if self._book_ms is None:
            return "no order book update yet"
        return unservable_reason(now_ms=self._clock(), book_ms=self._book_ms, book_seq=self._book_seq,
                                 conn_last_ms=self._last_ms, last_trade_seq=self._trade_seq,
                                 unbooked_ms=self._unbooked_ms, covered_since_ms=self._covered_ms,
                                 what="order book", conn="the perps connection", max_age_ms=self.max_age_ms)

    def quote(self) -> PerpQuote | None:
        if self.why_none() is not None:
            return None
        now = self._clock()
        bid, ask, bq, aq = self._book.top()
        t_age = None if self._tick_ms is None else now - self._tick_ms
        tick = self._tick if t_age is not None and t_age <= self.max_age_ms else {}
        return PerpQuote(self.ticker, bid, ask, bq, aq, now - self._book_ms,
                         tick.get("last_price"), tick.get("reference_price"), tick.get("settlement_mark"),
                         tick.get("liquidation_mark"), tick.get("funding_rate"), tick.get("next_funding_ms"), t_age)


# -- stream -------------------------------------------------------------------------------------------
class MarginStream:
    """One authenticated margin connection for a perp env (perp_demo|perp_prod) and the PerpSpec's ticker.

    `connect(url, headers)` returns an object with async send(str), recv() and close() (tests pass a fake)."""

    def __init__(self, env: str, spec: PerpSpec, signer: Signer, on_event: Emit, *, url: str | None = None,
                 connect: Callable[[str, dict], Any] = _ws_connect,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 backoff: tuple[float, float] = (1.0, 30.0), sleep: Callable[[float], Any] = asyncio.sleep,
                 max_age_ms: int = MAX_AGE_MS) -> None:
        if env not in MARGIN_WS_URLS:
            raise ValueError(f"margin stream env must be one of {sorted(MARGIN_WS_URLS)}")
        self.env = env
        self.ticker = spec.ticker(env)
        self._tick = spec.tick
        self.url = url or MARGIN_WS_URLS[env]
        self._signer = signer
        self._emit_cb = on_event
        self._connect = connect
        self._clock = clock_ms
        self._backoff0, self._backoff_max = backoff
        self._sleep = sleep
        self.quotes = PerpQuoteCache(self.ticker, clock_ms, max_age_ms)
        self._stop = asyncio.Event()
        self._closed = False
        self._down = "not connected yet"  # why the last connection ended
        self._book_why = ""  # why there is no book while connected
        self._last_health: tuple | None = None
        self.connects = 0
        self._reset()

    def __repr__(self) -> str:
        return f"MarginStream(env={self.env!r}, ticker={self.ticker!r})"

    def _reset(self) -> None:
        self._ws = None
        self._cmd = 0
        self._sids: dict[str, int] = {}
        self._seq: dict[int, int] = {}
        self._connected = False
        self._held: list[dict] = []  # fills / orders that arrived before this connection's resync
        self._book: PerpBook | None = None
        self._snap_pending = False
        self._top: tuple | None = None
        self._book_why = "waiting for the order book snapshot"
        self.quotes.set_book(None, f"not connected ({self._down})")
        self.quotes.cover_trades(None)  # a new connection's prints are vouched for only once its trade sub is acked

    # -- port -------------------------------------------------------------------------------------
    def health(self) -> dict:
        """Connected = every channel acknowledged; book_live = a snapshot applied and no gap since; reason says why
        not (None when both hold); last_seq per subscription id."""
        book_live = self._book is not None
        reason = self._down if not self._connected else (None if book_live else self._book_why)
        return {"connected": self._connected, "book_live": book_live, "reason": reason,
                "last_msg_ms": self.quotes.last_msg_ms, "last_seq": dict(self._seq)}

    def close(self) -> None:
        self._closed = True
        self._stop.set()

    # -- events -----------------------------------------------------------------------------------
    def _emit(self, ev: dict) -> None:
        try:
            self._emit_cb({**ev, "env": self.env})
        except Exception:
            log.warning("margin stream consumer failed", exc_info=True)

    def _health_changed(self) -> None:
        h = self.health()
        key = (h["connected"], h["book_live"], h["reason"])
        if key != self._last_health:
            self._last_health = key
            self._emit({"type": "health", **h})

    def _private(self, ev: dict) -> None:
        if self._connected:
            self._emit(ev)
        else:
            self._held.append(ev)

    def _top_changed(self) -> None:
        if self._book is None:
            return
        b, a, _, _ = self._book.top()
        if (b, a) != self._top:
            self._top = (b, a)
            self._emit({"type": "book_top", "ticker": self.ticker, "bid": b, "ask": a, "ts_ms": self._clock()})

    # -- commands ---------------------------------------------------------------------------------
    async def _send(self, cmd: str, params: dict) -> int:
        self._cmd += 1
        await self._ws.send(json.dumps({"id": self._cmd, "cmd": cmd, "params": params}))
        return self._cmd

    async def _book_lost(self, why: str) -> None:
        """Serve no quote and ask for a fresh snapshot (once until it arrives)."""
        self._book, self._top, self._book_why = None, None, why
        self.quotes.set_book(None, why)
        sid = self._sids.get("orderbook_delta")
        if sid is not None and not self._snap_pending:
            self._snap_pending = True
            await self._send("update_subscription", {"sids": [sid], "market_tickers": [self.ticker],
                                                     "action": "get_snapshot"})

    # -- messages ---------------------------------------------------------------------------------
    async def handle(self, d: dict) -> None:
        """Apply one decoded message."""
        now = self._clock()
        self.quotes.heard(now)
        kind = d.get("type")
        sid, seq = d.get("sid"), d.get("seq")
        msg = d.get("msg") or {}
        if sid is not None and seq is not None:
            last = self._seq.get(sid)
            self._seq[sid] = seq
            if last is not None and seq != last + 1:
                log.warning("margin %s stream: seq gap on sid %s (%s -> %s)", self.env, sid, last, seq)
                if sid == self._sids.get("orderbook_delta"):
                    await self._book_lost("order book resyncing after a sequence gap")
                elif sid == self._sids.get("trade"):  # prints before now may be missing: vouch only from now
                    self.quotes.cover_trades(now)
        if kind == "subscribed":
            self._sids[msg.get("channel")] = msg.get("sid")
            if msg.get("channel") == "trade":
                self.quotes.cover_trades(now)
            if not self._connected and all(c in self._sids for c in PUBLIC + PRIVATE):
                self._connected = True
                if self._book is None:
                    self.quotes.set_book(None, self._book_why)
                self._emit({"type": "resync", "reason": "connected"})  # fills and orders may have been missed
                held, self._held = self._held, []
                for ev in held:
                    self._emit(ev)
        elif kind == "orderbook_snapshot":
            if msg.get("market_ticker") == self.ticker:
                self._snap_pending = False
                try:
                    book = PerpBook(msg, self._tick)
                except MarginMessageError as exc:
                    log.warning("margin %s stream: bad snapshot (%s)", self.env, exc)
                    await self._book_lost(f"order book unreadable ({exc})")
                else:
                    self._book = book
                    self.quotes.set_book(book)
                    self.quotes.book_updated(now)
                    self._top_changed()
        elif kind == "orderbook_delta":
            if self._book is not None and msg.get("market_ticker") == self.ticker:
                try:
                    self._book.apply(msg)
                except MarginMessageError as exc:
                    log.warning("margin %s stream: bad delta (%s)", self.env, exc)
                    await self._book_lost(f"order book resyncing ({exc})")
                else:
                    self.quotes.book_updated(now)
                    self._top_changed()
        elif kind == "ticker":
            if msg.get("market_ticker") == self.ticker:
                try:
                    self.quotes.on_ticker(msg, now)
                except MarginMessageError as exc:
                    log.warning("margin %s stream: bad ticker (%s)", self.env, exc)
                else:
                    self._emit({"type": "ticker", "ticker": self.ticker, "ts_ms": now})
        elif kind == "trade":
            if msg.get("market_ticker") == self.ticker:
                self.quotes.traded(now)  # a print after the book's last update: the quiet path needs a newer one
            try:
                self._emit(trade_event(msg))
            except MarginMessageError as exc:
                log.warning("margin %s stream: bad trade (%s)", self.env, exc)
        elif kind in ("fill", "user_order"):
            try:
                ev = fill_event(msg) if kind == "fill" else order_event(msg)
            except MarginMessageError as exc:  # never dropped silently: the service reconciles over REST
                log.warning("margin %s stream: unreadable %s (%s)", self.env, kind, exc)
                self._private({"type": "resync", "reason": f"unreadable {kind}"})
            else:
                self._private(ev)
        elif kind == "error":
            raise MarginStreamError(f"Kalshi refused command {d.get('id')}: code {msg.get('code')} {msg.get('msg')}")
        self._health_changed()

    # -- connection -------------------------------------------------------------------------------
    async def _session(self) -> None:
        headers = self._signer.headers("GET", MARGIN_WS_PATH)
        self._ws = await self._connect(self.url, headers)
        self.connects += 1
        self._opened_ms = self._clock()
        await self._send("subscribe", {"channels": list(PUBLIC), "market_tickers": [self.ticker]})
        await self._send("subscribe", {"channels": list(PRIVATE), "market_tickers": [self.ticker]})
        stopper = asyncio.ensure_future(self._stop.wait())
        reader = asyncio.ensure_future(self._ws.recv())
        try:
            while True:
                done, _ = await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    return
                raw = reader.result()  # raises when the connection closes -> reconnect
                await self.handle(json.loads(raw))
                reader = asyncio.ensure_future(self._ws.recv())
        finally:
            for fut in (reader, stopper):
                if fut.done() and not fut.cancelled():
                    fut.exception()  # retrieved: the close below is the expected end
                else:
                    fut.cancel()

    async def _teardown(self, reason: str) -> None:
        if self._held:  # the connection died before every channel was confirmed: still say "reconcile" first
            self._emit({"type": "resync", "reason": "connection lost before every channel was confirmed"})
            for ev in self._held:
                self._emit(ev)
        ws = self._ws
        self._down = reason
        self._reset()
        self._health_changed()
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def run(self) -> None:
        """Reconnect forever (tenacity, adapters/backoff.py): full jitter, starting over after a session that lasted
        RESET_AFTER_MS."""
        def dropped(state: Any) -> None:  # never log headers or key material
            exc = failure(state)
            if isinstance(exc, MarginStreamError):
                log.warning("margin %s stream: %s; reconnecting in %.1fs", self.env, exc, state.upcoming_sleep)
            else:
                log.warning("margin %s stream %s; reconnecting in %.1fs", self.env,
                            "closed" if exc is None else f"dropped ({type(exc).__name__})", state.upcoming_sleep)

        async for attempt in reconnecting(jittered(self._backoff0, self._backoff_max), closed=lambda: self._closed,
                                          sleep=self._sleep, before_sleep=dropped):
            with attempt:
                self._opened_ms: int | None = None
                try:
                    await self._session()
                except asyncio.CancelledError:
                    await self._teardown("closed")
                    raise
                except Exception as exc:  # never log headers or key material
                    await self._teardown(str(exc) if isinstance(exc, MarginStreamError) else type(exc).__name__)
                    raise
                else:
                    await self._teardown("closed")
                finally:
                    if self._opened_ms is not None and self._clock() - self._opened_ms >= RESET_AFTER_MS:
                        start_over(attempt)  # it ran a while: start the backoff over
