"""Polymarket US lost answers (review fix plan 2026-10-31: B3#2, B3#5, B3#14; domain/order_resolution.py).

A lost create is resolved from the account's activity trades, never from the lagging positions list; an IOC answer
that is not final is read until it is; only a 4xx that proves a refusal is a rejection. Fake SDK client, no network."""

from __future__ import annotations

import asyncio

import pytest

from polymarket_us import APIStatusError

from btc_signal_engine.adapters.polymarket_us.trading import OrderOutcomeUnknown, OrderRejected, PolymarketTrading
from tests.polymarket_us.fakes import NOW, SLUG, APIConnectionError, Clock, client, http_error


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_):
    return None


def gw(c, clock=None) -> PolymarketTrading:
    a = c.acct
    quote = lambda s: {"ticker": s, "yes_bid": a.bid, "yes_ask": a.ask, "no_bid": round(1 - a.ask, 2),  # noqa: E731
                       "no_ask": round(1 - a.bid, 2)}
    return PolymarketTrading(c, quote_fn=quote, clock_ms=clock or Clock(), sleep=_no_sleep)


def buy(g, side="yes", count=2, price=0.71, coid="c1"):
    return g.place_limit(ticker=SLUG, side=side, action="buy", count=count, price=price, client_order_id=coid,
                         tif="immediate_or_cancel")


# -- B3#2 ---------------------------------------------------------------------------------------------------------------
def test_lost_filled_buy_is_resolved_from_the_activities_while_the_positions_list_lags():
    """The positions list lags our own fills: main read the (missing) change in it as 'canceled, 0 filled' for a buy
    that filled."""
    c = client(ask=0.70)
    clock = Clock()
    g = gw(c, clock)
    c.acct.frozen = {}  # the list does not show the fill yet
    c.acct.fail_next = (APIConnectionError(message="timeout"), True)  # placed and filled; the answer lost
    with pytest.raises(OrderOutcomeUnknown):
        run(buy(g))
    clock.t += 15_000
    found = run(g.find_order(SLUG, "c1", "yes"))
    assert found["status"] == "executed" and found["filled"] == 2 and found["avg_price"] == 0.70
    assert found["proof"] == "activities"
    assert g.local_position(SLUG) == 2.0  # in the local book once
    assert run(g.market_fills(SLUG, NOW - 5_000)) == []  # its trade is ours now, never a "manual" one
    assert run(g.find_order(SLUG, "c1", "yes")) == found  # stable


def test_lost_order_after_a_restart_still_finds_the_fill_from_the_activities():
    c = client(ask=0.70)
    clock = Clock()
    g = gw(c, clock)
    c.acct.fail_next = (APIConnectionError(message="timeout"), True)
    with pytest.raises(OrderOutcomeUnknown):
        run(buy(g))
    fresh = gw(c, clock)  # a restart: the in-memory send record is gone
    clock.t += 61_000
    assert run(fresh.find_order(SLUG, "c1", "yes")) is None
    got = run(fresh.lost_order(SLUG, "c1", side="yes", action="buy", since_ms=NOW, count=2, price=0.71))
    assert got["filled"] == 2 and got["avg_price"] == 0.70 and got["proof"] == "activities"


def test_activity_trades_that_cannot_be_the_order_are_never_claimed():
    c = client(ask=0.70)
    clock = Clock()
    g = gw(c, clock)
    c.acct.fail_next = (APIConnectionError(message="timeout"), False)  # never placed
    with pytest.raises(OrderOutcomeUnknown):
        run(buy(g, price=0.60))
    c.acct.activities = [  # a trade at 70c (above our 60c limit) and one before the send
        {"type": "ACTIVITY_TYPE_TRADE", "trade": {"id": "x1", "marketSlug": SLUG, "price": {"value": "0.70"},
                                                  "qty": "2", "createTime": "2026-10-29T12:05:31Z"}},
        {"type": "ACTIVITY_TYPE_TRADE", "trade": {"id": "x2", "marketSlug": SLUG, "price": {"value": "0.55"},
                                                  "qty": "2", "createTime": "2026-10-29T12:04:00Z"}}]
    clock.t += 15_000
    assert run(g.find_order(SLUG, "c1", "yes")) is None
    got = run(g.lost_order(SLUG, "c1", side="yes", action="buy", since_ms=NOW, count=2, price=0.60))
    assert got["status"] == "not_found" and got["filled"] == 0


# -- B3#5 ---------------------------------------------------------------------------------------------------------------
def _pending_answer(c, *, final_after: int):
    """create answers PENDING_NEW with no fill; the order becomes FILLED after `final_after` retrieves."""
    a = c.acct
    real_create, real_retrieve = a.orders_api.create, a.orders_api.retrieve
    reads = {"n": 0}

    async def create(params):
        resp = await real_create(params)
        o = dict(a.orders[resp["id"]])
        return {"id": resp["id"], "executions": [{"type": "EXECUTION_TYPE_NEW", "order": {
            **o, "state": "ORDER_STATE_PENDING_NEW", "cumQuantity": 0, "leavesQuantity": o["quantity"]}}]}

    async def retrieve(order_id):
        reads["n"] += 1
        got = await real_retrieve(order_id)
        if reads["n"] <= final_after:
            got["order"].update(state="ORDER_STATE_PENDING_NEW", cumQuantity=0)
        return got
    a.orders_api.create, a.orders_api.retrieve = create, retrieve
    c.orders = a.orders_api


def test_ioc_answered_pending_is_read_until_final_never_taken_as_canceled():
    c = client(ask=0.70)
    _pending_answer(c, final_after=1)
    g = gw(c)
    r = run(buy(g))
    assert r["status"] == "executed" and r["filled"] == 2 and r["avg_price"] == 0.70  # main: canceled, 0 filled
    assert g.local_position(SLUG) == 2.0


def test_ioc_still_pending_after_the_reads_is_unknown_and_findable():
    c = client(ask=0.70)
    _pending_answer(c, final_after=99)
    g = gw(c)
    with pytest.raises(OrderOutcomeUnknown) as e:
        run(buy(g))
    assert e.value.definitive is False
    assert g.order_id_for("c1") == "ord1"  # find_order keeps reading that order
    assert run(g.find_order(SLUG, "c1", "yes"))["status"] == "resting"  # not final: the app cancels, then reads


# -- B3#14 --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,definitive", [(408, False), (499, False), (425, False), (400, True), (422, True)])
def test_only_a_4xx_that_proves_a_refusal_is_a_rejection(status, definitive):
    c = client()
    c.acct.fail_next = http_error(APIStatusError, status)
    with pytest.raises(OrderRejected if definitive else OrderOutcomeUnknown) as e:
        run(buy(gw(c)))
    assert e.value.definitive is definitive  # main: every 4xx below 500 was "rejected"


def test_own_trade_ids_evict_the_oldest_first():
    g = gw(client())
    for i in range(4001):
        g.observe_trade_id(f"t{i}")
    assert "t0" not in g._own_trades and "t4000" in g._own_trades and "t1" in g._own_trades
    assert len(g._own_trades) == 4000
