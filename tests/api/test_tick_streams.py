"""Play's live stream (adapters/http/push.py TickStreams; review P1, B6#1-5): the first frame is the whole view, later
frames only the changed top-level fields, a changed key set is sent whole again, a failed read is an explicit `stale`
frame (never silence that leaves old numbers live), the cap answers 503, and close() ends every stream."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.play_routes import register_play_routes
from btc_signal_engine.adapters.http.push import TickStreams
from tests.api.test_push import parse, run, take


def test_full_then_changed_fields_then_stale_then_whole_again():
    views = iter([
        {"now": 1, "market": {"ask": 0.5}, "mode": "shadow"},
        {"now": 2, "market": {"ask": 0.5}, "mode": "shadow"},
        {"now": 3, "market": {"ask": 0.52}, "mode": "shadow"},
        RuntimeError("database away"),
        {"now": 5, "market": {"ask": 0.52}, "mode": "shadow"},
        {"now": 6, "market": None, "mode": "shadow", "reason": "closed"},
    ])

    def read():
        v = next(views)
        if isinstance(v, Exception):
            raise v
        return v

    async def go():
        hub = TickStreams(interval_s=0.01)
        frames = hub.stream(read)
        got = [parse(f) for f in await take(frames, 6)]
        await hub.close()
        rest = [f async for f in frames]
        return got, rest, hub.subscribers

    got, rest, subs = run(go())
    assert got[0] == ("view", {"seq": 1, "full": True, "view": {"now": 1, "market": {"ask": 0.5}, "mode": "shadow"}})
    assert got[1] == ("view", {"seq": 2, "full": False, "view": {"now": 2}})
    assert got[2] == ("view", {"seq": 3, "full": False, "view": {"now": 3, "market": {"ask": 0.52}}})
    assert got[3][0] == "stale" and got[3][1]["seq"] == 4 and "could not read" in got[3][1]["reason"]
    assert got[4] == ("view", {"seq": 5, "full": True, "view": {"now": 5, "market": {"ask": 0.52}, "mode": "shadow"}})
    assert got[5][1]["full"] is True and got[5][1]["view"]["reason"] == "closed"  # a new key: sent whole
    assert rest == [] and subs == 0


def test_the_route_streams_the_play_view_for_its_tab_and_amount_and_caps_streams():
    class Play:
        def now_view(self, now, venue, *, span, stake=None):
            return {"now": now, "venue": venue, "span": span}

    app = FastAPI()
    hub = TickStreams(interval_s=0.01, max_streams=1)
    register_play_routes(app, Play(), lambda: 7, streams=hub)

    async def go():
        frames = hub.stream(lambda: Play().now_view(7, "kalshi", span="15m", stake=5.0))
        first = parse((await take(frames, 1))[0])
        with TestClient(app) as client:
            busy = client.get("/api/play/stream", params={"venue": "kalshi", "span": "15m"})
            bad = client.get("/api/play/stream", params={"venue": "kalshi", "span": "2m"})
        await frames.aclose()
        return first, busy, bad

    first, busy, bad = run(go())
    assert first == ("view", {"seq": 1, "full": True, "view": {"now": 7, "venue": "kalshi", "span": "15m"}})
    assert busy.status_code == 503 and "Too many" in busy.json()["detail"]
    assert bad.status_code == 422  # the tab is required and checked, as on /api/play/now


def test_the_route_opens_a_stream_with_the_screens_parameters():
    seen: list = []

    class Play:
        def now_view(self, now, venue, *, span, stake=None):
            seen.append((venue, span, stake))
            return {"now": now}

    app = FastAPI()
    hub = TickStreams(interval_s=0.01)
    register_play_routes(app, Play(), lambda: 9, streams=hub)

    async def go():
        route = next(r for r in app.routes if getattr(r, "path", "") == "/api/play/stream")
        resp = route.endpoint(venue="polymarket", span="5m", stake=2.5)  # the route's own handler
        it = resp.body_iterator
        first = await asyncio.wait_for(it.__anext__(), 2)
        await it.aclose()
        return first

    first = run(go())
    ev, data = parse(first)
    assert ev == "view" and data["view"] == {"now": 9} and seen == [("polymarket", "5m", 2.5)]
    assert json.loads(json.dumps(data))["full"] is True
