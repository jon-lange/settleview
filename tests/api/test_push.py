"""The server-sent-events push channel (adapters/http/push.py, KTD11): the first frame is the full view, changes are
coalesced to at most one frame per interval carrying the newest values, a quiet stream sends a ping, and closing the
hub ends every stream."""

from __future__ import annotations

import asyncio
import json

from btc_signal_engine.adapters.http.push import PushHub


def run(coro):
    return asyncio.run(coro)


def parse(frame: str) -> tuple[str, object]:
    lines = dict(line.split(": ", 1) for line in frame.strip().split("\n"))
    return lines["event"], json.loads(lines["data"])


async def take(it, n: int, timeout: float = 2.0) -> list[str]:
    return [await asyncio.wait_for(it.__anext__(), timeout) for _ in range(n)]


def test_first_frame_is_the_full_view_then_changes_coalesce_to_the_newest_values():
    async def go():
        hub = PushHub(min_interval_s=0.1, heartbeat_s=15.0)
        frames = hub.stream(lambda: {"quote": {"bid": 7.0}, "mode": "perp_prod"})
        first = await take(frames, 1)
        for i in range(50):  # 50 quote changes well inside one 100 ms interval
            hub.publish({"quote": {"bid": 7.0 + i / 10_000}})
        hub.publish({"killed": True})
        t0 = asyncio.get_running_loop().time()
        second = await take(frames, 1)
        waited = asyncio.get_running_loop().time() - t0
        # nothing else is pending: the next frame would only be a ping after the 15 s heartbeat
        with_timeout = asyncio.wait_for(frames.__anext__(), 0.3)
        try:
            await with_timeout
            extra = True
        except TimeoutError:
            extra = False
        await frames.aclose()
        await hub.close()
        return first, second, waited, extra

    first, second, waited, extra = run(go())
    assert parse(first[0]) == ("view", {"quote": {"bid": 7.0}, "mode": "perp_prod"})
    assert parse(second[0]) == ("view", {"quote": {"bid": 7.0049}, "killed": True})
    assert waited < 0.25
    assert extra is False


def test_frames_are_spaced_by_the_interval():
    async def go():
        hub = PushHub(min_interval_s=0.1, heartbeat_s=15.0)
        frames = hub.stream(lambda: {})
        await take(frames, 1)
        loop = asyncio.get_running_loop()
        hub.publish({"a": 1})
        await take(frames, 1)
        t1 = loop.time()
        hub.publish({"a": 2})
        await take(frames, 1)
        gap = loop.time() - t1
        await frames.aclose()
        await hub.close()
        return gap

    assert run(go()) >= 0.09


def test_a_quiet_stream_sends_a_ping():
    async def go():
        hub = PushHub(min_interval_s=0.1, heartbeat_s=0.2)
        frames = hub.stream(lambda: {})
        await take(frames, 1)
        ping = await take(frames, 1)
        await frames.aclose()
        await hub.close()
        return ping[0]

    assert parse(run(go())) == ("ping", {})


def test_closing_the_hub_ends_every_stream_and_unsubscribes():
    async def go():
        hub = PushHub(min_interval_s=0.1, heartbeat_s=15.0)
        a, b = hub.stream(lambda: {}), hub.stream(lambda: {})
        await take(a, 1)
        await take(b, 1)
        assert hub.subscribers == 2
        await hub.close()
        rest_a = [f async for f in a]
        rest_b = [f async for f in b]
        return rest_a, rest_b, hub.subscribers

    assert run(go()) == ([], [], 0)
