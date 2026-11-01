"""Kalshi WebSocket stream for trading: the PRIMARY source of order/fill updates and of the stop monitor's prices.
REST polling in the application stays on as the backup (application/trading.py).

Verified against Kalshi's docs (read 2026-10-28) and live read-only subscriptions on demo and production:

* Endpoint: prod wss://external-api-ws.kalshi.com/trade-api/ws/v2, demo wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2
  (https://docs.kalshi.com/getting_started/api_environments.md), matching the external-api REST hosts.
* Auth: the handshake carries the same KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE headers as REST, signed over
  timestamp + "GET" + "/trade-api/ws/v2" (https://docs.kalshi.com/getting_started/quick_start_websockets.md).
  `TradingAuth` signs it only because that exact (GET, path) is on its allow-list.
* Commands: {"id", "cmd": "subscribe", "params": {"channels": [...], "market_tickers": [...]}}; update_subscription
  with action add_markets / delete_markets / get_snapshot and "sids": [sid]; unsubscribe {"sids": [...]}
  (https://docs.kalshi.com/websockets/websocket-connection.md, https://docs.kalshi.com/asyncapi.yaml).
* Channels: `fill` (https://docs.kalshi.com/websockets/user-fills.md), `user_orders` -> messages of type
  `user_order` carrying the REST Order fields (https://docs.kalshi.com/websockets/user-orders.md), and
  `orderbook_delta` -> `orderbook_snapshot` then `orderbook_delta` with a per-subscription `seq`
  (https://docs.kalshi.com/websockets/orderbook-updates.md). `ticker` has no seq, so prices come from the book.
* Seen live: every reply on a subscription (ok, error, a get_snapshot snapshot) takes the next `seq` of that
  subscription, so seq must be tracked over all messages carrying a sid. Re-adding a market sends a new snapshot.
* `use_yes_price: true` is sent explicitly (Kalshi will flip the default and then remove the flag): the NO side then
  arrives in YES prices, i.e. each "no" level is a YES ask at that price. Checked live against the ticker channel.
* Keep-alive: Kalshi pings every 10 s (https://docs.kalshi.com/websockets/connection-keep-alive.md); the
  `websockets` library answers pings and pings the server itself, closing a dead connection.
* Public trades (opt-in, `trades=True`): channel `trade` (https://docs.kalshi.com/websockets/public-trades.md),
  one message per print with its own `seq`: msg {trade_id, market_ticker, yes_price_dollars, no_price_dollars,
  count_fp, taker_side, taker_outcome_side, taker_book_side (bid = the taker bought YES, so it hit resting YES asks;
  ask = the taker sold YES, so it hit resting YES bids), is_block_trade, ts (deprecated, s), ts_ms}. Captured live
  read-only 2026-10-28 in tests/fixtures/kalshi_ws/prod_trade.jsonl. It is a separate subscription (own sid) kept on
  the same set of markets as the order book.

Emitted events (one callback, plain dicts; `env` on each):
  {"type": "order", "order": normalize_order(...)}               a user_order update (YES prices)
  {"type": "fill", "order_id", "client_order_id", "ticker", "count", "yes_price", "fee", "action", "side", ...}
  {"type": "quote", "ticker", "yes_bid", "yes_ask", "no_bid", "no_ask", "ts_ms"}  top of book (same keys as quote())
  {"type": "health", "connected", "last_msg_ms", "stale"}       on every change of connected/stale
  {"type": "resync", "reason"}                                   updates may have been missed: poll now
With `trades=True` also:
  {"type": "trade", "ticker", "trade_id", "yes_price", "count", "taker_book_side", "is_block_trade", "ts_ms"}
  {"type": "trade_feed", "tickers", "ts_ms"}  the markets whose prints are covered from now on (on every change)
  {"type": "trade_gap", "ts_ms"}              a trade seq gap: prints before now may be missing
Quotes are emitted when the top of book changes and re-emitted every `requote_ms` while the book is known to be
current, so a quiet market still has a fresh price. A seq gap or a reconnect marks the stream stale until fresh
snapshots arrive; the application falls back to REST until then. So does silence: while an order-book market is
subscribed, no message from Kalshi for SILENCE_MS (the requote is not a message) marks the stream stale, since a
frozen socket raises nothing. A private stream with no markets may legitimately hear nothing for hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from btc_signal_engine.adapters.kalshi.auth import KalshiSigner, PrivateKey, load_private_key, now_ms
from btc_signal_engine.adapters.kalshi.trading import TradingAuth, TradingViolation, normalize_order
from btc_signal_engine.adapters.retry import Backoff

log = logging.getLogger(__name__)

WS_PATH = "/trade-api/ws/v2"
WS_URLS = {
    "prod": "wss://external-api-ws.kalshi.com" + WS_PATH,
    "demo": "wss://external-api-ws.demo.kalshi.co" + WS_PATH,
}
PRIVATE = ("fill", "user_orders")
SILENCE_MS = 12_000  # a subscribed book with no message for this long is stale (the live book sends many a second)

Emit = Callable[[dict], None]


def _f(x: Any) -> float | None:
    if x in (None, ""):
        return None
    return float(x)


def _px(p: float | None) -> float | None:
    return round(p, 4) if p is not None and 0.0 < p < 1.0 else None


class Book:
    """One market's book: YES bids and YES asks at YES prices (the use_yes_price form)."""

    def __init__(self, yes: Iterable, no: Iterable) -> None:
        self.bids = {float(p): float(s) for p, s in yes if float(s) > 0}
        self.asks = {float(p): float(s) for p, s in no if float(s) > 0}  # "no" levels in YES prices = YES asks

    def apply(self, side: str, price: str, delta: str) -> None:
        levels = self.bids if side == "yes" else self.asks
        p = float(price)
        size = round(levels.get(p, 0.0) + float(delta), 2)
        if size > 0:
            levels[p] = size
        else:
            levels.pop(p, None)

    def top(self) -> tuple[float | None, float | None]:
        return (max(self.bids) if self.bids else None, min(self.asks) if self.asks else None)


def quote_from_top(ticker: str, yes_bid: float | None, yes_ask: float | None, ts_ms: int) -> dict:
    return {"ticker": ticker, "yes_bid": _px(yes_bid), "yes_ask": _px(yes_ask),
            "no_bid": _px(None if yes_ask is None else 1 - yes_ask),
            "no_ask": _px(None if yes_bid is None else 1 - yes_bid), "ts_ms": ts_ms}


def fill_event(msg: dict) -> dict:
    return {"type": "fill", "order_id": msg.get("order_id"), "client_order_id": msg.get("client_order_id"),
            "ticker": msg.get("market_ticker"), "trade_id": msg.get("trade_id"), "count": _f(msg.get("count_fp")) or 0.0,
            "yes_price": _f(msg.get("yes_price_dollars")), "fee": _f(msg.get("fee_cost")) or 0.0,
            "action": msg.get("action"), "side": msg.get("side"), "is_taker": msg.get("is_taker"),
            "post_position": _f(msg.get("post_position_fp")), "ts_ms": msg.get("ts_ms")}


def trade_event(msg: dict) -> dict:
    """A public `trade` message -> the stream's `trade` event (YES price; taker_book_side bid|ask)."""
    return {"type": "trade", "ticker": msg.get("market_ticker"), "trade_id": msg.get("trade_id"),
            "yes_price": _f(msg.get("yes_price_dollars")), "count": _f(msg.get("count_fp")) or 0.0,
            "taker_book_side": msg.get("taker_book_side"), "is_block_trade": bool(msg.get("is_block_trade")),
            "ts_ms": msg.get("ts_ms")}


class WsOnlyAuth(KalshiSigner):
    """Signs the WebSocket handshake and nothing else (for the market-data stream: no REST, no orders; it pins no
    REST host, so as an httpx auth it refuses every request)."""

    violation = TradingViolation
    refuses = "market-data stream refuses"

    def __init__(self, key_id: str, private_key: PrivateKey, clock_ms: Callable[[], int] = now_ms) -> None:
        super().__init__(key_id, private_key, hosts=(), clock_ms=clock_ms)

    def allows(self, method: str, path: str) -> bool:
        return (method, path) == ("GET", WS_PATH)


# The largest WebSocket frame accepted (B3#11): an orderbook_snapshot of a busy market is tens of KB; anything near
# this is not a Kalshi message we use, and an unbounded frame would be read whole into memory first.
MAX_FRAME_BYTES = 8 * 1024 * 1024


async def _ws_connect(url: str, headers: dict[str, str]):
    import websockets

    # compression=None: inflating large frames on the loop thread stalled the app (KTD2).
    return await websockets.connect(url, additional_headers=headers, max_size=MAX_FRAME_BYTES, ping_interval=10,
                                    ping_timeout=10, open_timeout=10, compression=None)


class KalshiStream:
    """One authenticated connection for an environment (demo|prod). Implements the `TradingStream` port.

    `connect(url, headers)` returns an object with async send(str), recv() and close() (tests pass a fake)."""

    def __init__(self, env: str, auth: TradingAuth | WsOnlyAuth, on_event: Emit, *, url: str | None = None,
                 private: bool = True, trades: bool = False,
                 connect: Callable[[str, dict], Any] = _ws_connect,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000), backoff: tuple[float, float] = (1.0, 30.0),
                 requote_ms: int = 1000, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        if env not in WS_URLS:
            raise ValueError("stream env must be 'demo' or 'prod'")
        self.env = env
        self.url = url or WS_URLS[env]
        self._private = PRIVATE if private else ()  # () = market data only (the Coach's price feed)
        self._trades = trades  # also subscribe the public `trade` channel on the same markets
        self._auth = auth
        self._emit_cb = on_event
        self._connect = connect
        self._clock = clock_ms
        self._backoff0, self._backoff_max = backoff
        self._requote_ms = requote_ms
        self._sleep = sleep
        self._closed = False
        self._wake = asyncio.Event()
        self._want: set[str] = set()
        self._reset()
        self._last_health: tuple[bool, bool] = (False, True)  # emit only real changes
        self.connects = 0

    def __repr__(self) -> str:
        return f"KalshiStream(env={self.env!r})"

    def _reset(self) -> None:
        self._ws = None
        self._connected = False
        self._last_msg_ms: int | None = getattr(self, "_last_msg_ms", None)
        self._cmd = 0
        self._sids: dict[str, int] = {}  # channel -> sid
        self._seq: dict[int, int] = {}
        self._subbed: set[str] = set()  # markets in the orderbook subscription (sent or acked)
        self._ob_pending: int | None = None  # id of an orderbook subscribe in flight
        self._books: dict[str, Book] = {}
        self._tops: dict[str, tuple] = {}
        self._gap = False  # a seq gap: books are being re-snapshotted
        self._tr_subbed: set[str] = set()  # markets in the trade subscription (sent or acked)
        self._tr_live: set[str] = set()  # markets whose trade subscription Kalshi acknowledged
        self._tr_pending: int | None = None  # id of a trade subscribe in flight
        self._tr_adds: dict[int, set[str]] = {}  # update_subscription id -> markets it adds

    # -- port ---------------------------------------------------------------------------------------
    def subscribe_tickers(self, tickers: Iterable[str]) -> None:
        want = {t for t in tickers if t}
        if want != self._want:
            self._want = want
            self._wake.set()

    def health(self) -> dict:
        last = self._last_msg_ms
        silent = bool(self._subbed) and (last is None or self._clock() - last > SILENCE_MS)
        return {"connected": self._connected, "last_msg_ms": last,
                "stale": (not self._connected) or self._gap or silent}

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    # -- events -------------------------------------------------------------------------------------
    def _emit(self, ev: dict) -> None:
        try:
            self._emit_cb({**ev, "env": self.env})
        except Exception:
            log.warning("kalshi stream consumer failed", exc_info=True)

    def _health_changed(self) -> None:
        h = self.health()
        key = (h["connected"], h["stale"])
        if key != self._last_health:
            self._last_health = key
            self._emit({"type": "health", **h})

    def _quote(self, ticker: str, force: bool = False) -> None:
        book = self._books.get(ticker)
        if book is None or self._gap:
            return
        top = book.top()
        if force or top != self._tops.get(ticker):
            self._tops[ticker] = top
            self._emit({"type": "quote", **quote_from_top(ticker, top[0], top[1], self._clock())})

    # -- commands -----------------------------------------------------------------------------------
    async def _send(self, cmd: str, params: dict) -> int:
        self._cmd += 1
        await self._ws.send(json.dumps({"id": self._cmd, "cmd": cmd, "params": params}))
        return self._cmd

    async def _sync_markets(self) -> None:
        """Bring the orderbook subscription to the wanted set of markets."""
        if self._ws is None or self._ob_pending is not None:
            return
        sid = self._sids.get("orderbook_delta")
        want = set(self._want)
        if sid is None:
            if want:
                self._ob_pending = await self._send("subscribe", {"channels": ["orderbook_delta"],
                                                                  "market_tickers": sorted(want),
                                                                  "use_yes_price": True})
                self._subbed = want
            return
        if not want:
            await self._send("unsubscribe", {"sids": [sid]})
            self._sids.pop("orderbook_delta", None)
            self._subbed, self._books, self._tops, self._gap = set(), {}, {}, False
            return
        add, drop = sorted(want - self._subbed), sorted(self._subbed - want)
        if add:
            await self._send("update_subscription", {"sids": [sid], "market_tickers": add, "action": "add_markets"})
        if drop:
            await self._send("update_subscription", {"sids": [sid], "market_tickers": drop,
                                                     "action": "delete_markets"})
            for t in drop:
                self._books.pop(t, None)
                self._tops.pop(t, None)
        self._subbed = want
        self._gap_check()

    async def _sync_trades(self) -> None:
        """Bring the public trade subscription to the wanted set of markets (only with trades=True)."""
        if not self._trades or self._ws is None or self._tr_pending is not None:
            return
        sid = self._sids.get("trade")
        want = set(self._want)
        if sid is None:
            if want:
                self._tr_pending = await self._send("subscribe", {"channels": ["trade"],
                                                                  "market_tickers": sorted(want)})
                self._tr_subbed = want
            return
        if not want:
            await self._send("unsubscribe", {"sids": [sid]})
            self._sids.pop("trade", None)
            self._tr_subbed, self._tr_adds = set(), {}
            self._trade_feed(set())
            return
        add, drop = sorted(want - self._tr_subbed), sorted(self._tr_subbed - want)
        if add:
            cid = await self._send("update_subscription", {"sids": [sid], "market_tickers": add,
                                                           "action": "add_markets"})
            self._tr_adds[cid] = set(add)
        if drop:
            await self._send("update_subscription", {"sids": [sid], "market_tickers": drop,
                                                     "action": "delete_markets"})
            self._trade_feed(self._tr_live - set(drop))
        self._tr_subbed = want

    def _trade_feed(self, live: set[str]) -> None:
        if live != self._tr_live:
            self._tr_live = live
            self._emit({"type": "trade_feed", "tickers": sorted(live), "ts_ms": self._clock()})

    def _gap_check(self) -> None:
        if self._gap and all(x in self._books for x in self._subbed):
            self._gap = False

    async def _resnapshot(self) -> None:
        sid = self._sids.get("orderbook_delta")
        if sid is not None and self._subbed:
            await self._send("update_subscription", {"sids": [sid], "market_tickers": sorted(self._subbed),
                                                     "action": "get_snapshot"})

    # -- messages -----------------------------------------------------------------------------------
    async def handle(self, d: dict) -> None:
        """Apply one decoded message (public for tests)."""
        self._last_msg_ms = self._clock()
        kind = d.get("type")
        sid, seq = d.get("sid"), d.get("seq")
        if sid is not None and seq is not None:
            last = self._seq.get(sid)
            self._seq[sid] = seq
            if last is not None and seq != last + 1:
                await self._on_gap(sid, last, seq)
        msg = d.get("msg") or {}
        if kind == "subscribed":
            ch = msg.get("channel")
            self._sids[ch] = msg.get("sid")
            if ch == "orderbook_delta":
                self._ob_pending = None
            if ch == "trade":
                self._tr_pending = None
                self._trade_feed(set(self._tr_subbed))
            if all(c in self._sids for c in self._private) and not self._connected:
                self._connected = True
                self._emit({"type": "resync", "reason": "connected"})  # anything during the outage was missed
        elif kind == "orderbook_snapshot":
            t = msg.get("market_ticker")
            if t in self._subbed:
                self._books[t] = Book(msg.get("yes_dollars_fp") or [], msg.get("no_dollars_fp") or [])
                self._gap_check()
                self._quote(t, force=True)
        elif kind == "orderbook_delta":
            t = msg.get("market_ticker")
            book = self._books.get(t)
            if book is not None:
                book.apply(msg.get("side"), msg.get("price_dollars"), msg.get("delta_fp"))
                self._quote(t)
        elif kind == "user_order":
            self._emit({"type": "order", "order": normalize_order(msg), "ts_ms": self._clock()})
        elif kind == "fill":
            self._emit(fill_event(msg))
        elif kind == "trade":
            if msg.get("market_ticker") in self._tr_live:
                self._emit(trade_event(msg))
        elif kind == "ok":
            added = self._tr_adds.pop(d.get("id"), None)
            if added:
                self._trade_feed(self._tr_live | (added & self._tr_subbed))
        elif kind == "error":
            log.warning("kalshi %s stream error %s: %s", self.env, msg.get("code"), msg.get("msg"))
            if d.get("id") is not None and d.get("id") == self._ob_pending:
                self._ob_pending = None  # retried on the next sync
                self._subbed = set()
            if d.get("id") is not None and d.get("id") == self._tr_pending:
                self._tr_pending = None  # retried on the next sync
                self._tr_subbed = set()
            self._tr_adds.pop(d.get("id"), None)
        elif kind == "unsubscribed":
            if sid is not None and self._sids.get("orderbook_delta") == sid:
                self._sids.pop("orderbook_delta", None)
            if sid is not None and self._sids.get("trade") == sid:
                self._sids.pop("trade", None)
        self._health_changed()

    async def _on_gap(self, sid: int, last: int, seq: int) -> None:
        log.warning("kalshi %s stream: seq gap on sid %s (%s -> %s)", self.env, sid, last, seq)
        if sid == self._sids.get("orderbook_delta"):
            self._gap = True
            self._books, self._tops = {}, {}
            await self._resnapshot()
        elif sid == self._sids.get("trade"):
            self._emit({"type": "trade_gap", "ts_ms": self._clock()})  # prints cannot be replayed: say so
        else:
            self._emit({"type": "resync", "reason": "gap"})

    # -- connection ---------------------------------------------------------------------------------
    async def _session(self) -> None:
        headers = self._auth.headers("GET", WS_PATH)
        self._ws = await self._connect(self.url, headers)
        self.connects += 1
        reader = None
        try:
            for ch in self._private:
                await self._send("subscribe", {"channels": [ch]})
            if not self._private:  # market data only: up once the socket is open
                self._connected = True
                self._emit({"type": "resync", "reason": "connected"})
                self._health_changed()
            await self._sync_markets()
            await self._sync_trades()
            reader = asyncio.ensure_future(self._ws.recv())
            next_requote = self._clock() + self._requote_ms
            while not self._closed:
                waker = asyncio.ensure_future(self._wake.wait())
                timeout = max(0.0, (next_requote - self._clock()) / 1000)
                done, _ = await asyncio.wait({reader, waker}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if waker not in done:
                    waker.cancel()
                if reader in done:
                    raw = reader.result()  # raises ConnectionClosed -> reconnect
                    await self.handle(json.loads(raw))
                    reader = asyncio.ensure_future(self._ws.recv())
                if self._wake.is_set():
                    self._wake.clear()
                    await self._sync_markets()
                    await self._sync_trades()
                if self._clock() >= next_requote:
                    next_requote = self._clock() + self._requote_ms
                    if self._connected:
                        for t in list(self._books):
                            self._quote(t, force=True)
                    self._health_changed()  # silence turns the stream stale without any message
        finally:
            if reader is not None:
                if reader.done() and not reader.cancelled():
                    reader.exception()  # retrieved: the close below is the expected end
                else:
                    reader.cancel()
            ws, self._connected = self._ws, False
            self._reset()
            self._health_changed()
            with contextlib.suppress(Exception):
                await ws.close()

    async def run(self) -> None:
        backoff = Backoff(self._backoff0, self._backoff_max)  # full jitter: streams never reconnect in lockstep
        while not self._closed:
            started = self._clock()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never log headers or key material
                log.warning("kalshi %s stream dropped (%s); reconnecting within %.1fs", self.env,
                            type(exc).__name__, backoff.ceiling)
            if self._closed:
                break
            if self._clock() - started > 60_000:
                backoff.reset()  # it ran a while: start the backoff over
            await self._sleep(backoff.next())


def market_stream_from_settings(key_id: str | None, key_path: str | Path | None, on_event: Emit, *,
                                clock_ms: Callable[[], int], trades: bool = False) -> KalshiStream | None:
    """Production order-book stream for live PRICES only (no private channels; the key signs only the handshake).
    `trades=True` adds the public `trade` channel (the shadow lab's fill model)."""
    if not key_id or not key_path:
        return None
    try:
        key = load_private_key(key_path)
    except Exception as exc:
        log.warning("market-data stream key could not be loaded (%s); order book polling only", type(exc).__name__)
        return None
    return KalshiStream("prod", WsOnlyAuth(key_id, key), on_event, private=False, trades=trades,
                        clock_ms=clock_ms)


def stream_from_settings(env: str, key_id: str | None, key_path: str | Path | None, on_event: Emit, *,
                         clock_ms: Callable[[], int]) -> KalshiStream | None:
    """The stream for `env` (demo|prod), or None unless that environment's own trading key is configured."""
    if not key_id or not key_path:
        return None
    try:
        key = load_private_key(key_path)
    except Exception as exc:  # never log the path contents
        log.warning("%s stream key could not be loaded (%s); polling only", env, type(exc).__name__)
        return None
    return KalshiStream(env, TradingAuth(key_id, key), on_event, clock_ms=clock_ms)  # signs like the REST client
