"""Lost-response orders on the Kalshi path (review fix plan 2026-10-31, P0 #2 and #3; domain/order_resolution.py).

An order whose answer was lost stays unknown until fills prove its outcome; a list miss is never proof of no fill; a
recovered entry is booked at its real average price; exits are checked against the bracket's own environment.
Exchange = the in-memory simulated Kalshi book behind an httpx.MockTransport (tests/trading/helpers.py); no network."""

from __future__ import annotations

import asyncio

import httpx

from btc_signal_engine.adapters.kalshi.trading import OrderRejected, OrderUnknown
from btc_signal_engine.domain import order_resolution as orr
from tests.trading.helpers import NOW, TICKER, Exchange, Market, gateway, proposal, ready, service

PREFIX = "/trade-api/v2"
CREATE = PREFIX + "/portfolio/events/orders"


def run(coro):
    return asyncio.run(coro)


class LostAnswers(Exchange):
    """Every create reaches the exchange (and fills there) but its answer is lost. `hide_lists`: the order lists do
    not show our orders (they lag); `hide_fills`: nor does the fills list."""

    def __init__(self, market: Market, *, hide_lists: bool = True, hide_fills: bool = False) -> None:
        super().__init__(market)
        self.hide_lists, self.hide_fills = hide_lists, hide_fills
        self.lose = True

    async def handler(self, request: httpx.Request) -> httpx.Response:
        resp = await super().handler(request)
        path = request.url.path
        if self.lose and request.method == "POST" and path == CREATE:
            raise httpx.ReadTimeout("lost", request=request)
        if request.method == "GET" and ((self.hide_lists and path == PREFIX + "/portfolio/orders")
                                        or (self.hide_fills and path == PREFIX + "/portfolio/fills")):
            await resp.aread()
            key = "orders" if path.endswith("orders") else "fills"
            return httpx.Response(200, json={key: [], "cursor": ""})
        return resp


def two_level_market() -> Market:
    """YES asks 0.56 x 3 then 0.58 x 100 (YES ask = 1 - NO bid): a 5 $ IOC buy at 0.58 sweeps both levels."""
    m = Market()
    m.set(0, 0, depth=([(0.55, 100.0)], [(0.44, 3.0), (0.42, 100.0)]))
    return m


async def lost_entry(store, ex: Exchange, **prop):
    svc = await ready(service(store, ex))
    prep = await svc.prepare(proposal(**prop), NOW)
    assert prep["ok"], prep
    r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
    assert r["code"] == "unknown", r
    return svc, prep


def entry_fill(ex: Exchange) -> tuple[float, float]:
    """(contracts, average price) the simulated exchange really filled for our entry."""
    fills = [f for f in ex.sim.fills if f["book_side"] == "bid"]
    n = sum(f["count"] for f in fills)
    return round(n, 2), round(sum(f["count"] * f["yes_price"] for f in fills) / n, 4)


# -- B1#4: a recovered entry is booked at its average fill price, not its limit ------------------------------------
def test_recovered_entry_is_booked_at_its_average_fill_price_not_the_limit(store):
    async def go():
        ex = LostAnswers(two_level_market(), hide_lists=False)
        svc, prep = await lost_entry(store, ex)
        ex.lose = False
        await svc.tick(NOW + 2_000)  # find_order lists it now
        b = store.select("trading_brackets")[0]
        n, avg = entry_fill(ex)
        assert b["state"] == "open" and b["held"] == n
        assert avg < prep["ticket"]["limit_price"]  # two levels: the average is below the 58c limit
        assert b["entry_avg"] == avg  # main booked it at the limit (avg_price None)
        fee = sum(f["fee"] for f in ex.sim.fills if f["book_side"] == "bid")
        assert abs(b["entry_cost"] - round(n * avg + b["entry_fees"], 4)) < 1e-9
        assert abs(b["entry_fees"] - fee) < 0.0002
        assert b["tp_order_id"]  # its take profit went on
    run(go())


# -- B1#5: a list miss is never proof of no fill ----------------------------------------------------------------------
def test_lost_entry_the_lists_never_show_is_found_from_the_fills_after_60_s(store):
    async def go():
        ex = LostAnswers(Market())
        svc, prep = await lost_entry(store, ex)
        ex.lose = False
        await svc.tick(NOW + 30_000)
        assert store.select("trading_brackets")[0]["state"] == "unknown"  # the lists may lag: no guess
        await svc.tick(NOW + 61_000)
        b = store.select("trading_brackets")[0]
        n, avg = entry_fill(ex)
        assert b["state"] == "open" and b["held"] == n and b["entry_avg"] == avg  # main: closed "not_found"
        assert b["tp_order_id"]  # protected like any bracket: take profit placed, the stop watched
        assert any(a["code"] == "lost_entry_found" for a in store.select("trading_audit"))
        assert len([o for o in ex.sim.orders.values() if o["book_side"] == "bid"]) == 1  # never re-sent
    run(go())


def test_lost_entry_with_an_unexplained_position_stays_unknown_committed_and_warned(store):
    async def go():
        ex = LostAnswers(Market(), hide_fills=True)  # neither the lists nor the fills show it, the position does
        svc, prep = await lost_entry(store, ex)
        ex.lose = False
        for t in (61_000, 75_000, 200_000):
            await svc.tick(NOW + t)
        b = store.select("trading_brackets")[0]
        assert b["state"] == "unknown" and b["exit_reason"] is None  # main: closed "not_found" at 61 s
        assert svc.exposure("prod", NOW + 200_000)["day"] == prep["ticket"]["max_cost"]  # risk still committed
        warn = [w for w in svc.settle_warnings(NOW + 200_000) if w.get("kind") == "unresolved_order"]
        assert len(warn) == 1 and "could not confirm" in warn[0]["message"]
        assert len([a for a in store.select("trading_audit") if a["event"] == "order_unresolved"]) == 1  # once
        p2 = await svc.prepare(proposal(stake=1.0), NOW + 200_000)
        assert not p2["ok"]  # nothing else goes on while it is unknown
        ex.hide_fills = False  # the fills catch up: resolved, warning gone
        await svc.tick(NOW + 211_000)
        b = store.select("trading_brackets")[0]
        assert b["state"] == "open" and b["held"] == entry_fill(ex)[0]
        assert not [w for w in svc.settle_warnings(NOW + 211_000) if w.get("kind") == "unresolved_order"]
    run(go())


def test_reconcile_counts_a_position_in_a_market_whose_only_bracket_bought_nothing_as_external(store):
    class Refuses(Exchange):
        async def handler(self, request):
            if request.method == "POST" and request.url.path == CREATE:
                self.seen.append({"method": "POST", "path": request.url.path, "body": None, "query": ""})
                return httpx.Response(400, json={"error": {"code": "invalid_order"}})
            return await super().handler(request)

    async def go():
        ex = Refuses(Market())
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        assert (await svc.approve(prep["token"], "yes", prep["ticket"], NOW))["code"] == "rejected"
        ex.sim.positions[TICKER] = 3.0  # contracts bought outside the app in that market
        out = await svc.reconcile("prod", NOW + 1_000)
        assert [p["ticker"] for p in out["external"]["positions"]] == [TICKER]  # main: hidden as "ours"
    run(go())


# -- B3#14: Kalshi pagination and non-definitive 4xx ------------------------------------------------------------------
class Paged(Exchange):
    """Positions, fills and order lists answered one row per page with a cursor."""

    async def handler(self, request: httpx.Request) -> httpx.Response:
        resp = await super().handler(request)
        path = request.url.path
        keys = {PREFIX + "/portfolio/positions": "market_positions", PREFIX + "/portfolio/fills": "fills",
                PREFIX + "/portfolio/orders": "orders"}
        if request.method != "GET" or path not in keys:
            return resp
        await resp.aread()
        rows = resp.json()[keys[path]]
        start = int(dict(httpx.QueryParams(request.url.query)).get("cursor") or 0)
        nxt = str(start + 1) if start + 1 < len(rows) else ""
        return httpx.Response(200, json={keys[path]: rows[start:start + 1], "cursor": nxt})


def test_kalshi_lists_are_read_across_every_cursor_page(store):
    async def go():
        ex = Paged(Market())
        gw = gateway(ex)
        ex.sim.positions.update({TICKER: 2.0, "KXBTC15M-26OCT281330-30": -1.0})
        assert {p["ticker"] for p in await gw.positions()} == {TICKER, "KXBTC15M-26OCT281330-30"}  # main: one page
        for i in range(3):
            await gw.place_limit(ticker=TICKER, side="yes", action="buy", count=1, price=0.58,
                                 client_order_id=f"c{i}")
        assert len(await gw.market_fills(TICKER, NOW - 1_000)) == 3
        assert (await gw.find_order(TICKER, "c2", "yes"))["client_order_id"] == "c2"
    run(go())


def test_kalshi_create_answered_408_is_unknown_never_a_rejection(store):
    class Answers(Exchange):
        def __init__(self, market, status):
            super().__init__(market)
            self.status = status

        async def handler(self, request):
            if request.method == "POST":
                return httpx.Response(self.status, json={"error": {"code": "x"}})
            return await super().handler(request)

    async def go():
        for status, exc in ((408, OrderUnknown), (499, OrderUnknown), (400, OrderRejected), (422, OrderRejected)):
            gw = gateway(Answers(Market(), status))
            try:
                await gw.place_limit(ticker=TICKER, side="yes", action="buy", count=1, price=0.58, client_order_id="x")
            except Exception as e:  # noqa: BLE001
                assert type(e) is exc and e.definitive is (exc is OrderRejected), (status, e)
            else:
                raise AssertionError(f"HTTP {status} did not raise")
    run(go())


# -- B1#3: exits after a mode change -----------------------------------------------------------------------------
def test_a_prod_position_can_still_be_sold_after_the_mode_drops_to_sandbox(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex, env_gateways={"prod": gateway(ex), "demo": None}))
        prep = await svc.prepare(proposal(), NOW)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        assert r["ok"] and r["bracket"]["state"] == "open"
        await svc.set_mode("sandbox", NOW + 500)  # e.g. after a credential problem
        assert svc.mode == "sandbox"
        e = await svc.prepare_exit(r["bracket"]["id"], NOW + 1_000)
        assert e["ok"] and e["ticket"]["mode"] == "prod" and e["ticket"]["env"] == "prod"
        out = await svc.approve(e["token"], "yes", e["ticket"], NOW + 1_500)
        assert out.get("ok"), out  # main: refused "mode_changed"
        assert store.select("trading_brackets")[0]["exit_reason"] == "exit"
        p2 = await svc.prepare(proposal(), NOW + 2_000)  # entries still follow the current mode
        assert p2.get("ticket", {}).get("env") != "prod"
    run(go())


def test_the_rule_helpers():
    assert orr.decide(None, 0.0) == orr.UNKNOWN
    assert orr.decide({"filled": 2}, None) == orr.FILLED
    assert orr.decide({"filled": 0}, None) == orr.UNKNOWN  # no position read: not proven
    assert orr.decide({"filled": 0}, 1.0) == orr.UNKNOWN  # contracts nobody explains
    assert orr.decide({"filled": 0}, 0.0) == orr.NOT_FILLED
    fills = [("o1", 1000, "bid"), ("o2", 5000, "ask"), ("o3", 9000, "bid"), ("o1", 9500, "bid"), (None, 9600, "bid")]
    assert orr.candidate_orders(fills, since_ms=4000, book_side="bid", known={"o9"}) == ["o3", "o1"]  # o1 filled again
    assert orr.candidate_orders(fills, since_ms=1500, book_side="bid") == ["o1", "o3"]  # 1 s slack
    assert [s for s in (400, 401, 403, 404, 408, 409, 422, 425, 499) if orr.definitive_status(s)] == [400, 401, 403,
                                                                                                   404, 422]
