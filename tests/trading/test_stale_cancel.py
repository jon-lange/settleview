"""Regression: production's single-order GET is stale right after a cancel (2026-10-28 $1 production test).

After the stop cancelled the take profit, GET /portfolio/orders/{id} kept answering status "resting" for 20+ s (and
404 in an earlier test) while the LIST endpoint was already right. The old code waited on that GET, so the stop sale
was never sent. A cancel that answered is authoritative; the sale must go out once, reduce-only, and close the bracket.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from tests.trading.helpers import NOW, TICKER, Exchange, Market, proposal, ready, service

ONE = re.compile(r"^/trade-api/v2/portfolio/orders/([A-Za-z0-9\-_]+)$")
LIST = "/trade-api/v2/portfolio/orders"


def run(coro):
    return asyncio.run(coro)


class StaleAfterCancel(Exchange):
    """`get`: the single-order GET of a cancelled order answers "stale" (status resting) or "404".
    `lst`: the list shows it "ok" (the truth), "stale" (resting) or "missing".
    `cancel404`: the DELETE cancels on the exchange but answers 404."""

    def __init__(self, market, *, get="stale", lst="ok", cancel404=False):
        super().__init__(market)
        self.get, self.lst, self.cancel404 = get, lst, cancel404
        self.cancelled: set[str] = set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        resp = await super().handler(request)
        path = request.url.path
        if request.method == "DELETE":
            oid = path.rsplit("/", 1)[1]
            self.cancelled.add(oid)
            if self.cancel404:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
        if request.method == "GET" and (m := ONE.match(path)) and m.group(1) in self.cancelled:
            if self.get == "404":
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(200, json={"order": {**self.sim._order_obj(self.sim.orders[m.group(1)]),
                                                       "status": "resting"}})
        if request.method == "GET" and path == LIST and self.lst != "ok":
            body = resp.json() if isinstance(resp, httpx.Response) else {}
            rows = []
            for o in body.get("orders", []):
                if o["order_id"] in self.cancelled:
                    if self.lst == "missing":
                        continue
                    o = {**o, "status": "resting"}
                rows.append(o)
            return httpx.Response(200, json={"orders": rows, "cursor": ""})
        return resp


async def stop_once(store, ex: StaleAfterCancel, m: Market):
    svc = await ready(service(store, ex))
    prep = await svc.prepare(proposal(), NOW)
    r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1000)
    assert r["ok"] and r["bracket"]["state"] == "open"
    return svc, r["bracket"]["held"]


@pytest.mark.parametrize("get,lst,cancel404", [
    ("stale", "ok", False),       # the production case: GET says resting, list is right
    ("404", "ok", False),         # GET answers 404 after the cancel
    ("stale", "stale", False),    # list stale too: the fill count comes from the cancel response
    ("stale", "missing", False),  # list does not show it yet: the cancel response again
    ("404", "missing", True),     # cancel answered 404 and nothing shows it: sell what we hold, reduce-only
])
def test_stop_sells_once_after_cancel_despite_stale_order_reads(store, get, lst, cancel404):
    async def go():
        m = Market()
        ex = StaleAfterCancel(m, get=get, lst=lst, cancel404=cancel404)
        svc, held = await stop_once(store, ex, m)
        m.set(0.43, 0.50)  # bid at the 43c stop
        await svc.tick(NOW + 3000)
        w = ex.writes()
        assert [x["method"] for x in w] == ["POST", "POST", "DELETE", "POST"]  # cancel, then the sale at once
        sale = w[3]["body"]
        assert sale["reduce_only"] is True and sale["time_in_force"] == "immediate_or_cancel"
        assert float(sale["count"]) == held
        b = store.select("trading_brackets")[0]
        assert b["state"] == "closed" and b["exit_reason"] == "stop" and b["held"] == 0
        for t in (4000, 5000, 6000):
            await svc.tick(NOW + t)
        assert len(ex.writes()) == 4 and ex.sim.positions[TICKER] == 0
        ev = [a["event"] for a in svc.audit()]
        assert ev.count("stop_sent") == 1 and "cancelled" in ev
    run(go())


def test_uncertain_tp_fills_still_sell_reduce_only_and_reconcile_the_rest(store):
    """The take profit filled 2 contracts we never heard about; the cancel answered 404 and the list does not show
    the order. The stop sells `held` reduce-only (Kalshi caps it at the real position), then reads the position."""
    async def go():
        m = Market()
        ex = StaleAfterCancel(m, get="404", lst="missing", cancel404=True)
        svc, held = await stop_once(store, ex, m)
        m.set(0.72, 0.25, size=2.0)
        await ex.sim._advance(TICKER)  # 2 filled on the exchange, unseen by the app
        bid = store.select("trading_brackets")[0]["id"]
        svc._last_poll[bid] = NOW + 3000  # (no routine poll before the stop, so the app never learns of them)
        m.set(0.43, 0.50)
        await svc.tick(NOW + 3000)
        w = ex.writes()
        assert [x["method"] for x in w] == ["POST", "POST", "DELETE", "POST"]
        assert w[3]["body"]["reduce_only"] is True and float(w[3]["body"]["count"]) == held
        b = store.select("trading_brackets")[0]
        assert b["state"] == "closed" and b["held"] == 0 and ex.sim.positions[TICKER] == 0
        await svc.tick(NOW + 4000)
        assert len(ex.writes()) == 4
    run(go())


class FailingCancel(StaleAfterCancel):
    async def handler(self, request):
        if request.method == "DELETE":
            raise httpx.ConnectError("down", request=request)
        return await super().handler(request)


def test_a_cancel_that_fails_is_audited_not_silent(store):
    async def go():
        m = Market()
        ex = FailingCancel(m)
        svc, held = await stop_once(store, ex, m)
        m.set(0.43, 0.50)
        await svc.tick(NOW + 3000)
        errs = [a for a in svc.audit() if a["event"] == "error"]
        assert any(a["code"] == "cancel_failed" for a in errs)
        assert store.select("trading_brackets")[0]["state"] == "stopping"
    run(go())
