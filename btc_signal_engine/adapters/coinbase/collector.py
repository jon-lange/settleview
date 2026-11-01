"""Live collector: runs the Coinbase feed, persists everything, forwards events to the engine (U3).

Trades, `book_1s` and `index_1s` rows are written through the store (a `BufferedStore` in
production, so writes are batched by the single writer). Each Trade and BookSecond is then
passed to `on_event` (the engine queue, KTD4); index rows are stored only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from btc_signal_engine.adapters.coinbase.aggregator import SecondAggregator
from btc_signal_engine.adapters.coinbase.gaps import GapRecorder
from btc_signal_engine.adapters.coinbase.stream import CoinbaseStream
from btc_signal_engine.application.ports import Clock
from btc_signal_engine.domain.models.events import BookSecond, IndexSecond, MarketEvent, Trade

OnEvent = Callable[[MarketEvent], Awaitable[None]]


class CoinbaseCollector:
    def __init__(self, stream: CoinbaseStream, store, on_event: OnEvent | None = None) -> None:
        self._stream = stream
        self._store = store
        self._on_event = on_event

    @classmethod
    def live(cls, store, clock: Clock, on_event: OnEvent | None = None, *, use_ccxt: bool = False) -> CoinbaseCollector:
        """Wire the live feed, resuming the index carry and trade-ID sequence from the store."""
        now = clock.now_ms()
        aggregator = SecondAggregator(carry=store.last_index_before(now))
        gaps = GapRecorder(store, last_trade_id=store.max_trade_id())
        return cls(CoinbaseStream(clock, aggregator, gaps, use_ccxt=use_ccxt), store, on_event)

    def close(self) -> None:
        self._stream.close()

    async def run(self) -> None:
        async for ev in self._stream.events():
            if isinstance(ev, IndexSecond):
                self._store.add_index_seconds([ev])
                continue
            if isinstance(ev, Trade):
                self._store.add_trades([ev])
            elif isinstance(ev, BookSecond):
                self._store.add_book_seconds([ev])
            if self._on_event is not None:
                await self._on_event(ev)
