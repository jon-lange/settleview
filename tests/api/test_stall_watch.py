"""The stall detector logs the loop thread's stack while the event loop is blocked, and the stall's length after."""

from __future__ import annotations

import asyncio
import logging
import time

from btc_signal_engine.bootstrap.stall import StallWatch


def test_a_blocked_loop_is_reported_with_the_blocking_code(caplog):
    async def go():
        w = StallWatch(asyncio.get_running_loop(), threshold_s=0.1, interval_s=0.02)
        w.start()
        await asyncio.sleep(0.05)

        def busy_blocking_function():
            time.sleep(0.4)  # synchronous work on the loop thread
        busy_blocking_function()
        await asyncio.sleep(0.1)
        w.stop()
    with caplog.at_level(logging.WARNING, logger="btc_signal_engine.bootstrap.stall"):
        asyncio.run(go())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "event loop stalled" in text and "busy_blocking_function" in text and "stall ended after" in text


def test_a_responsive_loop_logs_nothing(caplog):
    async def go():
        w = StallWatch(asyncio.get_running_loop(), threshold_s=0.2, interval_s=0.02)
        w.start()
        await asyncio.sleep(0.3)
        w.stop()
    with caplog.at_level(logging.WARNING, logger="btc_signal_engine.bootstrap.stall"):
        asyncio.run(go())
    assert not caplog.records


def test_a_stall_inside_a_task_names_the_task(caplog):
    async def go():
        w = StallWatch(asyncio.get_running_loop(), threshold_s=0.1, interval_s=0.02)
        w.start()
        await asyncio.sleep(0.05)

        async def blocker():
            time.sleep(0.4)  # synchronous work inside a named task
        await asyncio.create_task(blocker(), name="feed:coinbase-test")
        await asyncio.sleep(0.1)
        w.stop()
    with caplog.at_level(logging.WARNING, logger="btc_signal_engine.bootstrap.stall"):
        asyncio.run(go())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "event loop stalled" in text and "task feed:coinbase-test" in text


def test_the_rolling_count_keeps_stalls_of_the_last_hour_only():
    now = [1_000.0]  # the watch's clock, seconds; only the hour window reads it

    async def go():
        w = StallWatch(asyncio.get_running_loop(), threshold_s=0.1, interval_s=0.02, clock=lambda: now[0])
        w.start()
        await asyncio.sleep(0.05)
        time.sleep(0.3)  # one stall
        await asyncio.sleep(0.1)
        w.stop()
        return w
    w = asyncio.run(go())
    assert w.stalls_last_hour() == 1
    now[0] += 3_599
    assert w.stalls_last_hour() == 1
    now[0] += 2  # the stall is now more than an hour old
    assert w.stalls_last_hour() == 0


def test_the_lag_histogram_counts_every_answer_and_keeps_lags_over_50_ms():
    async def go():
        w = StallWatch(asyncio.get_running_loop(), threshold_s=1.0, interval_s=0.01)
        w.start()
        await asyncio.sleep(0.15)
        time.sleep(0.12)  # one lag of ~120 ms: over 50 ms, under the 1 s stall threshold
        await asyncio.sleep(0.15)
        w.stop()
        await asyncio.sleep(0.05)
        return w.lag_view()
    v = asyncio.run(go())
    assert sum(v["buckets_ms"].values()) >= 5
    assert v["over_50ms_last_hour"] >= 1
    assert 100 <= v["max_ms_last_hour"] < 1000
    assert list(v["buckets_ms"]) == ["<10", "<25", "<50", "<100", "<250", "<500", ">=500"]
