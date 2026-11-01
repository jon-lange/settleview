"""Changing an open bracket's resting take profit (the phone's "auto-sell"; 99c = ride): the old TP is cancelled with
the monitor's own path (fills learnt first), then the new price rests post-only; an unconfirmed cancel changes
nothing; a price at or under the bid, a closed bracket or another strategy's bracket is refused; the index stop keeps
working after a change. Simulated exchange only."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from tests.trading.helpers import NOW, TICKER, Exchange
from tests.trading.test_index_stop import K, Index, fav_market, pnw
from tests.trading.test_strategy_scope import enter, svc_for, up


def run(coro):
    return asyncio.run(coro)


async def opened(store, idx: Index | None = None):
    m = fav_market()  # YES 69c / 70c
    ex = Exchange(m)
    svc = await up(svc_for(store, ex, "you", index_price_fn=idx or Index()))
    r = await enter(svc, pnw(take_profit=0.99))
    assert r["ok"] and r["bracket"]["state"] == "open", r
    return svc, ex, m, r["bracket"]


def _row(store) -> dict:
    return store.select("trading_brackets")[0]


def test_change_99_to_80_cancels_the_old_tp_and_rests_a_new_one(store):
    async def go():
        svc, ex, m, b = await opened(store)
        r = await svc.set_take_profit(b["id"], 0.80, NOW + 2000)
        assert r["ok"], r
        w = ex.writes()
        assert [x["method"] for x in w] == ["POST", "POST", "DELETE", "POST"]
        assert w[1]["body"]["price"] == "0.9900"
        new = w[3]["body"]
        assert new["side"] == "ask" and new["price"] == "0.8000" and new["post_only"] is True
        assert new["time_in_force"] == "good_till_canceled" and float(new["count"]) == b["held"]
        row = _row(store)
        assert row["take_profit"] == 0.80 and row["tp_order_id"] and row["state"] == "open"
        ch = [a for a in svc.audit() if a["event"] == "tp_changed"]
        assert len(ch) == 1 and ch[0]["detail"] == {"from": 0.99, "to": 0.80}
        assert (await svc.set_take_profit(b["id"], 0.80, NOW + 2500))["ok"]  # same price: nothing sent
        assert len(ex.writes()) == 4
    run(go())


def test_a_partial_fill_on_the_old_tp_is_booked_at_the_old_price_before_the_new_one(store):
    async def go():
        svc, ex, m, b = await opened(store)
        held = b["held"]
        assert (await svc.set_take_profit(b["id"], 0.80, NOW + 1500))["ok"]
        m.set(0.81, 0.18, depth=([(0.81, 1.0)], [(0.18, 100.0)]))  # 1 contract bid through our 80c ask
        await svc.tick(NOW + 4000)
        row = _row(store)
        assert row["tp_filled"] == 1.0 and row["held"] == round(held - 1.0, 2) and row["proceeds"] == 0.80
        m.set(0.72, 0.27)
        r = await svc.set_take_profit(b["id"], 0.85, NOW + 5000)
        assert r["ok"], r
        # Every fill the exchange reports on the old 80c order (incl. any learnt at the cancel) is booked at 80c
        # before the new order is sized: it sells exactly what is still held.
        sold = sum(f["count"] for f in ex.sim.fills if f["client_order_id"].endswith("-tp2"))
        row = _row(store)
        assert sold >= 1.0 and row["tp_filled"] == sold and row["held"] == round(held - sold, 2)
        assert row["proceeds"] == round(0.80 * sold, 4) and row["take_profit"] == 0.85
        new = ex.writes()[-1]["body"]
        assert new["price"] == "0.8500" and float(new["count"]) == row["held"]
    run(go())


def test_a_failed_or_unknown_cancel_keeps_the_old_tp_and_never_double_places(store):
    async def go():
        svc, ex, m, b = await opened(store)
        before = _row(store)
        orig = ex.sim.handle_async_request
        for failure in ("500", "timeout"):
            async def broken(request, failure=failure):
                if request.method == "DELETE":
                    if failure == "timeout":
                        raise httpx.ReadTimeout("lost", request=request)
                    return httpx.Response(500)
                return await orig(request)
            ex.sim.handle_async_request = broken
            r = await svc.set_take_profit(b["id"], 0.80, NOW + 2000)
            assert not r["ok"] and r["code"] == "cancel_failed" and "old one is still working" in r["reason"]
            row = _row(store)
            assert row["take_profit"] == 0.99 and row["tp_order_id"] == before["tp_order_id"]
            assert [x["method"] for x in ex.writes()].count("POST") == 2  # entry + the original TP only
        assert not [a for a in svc.audit() if a["event"] == "tp_changed"]
    run(go())


def test_price_at_or_under_the_bid_or_entry_is_refused(store):
    async def go():
        svc, ex, m, b = await opened(store)
        m.set(0.85, 0.12)
        r = await svc.set_take_profit(b["id"], 0.85, NOW + 2000)
        assert not r["ok"] and r["code"] == "at_bid" and r["reason"].startswith("The bid is already 85¢")
        r = await svc.set_take_profit(b["id"], b["entry_avg"], NOW + 2000)
        assert not r["ok"] and r["code"] == "invalid"
        r = await svc.set_take_profit(b["id"], 1.0, NOW + 2000)
        assert not r["ok"] and r["code"] == "invalid"
        assert len(ex.writes()) == 2 and _row(store)["take_profit"] == 0.99
    run(go())


def test_wrong_strategy_or_closed_bracket_is_refused_over_http(store):
    svc, ex, m, b = run(opened(store))
    other = run(up(svc_for(store, Exchange(fav_market()), "you_poly")))
    app = FastAPI()
    register_trading_routes(app, {"you_poly": other, "you": svc}, lambda: NOW + 2000)
    c = TestClient(app)
    body = {"bracket_id": b["id"], "price": 0.80}
    assert c.post("/api/trading/take_profit", json={**body, "strategy": "nope"}).json()["code"] == "unknown_strategy"
    assert c.post("/api/trading/take_profit", json={**body, "strategy": "you_poly"}).json()["code"] == "not_found"
    assert c.post("/api/trading/take_profit", json={**body, "price": 1.0, "strategy": "you"}).status_code == 422
    assert c.get("/api/trading/take_profit").status_code == 405  # POST only
    m.set(0.30, 0.65)
    px = run(svc.prepare_exit(b["id"], NOW + 2000))
    assert run(svc.approve(px["token"], "yes", px["ticket"], NOW + 2100))["ok"]
    assert _row(store)["state"] == "closed"
    r = c.post("/api/trading/take_profit", json={**body, "strategy": "you"}).json()
    assert not r["ok"] and r["code"] == "not_found"


def test_the_index_stop_still_fires_after_a_change(store):
    async def go():
        idx = Index()
        svc, ex, m, b = await opened(store, idx)
        assert (await svc.set_take_profit(b["id"], 0.80, NOW + 2000))["ok"]
        idx.set(K + 20, NOW + 2500)  # back to the stop level
        await svc.tick(NOW + 3000)
        w = ex.writes()
        assert [x["method"] for x in w] == ["POST", "POST", "DELETE", "POST", "DELETE", "POST"]
        assert w[5]["body"]["reduce_only"] is True and w[5]["body"]["time_in_force"] == "immediate_or_cancel"
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "stop" and ex.sim.positions[TICKER] == 0
    run(go())
