"""Exits (and additions) made by hand in the exchanges' own apps while a bracket is open (2026-10-29: the user sold two
positions by hand, on Kalshi and on Polymarket; both brackets stayed open with their take profits resting).

Detected from the exchange's raw positions after a grace period with none of our own orders in flight and no fill of
ours on the market in the last 10 s (Polymarket's list lags our own fills: that must NOT trigger). Response: cancel
our resting take profit, reduce held to the exchange's quantity, close with exit_reason "manual" at 0, P&L from the
exchange's fills when they cover it (else estimated from the bid, and labelled so), audit `manual_exit`, never an
order. A manual addition is only audited. Both venues."""

from __future__ import annotations

import asyncio

import httpx

from btc_signal_engine.application.trading import TradingService
from tests.polymarket_us.fakes import SLUG
from tests.polymarket_us.test_poly_trading import K, book, enter, prod_rig, up_bracket
from tests.trading.helpers import NOW, TICKER, Exchange, iso
from tests.trading.test_index_stop import Index as KIndex, fav_market, pnw
from tests.trading.test_strategy_scope import enter as kenter, svc_for, up


def run(coro):
    return asyncio.run(coro)


def audits(store, strategy: str, event: str) -> list[dict]:
    return store.select("trading_audit", {"strategy": strategy, "event": event})


# -- Kalshi ---------------------------------------------------------------------------------------------------------
class KExchange(Exchange):
    """The simulated Kalshi exchange plus a controllable /portfolio/fills (the account's fills, incl. manual ones)."""

    def __init__(self, market):
        super().__init__(market)
        self.fills: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/portfolio/fills"):
            self.seen.append({"method": "GET", "path": request.url.path, "body": None,
                              "query": request.url.query.decode()})
            return httpx.Response(200, json={"fills": self.fills, "cursor": ""})
        return await super().handler(request)


async def k_open(store, ex: KExchange):
    svc = await up(svc_for(store, ex, "you", index_price_fn=KIndex()))
    r = await kenter(svc, pnw())
    assert r["ok"] and r["bracket"]["state"] == "open"
    return svc, r["bracket"]


async def k_ticks(svc: TradingService, times: list[int]) -> None:
    for t in times:
        await svc.tick(t)


def test_kalshi_manual_full_exit_cancels_tp_and_closes_with_fill_prices(store):
    async def go():
        ex = KExchange(fav_market())
        svc, b = await k_open(store, ex)
        held = b["held"]
        ex.sim.positions[TICKER] = 0.0  # sold by hand in the Kalshi app
        ex.fills = [{"fill_id": "f-manual", "order_id": "someone-else", "market_ticker": TICKER, "book_side": "ask",
                     "outcome_side": "yes", "side": "yes", "count_fp": f"{held:.2f}", "yes_price_dollars": "0.8000",
                     "fee_cost": "0.0500", "created_time": iso(NOW + 2_000)}]
        posts_before = len(ex.posts())
        await k_ticks(svc, [NOW + 1_000, NOW + 11_000, NOW + 16_000])
        assert store.select("trading_brackets")[0]["state"] == "open"  # not yet: must persist 10 s
        await svc.tick(NOW + 21_000)
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "manual" and row["held"] == 0
        assert row["exit_avg"] == 0.80 and row["exit_fees"] == 0.05 and row["proceeds"] == round(0.8 * held, 4)
        assert len(ex.posts()) == posts_before  # never an order in response
        assert any(w["method"] == "DELETE" for w in ex.writes())  # the take profit was cancelled
        assert all(o["status"] != "resting" for o in ex.sim.orders.values())
        a = audits(store, "you", "manual_exit")
        assert len(a) == 1 and a[0]["detail"]["pnl_source"] == "fills" and a[0]["detail"]["sold"] == held
        t = svc.trades(0, NOW + 21_000)[0]
        assert t["exit"] == "manual" and t["pnl_source"] == "fills" and t["pnl"] is not None
    run(go())


def test_kalshi_manual_partial_exit_keeps_the_rest_and_its_exits(store):
    async def go():
        ex = KExchange(fav_market())
        svc, b = await k_open(store, ex)
        held = b["held"]
        ex.sim.positions[TICKER] = round(held - 2, 2)
        await k_ticks(svc, [NOW + 1_000, NOW + 11_000, NOW + 21_000])
        row = store.select("trading_brackets")[0]
        assert row["state"] == "open" and row["held"] == round(held - 2, 2) and row["exit_filled"] == 2.0
        a = audits(store, "you", "manual_exit")[0]
        assert a["detail"]["pnl_source"] == "estimated" and a["detail"]["price"] == 0.69  # no fills: the bid
        await svc.tick(NOW + 22_000)  # the take profit is re-placed for what is left
        tp = ex.posts()[-1]
        assert tp["post_only"] is True and float(tp["count"]) == round(held - 2, 2)
    run(go())


def test_kalshi_manual_add_is_only_audited(store):
    async def go():
        ex = KExchange(fav_market())
        svc, b = await k_open(store, ex)
        ex.sim.positions[TICKER] = round(b["held"] + 3, 2)
        await k_ticks(svc, [NOW + 11_000, NOW + 21_000, NOW + 31_000])
        row = store.select("trading_brackets")[0]
        assert row["state"] == "open" and row["held"] == b["held"]
        a = audits(store, "you", "external_position")
        assert len(a) == 1 and a[0]["detail"]["extra"] == 3.0
    run(go())


def test_kalshi_no_trigger_after_the_window_closes(store):
    async def go():
        ex = KExchange(fav_market())
        svc, b = await k_open(store, ex)
        ex.sim.positions[TICKER] = 0.0  # settlement removes positions: that is not a manual exit
        close = b["window_end"]
        await k_ticks(svc, [close - 500, close + 1_000, close + 20_000])
        assert audits(store, "you", "manual_exit") == []
    run(go())


# -- Polymarket -----------------------------------------------------------------------------------------------------
async def pm_ticks(svc, clock, md, times):
    for t in times:
        clock.t = t
        book(md, bid=0.30, ask=0.31)
        await svc.tick(t)


async def pm_down(store):
    clock, md, c, gw, idx, svc = prod_rig(store, bid=0.30, ask=0.31)
    await svc.reconcile("pm_prod", clock())
    svc.heartbeat(clock())
    idx.price = K - 60
    r = await enter(svc, up_bracket(side="DOWN", stop_index_below=None, stop_index_above=K + 10), clock())
    assert r["ok"] and r["bracket"]["held"] == 6.0
    return clock, md, c, gw, idx, svc


def test_pm_own_fill_lag_does_not_trigger(store):
    async def go():
        clock, md, c, gw, idx, svc = await pm_down(store)
        t0 = clock()
        c.acct.frozen = {}  # the list lags our own Down buy for 9 s
        await pm_ticks(svc, clock, md, [t0 + 1_000, t0 + 5_000, t0 + 9_000])
        c.acct.frozen = None  # ... then catches up
        await pm_ticks(svc, clock, md, [t0 + 14_000, t0 + 25_000, t0 + 40_000])
        assert store.select("trading_brackets")[0]["state"] == "open"
        assert audits(store, "you_poly", "manual_exit") == []
    run(go())


def test_pm_manual_full_exit_with_activity_prices_and_local_book_resync(store):
    async def go():
        clock, md, c, gw, idx, svc = await pm_down(store)
        t0 = clock()
        tp_id = store.select("trading_brackets")[0]["tp_order_id"]
        c.acct.pos[SLUG] = 0  # sold by hand in the Polymarket app at NO 84c (YES 16c)
        c.acct.activities = [{"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "id": "manual-t1", "marketSlug": SLUG, "price": {"value": "0.16", "currency": "USD"}, "qty": "6",
            "createTime": "2026-10-29T12:05:45Z"}}]
        creates = len(c.acct.creates())
        await pm_ticks(svc, clock, md, [t0 + 2_000, t0 + 12_000, t0 + 17_000, t0 + 23_000])
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "manual" and row["exit_avg"] == 0.84
        assert len(c.acct.creates()) == creates  # never an order
        assert c.acct.orders[tp_id]["state"] == "ORDER_STATE_CANCELED"  # the resting TP was cancelled
        assert gw.local_position(SLUG) == 0.0  # the local book follows: nothing to resurrect
        assert svc.trades(0, clock())[0]["pnl_source"] == "fills"
    run(go())


def test_pm_stop_after_a_manual_partial_exit_never_sells_more_than_is_held(store):
    async def go():
        clock, md, c, gw, idx, svc = await pm_down(store)
        t0 = clock()
        c.acct.pos[SLUG] = -2  # the user sold 4 of 6 by hand ...
        clock.t = t0 + 12_000
        idx.price = K + 10  # ... and the stop fires before the detector has acted
        book(md, bid=0.30, ask=0.31)
        await svc.tick(clock())
        sale = c.acct.creates()[-1]
        assert sale["intent"] == "ORDER_INTENT_SELL_SHORT" and sale["quantity"] == 2  # only what is truly held
        assert c.acct.pos[SLUG] == 0  # flat, never the opposite side
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["held"] == 0
    run(go())


def test_pm_manual_add_is_audited_and_never_sold(store):
    async def go():
        clock, md, c, gw, idx, svc = await pm_down(store)
        t0 = clock()
        c.acct.pos[SLUG] = -8  # the user bought 2 more NO by hand
        await pm_ticks(svc, clock, md, [t0 + 12_000, t0 + 25_000])
        assert len(audits(store, "you_poly", "external_position")) == 1
        idx.price = K + 10
        await pm_ticks(svc, clock, md, [t0 + 26_000])
        assert c.acct.creates()[-1]["quantity"] == 6 and c.acct.pos[SLUG] == -2  # ours only
    run(go())


def test_pm_restart_after_a_manual_exit(store):
    async def go():
        clock, md, c, gw, idx, svc = await pm_down(store)
        t0 = clock()
        c.acct.pos[SLUG] = 0  # sold by hand while the app was down
        from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
        from tests.polymarket_us.test_poly_trading import CONFIG, PM_MODES, Seq
        from btc_signal_engine.domain.settlement.fees import polymarket_fee

        def seed():
            out = {}
            for b in store.select("trading_brackets", {"strategy": "you_poly"}):
                if b["state"] in ("open", "stopping"):
                    out[b["ticker"]] = out.get(b["ticker"], 0.0) - b["held"]
            return out
        clock.t = t0 + 60_000
        book(md, bid=0.30, ask=0.31)
        gw2 = PolymarketTrading(c, quote_fn=md.quote, clock_ms=clock, seed_fn=seed)
        svc2 = TradingService(store, {"pm_prod": gw2}, CONFIG,
                              id_fn=Seq("r"), token_fn=Seq("u"), strategy="you_poly", modes=PM_MODES,
                              venue="Polymarket US", fee_fn=polymarket_fee, now=clock(), index_price_fn=idx)
        await svc2.reconcile("pm_prod", clock())
        creates = len(c.acct.creates())
        await pm_ticks(svc2, clock, md, [t0 + 61_000, t0 + 72_000, t0 + 77_000])
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "manual"
        assert svc2.trades(0, clock())[0]["pnl_source"] == "estimated"  # no activity fills: the bid, labelled
        assert len(c.acct.creates()) == creates
        assert seed() == {} and await gw2.positions() == []  # a later restart seeds nothing: not resurrected
    run(go())
