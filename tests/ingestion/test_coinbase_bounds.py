"""The Coinbase feed's transport -> consumer queue is bounded (review fix plan 2026-10-31, B3#11): a slow consumer drops
the OLDEST updates (counted, logged, metered), the queue never grows past its bound, and a dropped trade still shows
up as a recorded trade-id gap for the repair job. No network."""

from __future__ import annotations

import asyncio
import logging

from btc_signal_engine.adapters import telemetry
from btc_signal_engine.adapters.coinbase import stream as cb
from btc_signal_engine.adapters.coinbase.aggregator import SecondAggregator
from btc_signal_engine.adapters.coinbase.gaps import GapRecorder
from btc_signal_engine.adapters.coinbase.stream import CoinbaseStream
from btc_signal_engine.domain.models.events import Trade

T0 = 1_793_188_800_000


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def now_ms(self) -> int:
        return self.t


def trade(i: int) -> Trade:
    return Trade(trade_id=i, ts=T0 + i, ingest_ts=T0 + i, price=100.0, size=0.1, buy=True)


def test_the_put_never_grows_the_queue_past_its_bound_and_drops_the_oldest(store, monkeypatch, caplog):
    metered: list[tuple] = []
    monkeypatch.setattr(telemetry, "dropped", lambda component, buffer, n=1: metered.append((component, buffer, n)))
    stream = CoinbaseStream(Clock(), SecondAggregator(), GapRecorder(store), transport=lambda put: None, queue_max=3)
    q: asyncio.Queue = asyncio.Queue(maxsize=3)
    put = stream._putter(q)
    with caplog.at_level(logging.WARNING):
        for i in range(10):
            put(i)
            assert q.qsize() <= 3
    assert [q.get_nowait() for _ in range(3)] == [7, 8, 9]
    assert stream.dropped == 7 and metered == [("coinbase_stream", "updates", 1)] * 7
    assert sum("dropping the oldest update" in r.getMessage() for r in caplog.records) == 1  # the first, then every 1000th


def test_a_burst_behind_the_consumer_keeps_the_newest_trades(store):
    async def transport(put):
        for i in range(1, 51):  # 50 trades in one burst, before the consumer reads anything
            put(trade(i))
        await asyncio.sleep(3600)

    stream = CoinbaseStream(Clock(), SecondAggregator(), GapRecorder(store), transport=transport, poll_s=0.01,
                            queue_max=10)
    seen: list[int] = []

    async def main():
        agen = stream.events()
        while len(seen) < 10:
            ev = await asyncio.wait_for(agen.__anext__(), 2)
            if isinstance(ev, Trade):
                seen.append(ev.trade_id)
        stream.close()
        await agen.aclose()

    asyncio.run(main())
    assert seen == list(range(41, 51)) and stream.dropped == 40


def test_a_dropped_trade_between_two_kept_ones_is_a_recorded_gap(store):
    gaps = GapRecorder(store, last_trade_id=40)  # what the feed had before a burst dropped 41-45
    gaps.on_trade(trade(46))
    [g] = store.select("gaps")
    assert (g["cause"], g["from_trade_id"], g["to_trade_id"], g["repaired"]) == ("trade_id_jump", 41, 45, False)


def test_the_raw_feed_caps_frame_size():
    assert cb.MAX_FRAME_BYTES == 16 * 1024 * 1024 and cb.QUEUE_MAX > 0
