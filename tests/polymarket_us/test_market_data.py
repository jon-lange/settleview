"""The WebSocket market-data cache (staleness, book shape, trade coverage), the private trading stream (the markets
WebSocket pool is in test_market_pool.py)."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.adapters.polymarket_us.market_data import (CONN_LIVE_MS, PolymarketMarketData,
                                                                  StaleMarketData)
from btc_signal_engine.adapters.polymarket_us.markets import MarketTerms
from btc_signal_engine.adapters.polymarket_us.stream import PolymarketTradingStream
from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
from tests.polymarket_us.fakes import (CLOSE, NEXT, NOW, SLUG, Clock, FakeWS, amount, client, full_book, lite,
                                       trade)


def run(coro):
    return asyncio.run(coro)


def cache(clock=None) -> PolymarketMarketData:
    c = PolymarketMarketData(clock_ms=clock or Clock())
    c.set_connected(True)
    return c


def test_quote_from_lite_and_staleness():
    clock = Clock()
    c = cache(clock)
    assert c.quote(SLUG) is None
    c.on_message(lite(SLUG, 0.69, 0.70))
    q = c.quote(SLUG)
    assert (q["yes_bid"], q["yes_ask"], q["no_bid"], q["no_ask"]) == (0.69, 0.70, 0.30, 0.31)
    assert q["status"] == "open" and q["open_ts"] == CLOSE - 900_000 and q["close_ts"] == CLOSE
    clock.t += 2_001
    assert c.quote(SLUG) is None  # older than 2 s: refused
    c.on_message(lite(SLUG, 0.69, 0.70))
    assert c.quote(SLUG) is not None
    c.set_connected(False)
    assert c.quote(SLUG) is None and c.health()["stale"]


def test_quote_after_close_is_closed_and_non_series_is_ignored():
    clock = Clock(CLOSE + 1)
    c = cache(clock)
    c.on_message(lite(SLUG, 0.98, 0.99))
    c.on_message(lite("some-other-market", 0.5, 0.6))
    assert c.quote(SLUG)["status"] == "closed" and c.quote("some-other-market") is None


def test_book_in_kalshi_shape_full_book_first_then_lite_depth():
    clock = Clock()
    c = cache(clock)
    with pytest.raises(StaleMarketData):
        run(c.book(SLUG))
    c.on_message(lite(SLUG, 0.69, 0.70, bid_depth=12, ask_depth=7))
    assert run(c.book(SLUG)) == {"yes": [(0.69, 12.0)], "no": [(0.30, 7.0)]}
    c.on_message(full_book(SLUG, [(0.69, 10), (0.68, 40)], [(0.70, 5), (0.71, 20)]))
    assert run(c.book(SLUG)) == {"yes": [(0.69, 10.0), (0.68, 40.0)], "no": [(0.30, 5.0), (0.29, 20.0)]}
    q = c.quote(SLUG)
    assert (q["yes_bid"], q["yes_ask"]) == (0.69, 0.70)
    c.on_message(full_book(SLUG, [(0.5, 1)], [(0.6, 1)], state="MARKET_STATE_HALTED"))
    assert c.quote(SLUG)["status"] == "closed"
    clock.t += 3_000
    with pytest.raises(StaleMarketData):
        run(c.book(SLUG))


def test_trades_since_only_with_full_coverage():
    clock = Clock()
    c = cache(clock)
    c.on_message(trade(SLUG, 0.70, 3, "ORDER_INTENT_BUY_LONG"))
    assert c.trades_since(SLUG, NOW - 1) is None  # not subscribed to its trades yet
    c.trades_subscribed([SLUG])
    clock.t += 100
    c.on_message(trade(SLUG, 0.70, 3, "ORDER_INTENT_BUY_LONG"))
    clock.t += 100
    c.on_message(trade(SLUG, 0.69, 2, "ORDER_INTENT_BUY_SHORT"))
    got = c.trades_since(SLUG, NOW)
    assert [(t["yes_price"], t["count"], t["taker_book_side"]) for t in got] == [(0.70, 3.0, "bid"), (0.69, 2.0, "ask")]
    clock.t += 5_000  # a quiet 5 s on a live socket: still vouched for (TCP loses nothing while it is alive)
    assert len(c.trades_since(SLUG, NOW)) == 2
    clock.t += CONN_LIVE_MS  # no message for over CONN_LIVE_MS: not alive, cannot vouch
    assert c.trades_since(SLUG, NOW) is None
    c.set_connected(False)
    assert c.trades_since(SLUG, NOW) is None


def test_market_terms_cache_the_strike_and_retry_a_missing_one_after_10s():
    calls = []

    async def fn(slug):
        calls.append(slug)
        ptb = {"value": "80000.00"} if len(calls) > 1 else None
        return {"market": {"assetPriceTerms": {"priceToBeat": ptb}}}

    t = {"s": 0.0}
    terms = MarketTerms(fn, clock_s=lambda: t["s"])
    assert run(terms.strike(SLUG)) is None and terms.cached_strike(SLUG) is None  # not published yet
    assert run(terms.strike(SLUG)) is None and len(calls) == 1  # throttled (asked < 10 s ago)
    t["s"] = 11
    assert run(terms.strike(SLUG)) == 80000.0 and terms.cached_strike(SLUG) == 80000.0
    assert run(terms.strike(SLUG)) == 80000.0 and len(calls) == 2  # kept: never asked again
    assert run(terms.strike("KXBTC15M-x")) is None


# -- private trading stream ---------------------------------------------------------------------------------------
def test_private_stream_emits_orders_fills_quotes_and_updates_the_account():
    clock = Clock()
    md = cache(clock)
    events = []
    g = PolymarketTrading(client(), quote_fn=md.quote, clock_ms=clock)
    g.remember("bfe-1-tp0", "ord9")
    st = PolymarketTradingStream(lambda: FakeWS(), g, md, events.append, clock_ms=clock)
    st.subscribe_tickers({SLUG, "not-a-series-market"})
    md.on_message(lite(SLUG, 0.69, 0.70))
    md.on_message(lite(NEXT, 0.40, 0.41))  # not watched
    quotes = [e for e in events if e["type"] == "quote"]
    assert [(e["ticker"], e["yes_bid"], e["no_ask"], e["env"]) for e in quotes] == [(SLUG, 0.69, 0.31, "pm_prod")]
    order = {"id": "ord9", "marketSlug": SLUG, "intent": "ORDER_INTENT_SELL_LONG", "price": amount(0.99),
             "quantity": 2, "cumQuantity": 1, "leavesQuantity": 1, "state": "ORDER_STATE_PARTIALLY_FILLED"}
    st.on_message({"orderSubscriptionUpdate": {"execution": {"type": "EXECUTION_TYPE_PARTIAL_FILL", "order": order,
                                                              "lastPx": amount(0.99), "lastShares": "1"}}})
    o = [e for e in events if e["type"] == "order"][-1]["order"]
    assert (o["order_id"], o["client_order_id"], o["filled"], o["status"], o["price"]) == (
        "ord9", "bfe-1-tp0", 1.0, "resting", 0.99)
    f = [e for e in events if e["type"] == "fill"][-1]
    assert (f["client_order_id"], f["count"], f["yes_price"]) == ("bfe-1-tp0", 1.0, 0.99)
    st.on_message({"positionSubscription": {"afterPosition": {"netPosition": "1", "marketMetadata": {"slug": SLUG}}}})
    assert g.stream_positions[SLUG] == 1.0
    assert g.local_position(SLUG) == -1.0  # the stream's fill of our SELL_LONG reached the local book
    st.on_message({"accountBalancesUpdate": {"balanceChange": {"afterBalance": {"currentBalance": 42.5}}}})
    assert g.balance[0] == 42.5
    assert st.health()["connected"] is False  # private socket not connected yet


def test_private_stream_run_once_subscribes_resyncs_and_reports_health():
    async def go():
        clock = Clock()
        md = cache(clock)
        md.on_message(lite(SLUG, 0.69, 0.70))
        events, ws = [], FakeWS()
        st = PolymarketTradingStream(lambda: ws, PolymarketTrading(client(), quote_fn=md.quote), md, events.append,
                                     clock_ms=clock)
        n = {"i": 0}

        async def sleep(_):
            n["i"] += 1
            if n["i"] == 1:
                assert st.health()["connected"] and not st.health()["stale"]
                ws.emit("close")

        st._sleep = sleep
        with pytest.raises(ConnectionError):
            await st.run_once()
        assert [t for _, t, _ in ws.subs] == ["SUBSCRIPTION_TYPE_ORDER", "SUBSCRIPTION_TYPE_POSITION",
                                             "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE"]
        kinds = [e["type"] for e in events]
        assert kinds[0] == "resync" and "health" in kinds and ws.closed
        assert not st.health()["connected"]
    run(go())


def test_prune_forgets_a_past_window_once_it_is_unsubscribed_but_keeps_a_subscribed_one():
    # Two windows that have both ended; stream-managed, both had prints. SLUG gets unsubscribed, NEXT stays subscribed
    # (a trading service still watches it). Every 5 and 15 minutes a new slug appears, so leftovers would grow forever.
    clock = Clock()
    c = cache(clock)
    c.managed = True
    for s in (SLUG, NEXT):
        c.mark_subscribed(s, "main")
        c.cover_trades(s, "main")
        c.on_message(trade(s, 0.70, 3, "ORDER_INTENT_BUY_LONG"))
    clock.t = CLOSE + 900_000 + 200_000  # both windows over and their prints older than keep_ms (120 s)
    c.on_message(trade(NEXT, 0.95, 1, "ORDER_INTENT_BUY_LONG"))  # NEXT's earlier print is pruned off its deque
    c.mark_unsubscribed(SLUG, "main")
    c.on_message({"heartbeat": {}})  # any message prunes
    assert SLUG not in c._trades and SLUG not in c._pruned and SLUG not in c._covered
    assert NEXT in c._trades and NEXT in c._pruned and NEXT in c._covered


def test_prune_keeps_a_window_that_is_still_open_even_unsubscribed():
    clock = Clock()
    c = cache(clock)
    c.managed = True
    c.mark_subscribed(SLUG, "main")
    c.on_message(trade(SLUG, 0.70, 3, "ORDER_INTENT_BUY_LONG"))
    clock.t += 130_000  # print older than keep_ms, window (ends at CLOSE) still open
    c.mark_unsubscribed(SLUG, "main")
    c.on_message({"heartbeat": {}})
    assert SLUG in c._pruned
