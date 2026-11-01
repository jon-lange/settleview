"""The markets WebSocket pool: no connection ever holds more subscriptions than the server allows (steady state and
during window rolls, 15m + 5m + a market a trading service holds), a refused subscription is noticed (market marked
unsubscribed, quote None, trades not vouched for, retried), freshness and backoff are per connection."""

from __future__ import annotations

import asyncio

from polymarket_us.errors import WebSocketError

from btc_signal_engine.adapters.polymarket_us.market_data import (CONN_LIVE_MS, MAX_SUBS_PER_CONN, PolymarketMarketData,
                                                                  PolymarketMarketStream)
from btc_signal_engine.adapters.polymarket_us.markets import current_slugs, slug_for, window_of
from tests.polymarket_us.fakes import NOW, W0, Clock, FakeWS, lite

HELD = slug_for(W0 - 900_000)  # a closed 15m window a trading service still holds (waiting for settlement)


class ServerWS(FakeWS):
    """A markets socket that enforces a per-connection subscription limit like the live server (which answers
    "max subscriptions per connection reached" naming the request)."""

    def __init__(self, limit: int = MAX_SUBS_PER_CONN, fail_connect: bool = False, rid_in_error: bool = True) -> None:
        super().__init__(fail_connect)
        self.limit, self.rid_in_error = limit, rid_in_error
        self.live: dict[str, tuple[str, str]] = {}  # request id -> (type, slug)
        self.peak = 0
        self.refusals: list[str] = []

    async def subscribe(self, rid, typ, slugs=None):
        await super().subscribe(rid, typ, slugs)
        if len(self.live) >= self.limit:
            self.refusals.append(rid)
            self.emit("error", WebSocketError("max subscriptions per connection reached",
                                              rid if self.rid_in_error else None))
            return
        self.live[rid] = (typ, slugs[0])
        self.peak = max(self.peak, len(self.live))

    async def unsubscribe(self, rid):
        await super().unsubscribe(rid)
        self.live.pop(rid, None)

    def slugs(self) -> set[str]:
        return {s for _, s in self.live.values()}

    def feed(self, only=None) -> None:
        for s in sorted(self.slugs()):
            if only is not None and not only(s):
                continue
            self.emit("message", lite(s, 0.50, 0.51))


class Rig:
    def __init__(self, limit: int = MAX_SUBS_PER_CONN, rid_in_error: bool = True, **kw) -> None:
        self.clock = Clock(NOW)
        self.cache = PolymarketMarketData(clock_ms=self.clock)
        self.sockets: list[ServerWS] = []
        self.fail_next = 0
        self.waits: list[float] = []
        self.feeding = True
        self.only = None  # which markets send data (None: all)

        def factory():
            ws = ServerWS(limit, fail_connect=self.fail_next > 0, rid_in_error=rid_in_error)
            self.fail_next = max(0, self.fail_next - 1)
            self.sockets.append(ws)
            return ws

        async def sleep(s):
            if s >= 1:
                self.waits.append(s)
            await asyncio.sleep(0)

        self.st = PolymarketMarketStream(factory, self.cache, clock_ms=self.clock, sleep=sleep, **kw)
        self.task: asyncio.Task | None = None

    def open_sockets(self) -> list[ServerWS]:
        return [ws for ws in self.sockets if ws.connected and not ws.closed]

    async def settle(self, n: int = 40) -> None:
        for _ in range(n):
            await asyncio.sleep(0)
            if self.feeding:
                for ws in self.open_sockets():
                    ws.feed(self.only)

    async def start(self) -> None:
        self.task = asyncio.create_task(self.st.run())
        await self.settle()

    async def stop(self) -> None:
        self.st.close()
        await self.task

    def live_everywhere(self) -> set[str]:
        out: set[str] = set()
        for ws in self.open_sockets():
            out |= ws.slugs()
        return out


def run(coro):
    return asyncio.run(coro)


def test_steady_state_and_every_roll_stay_under_the_limit_with_15m_5m_and_a_held_market():
    async def go():
        rig = Rig()
        rig.st.want("svc", {HELD})
        await rig.start()
        want = set(current_slugs(NOW)) | {HELD}
        assert len(want) == 5  # 15m current + next, 5m current + next, the held market
        assert rig.live_everywhere() == want
        assert all(len(ws.live) == 3 * len(ws.slugs()) for ws in rig.open_sockets())  # all three types each
        assert len(rig.open_sockets()) == 2 and rig.st.refused == 0
        assert all(rig.cache.quote(s) is not None for s in want)
        h = rig.cache.health()
        assert not h["stale"] and h["connected"] and h["unsubscribed"] == []
        # Every 5 minutes for an hour (15m and 5m roll together on the quarter hours): never over the limit,
        # never a refusal, the new windows live, the closed ones dropped.
        t = NOW
        for _ in range(12):
            t = t - t % 300_000 + 300_000 + 1
            rig.clock.t = t
            await rig.settle()
            assert rig.live_everywhere() == set(current_slugs(t)) | {HELD}
            assert rig.cache.health()["unsubscribed"] == []
        assert all(ws.peak <= MAX_SUBS_PER_CONN and not ws.refusals for ws in rig.sockets)
        assert rig.st.refused == 0 and rig.st.reconnects == 0
        rig.st.want("svc", set())  # the held market settles: dropped
        await rig.settle()
        assert HELD not in rig.live_everywhere()
        await rig.stop()
        assert all(ws.closed for ws in rig.sockets) and not rig.cache.connected
    run(go())


def test_old_single_socket_layout_would_have_been_refused():
    """The bug: 4 markets x 3 types on one socket = 12 > 10. A pool capped at one connection's worth shows it."""
    ws = ServerWS()

    async def go():
        for i, s in enumerate(current_slugs(NOW)):
            for typ in ("A", "B", "C"):
                await ws.subscribe(f"r{i}{typ}", typ, [s])
    run(go())
    assert len(ws.refusals) == 2  # exactly what the live log showed at 19:02:04


def test_refused_subscription_is_marked_unsubscribed_retried_and_the_cap_lowered():
    async def go():
        rig = Rig(limit=7)  # a server stricter than measured: the third market on a socket is cut off part-way
        rig.st.want("svc", {HELD})
        await rig.start()
        assert rig.st.refused >= 1 and rig.st.per_conn == 2
        refused = [ws for ws in rig.sockets if ws.refusals]
        assert refused
        # whatever was refused is not left looking live: every market the cache calls subscribed is fully live
        for slug, _cid in rig.cache._subscribed.items():
            on = [ws for ws in rig.open_sockets() if slug in ws.slugs()]
            assert on and all(sum(1 for _, s in ws.live.values() if s == slug) == 3 for ws in on)
        unsub = set(rig.cache.health()["unsubscribed"])
        for s in unsub:
            assert rig.cache.quote(s) is None and rig.cache.trades_since(s, NOW - 1) is None
        # retried after its backoff, on a socket with room: everything live, within the limit
        rig.clock.t += 2_000
        await rig.settle()
        rig.clock.t += 4_000
        await rig.settle()
        want = set(current_slugs(rig.clock.t)) | {HELD}
        assert rig.live_everywhere() == want and rig.cache.health()["unsubscribed"] == []
        assert all(rig.cache.quote(s) is not None for s in want)
        assert all(len(ws.live) <= 6 for ws in rig.open_sockets())  # 2 markets x 3 types under the lowered cap
        await rig.stop()
    run(go())


def test_refused_trade_subscription_means_trades_are_not_vouched_for():
    async def go():
        rig = Rig(limit=8, markets_per_conn=3)  # third market: book + top accepted, TRADE refused
        await rig.start()
        ws = rig.sockets[0]
        assert len(ws.refusals) == 1
        _, typ, (slug,) = next(x for x in ws.subs if x[0] == ws.refusals[0])
        assert typ == "SUBSCRIPTION_TYPE_TRADE"
        # its book and top were accepted, but the market is all-or-nothing: dropped there, not shown as live
        assert slug not in ws.slugs() and slug in rig.cache.health()["unsubscribed"]
        assert rig.cache.quote(slug) is None and rig.cache.trades_since(slug, NOW - 1) is None
        rig.clock.t += 1_500  # retried (on a socket with room)
        await rig.settle()
        assert slug in rig.live_everywhere() and rig.cache.quote(slug) is not None
        rig.clock.t += 10
        assert rig.cache.trades_since(slug, rig.clock.t - 5) == []
        await rig.stop()
    run(go())


def test_unattributed_max_subscriptions_error_rebuilds_the_socket_under_a_lower_cap():
    async def go():
        rig = Rig(limit=7, rid_in_error=False)
        await rig.start()
        rig.clock.t += 3_000
        await rig.settle(80)
        assert rig.st.per_conn == 2 and rig.st.reconnects >= 1
        assert rig.live_everywhere() == set(current_slugs(rig.clock.t))
        assert all(len(ws.live) <= 7 for ws in rig.open_sockets())
        await rig.stop()
    run(go())


def test_freshness_and_health_are_per_connection():
    async def go():
        rig = Rig()
        await rig.start()
        cur15, nxt15, cur5, nxt5 = current_slugs(NOW)
        b = next(ws for ws in rig.open_sockets() if cur15 in ws.slugs())  # carries open windows
        a = next(ws for ws in rig.open_sockets() if ws is not b)  # carries only the 5m next window
        on_a, on_b = sorted(a.slugs()), sorted(b.slugs())
        assert on_a == [nxt5]
        rig.feeding = False
        b.emit("close")  # the socket carrying the current windows dies
        await asyncio.sleep(0)
        assert all(rig.cache.quote(s) is None for s in on_b)  # its markets read stale at once
        assert all(rig.cache.quote(s) is not None for s in on_a)  # the other socket's are still fresh
        h = rig.cache.health()
        assert h["stale"] and not h["connected"] and set(h["unsubscribed"]) >= set(on_b)
        rig.feeding = True
        await rig.settle()  # reconnected after 1 s backoff and re-subscribed
        assert rig.st.reconnects == 1 and rig.waits == [1.0]
        assert all(rig.cache.quote(s) is not None for s in on_a + on_b)
        assert not rig.cache.health()["stale"] and rig.cache.health()["unsubscribed"] == []
        # the next-window-only socket dying does not make the feed stale (nothing needs it yet)...
        a = next(ws for ws in rig.open_sockets() if ws.slugs() == {nxt5})
        a.emit("close")
        await asyncio.sleep(0)
        h = rig.cache.health()
        assert not h["stale"] and h["connected"] and nxt5 in h["unsubscribed"]
        await rig.settle()
        # ...a quiet 2.5 s on the socket carrying open windows is not a problem (the server sends a book only when it
        # changes; its trade subscription is confirmed and no print came after the book)...
        rig.feeding = False

        def feed_next5():
            for ws in rig.open_sockets():
                if ws.slugs() == {nxt5}:
                    ws.feed()
        rig.clock.t += 2_500
        feed_next5()
        h = rig.cache.health()
        assert not h["stale"] and all(rig.cache.quote(s)["basis"] == "quiet" for s in on_b)
        # ...but once it has been silent for over CONN_LIVE_MS it is not alive: stale on its own
        rig.clock.t += CONN_LIVE_MS
        feed_next5()
        h = rig.cache.health()
        assert h["stale"] and sum(1 for c in h["connections"].values() if c["stale"]) == 1
        assert h["connected"] and h["hard"] == [] and all("silent" in p for p in h["problems"])
        await rig.stop()
    run(go())


def test_each_connection_reconnects_with_its_own_backoff():
    async def go():
        rig = Rig()
        rig.fail_next = 6  # both sockets fail to connect three times each
        await rig.start()
        await rig.settle(80)
        assert sorted(rig.waits) == [1.0, 1.0, 2.0, 2.0, 4.0, 4.0] and rig.st.reconnects == 6
        assert rig.live_everywhere() == set(current_slugs(NOW))
        assert not rig.cache.health()["stale"]
        await rig.stop()
    run(go())


def test_trades_are_vouched_for_only_after_the_subscription_sends_data():
    async def go():
        rig = Rig()
        rig.feeding = False
        await rig.start()
        assert rig.cache.trades_since(current_slugs(NOW)[0], NOW - 1) is None  # nothing seen yet
        rig.feeding = True
        await rig.settle()
        rig.clock.t += 10
        assert rig.cache.trades_since(current_slugs(NOW)[0], NOW) == []
        await rig.stop()
    run(go())


def test_close_is_clean():
    async def go():
        rig = Rig()
        await rig.start()
        await rig.stop()
        assert rig.sockets and all(ws.closed for ws in rig.sockets)
        h = rig.cache.health()
        assert h["stale"] and not h["connected"] and h["connections"] == {}
    run(go())


def test_next_windows_alone_on_a_silent_socket_do_not_make_the_feed_stale():
    """Live 2026-10-29 20:46: next windows send nothing until they open; the pool socket carrying only next windows
    was silent, health() went stale and every Polymarket entry was refused (connectivity_lost after 30 s)."""
    async def go():
        rig = Rig()  # 3 markets per socket: {15m cur, 15m next, 5m cur} + {5m next} alone, as live
        cur15, nxt15, cur5, nxt5 = current_slugs(NOW)
        rig.only = lambda s: window_of(s)[0] <= rig.clock.t  # only windows already open send data
        await rig.start()
        conns = rig.st.connections()
        silent = [cid for cid, ms in conns.items() if ms <= {nxt15, nxt5}]
        assert silent, conns  # one socket carries only next windows (it never sends)
        rig.clock.t += CONN_LIVE_MS + 1
        await rig.settle()
        h = rig.cache.health()
        assert h["connections"][silent[0]]["stale"]  # that socket is silent...
        assert not h["stale"] and h["connected"] and h["problems"] == []  # ...but the feed is fine
        assert h["last_msg_ms"] == rig.clock.t  # from the sockets carrying the open windows
        assert rig.cache.quote(cur15) is not None and rig.cache.quote(cur5) is not None
        # the current windows go quiet: still servable for a while, stale once their socket is not alive
        rig.only = lambda s: False
        rig.clock.t += 2_500
        await rig.settle()
        assert not rig.cache.health()["stale"]
        rig.clock.t += CONN_LIVE_MS
        h = rig.cache.health()
        assert h["stale"] and any(p.startswith(cur15) for p in h["problems"])
        await rig.stop()
    run(go())


def test_a_needed_market_that_is_refused_or_on_a_dead_socket_makes_the_feed_stale():
    async def go():
        rig = Rig()
        rig.st.want("svc", {HELD})  # a held position in a closed window: needed, may be quiet
        rig.only = lambda s: window_of(s)[0] <= rig.clock.t < window_of(s)[1]  # closed and next windows silent
        await rig.start()
        rig.clock.t += 3_000
        await rig.settle()
        assert not rig.cache.health()["stale"]  # quiet held market and quiet next windows: fine
        held_ws = next(ws for ws in rig.open_sockets() if HELD in ws.slugs())
        rig.feeding = False
        held_ws.emit("close")
        await asyncio.sleep(0)
        h = rig.cache.health()
        assert h["stale"] and not h["connected"] and any(p.startswith(HELD) for p in h["problems"])
        await rig.stop()
    run(go())
