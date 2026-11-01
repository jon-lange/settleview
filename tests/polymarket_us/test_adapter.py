"""PolymarketTrading against a fake SDK client: intent/price mapping (DOWN = NO at 1 - q on the YES grid), guards
(series, whole contracts, cent grid, notional cap), sells capped at holdings (no short ever opened), error semantics
(definitive vs unknown, 401/403 status), no create is ever retried, lost answers resolved through the account, and
cancels that answer authoritatively."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.adapters.polymarket_us.markets import slug_for, window_of
from btc_signal_engine.adapters.polymarket_us.trading import (OrderOutcomeUnknown, OrderRejected, PolymarketTrading,
                                                              TradingViolation)
from btc_signal_engine.domain.settlement.fees import polymarket_fee
from tests.polymarket_us.fakes import (CLOSE, NOW, SLUG, W0, APIConnectionError, AuthenticationError, BadRequestError,
                                       Clock, InternalServerError, RateLimitError, client, http_error)


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_):
    return None


def live_quote(c):
    """The fake book's top as the WebSocket quote."""
    a = c.acct
    return lambda s: {"ticker": s, "yes_bid": a.bid, "yes_ask": a.ask, "no_bid": round(1 - a.ask, 2),
                      "no_ask": round(1 - a.bid, 2)}


def gw(c=None, *, cap=25.0, clock=None, quote=True, **kw) -> PolymarketTrading:
    c = c or client()
    return PolymarketTrading(c, quote_fn=live_quote(c) if quote else (lambda s: None), max_order_cost=cap,
                             clock_ms=clock or Clock(), sleep=_no_sleep, **kw)


def buy(g, side="yes", count=2, price=0.71, coid="c1", tif="immediate_or_cancel", **kw):
    return g.place_limit(ticker=SLUG, side=side, action="buy", count=count, price=price, client_order_id=coid,
                         tif=tif, **kw)


def sell(g, side="yes", count=2, price=0.60, coid="s1", tif="immediate_or_cancel", **kw):
    return g.place_limit(ticker=SLUG, side=side, action="sell", count=count, price=price, client_order_id=coid,
                         tif=tif, **kw)


# -- markets and fees ---------------------------------------------------------------------------------------------
def test_slugs_and_windows():
    assert SLUG == "cpc-btc-updown-15m-2026-10-29-1200z"
    assert slug_for(W0 + 899_999) == SLUG and window_of(SLUG) == (W0, CLOSE)
    assert window_of("KXBTC15M-26OCT291215-15") is None and window_of("cpc-btc-updown-5m-2026-10-29-1200z") == (W0, W0 + 300_000)
    assert slug_for(W0 + 299_999, "5m") == "cpc-btc-updown-5m-2026-10-29-1200z" and window_of("cpc-btc-updown-2m-x") is None


def test_polymarket_fee_rounds_to_the_nearest_cent_as_observed():
    assert polymarket_fee(2, 0.22) == 0.02 and polymarket_fee(2, 0.17) == 0.02  # prod fills 2026-10-29
    assert polymarket_fee(1, 0.70) == 0.01 and polymarket_fee(10, 0.5) == 0.17


# -- mapping ------------------------------------------------------------------------------------------------------
def test_up_buy_is_buy_long_at_the_yes_price_and_reports_fills_and_fees():
    c = client(ask=0.70)
    g = gw(c)
    r = run(buy(g, "yes", 2, 0.71))
    body = c.acct.creates()[0]
    assert body["intent"] == "ORDER_INTENT_BUY_LONG" and body["price"] == {"value": "0.71", "currency": "USD"}
    assert body["quantity"] == 2 and body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert body["synchronousExecution"] is True and body["type"] == "ORDER_TYPE_LIMIT"
    assert r["status"] == "executed" and r["filled"] == 2 and r["avg_price"] == 0.70 and r["fees"] == 0.03
    assert r["order_id"] == "ord1" and g.order_id_for("c1") == "ord1" and g.coid_for("ord1") == "c1"


def test_down_buy_is_buy_short_at_one_minus_the_no_price():
    c = client(bid=0.30, ask=0.31)  # NO ask = 1 - 0.30 = 70c
    g = gw(c)
    r = run(buy(g, "no", 1, 0.71))
    body = c.acct.creates()[0]
    assert body["intent"] == "ORDER_INTENT_BUY_SHORT" and body["price"]["value"] == "0.29"
    assert r["filled"] == 1 and r["avg_price"] == 0.70  # back on the NO side
    assert c.acct.pos[SLUG] == -1


def test_take_profit_is_post_only_gtc_without_synchronous_execution():
    c = client(bid=0.69, ask=0.70)
    c.acct.pos[SLUG] = 2
    g = gw(c)
    r = run(sell(g, "yes", 2, 0.99, coid="tp1", tif="good_till_canceled", post_only=True))
    body = c.acct.creates()[0]
    assert body["intent"] == "ORDER_INTENT_SELL_LONG" and body["participateDontInitiate"] is True
    assert body["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL" and "synchronousExecution" not in body
    assert r["status"] == "resting" and r["order_id"]


# -- guards -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    {"ticker": "KXBTC15M-26OCT291215-15"}, {"ticker": "cpc-btc-updown-1h-2026-10-29-1200z"},
    {"count": 1.5}, {"count": 0}, {"price": 0.715}, {"price": 1.0}, {"side": "maybe"}, {"count": 40, "price": 0.70},
])
def test_guards_refuse_before_anything_is_sent(kw):
    c = client()
    g = gw(c)
    args = {"ticker": SLUG, "side": "yes", "action": "buy", "count": 1, "price": 0.70, "client_order_id": "x"}
    args.update(kw)
    with pytest.raises(TradingViolation) as e:
        run(g.place_limit(**args))
    assert e.value.definitive is True
    assert c.acct.calls == []


def test_sell_is_capped_at_the_holding_and_never_opens_a_short():
    c = client(bid=0.60)
    c.acct.pos[SLUG] = 1
    g = gw(c)
    r = run(sell(g, "yes", 3, 0.55))
    assert c.acct.creates()[0]["quantity"] == 1 and r["filled"] == 1 and c.acct.pos[SLUG] == 0
    r2 = run(sell(g, "yes", 1, 0.55, coid="s2"))  # nothing left: not sent at all
    assert r2["not_sent"] and r2["filled"] == 0 and len(c.acct.creates()) == 1


def test_buy_refused_while_holding_the_other_side():
    c = client()
    c.acct.pos[SLUG] = -2  # holds NO
    with pytest.raises(TradingViolation):
        run(buy(gw(c), "yes", 1, 0.71))
    assert c.acct.creates() == []


def test_unreadable_position_refuses_the_order():
    c = client()
    c.acct.positions_error = http_error(InternalServerError, 503)
    with pytest.raises(TradingViolation):
        run(buy(gw(c)))
    assert c.acct.creates() == []


# -- errors -------------------------------------------------------------------------------------------------------
def test_rejections_are_definitive_and_credentials_carry_their_status():
    c = client()
    c.acct.fail_next = http_error(BadRequestError, 400, "ORD_REJECT_REASON_INVALID_PRICE_INCREMENT")
    with pytest.raises(OrderRejected) as e:
        run(buy(gw(c)))
    assert e.value.definitive and e.value.status == 400
    c.acct.fail_next = http_error(AuthenticationError, 401, "bad signature")
    with pytest.raises(OrderRejected) as e:
        run(buy(gw(c), coid="c2"))
    assert e.value.status == 401


def test_a_rejected_execution_is_definitive():
    c = client(bid=0.69, ask=0.70)
    c.acct.pos[SLUG] = 1
    g = PolymarketTrading(c, quote_fn=lambda s: {"yes_bid": 0.55, "yes_ask": 0.56}, clock_ms=Clock(),
                          sleep=_no_sleep)  # our quote says it rests; the exchange (bid 69c) rejects it
    with pytest.raises(OrderRejected):
        run(sell(g, "yes", 1, 0.60, coid="tp", tif="good_till_canceled", post_only=True))


@pytest.mark.parametrize("side,action,price", [("yes", "sell", 0.69), ("yes", "buy", 0.70), ("no", "buy", 0.31),
                                               ("no", "sell", 0.30)])
def test_post_only_that_would_cross_the_live_quote_is_refused_before_sending(side, action, price):
    c = client(bid=0.69, ask=0.70)  # NO bid 30c, NO ask 31c
    c.acct.pos[SLUG] = 2 if side == "yes" else -2
    with pytest.raises(TradingViolation):
        run(c and gw(c).place_limit(ticker=SLUG, side=side, action=action, count=1, price=price,
                                    client_order_id="p", tif="good_till_canceled", post_only=True))
    assert c.acct.creates() == []
    with pytest.raises(TradingViolation):  # and without a live quote it is never sent either
        run(gw(c, quote=False).place_limit(ticker=SLUG, side="yes", action="sell", count=1, price=0.99,
                                           client_order_id="q", tif="good_till_canceled", post_only=True))


@pytest.mark.parametrize("exc", [APIConnectionError(message="reset"), http_error(InternalServerError, 502),
                                 http_error(RateLimitError, 429)])
def test_lost_answers_are_unknown_and_never_retried(exc):
    c = client()
    c.acct.fail_next = exc
    with pytest.raises(OrderOutcomeUnknown) as e:
        run(buy(gw(c)))
    assert e.value.definitive is False
    assert len(c.acct.creates()) == 1


def test_lost_create_found_through_the_activity_trades():
    c = client(ask=0.70)
    clock = Clock()
    g = gw(c, clock=clock)
    c.acct.fail_next = (APIConnectionError(message="timeout"), True)  # placed and filled; answer lost
    with pytest.raises(OrderOutcomeUnknown):
        run(buy(g, count=2))
    assert run(g.find_order(SLUG, "c1", "yes")) is None  # may still be in flight: wait settle_ms
    clock.t += 15_000
    found = run(g.find_order(SLUG, "c1", "yes"))
    assert found["status"] == "executed" and found["filled"] == 2 and found["order_id"].startswith("pm-lost-")
    assert run(g.find_order(SLUG, "c1", "yes")) == found  # stable
    again = run(buy(g, count=2))  # the same client_order_id is never sent twice: it resolves instead
    assert again["duplicate"] is True and again["filled"] == 2 and len(c.acct.creates()) == 1


def test_lost_create_not_placed_stays_none_and_lost_order_reports_no_trade():
    """A list miss is never proof of no fill (B3#2): find_order keeps answering None; lost_order reports that the
    activities hold no trade that could be it (the application then checks the position before closing it)."""
    c = client()
    clock = Clock()
    g = gw(c, clock=clock)
    c.acct.fail_next = (APIConnectionError(message="timeout"), False)
    with pytest.raises(OrderOutcomeUnknown):
        run(buy(g))
    assert run(g.find_order(SLUG, "c1", "yes")) is None  # may still be in flight
    clock.t += 15_000
    assert run(g.find_order(SLUG, "c1", "yes")) is None  # no trade proves it: still unknown, never "canceled"
    got = run(g.lost_order(SLUG, "c1", side="yes", action="buy", since_ms=NOW, count=2, price=0.71))
    assert got["status"] == "not_found" and got["filled"] == 0 and got["proof"] == "activities"


def test_lost_resting_order_found_in_the_open_orders():
    c = client(bid=0.69, ask=0.70)
    c.acct.pos[SLUG] = 2
    g = gw(c)
    c.acct.fail_next = (APIConnectionError(message="timeout"), True)
    with pytest.raises(OrderOutcomeUnknown):
        run(sell(g, "yes", 2, 0.99, coid="tp1", tif="good_till_canceled", post_only=True))
    found = run(g.find_order(SLUG, "tp1", "yes"))
    assert found["status"] == "resting" and found["order_id"] == "ord1" and g.order_id_for("tp1") == "ord1"


def test_restart_finds_orders_through_the_app_rows():
    c = client(ask=0.70)
    run(buy(gw(c)))
    fresh = gw(c, coid_lookup=lambda coid: "ord1" if coid == "c1" else None)
    found = run(fresh.find_order(SLUG, "c1", "yes"))
    assert found["order_id"] == "ord1" and found["filled"] == 2 and found["client_order_id"] == "c1"


# -- cancel, reads ------------------------------------------------------------------------------------------------
def test_cancel_answers_authoritatively():
    c = client(bid=0.69, ask=0.70)
    c.acct.pos[SLUG] = 2
    g = gw(c)
    r = run(sell(g, "yes", 2, 0.99, coid="tp1", tif="good_till_canceled", post_only=True))
    assert run(g.cancel(r["order_id"], SLUG)) == {"order_id": "ord1", "reduced_by": None}
    assert run(g.cancel(r["order_id"], SLUG))["not_found"] is True  # 404: no longer resting
    with pytest.raises(TradingViolation):
        run(g.cancel("ord1", "KXBTC15M-26OCT291215-15"))


def test_cancel_refused_is_authoritative_only_when_the_order_is_finished():
    c = client(bid=0.69, ask=0.70)
    c.acct.pos[SLUG] = 2
    g = gw(c)
    r = run(sell(g, "yes", 2, 0.99, coid="tp1", tif="good_till_canceled", post_only=True))
    c.acct.cancel_error = http_error(BadRequestError, 400, "busy")
    with pytest.raises(BadRequestError):  # still resting: the caller must not sell yet
        run(g.cancel(r["order_id"], SLUG))
    c.acct.orders["ord1"]["state"] = "ORDER_STATE_FILLED"
    assert run(g.cancel(r["order_id"], SLUG))["not_found"] is True


def test_positions_orders_balance_and_stale_quote():
    c = client(bid=0.69, ask=0.70)
    c.acct.pos = {SLUG: 3, "cpc-btc-updown-15m-2026-10-29-1145z": -1}
    g = gw(c)
    assert sorted((p["ticker"], p["position"]) for p in run(g.positions())) == [
        ("cpc-btc-updown-15m-2026-10-29-1145z", -1.0), (SLUG, 3.0)]
    r = run(sell(g, "yes", 3, 0.99, coid="tp1", tif="good_till_canceled", post_only=True))
    rows = run(g.orders(status="resting"))
    assert [(o["order_id"], o["client_order_id"], o["status"], o["price"]) for o in rows] == [
        (r["order_id"], "tp1", "resting", 0.99)]
    assert run(g.orders(status="executed")) == []
    assert run(g.read_balance())[0] == 100.0
    with pytest.raises(RuntimeError):
        run(gw(c, quote=False).quote(SLUG))  # no live WebSocket quote: refused, never the REST BBO
    assert "secret" not in repr(g).lower()
