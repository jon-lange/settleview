"""A refused private subscription is not a healthy feed (review fix plan 2026-10-31, B3#7): the private stream reads
down with the reason, polls fast meanwhile (the service's cadence follows `stale`), reconnects, and comes back up only
once a session has held every subscription. Fakes only; no network."""

from __future__ import annotations

import asyncio

import pytest
from polymarket_us.errors import WebSocketError

from btc_signal_engine.adapters.polymarket_us.market_data import PolymarketMarketData
from btc_signal_engine.adapters.polymarket_us.stream import (UP_AFTER_MS, PolymarketTradingStream,
                                                             PrivateSubscriptionRefused)
from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
from tests.polymarket_us.fakes import SLUG, Clock, FakeWS, client, lite


def run(coro):
    return asyncio.run(coro)


def cache(clock) -> PolymarketMarketData:
    c = PolymarketMarketData(clock_ms=clock)
    c.set_connected(True)
    c.on_message(lite(SLUG, 0.69, 0.70))
    return c


class RefusingWS(FakeWS):
    """Answers the subscribe naming `rid` with the server's error message, as the SDK emits it (a WebSocketError
    carrying our request id)."""

    def __init__(self, rid: str) -> None:
        super().__init__()
        self.rid = rid

    async def subscribe(self, rid, typ, slugs=None):
        await super().subscribe(rid, typ, slugs)
        if rid == self.rid:
            self.emit("error", WebSocketError("subscription not permitted for this account", rid,
                                              subscription_type=typ))


def stream(clock, md, factory, events):
    return PolymarketTradingStream(factory, PolymarketTrading(client(), quote_fn=md.quote), md, events.append,
                                   clock_ms=clock)


def test_a_refusal_that_arrives_after_subscribing_takes_the_feed_down_with_its_reason():
    async def go():
        clock = Clock()
        md = cache(clock)
        events: list[dict] = []
        ws = FakeWS()
        st = stream(clock, md, lambda: ws, events)
        n = {"i": 0}

        async def sleep(_):
            n["i"] += 1
            if n["i"] == 1:
                assert st.health()["connected"] and not st.health()["stale"]  # up: the subscribes were sent
                # the server's answer to pmpriv0 arrives a moment later, on the live socket
                ws.emit("error", WebSocketError("subscription not permitted", "pmpriv0",
                                                subscription_type="SUBSCRIPTION_TYPE_ORDER"))
        st._sleep = sleep
        with pytest.raises(PrivateSubscriptionRefused):
            await st.run_once()
        h = st.health()
        assert not h["connected"] and h["stale"]
        assert h["reason"].startswith("private ORDER subscription refused: subscription not permitted")
        assert st.refusals == 1 and ws.closed
    run(go())


def test_a_server_message_naming_no_request_of_ours_keeps_the_feed_up():
    async def go():
        clock = Clock()
        md = cache(clock)
        ws = FakeWS()
        st = stream(clock, md, lambda: ws, [])
        n = {"i": 0}

        async def sleep(_):
            n["i"] += 1
            if n["i"] == 1:
                ws.emit("error", WebSocketError("market not found", "someone-else-7"))
            elif n["i"] == 3:
                assert st.health()["connected"] and not st.health()["stale"] and st.refusals == 0
                ws.emit("close")
        st._sleep = sleep
        with pytest.raises(ConnectionError) as e:
            await st.run_once()
        assert not isinstance(e.value, PrivateSubscriptionRefused)
    run(go())


def test_refused_at_subscribe_never_reads_connected_then_a_held_session_comes_back_up():
    async def go():
        clock = Clock()
        md = cache(clock)
        events: list[dict] = []
        sockets = [RefusingWS("pmpriv1"), FakeWS()]
        st = stream(clock, md, lambda: sockets.pop(0), events)
        seen: list[dict] = []

        async def sleep(_):
            clock.t += 500
            md.on_message(lite(SLUG, 0.69, 0.70))  # the market data stays live throughout
            seen.append(st.health())
            if len(seen) > 40:
                st.close()
        st._sleep = sleep
        await asyncio.wait_for(st.run(), 5)
        assert sum(e["type"] == "resync" for e in events) == 1  # the refused session never said "connected"
        down = [h for h in seen if h["stale"]]
        assert down and down[0]["reason"].startswith("private POSITION subscription refused")
        # the second socket held every subscription for UP_AFTER_MS: the refusal is history and the feed is up
        assert seen[-1]["connected"] and not seen[-1]["stale"]
        first_up = next(i for i, h in enumerate(seen) if not h["stale"])
        assert first_up * 500 >= UP_AFTER_MS
    run(go())
