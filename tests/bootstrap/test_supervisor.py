"""Loop supervision (KTD7): a loop that raises or returns is logged by name and restarted with backoff; a critical loop
that fails 5 times in 10 minutes turns on the kill switch; the process (and every other loop) keeps running."""

from __future__ import annotations

import asyncio
import logging

import pytest

from btc_signal_engine.bootstrap import supervisor as sup_mod
from btc_signal_engine.bootstrap.supervisor import Supervisor


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(sup_mod, "BASE_DELAY_S", 0.01)
    monkeypatch.setattr(sup_mod, "MAX_DELAY_S", 0.04)
    for name in ("btc_signal_engine", sup_mod.__name__):  # other tests may have detached them (dictConfig)
        lg = logging.getLogger(name)
        monkeypatch.setattr(lg, "disabled", False)
        monkeypatch.setattr(lg, "propagate", True)


async def _until(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.005)


def test_an_ordinary_loop_that_raises_is_restarted_and_the_others_keep_running(caplog):
    runs = {"bad": 0, "good": 0}

    async def bad():
        runs["bad"] += 1
        raise RuntimeError("boom")

    async def good():
        while True:
            runs["good"] += 1
            await asyncio.sleep(0.005)

    async def scenario():
        sup = Supervisor()
        sup.start("bad", bad)
        good_task = sup.start("good", good)
        await _until(lambda: runs["bad"] >= 3)
        before = runs["good"]
        await _until(lambda: runs["good"] > before + 2)
        assert not good_task.done()
        snap = sup.heartbeats()
        await sup.stop()
        return snap

    with caplog.at_level(logging.WARNING):
        snap = asyncio.run(scenario())
    assert snap["bad"]["restarts"] >= 2 and snap["good"]["restarts"] == 0
    assert any("bad" in r.getMessage() and r.exc_info for r in caplog.records)  # logged by name with the traceback


def test_backoff_doubles_up_to_the_cap(monkeypatch):
    monkeypatch.setattr(sup_mod, "BASE_DELAY_S", 1.0)
    monkeypatch.setattr(sup_mod, "MAX_DELAY_S", 30.0)
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        waits.append(s)
        await real_sleep(0)

    monkeypatch.setattr(sup_mod.asyncio, "sleep", fake_sleep)

    async def bad():
        raise RuntimeError("boom")

    async def scenario():
        sup = Supervisor()
        sup.start("bad", bad)
        while len(waits) < 7:
            await real_sleep(0)
        await sup.stop()

    asyncio.run(scenario())
    assert waits[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_a_loop_that_returns_is_treated_as_a_failure_and_restarted(caplog):
    runs = []

    async def quits():
        runs.append(1)

    async def scenario():
        sup = Supervisor()
        sup.start("quits", quits)
        await _until(lambda: len(runs) >= 3)
        await sup.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert any("quits" in r.getMessage() and "returned" in r.getMessage() for r in caplog.records)


def test_a_critical_loop_failing_5_times_in_10_minutes_trips_the_kill_switch_and_you_keeps_running(caplog):
    trips: list[tuple[str, int]] = []
    fails = {"n": 0}
    you_beats = {"n": 0}

    async def on_trip(name):
        trips.append((name, fails["n"]))

    async def writer():
        fails["n"] += 1
        raise RuntimeError("database is locked")

    async def you():
        while True:
            you_beats["n"] += 1
            await asyncio.sleep(0.005)

    async def scenario():
        sup = Supervisor(on_trip=on_trip)
        sup.start("writer", writer, critical=True)
        you_task = sup.start("you", you, critical=True)
        await _until(lambda: trips)
        after_trip = you_beats["n"]
        await _until(lambda: you_beats["n"] > after_trip + 2 and fails["n"] >= 7)  # both keep going
        assert not you_task.done()
        await sup.stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert trips[0] == ("writer", 5)  # tripped on the 5th failure
    assert len(trips) == fails["n"] // 5  # and again after 5 more, never on every failure
    assert any(r.levelno == logging.ERROR and "writer" in r.getMessage() and "kill switch" in r.getMessage()
               for r in caplog.records)


def test_an_ordinary_loop_never_trips_the_kill_switch():
    trips: list[str] = []
    fails = {"n": 0}

    async def on_trip(name):
        trips.append(name)

    async def periodic():
        fails["n"] += 1
        raise RuntimeError("x")

    async def scenario():
        sup = Supervisor(on_trip=on_trip)
        sup.start("periodic", periodic)
        await _until(lambda: fails["n"] >= 8)
        await sup.stop()

    asyncio.run(scenario())
    assert trips == []


def test_heartbeats_record_the_last_beat_per_loop():
    async def scenario():
        sup = Supervisor()

        async def loop():
            while True:
                sup.beat("ticker")
                await asyncio.sleep(0.005)

        sup.start("ticker", loop)
        await asyncio.sleep(0.02)
        first = sup.heartbeats()["ticker"]["last_beat"]
        await asyncio.sleep(0.03)
        second = sup.heartbeats()["ticker"]["last_beat"]
        await sup.stop()
        return first, second

    first, second = asyncio.run(scenario())
    assert second > first


def test_stop_cancels_every_task_and_waits_for_it_to_finish():
    finished: list[str] = []

    async def slow_cleanup():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)  # cleanup that takes a moment
            finished.append("cleanup")

    async def scenario():
        sup = Supervisor()
        task = sup.start("slow", slow_cleanup)
        await asyncio.sleep(0.01)
        await sup.stop()
        assert task.done() and finished == ["cleanup"]

    asyncio.run(scenario())


# -- the app's wiring ---------------------------------------------------------------------------------------------

@pytest.fixture
def rt(tmp_path, monkeypatch):
    from btc_signal_engine.bootstrap.app import Runtime
    from btc_signal_engine.bootstrap.config import Settings

    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_ACCESS_PIN", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "sup.db", trading_stream=False))
    yield r
    r.writer.close()


def test_a_trip_turns_on_the_shared_kill_switch_for_every_phone_service(rt):
    assert not rt.you_trading.killed()
    asyncio.run(rt.trip_kill_switch("writer"))
    assert rt.you_trading.killed()  # read back from the shared persisted "killed" setting


def test_runtime_run_survives_a_failing_loop_and_closes_the_writer_only_after_every_task_finished(rt, monkeypatch):
    from btc_signal_engine.bootstrap import app as app_mod

    events: list[str] = []
    periodic_runs = {"n": 0}
    you_ticks = {"n": 0}

    class FakeCollector:
        async def run(self):
            await asyncio.Event().wait()

        def close(self):
            events.append("collector closed")

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        async def serve(self):
            # Stay up until periodic has failed and been restarted twice while `you` kept ticking, then stop the way
            # uvicorn does on Ctrl+C.
            while periodic_runs["n"] < 3 or you_ticks["n"] < 5:
                await asyncio.sleep(0.005)
            self.should_exit = True

    async def periodic():
        periodic_runs["n"] += 1
        raise RuntimeError("forced failure in periodic")

    async def you():
        try:
            while True:
                you_ticks["n"] += 1
                await asyncio.sleep(0.005)
        finally:
            await asyncio.sleep(0.01)  # a task that takes a moment to finish after its cancel
            events.append("you finished")

    real_close = rt.writer.close

    def close():
        events.append("writer closed")
        real_close()

    async def no_startup():
        return None

    class NoStallWatch:
        def __init__(self, loop):
            pass

        def start(self):
            pass

    monkeypatch.setattr(app_mod.CoinbaseCollector, "live", classmethod(lambda cls, *a, **k: FakeCollector()))
    monkeypatch.setattr(app_mod.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(app_mod, "StallWatch", NoStallWatch)
    monkeypatch.setattr(rt, "startup", no_startup)
    monkeypatch.setattr(rt, "periodic", periodic)
    monkeypatch.setattr(rt, "loops", lambda: {"writer": rt.writer.run, "periodic": rt.periodic, "you": you})
    monkeypatch.setattr(rt.writer, "close", close)

    asyncio.run(asyncio.wait_for(rt.run(), timeout=10))
    assert periodic_runs["n"] >= 3
    assert events.index("you finished") < events.index("writer closed")


def test_heartbeats_survive_a_loop_starting_while_the_view_is_built():
    """heartbeats() is read from FastAPI's threadpool while the loop thread starts or forgets loops; a loop added
    mid-read must not raise 'dictionary changed size during iteration'."""
    sup = Supervisor()
    sup._critical.update(a=False, b=False)

    class Racing(dict):
        def get(self, key, default=None):
            sup._critical.setdefault("late", False)  # the loop thread starts a loop meanwhile
            return super().get(key, default)

    sup._beats = Racing()
    snap = sup.heartbeats()
    assert {"a", "b"} <= set(snap)
