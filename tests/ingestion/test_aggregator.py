import asyncio

from btc_signal_engine.adapters.coinbase.aggregator import BookMetrics, SecondAggregator
from btc_signal_engine.adapters.coinbase.collector import CoinbaseCollector
from btc_signal_engine.adapters.coinbase.gaps import GapRecorder
from btc_signal_engine.adapters.coinbase.stream import CoinbaseStream
from btc_signal_engine.domain.models.events import BookSecond, IndexSecond, Trade

T0 = 1_793_188_800_000  # whole second


def tr(i, ts, price):
    return Trade(trade_id=i, ts=ts, ingest_ts=ts + 50, price=price, size=0.1, buy=True)


def bm(ts, bid=100.0, ask=102.0, bid_size=0.5, ask_size=0.25):
    return BookMetrics(ts=ts, bid=bid, ask=ask, bid_depth_10=1.0, ask_depth_10=2.0, bid_depth_25=3.0, ask_depth_25=4.0,
                       bid_size=bid_size, ask_size=ask_size)


def test_duplicate_and_out_of_order_trades_stored_once_and_price_never_moves_back():
    agg = SecondAggregator(lateness_ms=1_000)
    assert agg.add_trade(tr(5, T0 + 800, 101.0)) is True
    assert agg.add_trade(tr(5, T0 + 800, 101.0)) is False   # duplicate
    assert agg.add_trade(tr(4, T0 + 200, 100.0)) is True    # late, lower id: new, but older
    assert agg.add_trade(tr(4, T0 + 200, 100.0)) is False
    rows = agg.flush(now=T0 + 2_000)
    index = [r for r in rows if isinstance(r, IndexSecond)]
    assert [(r.ts, r.price, r.last_trade_ts) for r in index] == [(T0, 101.0, T0 + 800)]


def test_second_with_no_trade_carries_price_and_mid_is_diagnostic_only():
    agg = SecondAggregator(lateness_ms=1_000)
    agg.add_trade(tr(1, T0 + 300, 100.5))
    agg.add_book(bm(T0 + 1_400, bid=200.0, ask=202.0))  # book for the quiet second
    rows = agg.flush(now=T0 + 3_000)
    index = [r for r in rows if isinstance(r, IndexSecond)]
    assert [(r.ts - T0, r.price, r.last_trade_ts - T0, r.mid) for r in index] == [
        (0, 100.5, 300, None), (1_000, 100.5, 300, 201.0)]


def test_book_second_uses_last_book_in_second_with_emission_ingest_ts():
    agg = SecondAggregator(lateness_ms=1_000)
    agg.add_book(bm(T0 + 100, bid=99.0, ask=101.0))
    agg.add_book(bm(T0 + 900, bid=100.0, ask=102.0))
    assert agg.flush(now=T0 + 1_999) == []           # still inside the lateness budget
    rows = agg.flush(now=T0 + 2_100)
    assert rows == [BookSecond(ts=T0, ingest_ts=T0 + 2_100, bid=100.0, ask=102.0,
                               bid_depth_10=1.0, ask_depth_10=2.0, bid_depth_25=3.0, ask_depth_25=4.0,
                               bid_size=0.5, ask_size=0.25)]
    assert agg.flush(now=T0 + 2_200) == []           # each second emitted once


def test_book_is_carried_into_quiet_seconds_but_not_once_stale():
    agg = SecondAggregator(lateness_ms=0, book_stale_ms=5_000)
    agg.add_book(bm(T0 + 900))
    books = [r for r in agg.flush(now=T0 + 10_000) if isinstance(r, BookSecond)]
    # book seen at +0.9s still counts for seconds ending within 5s of it: 0..4
    assert [b.ts - T0 for b in books] == [0, 1_000, 2_000, 3_000, 4_000]


class FakeClock:
    def __init__(self, t):
        self.t = t

    def now_ms(self):
        return self.t


def test_collector_writes_trades_book_and_index_rows_with_ingest_ts(store):
    clock = FakeClock(T0)
    end = T0 + 4_000
    updates = [
        tr(100, T0 + 100, 100.0), tr(101, T0 + 700, 101.0), tr(101, T0 + 700, 101.0),
        bm(T0 + 800, bid=100.5, ask=101.5),
        tr(102, T0 + 2_300, 103.0),
    ]

    async def transport(put):
        for u in updates:  # the wall clock moves with the feed
            clock.t = (u.ingest_ts if isinstance(u, Trade) else u.ts)
            put(u)
            await asyncio.sleep(0.02)
        clock.t = end
        put(None)  # heartbeat
        done.set()
        await asyncio.sleep(3600)

    stream = CoinbaseStream(clock, SecondAggregator(lateness_ms=1_000), GapRecorder(store),
                            transport=transport, poll_s=0.01)
    forwarded = []
    done = None

    async def on_event(ev):
        forwarded.append(ev)

    async def main():
        nonlocal done
        done = asyncio.Event()
        task = asyncio.create_task(CoinbaseCollector(stream, store, on_event).run())
        await asyncio.wait_for(done.wait(), 2)
        await asyncio.sleep(0.05)  # let the last poll flush
        stream.close()
        await asyncio.wait_for(task, 2)

    asyncio.run(main())

    assert [t.trade_id for t in store.trades_range(T0, end)] == [100, 101, 102]
    assert [(r.ts - T0, r.price, r.mid) for r in store.index_range(T0, end)] == [
        (0, 101.0, 101.0), (1_000, 101.0, 101.0), (2_000, 103.0, 101.0)]
    books = store.book_range(T0, end)
    assert [(b.ts - T0, b.ingest_ts, b.bid, b.ask, b.ask_depth_25) for b in books] == [
        (0, T0 + 2_350, 100.5, 101.5, 4.0), (1_000, end, 100.5, 101.5, 4.0), (2_000, end, 100.5, 101.5, 4.0)]
    assert {(b.bid_size, b.ask_size) for b in books} == {(0.5, 0.25)}  # best-level sizes round-trip the store
    # the engine sees trades and book seconds only, never index rows
    assert {type(e) for e in forwarded} == {Trade, BookSecond}
    assert len([e for e in forwarded if isinstance(e, Trade)]) == 3


def test_book_second_without_sizes_still_constructs_and_round_trips_as_null(store):
    b = BookSecond(ts=T0, ingest_ts=T0 + 1_000, bid=1.0, ask=2.0, bid_depth_10=0.0, ask_depth_10=0.0,
                   bid_depth_25=0.0, ask_depth_25=0.0)
    assert (b.bid_size, b.ask_size) == (None, None)
    store.add_book_seconds([b])
    assert store.book_range(T0, T0 + 1_000) == [b]
