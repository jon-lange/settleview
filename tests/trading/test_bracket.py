"""One Yes approves the whole bracket: entry -> take profit (resting maker sell) -> stop (app-watched).

Exchange = httpx.MockTransport around the in-memory simulated book (tests/trading/helpers.py); no network."""

from __future__ import annotations

import asyncio

import httpx

from btc_signal_engine.domain.coach.money import position_for_stake
from tests.trading.helpers import CLOSE, NOW, TICKER, Exchange, Market, proposal, ready, service

PREFIX = "/trade-api/v2"


def run(coro):
    return asyncio.run(coro)


async def opened(store, m: Market, ex: Exchange | None = None, **kw):
    ex = ex or Exchange(m)
    svc = await ready(service(store, ex, **kw))
    prep = await svc.prepare(proposal(), NOW)
    assert prep["ok"], prep
    r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1000)
    return svc, ex, prep, r


def test_happy_path_entry_fill_tp_rests_then_fills_and_ends_the_stop(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        c = position_for_stake(5.0, 0.58)
        assert r["ok"] and r["bracket"]["state"] == "open" and r["bracket"]["held"] == c
        entry, tp = ex.posts()
        assert entry == {"ticker": TICKER, "client_order_id": entry["client_order_id"], "side": "bid",
                         "count": f"{c:.2f}", "price": "0.5800", "time_in_force": "immediate_or_cancel",
                         "self_trade_prevention_type": "taker_at_cross", "post_only": False, "reduce_only": False,
                         "cancel_order_on_pause": False}
        assert tp["side"] == "ask" and tp["price"] == "0.7000" and tp["count"] == f"{c:.2f}"
        assert tp["time_in_force"] == "good_till_canceled" and tp["post_only"] is True
        # Not reduce-only: Kalshi production rejects reduce_only on resting orders ("reduce_only can only be used
        # with IoC orders", penny-tested 2026-10-28). The stop cancels the TP before its reduce-only IOC sale.
        assert tp["reduce_only"] is False
        await svc.tick(NOW + 3000)
        assert len(ex.writes()) == 2  # nothing new while the bid is between stop and take profit
        m.set(0.72, 0.25)  # the real bid trades through 70c
        await svc.tick(NOW + 6000)
        b = store.select("trading_brackets")[0]
        assert b["state"] == "closed" and b["exit_reason"] == "tp" and b["held"] == 0 and b["tp_filled"] == c
        m.set(0.30, 0.65)  # would be a stop, but the bracket is done: one-cancels-other
        await svc.tick(NOW + 9000)
        assert len(ex.writes()) == 2
        events = [a["event"] for a in reversed(svc.audit())]
        for e in ("proposed", "approved", "sent", "filled", "tp_placed", "tp_filled"):
            assert e in events
    run(go())


def test_stop_cancels_tp_then_sells_with_slippage_cap_and_closes(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        held = r["bracket"]["held"]
        m.set(0.43, 0.50)  # bid at the 43c stop
        await svc.tick(NOW + 3000)
        w = ex.writes()
        assert [x["method"] for x in w] == ["POST", "POST", "DELETE", "POST"]  # TP cancelled BEFORE the sale
        sale = w[3]["body"]
        assert sale["side"] == "ask" and sale["price"] == "0.3800" and sale["reduce_only"] is True
        assert sale["time_in_force"] == "immediate_or_cancel" and float(sale["count"]) == held
        b = store.select("trading_brackets")[0]
        assert b["state"] == "closed" and b["exit_reason"] == "stop" and b["held"] == 0
        assert b["stop_trigger_bid"] == 0.43 and b["exit_avg"] == 0.43
        ev = [a["event"] for a in svc.audit()]
        assert {"stop_triggered", "cancelled", "stop_sent", "closed"} <= set(ev)
        assert ex.sim.positions[TICKER] == 0  # never sold more than held
    run(go())


def test_down_bracket_maps_to_the_no_leg(store):
    async def go():
        m = Market(yes_bid=0.55, no_bid=0.40)  # NO ask = 1 - 0.55 = 45c
        ex = Exchange(m)
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(side="DOWN", max_price=0.45, take_profit=0.60, stop=0.30), NOW)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        assert r["bracket"]["state"] == "open"
        entry, tp = ex.posts()
        assert entry["side"] == "ask" and entry["price"] == "0.5500"  # buy NO @45c == sell YES @55c
        assert tp["side"] == "bid" and tp["price"] == "0.4000"  # sell NO @60c == buy YES @40c
        assert ex.sim.positions[TICKER] < 0
    run(go())


def test_partial_entry_fill_sizes_take_profit_to_what_filled(store):
    async def go():
        m = Market(no_bid=0.42, size=3.0)  # only 3 contracts offered at 58c
        svc, ex, prep, r = await opened(store, m)
        assert r["bracket"]["held"] == 3.0
        assert ex.posts()[1]["count"] == "3.00"
        assert "partial" in [a["event"] for a in svc.audit()]
    run(go())


def test_nothing_offered_closes_the_bracket_unfilled(store):
    async def go():
        m = Market(no_bid=0.40)  # ask 60c > 58c... but the gate refuses that; empty the book instead
        m.book["no"] = [(0.42, 0.0)]
        ex = Exchange(m)
        svc = await ready(service(store, ex))
        m.book["no"] = [(0.42, 50.0)]
        prep = await svc.prepare(proposal(), NOW)
        m.book["no"] = [(0.42, 0.001)]  # quote still shows 58c but no size
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        assert r["bracket"]["state"] == "closed" and r["bracket"]["exit_reason"] == "unfilled"
        assert len(ex.posts()) == 1  # no take profit for nothing
    run(go())


def test_retry_reuses_client_order_id_and_never_double_places(store):
    async def go():
        m = Market()
        ex = Exchange(m, drop_first_post_response=True)
        svc, ex, prep, r = await opened(store, m, ex)
        posts = ex.posts()
        assert posts[0]["client_order_id"] == posts[1]["client_order_id"]  # the retry
        entries = [o for o in ex.sim.orders.values() if o["book_side"] == "bid"]
        assert len(entries) == 1 and r["bracket"]["state"] == "open"
    run(go())


class LossyExchange(Exchange):
    """Every POST reaches the exchange but every response is lost (the outcome is unknown to the app)."""

    async def handler(self, request: httpx.Request) -> httpx.Response:
        resp = await super().handler(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("lost", request=request)
        return resp


def test_unknown_outcome_blocks_everything_then_restart_reconciles(store):
    async def go():
        m = Market()
        ex = LossyExchange(m)
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        assert r["code"] == "unknown"
        assert store.select("trading_brackets")[0]["state"] == "unknown"
        p2 = await svc.prepare(proposal(stake=1.0), NOW)
        assert p2["code"] == "max_open"  # an unknown order counts as an open position
        # restart with a healthy connection to the same exchange
        good = Exchange(m)
        good.sim = ex.sim
        await ready(service(store, good))
        b = store.select("trading_brackets")[0]
        assert b["state"] == "open" and b["held"] == position_for_stake(5.0, 0.58)
        assert b["tp_order_id"]  # take profit placed after reconciliation
        assert len([o for o in ex.sim.orders.values() if o["book_side"] == "bid"]) == 1
    run(go())


def test_restart_rebuilds_open_bracket_and_keeps_watching(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        svc2 = service(store, ex)
        assert (await svc2.prepare(proposal(), NOW))["code"] == "not_reconciled"
        await ready(svc2)
        assert list(svc2._brackets) == [r["bracket"]["id"]]
        m.set(0.43, 0.50)
        await svc2.tick(NOW + 5000)
        assert store.select("trading_brackets")[0]["exit_reason"] == "stop"
    run(go())


def test_reconcile_trims_held_to_the_exchange_position(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        ex.sim.positions[TICKER] = 2.0  # e.g. sold by hand on kalshi.com
        await ready(service(store, ex))
        assert store.select("trading_brackets")[0]["held"] == 2.0
    run(go())


def test_window_close_holds_to_settlement_without_selling(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        n = len(ex.writes())
        await svc.tick(CLOSE)
        b = store.select("trading_brackets")[0]
        assert b["state"] == "settling" and b["exit_reason"] is None and len(ex.writes()) == n
        m.settle("yes")
        await svc.tick(CLOSE + 15_000)
        b = store.select("trading_brackets")[0]
        assert b["state"] == "closed" and b["exit_reason"] == "settled" and len(ex.writes()) == n
        assert b["proceeds"] == b["held"] > 0  # an UP bracket on a YES result: $1 a contract
    run(go())


def test_last_minute_does_not_auto_exit(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        n = len(ex.writes())
        await svc.tick(CLOSE - 30_000)  # time rule window: an unanswered card keeps the bracket
        assert len(ex.writes()) == n and store.select("trading_brackets")[0]["state"] == "open"
    run(go())


def test_discretionary_exit_card_sells_after_its_own_yes(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        m.set(0.62, 0.36)
        e = await svc.prepare_exit(r["bracket"]["id"], NOW + 2000)
        assert e["ok"] and e["ticket"]["limit_price"] == 0.62
        assert (await svc.approve(e["token"], "no", e["ticket"], NOW + 2500))["declined"]
        assert len(ex.writes()) == 2  # No places nothing
        e = await svc.prepare_exit(r["bracket"]["id"], NOW + 3000)
        out = await svc.approve(e["token"], "yes", e["ticket"], NOW + 3500)
        assert out["bracket"]["state"] == "closed" and out["bracket"]["exit_reason"] == "exit"
        assert [w["method"] for w in ex.writes()][-2:] == ["DELETE", "POST"]
    run(go())


def test_tp_cancel_failure_never_sells(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        orig = ex.sim.handle_async_request

        async def broken(request):
            if request.method == "DELETE":
                return httpx.Response(500)
            return await orig(request)
        ex.sim.handle_async_request = broken
        m.set(0.40, 0.55)
        await svc.tick(NOW + 3000)
        assert [w["method"] for w in ex.writes()].count("POST") == 2  # no stop sale while the TP may still fill
        assert store.select("trading_brackets")[0]["state"] == "stopping"
        ex.sim.handle_async_request = orig
        await svc.tick(NOW + 4000)
        assert store.select("trading_brackets")[0]["exit_reason"] == "stop"
    run(go())


class ListLagExchange(Exchange):
    """The entry's first answer is lost and its retry is answered 409 order_already_exists, while the order lists
    (and, with `hide_fills`, the fills list) hide it (as the demo exchange's lists lag a placement) until `hide` is
    cleared."""

    def __init__(self, market: Market, hide_fills: bool = True) -> None:
        super().__init__(market, drop_first_post_response=True)
        self.hide, self.hide_fills = True, hide_fills

    async def handler(self, request: httpx.Request) -> httpx.Response:
        resp = await super().handler(request)
        if self.hide and request.method == "GET" and request.url.path == PREFIX + "/portfolio/orders":
            await resp.aread()
            return httpx.Response(200, json={"orders": []})
        if self.hide and self.hide_fills and request.method == "GET" and request.url.path == PREFIX + "/portfolio/fills":
            await resp.aread()
            return httpx.Response(200, json={"fills": []})
        return resp


def test_a_409_duplicate_entry_stays_pending_past_60_s_until_the_order_is_found(store):
    """Kalshi said the client_order_id exists: never closed as not_found on a timer (unlike a plain lost answer)."""
    async def go():
        m = Market()
        ex = ListLagExchange(m)
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        assert r["code"] == "unknown"
        assert [a["code"] for a in store.select("trading_audit") if a["code"] == "unknown_duplicate"]
        await svc.tick(NOW + 120_000)  # well past the 60 s rule
        b = store.select("trading_brackets")[0]
        assert b["state"] == "unknown" and b["exit_reason"] is None  # still pending, visible, not closed
        assert len([o for o in ex.sim.orders.values() if o["book_side"] == "bid"]) == 1  # one order, never re-sent
        ex.hide = False  # the lists catch up
        await svc.tick(NOW + 125_000)
        b = store.select("trading_brackets")[0]
        assert b["state"] == "open" and b["held"] == position_for_stake(5.0, 0.58)
    run(go())


def test_a_lost_entry_with_no_409_still_closes_as_not_found_after_60_s(store):
    class NeverArrives(Exchange):
        async def handler(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == PREFIX + "/portfolio/events/orders":
                raise httpx.ConnectError("down", request=request)  # never reached the exchange
            return await super().handler(request)

    async def go():
        m = Market()
        ex = NeverArrives(m)
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        assert (await svc.approve(prep["token"], "yes", prep["ticket"], NOW))["code"] == "unknown"
        await svc.tick(NOW + 61_000)
        b = store.select("trading_brackets")[0]
        assert b["state"] == "closed" and b["exit_reason"] == "not_found"
    run(go())
