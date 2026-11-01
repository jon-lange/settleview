"""Store reads the event loop no longer waits for (docs/plans/2026-10-31-1800-review-fix-plan.md P0 #5, B4#3): the BTC
price a minute ago behind the crowd line, the decision loop's data-quality flags and the Polymarket gateway's position
notes run in worker threads when called on the loop."""

from __future__ import annotations

import asyncio
import threading

import pytest


@pytest.fixture
def rt(tmp_path, monkeypatch):
    from btc_signal_engine.bootstrap.app import Runtime
    from btc_signal_engine.bootstrap.config import Settings

    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_POLYMARKET_KEY_ID", "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "offloop.db", trading_stream=False))
    yield r
    r.writer.close()


def _threads(rt, monkeypatch) -> list[str]:
    seen: list[str] = []
    select = rt.store.select

    def spy(table, *a, **kw):
        seen.append(threading.current_thread().name)
        return select(table, *a, **kw)

    monkeypatch.setattr(rt.store, "select", spy)
    return seen


def test_the_minute_ago_price_is_read_off_the_loop_and_reused_for_a_second(rt, monkeypatch):
    now = 1_761_900_000_000
    rt.store.insert("index_1s", [{"ts": now - 61_000, "price": 80_000.0, "last_trade_ts": now - 61_000}], ignore_conflicts=False)
    seen = _threads(rt, monkeypatch)
    main = threading.current_thread().name

    async def scenario():
        first = rt._btc_minute_ago(now)  # nothing cached yet: a read starts in a worker thread
        for _ in range(200):
            await asyncio.sleep(0.005)
            if rt._btc_ref[0] == now:
                break
        return first, rt._btc_minute_ago(now + 500), rt._btc_minute_ago(now + 900)

    first, second, third = asyncio.run(scenario())
    assert first is None and second == third == 80_000.0
    assert len(seen) == 1 and seen[0] != main  # one read, in a worker thread
    assert rt._btc_minute_ago(now + 10_000) is None  # off the loop (a web worker thread) it reads directly
    assert len(seen) == 2 and seen[-1] == main


def test_the_decision_loop_flags_are_computed_off_the_loop(rt, monkeypatch):
    seen = _threads(rt, monkeypatch)
    main = threading.current_thread().name

    async def scenario():
        rt._flags_at = (60_000, await asyncio.to_thread(rt._compute_flags, 60_000))
        return rt.flags(60_000)

    assert asyncio.run(scenario()) == frozenset()
    assert seen and all(t != main for t in seen)


def test_a_position_note_on_the_loop_is_recorded_from_a_worker_thread(rt, monkeypatch):
    seen = _threads(rt, monkeypatch)
    main = threading.current_thread().name
    row = {"ts": 5, "env": "pm_prod", "slug": "s", "code": "position_mismatch", "reason": "r", "detail": {}}

    async def scenario():
        rt._pm_position_note(row)  # returns at once; the lookup and the write run in a worker thread
        for _ in range(400):
            await asyncio.sleep(0.005)
            if seen:
                break
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert seen and seen[0] != main
    audit = [r for r in rt.store.select("trading_audit") if r["event"] == "position_check"]
    assert [r["code"] for r in audit] == ["position_mismatch"]
