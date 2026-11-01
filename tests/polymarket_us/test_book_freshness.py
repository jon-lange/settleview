"""Polymarket book freshness (adapters/polymarket_us/market_data.py, "Servable") and the trading feed's hysteresis
(adapters/polymarket_us/stream.py), from the two things seen live on 2026-10-31
(docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md):
* a quiet Saturday: the server sends a book only when it changes, 2-18 s apart; the old "a message within 2 s" rule
  flipped the trading feed ~600 times in 4 h and refused valid quiet books;
* 13:59-14:45 CDT: every book message stopped while TRADE messages kept coming on the same live connections.
No network: the cache is fed message by message on a test clock."""

from __future__ import annotations

import asyncio
import random

import pytest

from btc_signal_engine.adapters.polymarket_us.market_data import (CONN_LIVE_MS, MAX_AGE_MS, QUIET_MAX_MS,
                                                                  TRADE_GRACE_MS, PolymarketMarketData,
                                                                  StaleMarketData)
from btc_signal_engine.adapters.polymarket_us.stream import DOWN_AFTER_MS, UP_AFTER_MS, PolymarketTradingStream
from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
from tests.polymarket_us.fakes import NOW, SLUG, W0, Clock, FakeWS, client, full_book, lite, trade
from tests.trading.helpers import Exchange, Market, ready, service

CONN = "pm-ws-1"
HEARTBEAT = {"heartbeat": {}}


def run(coro):
    return asyncio.run(coro)


def managed(clock: Clock) -> PolymarketMarketData:
    """A stream-managed cache as PolymarketMarketStream leaves it: SLUG wanted, subscribed and confirmed on CONN."""
    c = PolymarketMarketData(clock_ms=clock)
    c.managed = True
    c.set_wanted({SLUG})
    c.set_connected(True, CONN)
    c.mark_subscribed(SLUG, CONN)
    c.cover_trades(SLUG, CONN, since_ms=clock())
    return c


def msg(c: PolymarketMarketData, m: dict) -> None:
    c.on_message(m, conn=CONN)


def why(c: PolymarketMarketData) -> str:
    with pytest.raises(StaleMarketData) as e:
        run(c.book(SLUG))
    return str(e.value)


def test_constants_are_the_documented_ones():
    assert (MAX_AGE_MS, QUIET_MAX_MS, CONN_LIVE_MS, TRADE_GRACE_MS) == (2_000, 30_000, 15_000, 2_000)
    assert (DOWN_AFTER_MS, UP_AFTER_MS) == (10_000, 5_000)


def test_quiet_book_on_a_live_connection_is_servable_up_to_30_s():
    clock = Clock()
    c = managed(clock)
    msg(c, full_book(SLUG, [(0.60, 10)], [(0.62, 5)]))
    msg(c, lite(SLUG, 0.60, 0.62, bid_depth=10, ask_depth=5))
    t0 = clock.t
    for dt in (1_000, 2_500, 10_000, 20_000, QUIET_MAX_MS):
        clock.t = t0 + dt
        if dt in (10_000, 20_000):
            msg(c, HEARTBEAT)  # the connection stays alive (any message counts)
        q = c.quote(SLUG)
        assert q is not None and (q["yes_bid"], q["yes_ask"]) == (0.60, 0.62), dt
        assert q["servable"] is True and q["basis"] == ("fresh" if dt <= MAX_AGE_MS else "quiet")
        assert q["age_ms"] == dt and run(c.book(SLUG))["yes"] == [(0.60, 10.0)]
        assert not c.health()["stale"]
    clock.t = t0 + QUIET_MAX_MS + 1
    assert c.quote(SLUG) is None and "quiet limit 30 s" in why(c)
    assert c.health()["stale"] and c.health()["hard"] == []  # a soft problem: the feed's hysteresis waits it out
    msg(c, lite(SLUG, 0.61, 0.63))  # the book changes: servable again at once
    assert c.quote(SLUG)["basis"] == "fresh"


def test_a_trade_after_the_last_book_update_refuses_the_quiet_book_and_names_a_broken_feed_after_the_grace():
    clock = Clock()
    c = managed(clock)
    msg(c, lite(SLUG, 0.60, 0.62, bid_depth=10, ask_depth=5))
    clock.t += 1_000
    msg(c, trade(SLUG, 0.62, 3, "ORDER_INTENT_BUY_LONG"))
    assert c.quote(SLUG) is not None  # within MAX_AGE_MS of the book: (a) serves it, as before
    trades = c.trades_since(SLUG, NOW)
    assert len(trades) == 1 and trades[0]["mid"] == 0.61  # the print is stamped with the book just before it
    clock.t += 1_500  # the book is 2.5 s old and a print came after it: the quiet path refuses at once
    assert c.quote(SLUG) is None and "waiting for its book update" in why(c)
    clock.t += TRADE_GRACE_MS  # still no book update 3.5 s after the print: the book feed is broken
    assert c.quote(SLUG) is None
    reason = why(c)
    assert "book feed broken: trades for 3.5 s without a book update" in reason
    assert any("book feed broken" in p for p in c.health()["problems"])
    msg(c, lite(SLUG, 0.62, 0.64))  # its book update arrives: servable, and it stays so while quiet
    clock.t += 8_000
    msg(c, HEARTBEAT)
    assert c.quote(SLUG)["yes_ask"] == 0.64 and c.quote(SLUG)["basis"] == "quiet"


def test_a_trade_after_the_full_book_refuses_that_record_but_a_newer_top_still_serves():
    clock = Clock()
    c = managed(clock)
    msg(c, full_book(SLUG, [(0.60, 10)], [(0.62, 5)]))
    msg(c, trade(SLUG, 0.62, 5, "ORDER_INTENT_BUY_LONG"))
    msg(c, lite(SLUG, 0.60, 0.63, bid_depth=10, ask_depth=4))  # only the top followed the print
    clock.t += 5_000
    assert run(c.book(SLUG)) == {"yes": [(0.60, 10.0)], "no": [(0.37, 4.0)]}  # the top with depth, not the old book
    assert c.quote(SLUG)["yes_ask"] == 0.63


def test_a_dead_or_silent_connection_is_never_servable():
    clock = Clock()
    c = managed(clock)
    msg(c, lite(SLUG, 0.60, 0.62))
    clock.t += 3_000
    assert c.quote(SLUG) is not None
    clock.t += CONN_LIVE_MS  # 18 s without a single message on the connection: not alive
    assert c.quote(SLUG) is None and f"connection {CONN} silent for 18.0 s" in why(c)
    msg(c, lite(SLUG, 0.60, 0.62))
    assert c.quote(SLUG) is not None
    c.set_connected(False, CONN)  # the socket closed: refused at once, even within MAX_AGE_MS
    assert c.quote(SLUG) is None and "is down" in why(c)
    h = c.health()
    assert h["stale"] and not h["connected"] and h["hard"]


def test_a_quiet_book_needs_trade_coverage_since_it_arrived():
    clock = Clock()
    c = PolymarketMarketData(clock_ms=clock)  # fed directly: no TRADE subscription confirmed
    c.set_connected(True)
    c.on_message(lite(SLUG, 0.60, 0.62))
    clock.t += 2_500
    assert c.quote(SLUG) is None and "trade subscription" in why(c)
    c.trades_subscribed([SLUG])  # coverage starts now, after the book: a print before now could have been missed
    assert c.quote(SLUG) is None
    c.on_message(lite(SLUG, 0.60, 0.62))
    clock.t += 2_500
    assert c.quote(SLUG)["basis"] == "quiet"


def test_a_book_from_before_a_reconnect_is_not_served_quiet():
    """The connection id survives a reconnect; prints during the outage were missed, so a book or top that arrived
    before the new subscription is never served through the quiet path."""
    clock = Clock()
    c = managed(clock)
    msg(c, full_book(SLUG, [(0.60, 10)], [(0.62, 5)]))
    msg(c, lite(SLUG, 0.60, 0.62, bid_depth=10, ask_depth=5))
    clock.t += 1_000
    c.set_connected(False, CONN)
    clock.t += 1_000
    c.set_connected(True, CONN)
    c.mark_subscribed(SLUG, CONN)
    t0 = clock.t
    msg(c, lite(SLUG, 0.70, 0.72))  # only the top re-arrives; the stream confirms with the time it subscribed
    c.cover_trades(SLUG, CONN, since_ms=t0)
    clock.t += 3_000
    msg(c, HEARTBEAT)
    assert c.quote(SLUG)["yes_bid"] == 0.70  # the new top, quiet
    reason = why(c)  # the old full book (from before t0) is refused, and the new top has no depth
    assert "full book: its trade subscription on pm-ws-1 has not covered the time since this full book" in reason
    assert "top: the top carries no depth" in reason


def test_a_window_that_just_opened_has_30_s_to_send_its_first_book_before_health_counts_it():
    clock = Clock(W0 + 5_000)
    c = managed(clock)
    msg(c, HEARTBEAT)
    assert c.quote(SLUG) is None  # never priced without a book...
    assert not c.health()["stale"]  # ...but not a feed problem in its first 30 s
    clock.t = W0 + QUIET_MAX_MS + 1
    msg(c, HEARTBEAT)
    h = c.health()
    assert h["stale"] and h["hard"] == [] and "no full book received" in h["problems"][0]


# -- the trading feed: hysteresis and audit rows -------------------------------------------------------------------
class Rig:
    """A managed cache, the Polymarket trading stream over it (private socket up) and a TradingService that audits
    the stream's up / down transitions as it would live."""

    def __init__(self, store) -> None:
        self.clock = Clock()
        self.md = managed(self.clock)
        self.events: list[dict] = []
        gw = PolymarketTrading(client(), quote_fn=self.md.quote, clock_ms=self.clock)
        self.gw = gw
        self.st = PolymarketTradingStream(lambda: FakeWS(), gw, self.md, self.events.append, clock_ms=self.clock)
        self.st._connected = True  # the private socket is up (run_once sets this after subscribing)
        self.svc = run(ready(service(store, Exchange(Market()))))
        self.svc.attach_stream("prod", self.st, self.clock())

    def check(self) -> None:
        """What the service does every tick: judge the stream's health (and audit a transition)."""
        self.svc._stream_health("prod", self.clock())

    def transitions(self) -> list[str]:
        return [a["event"] for a in reversed(self.svc.audit()) if a["event"].startswith("stream_")]


def test_quiet_saturday_book_messages_every_3_to_10_s_never_take_the_feed_down(store):
    rig = Rig(store)
    msg(rig.md, lite(SLUG, 0.50, 0.52))
    rig.check()
    rng = random.Random(20261031)
    end = rig.clock.t + 8 * 60_000  # 8 minutes of a quiet open window
    nxt = rig.clock.t + rng.randint(3_000, 10_000)
    refused = 0
    while rig.clock.t < end:
        rig.clock.t += 500
        if rig.clock.t >= nxt:
            if rng.random() < 0.5:  # a print, and its book update right behind it, as the server sends them
                msg(rig.md, trade(SLUG, 0.52, 1, "ORDER_INTENT_BUY_LONG"))
            rig.clock.t += 20
            msg(rig.md, lite(SLUG, 0.50, round(0.52 + rng.choice((0, 0.01)), 2)))
            nxt = rig.clock.t + rng.randint(3_000, 10_000)
        refused += rig.md.quote(SLUG) is None
        rig.check()
        assert rig.st.health()["connected"] and not rig.st.health()["stale"]
    assert refused == 0  # every quiet book was servable
    assert rig.transitions() == ["stream_up"]  # no stream_down, so no forced polling and one audit row in 8 min


def test_a_real_outage_is_one_stream_down_and_one_stream_up(store):
    rig = Rig(store)
    msg(rig.md, lite(SLUG, 0.50, 0.52))
    rig.check()
    rig.clock.t += 1_000
    rig.md.set_connected(False, CONN)  # the markets socket died
    rig.check()
    assert rig.transitions() == ["stream_up", "stream_down"]  # a hard problem: down at once
    # reconnecting: up, re-subscribed, data, then a brief drop again, then steady
    for step in range(6):
        rig.clock.t += 1_000
        rig.md.set_connected(step != 2, CONN)
        if step != 2:
            rig.md.mark_subscribed(SLUG, CONN)
            rig.md.cover_trades(SLUG, CONN, since_ms=rig.clock.t)
            msg(rig.md, lite(SLUG, 0.50, 0.52))
        rig.check()
    assert rig.transitions() == ["stream_up", "stream_down"]  # not up again before UP_AFTER_MS without a problem
    for _ in range(4):
        rig.clock.t += 1_000
        msg(rig.md, lite(SLUG, 0.50, 0.52))
        rig.check()
    assert rig.transitions() == ["stream_up", "stream_down", "stream_up"]


def test_1359_outage_trades_keep_coming_but_no_book_is_refused_within_the_grace_and_stays_refused(store):
    """2026-10-31 13:59:56-14:45 CDT: no MARKET_DATA / MARKET_DATA_LITE at all, TRADE messages ~1 a second on the
    same live connection. Entries must never price from the last book; the feed goes down once and the service
    polls; when books return it comes back up once."""
    rig = Rig(store)
    msg(rig.md, lite(SLUG, 0.50, 0.52, bid_depth=10, ask_depth=10))
    rig.check()
    last_book = rig.clock.t
    stale_at = None
    for i in range(1, 300):  # 5 minutes of prints, no book (the window is still open)
        rig.clock.t = last_book + i * 1_000
        msg(rig.md, trade(SLUG, 0.51, 2, "ORDER_INTENT_BUY_LONG"))
        rig.check()
        q = rig.md.quote(SLUG)
        if q is None and stale_at is None:
            stale_at = rig.clock.t
        if stale_at is not None:
            assert q is None  # stays refused
            with pytest.raises(RuntimeError, match="no servable live Polymarket quote"):
                run(rig.gw.quote(SLUG))  # the order path's quote: refused
    assert stale_at is not None and stale_at - last_book <= MAX_AGE_MS + TRADE_GRACE_MS
    assert "book feed broken" in why(rig.md)
    h = rig.st.health()
    assert h["stale"] and h["connected"] and "book feed broken" in h["reason"]  # connection alive, book not
    assert rig.transitions() == ["stream_up", "stream_down"]
    downs = [a for a in rig.svc.audit() if a["event"] == "stream_down"]
    assert len(downs) == 1
    # the down came DOWN_AFTER_MS after the book became unservable, not on the first quiet second
    assert downs[0]["ts"] - stale_at >= DOWN_AFTER_MS - 1_000
    # books come back
    for _ in range(UP_AFTER_MS // 1_000 + 1):
        rig.clock.t += 1_000
        msg(rig.md, lite(SLUG, 0.49, 0.51, bid_depth=10, ask_depth=10))
        rig.check()
    assert rig.md.quote(SLUG) is not None
    assert rig.transitions() == ["stream_up", "stream_down", "stream_up"]
