"""Live Coinbase Exchange feed for BTC-USD (U3).

Two interchangeable transports share the same normalization helpers:

* raw ws-feed (default): `wss://ws-feed.exchange.coinbase.com`, channels `matches`,
  `level2_batch`, `heartbeat`; public, no auth.
* ccxt.pro `coinbaseexchange` (`use_ccxt=True`): `watchTrades` + `watchOrderBook`.
  ccxt 4.5.64 refuses `watchOrderBook` without an apiKey (its `level2` channel is
  authenticated), so we subscribe to the public `level2_batch` channel and skip its
  `authenticate()`; that leans on ccxt internals, which is why raw ws-feed is the default.

`CoinbaseStream.events()` yields deduped `Trade`s as they arrive and, once per completed
second, the aggregator's `BookSecond` and `IndexSecond` rows. It reconnects with backoff
(1.5s doubling to 30s, full jitter: adapters/retry.py) and after 15s of socket silence, recording gaps via
`GapRecorder`.

Bounds (review fix plan 2026-10-31, B3#11): the transport hands updates to the consumer through a queue of at most
`queue_max` (QUEUE_MAX) items. The consumer awaits store writes and the engine, so a slow consumer must not grow the
queue without limit: when it is full the OLDEST update is dropped (counted in `dropped`, logged at WARNING on the
first drop and every 1,000th, and the OpenTelemetry counter `settleview.venue.dropped` {component coinbase_stream,
buffer updates}). A dropped trade is a trade-id jump, so `GapRecorder` records the gap and the repair job fills it
from Coinbase's trade history; a dropped book update is superseded by the next one. The raw ws-feed accepts frames
up to MAX_FRAME_BYTES (16 MB: a full level2 snapshot of BTC-USD is a few MB).
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

from btc_signal_engine.adapters import telemetry
from btc_signal_engine.adapters.coinbase.aggregator import BookMetrics, SecondAggregator, book_metrics
from btc_signal_engine.adapters.coinbase.gaps import GapRecorder
from btc_signal_engine.adapters.retry import Backoff
from btc_signal_engine.application.ports import Clock
from btc_signal_engine.domain.models.events import BookSecond, IndexSecond, Trade
from btc_signal_engine.domain.timestamps import iso_to_ms

log = logging.getLogger(__name__)

WS_URL = "wss://ws-feed.exchange.coinbase.com"
PRODUCT = "BTC-USD"
SYMBOL = "BTC/USD"
QUEUE_MAX = 10_000  # transport -> consumer updates held at most (a few seconds of a busy book); oldest dropped
MAX_FRAME_BYTES = 16 * 1024 * 1024  # the largest ws-feed frame read (a level2 snapshot is a few MB)

Update = Trade | BookMetrics | None  # None = liveness only (heartbeat, subscription ack)
Transport = Callable[[Callable[[Update], None]], Awaitable[None]]



# -- normalization (shared by both transports) ------------------------------------------
def parse_time_ms(iso: str) -> int:
    """Coinbase ISO-8601 time ("2026-10-28T12:00:00.123456Z") to integer UTC ms (domain/timestamps.py)."""
    return iso_to_ms(iso)


def trade_from_match(msg: dict[str, Any], ingest_ts: int) -> Trade:
    """A raw `match` / `last_match` message to a Trade.

    Coinbase's `side` is the MAKER order's side, so the aggressor bought when side == "sell".
    """
    return Trade(
        trade_id=int(msg["trade_id"]), ts=parse_time_ms(msg["time"]), ingest_ts=ingest_ts,
        price=float(msg["price"]), size=float(msg["size"]), buy=msg["side"] == "sell",
    )


def trade_from_ccxt(trade: dict[str, Any], ingest_ts: int) -> Trade:
    """A ccxt.pro trade to a Trade, normalized from the raw message in `info`.

    ccxt's own `side` happens to be the taker side for public matches, but only through
    two inversions (base `parse_trade` keeps the maker side when maker/taker order ids are
    present, then `parse_ws_trade` flips it because there is no fee rate). Verified live
    2026-10-28: ccxt side "sell" with info.side "buy". We read `info` so a change in that
    chain cannot silently flip every aggressor.
    """
    return trade_from_match(trade["info"], ingest_ts)


class LocalBook:
    """Level-2 book rebuilt from `level2_batch` snapshot + l2update messages.

    Price lists are kept sorted so the top of book is read without scanning every level.
    """

    def __init__(self) -> None:
        self._size: dict[str, dict[float, float]] = {"buy": {}, "sell": {}}
        self._px: dict[str, list[float]] = {"buy": [], "sell": []}  # ascending

    def load(self, bids: list[list[str]], asks: list[list[str]]) -> None:
        for side, levels in (("buy", bids), ("sell", asks)):
            sizes = {float(p): float(s) for p, s in levels if float(s) > 0}
            self._size[side], self._px[side] = sizes, sorted(sizes)

    def apply(self, side: str, price: str, size: str) -> None:
        p, s = float(price), float(size)
        sizes, px = self._size[side], self._px[side]
        if s == 0:
            if sizes.pop(p, None) is not None:
                del px[bisect.bisect_left(px, p)]
        else:
            if p not in sizes:
                bisect.insort(px, p)
            sizes[p] = s

    def bids(self) -> Iterator[tuple[float, float]]:
        sizes = self._size["buy"]
        return ((p, sizes[p]) for p in reversed(self._px["buy"]))

    def asks(self) -> Iterator[tuple[float, float]]:
        sizes = self._size["sell"]
        return ((p, sizes[p]) for p in self._px["sell"])

    def ready(self) -> bool:
        return bool(self._px["buy"]) and bool(self._px["sell"])


# -- transports -----------------------------------------------------------------------
def raw_ws_transport(clock: Clock) -> Transport:
    async def run(put: Callable[[Update], None]) -> None:
        import websockets

        # compression=None: inflating large level2 frames on the loop thread stalled the app (KTD2).
        async with websockets.connect(WS_URL, max_size=MAX_FRAME_BYTES, ping_interval=20, compression=None) as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": [PRODUCT],
                                      "channels": ["matches", "level2_batch", "heartbeat"]}))
            book = LocalBook()
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                now = clock.now_ms()
                if kind in ("match", "last_match"):
                    put(trade_from_match(msg, now))
                elif kind == "snapshot":
                    book.load(msg["bids"], msg["asks"])
                    put(book_metrics(book.bids(), book.asks(), now) if book.ready() else None)
                elif kind == "l2update":
                    for side, price, size in msg["changes"]:
                        book.apply(side, price, size)
                    put(book_metrics(book.bids(), book.asks(), now) if book.ready() else None)
                elif kind == "error":
                    raise ConnectionError(f"coinbase ws error: {msg.get('message')} {msg.get('reason')}")
                else:
                    put(None)

    return run


def ccxt_transport(clock: Clock) -> Transport:
    async def run(put: Callable[[Update], None]) -> None:
        import ccxt.pro as ccxtpro

        class _PublicCoinbase(ccxtpro.coinbaseexchange):
            def authenticate(self, params={}):  # noqa: B006 - ccxt signature
                return {}  # level2_batch is public; ccxt would otherwise demand an apiKey

        ex = _PublicCoinbase()

        async def trades() -> None:
            while True:
                for t in await ex.watch_trades(SYMBOL):
                    put(trade_from_ccxt(t, clock.now_ms()))

        async def book() -> None:
            while True:
                ob = await ex.watch_order_book(SYMBOL, params={"channels": ["level2_batch"]})
                if ob["bids"] and ob["asks"]:
                    put(book_metrics(ob["bids"], ob["asks"], clock.now_ms()))

        try:
            await asyncio.gather(trades(), book())
        finally:
            await ex.close()

    return run


# -- the feed -------------------------------------------------------------------------
class CoinbaseStream:
    """MarketFeed for Coinbase: yields Trade as they arrive, BookSecond/IndexSecond per second.

    `transport` overrides the network transport (tests); otherwise `use_ccxt` picks one.
    """

    def __init__(
        self,
        clock: Clock,
        aggregator: SecondAggregator,
        gaps: GapRecorder,
        *,
        use_ccxt: bool = False,
        transport: Transport | None = None,
        poll_s: float = 0.25,
        backoff: tuple[float, float] = (1.5, 30.0),
        queue_max: int = QUEUE_MAX,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._agg = aggregator
        self._gaps = gaps
        self._transport = transport or (ccxt_transport(clock) if use_ccxt else raw_ws_transport(clock))
        self._poll_s = poll_s
        self._backoff0, self._backoff_max = backoff
        self._queue_max = int(queue_max)
        self._sleep = sleep
        self.dropped = 0
        self._closed = False

    def _putter(self, queue: asyncio.Queue) -> Callable[[Update], None]:
        """The transport's `put`: never blocks and never grows the queue past its bound (drop the oldest)."""
        def put(update: Update) -> None:
            while True:
                try:
                    queue.put_nowait(update)
                    return
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:  # (cannot happen on one loop; never spin)
                        return
                    self.dropped += 1
                    telemetry.dropped("coinbase_stream", "updates")
                    if self.dropped == 1 or self.dropped % 1000 == 0:
                        log.warning("coinbase feed: the consumer is behind; queue full (%d), dropping the oldest "
                                    "update (%d dropped so far; trade gaps are recorded and repaired)",
                                    self._queue_max, self.dropped)
        return put

    def close(self) -> None:
        self._closed = True

    async def events(self) -> AsyncIterator[Trade | BookSecond | IndexSecond]:
        backoff = Backoff(self._backoff0, self._backoff_max)
        while not self._closed:
            queue: asyncio.Queue[Update] = asyncio.Queue(maxsize=self._queue_max)
            task = asyncio.create_task(self._transport(self._putter(queue)))
            self._gaps.on_message(self._clock.now_ms())  # silence is measured from connect
            try:
                while not self._closed and not (task.done() and queue.empty()):
                    try:
                        update = await asyncio.wait_for(queue.get(), self._poll_s)
                        got = True
                    except TimeoutError:
                        update, got = None, False
                    now = self._clock.now_ms()
                    if got:
                        self._gaps.on_message(now)
                        backoff.reset()
                        if isinstance(update, Trade):
                            if self._agg.add_trade(update):
                                self._gaps.on_trade(update)
                                yield update
                        elif isinstance(update, BookMetrics):
                            self._agg.add_book(update)
                    for row in self._agg.flush(now):
                        yield row
                    if self._gaps.check_silence(now):
                        log.warning("coinbase feed silent for >%dms; reconnecting", self._gaps.silence_ms)
                        break
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # transport failure -> reconnect below
                    log.warning("coinbase transport failed: %r", exc)
            if self._closed:
                break
            wait = backoff.next()
            log.info("coinbase reconnecting in %.1fs", wait)
            await self._sleep(wait)
