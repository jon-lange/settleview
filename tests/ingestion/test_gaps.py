import asyncio

from btc_signal_engine.adapters.coinbase.aggregator import SecondAggregator
from btc_signal_engine.adapters.coinbase.gaps import GapRecorder
from btc_signal_engine.adapters.coinbase.stream import CoinbaseStream
from btc_signal_engine.domain.models.events import Trade

T0 = 1_793_188_800_000


def tr(i, ts, ingest_delay=50):
    return Trade(trade_id=i, ts=ts, ingest_ts=ts + ingest_delay, price=100.0, size=0.1, buy=True)


def test_trade_id_jump_records_one_gap_covering_missing_ids(store):
    gaps = GapRecorder(store)
    for i, ts in [(100, T0), (101, T0 + 100), (105, T0 + 900)]:
        gaps.on_trade(tr(i, ts))
    rows = store.select("gaps")
    assert len(rows) == 1
    g = rows[0]
    assert (g["cause"], g["from_trade_id"], g["to_trade_id"]) == ("trade_id_jump", 102, 104)
    assert (g["start_ts"], g["end_ts"], g["repaired"]) == (T0 + 100, T0 + 900, False)


def test_gap_against_last_stored_trade_id_on_start(store):
    gaps = GapRecorder(store, last_trade_id=90)
    gaps.on_trade(tr(100, T0))
    assert [(g["from_trade_id"], g["to_trade_id"]) for g in store.select("gaps")] == [(91, 99)]


def test_silence_over_15s_requests_reconnect_once_and_records_stale_gap(store):
    gaps = GapRecorder(store)
    gaps.on_message(T0)
    assert gaps.check_silence(T0 + 15_000) is False
    assert gaps.check_silence(T0 + 20_000) is True
    assert gaps.check_silence(T0 + 21_000) is False  # same silence, one gap
    [g] = store.select("gaps")
    assert (g["cause"], g["start_ts"], g["end_ts"], g["repaired"]) == ("stale", T0, T0 + 20_000, False)


def test_clock_skew_median_over_60s_written_to_data_quality(store):
    gaps = GapRecorder(store)
    delays = [100, 300, 200, 5_000, 250]
    for n, d in enumerate(delays):
        gaps.on_trade(tr(n + 1, T0 + n * 10_000, ingest_delay=d))
    assert store.select("data_quality") == []
    gaps.on_trade(tr(6, T0 + 60_000, ingest_delay=400))  # closes the 60s window
    [row] = store.select("data_quality")
    assert (row["kind"], row["value"], row["detail"]) == ("skew", 250.0, {"samples": 5})


class StepClock:
    """Each read advances time by `step` ms, so polls simulate silence quickly."""

    def __init__(self, t, step):
        self.t, self.step = t, step

    def now_ms(self):
        self.t += self.step
        return self.t


def test_stream_reconnects_after_20s_of_silence_and_records_stale_gap(store):
    connects = []

    async def silent_transport(put):
        connects.append(1)
        await asyncio.sleep(3600)

    stream = CoinbaseStream(StepClock(T0, 5_000), SecondAggregator(), GapRecorder(store),
                            transport=silent_transport, poll_s=0.001, backoff=(0.0, 0.0))

    async def main():
        async for _ in stream.events():
            pass

    async def run():
        task = asyncio.create_task(main())
        while len(connects) < 2:
            await asyncio.sleep(0.005)
        stream.close()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
    stale = [g for g in store.select("gaps") if g["cause"] == "stale"]
    assert len(stale) >= 1
    assert stale[0]["end_ts"] - stale[0]["start_ts"] > 15_000
