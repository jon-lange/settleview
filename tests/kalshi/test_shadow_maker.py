"""Shadow maker fill model (KTD11, U5; the Polymarket shadow's resting auto-sell fills this way): queue position from
public prints, latency, post_only at live time, the book-rule fallback, fees and cash, order expiry. Everything is
scripted: a controllable book (tests/trading/helpers.Market), a tape of prints in the shape a `trades_fn` returns,
and a fake clock that the exchange's latency sleep advances. Orders go through the real production client
(`shadow_trading`), so request shapes are the real V2 ones."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from btc_signal_engine.adapters.kalshi.shadow import ShadowExchange, shadow_trading
from btc_signal_engine.adapters.kalshi.trading import OrderRejected
from btc_signal_engine.domain.settlement.fees import kalshi_fee
from tests.trading.helpers import NOW, TICKER, Market

T0 = NOW


def run(coro):
    return asyncio.run(coro)


class Clock:
    """Fake local clock. The exchange's latency sleep advances it and runs `on_sleep` hooks (the world moving
    while a request is in flight)."""

    def __init__(self, t: int = T0):
        self.t = t
        self.on_sleep: list = []

    def __call__(self) -> int:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += int(round(s * 1000))
        for fn in self.on_sleep:
            fn(self.t)


class Tape:
    """Public prints as a `trades_fn` gives them; `covered=False` = the trade feed cannot vouch."""

    def __init__(self):
        self.prints: list[dict] = []
        self.covered = True
        self.n = 0

    def add(self, ts, price, count, taker, *, block=False, mid=None, ticker=TICKER):
        self.n += 1
        self.prints.append({"trade_id": f"t{self.n}", "ticker": ticker, "yes_price": price, "count": count,
                            "taker_book_side": taker, "ts_ms": ts, "is_block_trade": block, "mid": mid})

    def __call__(self, ticker, ts):
        if not self.covered:
            return None
        return [dict(p) for p in self.prints if p["ticker"] == ticker and p["ts_ms"] > ts]


SELL = "ask"  # a taker SELLING YES (taker_book_side ask) hits resting YES bids
BUY = "bid"  # a taker BUYING YES hits resting YES asks


def rig(market=None, **kw):
    m = market or Market()  # YES bid 55c x100, YES ask 58c x100 (NO bid 42c)
    clock, tape = Clock(), Tape()
    ex = ShadowExchange(book_fn=m.book_fn, market_fn=m.market_fn, clock_ms=clock, trades_fn=tape,
                        sleep=clock.sleep, **kw)
    gw = shadow_trading(max_order_cost=25, exchange=ex)
    return m, clock, tape, ex, gw


async def bid(gw, coid, price=0.55, count=5.0, **kw):
    return await gw.place_limit(ticker=TICKER, side="yes", action="buy", count=count, price=price,
                                client_order_id=coid, tif="good_till_canceled", post_only=True, **kw)


def raw_client(ex):
    """A plain client on the transport, for routes the trading client does not wrap yet (U6 adds them)."""
    return httpx.AsyncClient(base_url="https://x/trade-api/v2", transport=ex)


# -- queue position ----------------------------------------------------------------------------------------------
def test_queue_ahead_is_eaten_by_prints_at_our_price_before_we_fill():
    async def go():
        m, clock, tape, ex, gw = rig()
        r = await bid(gw, "q", count=15)
        assert r["status"] == "resting" and clock.t == T0 + 150  # live only after the place latency
        o = ex.orders[r["order_id"]]
        assert o["_queue"] == 100  # the 100 contracts shown at 55c are ahead of us
        tape.add(clock.t + 10, 0.55, 60, SELL)
        tape.add(clock.t + 20, 0.55, 30, SELL)
        clock.t += 30
        assert (await gw.get_order(r["order_id"]))["filled"] == 0 and o["_queue"] == 10
        tape.add(clock.t + 10, 0.55, 25, SELL, mid=0.565)  # 10 more ahead of us, then 15 for us
        clock.t += 20
        got = await gw.get_order(r["order_id"])
        assert got["filled"] == 15 and got["status"] == "executed" and got["fees"] == 0
        f = ex.fills[-1]
        assert (f["fill_model"], f["mid"], f["count"], f["is_taker"]) == ("trades", 0.565, 15, False)
        assert ex.cash == pytest.approx(-0.55 * 15) and ex.positions[TICKER] == 15
    run(go())


def test_a_print_through_our_price_fills_us_up_to_its_size_whatever_the_queue():
    async def go():
        m, clock, tape, ex, gw = rig()
        r = await bid(gw, "t")
        tape.add(clock.t + 5, 0.54, 3, SELL)  # someone sold at 54c: every 55c bid, ours included, was taken
        clock.t += 10
        assert (await gw.get_order(r["order_id"]))["filled"] == 3
        tape.add(clock.t + 5, 0.55, 1, SELL)  # our level was emptied: nothing is ahead of us any more
        clock.t += 10
        assert (await gw.get_order(r["order_id"]))["filled"] == 4
    run(go())


def test_prints_on_the_other_side_at_better_prices_or_off_book_never_fill_us():
    async def go():
        m, clock, tape, ex, gw = rig()
        r = await bid(gw, "n", count=1)
        ex.orders[r["order_id"]]["_queue"] = 0  # even at the front of the queue
        tape.add(clock.t + 1, 0.55, 50, BUY)  # a buyer: trades with asks, not with our bid
        tape.add(clock.t + 2, 0.56, 50, SELL)  # sold to a better bid than ours
        tape.add(clock.t + 3, 0.50, 50, SELL, block=True)  # matched off the book
        clock.t += 10
        assert (await gw.get_order(r["order_id"]))["filled"] == 0 and ex.fills == []
    run(go())


def test_prints_before_we_are_live_do_not_count():
    async def go():
        m, clock, tape, ex, gw = rig()
        tape.add(T0 + 100, 0.50, 50, SELL)  # during the 150 ms place latency
        r = await bid(gw, "early", count=1)
        clock.t += 50
        assert (await gw.get_order(r["order_id"]))["filled"] == 0
    run(go())


def test_two_of_our_orders_never_fill_from_the_same_contracts():
    async def go():
        m, clock, tape, ex, gw = rig()
        a = await bid(gw, "a", count=2)
        b = await bid(gw, "b", count=2)
        tape.add(clock.t + 5, 0.54, 3, SELL)
        clock.t += 10
        await ex.advance(TICKER)
        assert (ex.orders[a["order_id"]]["_filled"], ex.orders[b["order_id"]]["_filled"]) == (2, 1)  # time priority
    run(go())


# -- latency -----------------------------------------------------------------------------------------------------
def test_a_cancel_sent_100ms_before_a_print_still_fills_under_150ms_latency():
    async def go():
        m, clock, tape, ex, gw = rig()
        r = await bid(gw, "c")
        sent = clock.t
        tape.add(sent + 100, 0.54, 2, SELL)  # lands while our cancel is in flight
        tape.add(sent + 200, 0.54, 2, SELL)  # after the cancel took effect
        res = await gw.cancel(r["order_id"], TICKER)
        assert clock.t == sent + 150 and res["reduced_by"] == 3.0
        o = await gw.get_order(r["order_id"])
        assert (o["filled"], o["status"]) == (2.0, "canceled")
        clock.t = sent + 300
        await ex.advance()
        assert ex.positions[TICKER] == 2.0
    run(go())


def test_a_faster_cancel_beats_the_same_print():
    async def go():
        m, clock, tape, ex, gw = rig(cancel_latency_ms=50)
        r = await bid(gw, "c")
        tape.add(clock.t + 100, 0.54, 2, SELL)
        assert (await gw.cancel(r["order_id"], TICKER))["reduced_by"] == 5.0
    run(go())


def test_post_only_is_judged_when_the_order_goes_live():
    async def go():
        m, clock, tape, ex, gw = rig()
        clock.on_sleep.append(lambda t: m.set(0.50, 0.46))  # the ask drops to 54c while our 55c bid is in flight
        with pytest.raises(OrderRejected) as e:
            await bid(gw, "po")
        assert e.value.status == 400 and "post_only_cross" in e.value.body
        assert ex.orders == {} and clock.t == T0 + 150
        clock.on_sleep.clear()
        m.set(0.55, 0.42)
        clock.on_sleep.append(lambda t: m.set(0.53, 0.42))  # the book moves away instead: fine at live time
        r = await bid(gw, "po2", price=0.56)
        assert r["status"] == "resting"
    run(go())


def test_a_marketable_order_takes_the_book_as_it_is_when_it_arrives():
    async def go():
        m, clock, tape, ex, gw = rig()
        clock.on_sleep.append(lambda t: m.set(0.55, 0.40))  # ask 58c -> 60c during the latency
        r = await gw.place_limit(ticker=TICKER, side="yes", action="buy", count=2, price=0.60,
                                 client_order_id="tk", tif="immediate_or_cancel")
        assert r["avg_price"] == 0.60 and r["fees"] == pytest.approx(kalshi_fee(2, 0.60))
        f = ex.fills[-1]
        assert (f["fill_model"], f["is_taker"], f["mid"]) == ("taker", True, 0.575)
        assert ex.cash == pytest.approx(-1.20 - kalshi_fee(2, 0.60))
    run(go())


# -- fallback ----------------------------------------------------------------------------------------------------
def test_no_trade_feed_falls_back_to_the_book_rule_and_flags_it():
    async def go():
        m, clock, tape, ex, gw = rig()
        events: list = []
        ex.listeners.append(events.append)
        r = await bid(gw, "fb")
        tape.covered = False
        m.set(0.50, 0.47, size=2.0)  # the real ask fell to 53c: the book traded through our 55c bid
        clock.t += 500
        assert (await gw.get_order(r["order_id"]))["filled"] == 2.0
        assert ex.fills[-1]["fill_model"] == "book"
        fill = next(e for e in events if e["type"] == "fill")
        assert fill["fill_model"] == "book" and fill["mid"] == 0.515
        # the feed returns: the queue restarts at the back, and only prints from then on count
        tape.covered = True
        tape.add(clock.t - 100, 0.54, 5, SELL)  # before the feed came back: never used
        m.set(0.55, 0.42)
        clock.t += 10
        await ex.advance(TICKER)
        o = ex.orders[r["order_id"]]
        assert o["_filled"] == 2.0 and o["_queue"] == 100
        tape.add(clock.t + 5, 0.54, 1, SELL)
        clock.t += 10
        await ex.advance(TICKER)
        assert o["_filled"] == 3.0 and ex.fills[-1]["fill_model"] == "trades"
    run(go())


def test_without_a_trade_feed_the_original_shadow_rule_is_unchanged():
    async def go():
        m = Market()
        ex = ShadowExchange(book_fn=m.book_fn, market_fn=m.market_fn)
        assert not ex.maker_model and (ex.place_latency_ms, ex.cancel_latency_ms) == (0, 0)
        events: list = []
        ex.listeners.append(events.append)
        gw = shadow_trading(max_order_cost=25, exchange=ex)
        r = await bid(gw, "legacy")
        m.set(0.50, 0.47, size=2.0)
        assert (await gw.get_order(r["order_id"]))["filled"] == 2.0
        fill = next(e for e in events if e["type"] == "fill")
        assert "fill_model" not in fill and "mid" not in fill  # event shapes identical to the real stream's
    run(go())


# -- fees, cash, expiry --------------------------------------------------------------------------------------
def test_maker_fee_rate_and_taker_fees_reach_cash():
    async def go():
        m, clock, tape, ex, gw = rig(maker_fee_rate=0.0175)
        r = await bid(gw, "m", count=10)
        tape.add(clock.t + 5, 0.54, 10, SELL)
        clock.t += 10
        o = await gw.get_order(r["order_id"])
        fee = kalshi_fee(10, 0.55, 0.0175)
        assert o["fees"] == pytest.approx(fee) and ex.fills[-1]["fee"] == pytest.approx(fee)
        assert ex.cash == pytest.approx(-5.50 - fee)
    run(go())


def test_expiration_time_cancels_the_order_and_later_prints_do_not_fill_it():
    async def go():
        m, clock, tape, ex, gw = rig()
        cl = raw_client(ex)
        exp_s = (clock.t + 1150) // 1000 + 1
        oid = (await cl.post("/portfolio/events/orders", json={
            "ticker": TICKER, "client_order_id": "e", "side": "bid", "count": "1.00", "price": "0.5500",
            "time_in_force": "good_till_canceled", "post_only": True, "self_trade_prevention_type": "maker",
            "expiration_time": exp_s})).json()["order_id"]
        tape.add(exp_s * 1000 + 5, 0.54, 1, SELL)
        clock.t = exp_s * 1000 + 10
        await ex.advance()
        assert ex.orders[oid]["status"] == "canceled" and ex.fills == []
        ioc = await cl.post("/portfolio/events/orders", json={
            "ticker": TICKER, "client_order_id": "e2", "side": "bid", "count": "1.00", "price": "0.5500",
            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "maker", "expiration_time": exp_s})
        assert ioc.status_code == 400
        await cl.aclose()
    run(go())
