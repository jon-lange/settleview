"""you_poly (the Polymarket Play tab) end to end through the SAME TradingService (gates, approvals, index stop, audit)
as Kalshi: shadow fills on a (fake) live Polymarket book, and production through PolymarketTrading over a fake SDK.
Also the service generalizations: Polymarket env names and labels, no sandbox, the series guard, whole-contract sizing
with Polymarket's fee, and credential problems falling back to shadow. Kalshi services are unchanged."""

from __future__ import annotations

import asyncio

from btc_signal_engine.adapters.polymarket_us.market_data import PolymarketMarketData
from btc_signal_engine.adapters.polymarket_us.markets import SERIES
from btc_signal_engine.adapters.polymarket_us.shadow import polymarket_shadow
from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
from btc_signal_engine.application.trading import MODE_LABELS, MODES, TradingConfig, TradingService
from btc_signal_engine.domain.settlement.fees import POLYMARKET_FEE_RATE, polymarket_fee
from tests.polymarket_us.fakes import CLOSE, SLUG, AuthenticationError, Clock, client, full_book, http_error

K = 80_000.0
PM_MODES = {"shadow": "pm_shadow", "prod": "pm_prod"}
CONFIG = TradingConfig(enabled=True, fee_rate=POLYMARKET_FEE_RATE, series=SERIES, whole_contracts=True)


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_):
    return None


class Index:
    def __init__(self, clock: Clock, price: float = K + 60) -> None:
        self.clock, self.price = clock, price

    def __call__(self):
        return (self.clock(), self.price)


class Seq:
    def __init__(self, p: str) -> None:
        self.p, self.n = p, 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.p}{self.n:04d}"


def book(md: PolymarketMarketData, bid=0.69, ask=0.70, size=50) -> None:
    md.on_message(full_book(SLUG, [(bid, size), (round(bid - 0.01, 2), size)],
                            [(ask, size), (round(ask + 0.01, 2), size)]))


def service(store, gateways: dict, clock: Clock, mode: str, **kw) -> TradingService:
    store.insert("trading_settings", [{"key": "you_poly.mode", "value": {"mode": mode}, "updated_ts": 0}],
                 ignore_conflicts=False)
    return TradingService(store, gateways, kw.pop("config", CONFIG),
                          id_fn=Seq("b"), token_fn=Seq("t"), strategy="you_poly", modes=PM_MODES,
                          venue="Polymarket US", fee_fn=polymarket_fee, now=clock(), **kw)


def up_bracket(**kw) -> dict:
    p = {"ticker": SLUG, "side": "UP", "stake": 5.0, "max_price": 0.71, "take_profit": 0.99, "stop": None,
         "stop_index_below": K - 10, "window_end": CLOSE}
    p.update(kw)
    return p


async def enter(svc: TradingService, p: dict, now: int) -> dict:
    prep = await svc.prepare(p, now)
    assert prep["ok"], prep
    return await svc.approve(prep["token"], "yes", prep["ticket"], now + 200)


def shadow_rig(store, markets_fn=None):
    clock = Clock()
    md = PolymarketMarketData(clock_ms=clock)
    md.set_connected(True)
    book(md)
    ex, gw = polymarket_shadow(md, max_order_cost=25.0, clock_ms=clock, sleep=_no_sleep, maker_model=False,
                               markets_fn=markets_fn)
    idx = Index(clock)
    svc = service(store, {"pm_shadow": gw}, clock, "shadow", index_price_fn=idx)
    return clock, md, ex, idx, svc


# -- shadow ------------------------------------------------------------------------------------------------------
def test_shadow_entry_then_index_stop_on_the_polymarket_book(store):
    async def go():
        clock, md, ex, idx, svc = shadow_rig(store)
        assert svc.mode == "shadow" and svc.modes == PM_MODES and "Polymarket US" in svc.mode_labels["shadow"]
        await svc.reconcile("pm_shadow", clock())
        svc.heartbeat(clock())
        r = await enter(svc, up_bracket(), clock())
        b = r["bracket"]
        assert r["ok"] and b["state"] == "open" and b["env"] == "pm_shadow"
        assert b["contracts"] == 6.0 and b["filled"] == 6.0 and b["entry_avg"] == 0.70  # whole contracts
        row = store.select("trading_brackets")[0]
        assert row["strategy"] == "you_poly" and row["entry_fees"] == polymarket_fee(6, 0.70) == 0.09
        assert b["tp_order_id"]  # the 99c resting take profit
        clock.t += 3_000
        book(md)  # fresh book
        await svc.tick(clock())
        assert store.select("trading_brackets")[0]["state"] == "open"  # BTC still $60 over the strike
        idx.price = K - 10.0  # back to the stop level
        clock.t += 1_000
        book(md, bid=0.40, ask=0.42)
        await svc.tick(clock())
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "stop" and row["exit_avg"] == 0.40
        events = [a["event"] for a in store.select("trading_audit", {"strategy": "you_poly"})]
        assert events.count("stop_triggered") == 1 and "stop_sent" in events
        assert ex.positions.get(SLUG, 0.0) == 0.0 and all(f["fee"] >= 0 for f in ex.fills)
    run(go())


def test_shadow_stale_book_refuses_entries(store):
    async def go():
        clock, md, ex, idx, svc = shadow_rig(store)
        await svc.reconcile("pm_shadow", clock())
        svc.heartbeat(clock())
        clock.t += 2_500  # no market data for 2.5 s
        prep = await svc.prepare(up_bracket(), clock())
        assert not prep["ok"] and prep["code"] == "no_quote"
        assert ex.orders == {}
    run(go())


def test_shadow_held_to_the_close_is_settled(store):
    async def go():
        markets = {}  # slug -> the public market (GET /v1/market/slug/{slug}), as the SDK returns it

        async def retrieve_by_slug(slug):
            return {"market": markets.get(slug, {"status": "MARKET_STATUS_OPEN"})}
        clock, md, ex, idx, svc = shadow_rig(store, retrieve_by_slug)
        await svc.reconcile("pm_shadow", clock())
        svc.heartbeat(clock())
        await enter(svc, up_bracket(), clock())
        clock.t = CLOSE + 1
        await svc.tick(clock())
        row = store.select("trading_brackets")[0]
        assert row["state"] == "settling" and row["held"] == 6.0  # not resolved yet: pending, never a loss
        cash = ex.cash
        markets[SLUG] = {"status": "MARKET_STATUS_RESOLVED", "outcomePrices": '["1","0"]'}  # Up won
        clock.t += 15_000
        await svc.tick(clock())
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "settled" and row["held"] == 6.0
        assert row["proceeds"] == 6.0  # 6 contracts x $1
        assert ex.settlements[-1]["payout"] == 6.0 and ex.cash == cash + 6.0 and SLUG not in ex.positions
        t = svc.trades(0, clock())[0]
        assert t["pnl"] == round(6.0 - row["entry_cost"], 4) > 0 and t["pnl_source"] == "shadow_market_result"
    run(go())


def test_wrong_series_and_no_sandbox(store):
    async def go():
        clock, md, ex, idx, svc = shadow_rig(store)
        await svc.reconcile("pm_shadow", clock())
        svc.heartbeat(clock())
        r = await svc.prepare(up_bracket(ticker="KXBTC15M-26OCT290815-15"), clock())
        assert not r["ok"] and r["code"] == "wrong_series"
        r = await svc.set_mode("sandbox", clock())
        assert r["code"] == "invalid"
        view = svc.status(clock())
        assert set(view["modes"]) == {"shadow", "prod"} and view["venue"] == "Polymarket US"
        assert view["configured"] == {"pm_shadow": True, "pm_prod": False}
    run(go())


# -- production through PolymarketTrading over a fake SDK ---------------------------------------------------------
def prod_rig(store, **book_kw):
    clock = Clock()
    md = PolymarketMarketData(clock_ms=clock)
    md.set_connected(True)
    book(md, **book_kw)
    c = client(bid=book_kw.get("bid", 0.69), ask=book_kw.get("ask", 0.70))
    gw = PolymarketTrading(c, quote_fn=md.quote, clock_ms=clock, sleep=_no_sleep)
    idx = Index(clock)
    svc = service(store, {"pm_prod": gw}, clock, "prod", index_price_fn=idx)
    return clock, md, c, gw, idx, svc


def test_prod_up_entry_index_stop_cancels_tp_then_sells_capped(store):
    async def go():
        clock, md, c, gw, idx, svc = prod_rig(store)
        assert svc.mode == "prod"
        await svc.reconcile("pm_prod", clock())
        svc.heartbeat(clock())
        r = await enter(svc, up_bracket(), clock())
        assert r["ok"] and r["bracket"]["state"] == "open" and r["bracket"]["filled"] == 6.0
        entry, tp = c.acct.creates()
        assert (entry["intent"], entry["price"]["value"], entry["quantity"], entry["tif"]) == (
            "ORDER_INTENT_BUY_LONG", "0.71", 6, "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL")
        assert (tp["intent"], tp["price"]["value"], tp["participateDontInitiate"]) == ("ORDER_INTENT_SELL_LONG", "0.99", True)
        idx.price = K - 10.0
        clock.t += 1_000
        book(md, bid=0.40, ask=0.42)
        c.acct.bid, c.acct.ask = 0.40, 0.42
        await svc.tick(clock())
        kinds = [k[0] for k in c.acct.calls]
        assert "cancel" in kinds and kinds.index("cancel") < len(kinds) - 1
        sale = c.acct.creates()[-1]
        assert (sale["intent"], sale["price"]["value"], sale["quantity"]) == ("ORDER_INTENT_SELL_LONG", "0.35", 6)
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "stop" and row["exit_avg"] == 0.40
        assert row["exit_fees"] == polymarket_fee(6, 0.40) and c.acct.pos[SLUG] == 0
    run(go())


def test_prod_down_entry_maps_to_buy_short_and_exits_with_sell_short(store):
    async def go():
        clock, md, c, gw, idx, svc = prod_rig(store, bid=0.30, ask=0.31)
        await svc.reconcile("pm_prod", clock())
        svc.heartbeat(clock())
        p = up_bracket(side="DOWN", stop_index_below=None, stop_index_above=K + 10)
        idx.price = K - 60
        r = await enter(svc, p, clock())
        assert r["ok"] and r["bracket"]["entry_avg"] == 0.70, r
        entry, tp = c.acct.creates()
        assert (entry["intent"], entry["price"]["value"]) == ("ORDER_INTENT_BUY_SHORT", "0.29")
        assert (tp["intent"], tp["price"]["value"]) == ("ORDER_INTENT_SELL_SHORT", "0.01")
        assert c.acct.pos[SLUG] == -6
        idx.price = K + 10
        clock.t += 1_000
        book(md, bid=0.30, ask=0.31)
        await svc.tick(clock())
        sale = c.acct.creates()[-1]
        assert (sale["intent"], sale["price"]["value"]) == ("ORDER_INTENT_SELL_SHORT", "0.36")  # NO bid 69c - 5c
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "stop" and row["exit_avg"] == 0.69
        assert c.acct.pos[SLUG] == 0
    run(go())


def test_prod_credentials_refused_falls_back_to_shadow(store):
    async def go():
        clock, md, c, gw, idx, svc = prod_rig(store)
        await svc.reconcile("pm_prod", clock())
        svc.heartbeat(clock())
        c.acct.fail_next = http_error(AuthenticationError, 401, "bad key")
        prep = await svc.prepare(up_bracket(), clock())
        r = await svc.approve(prep["token"], "yes", prep["ticket"], clock() + 200)
        assert r["code"] == "rejected" and "Polymarket US" in r["reason"]
        assert svc.mode == "shadow"
    run(go())


def test_prod_lost_entry_answer_is_resolved_from_the_activity_trades(store):
    async def go():
        from tests.polymarket_us.fakes import APIConnectionError
        clock, md, c, gw, idx, svc = prod_rig(store)
        await svc.reconcile("pm_prod", clock())
        svc.heartbeat(clock())
        c.acct.fail_next = (APIConnectionError(message="timeout"), True)
        r = await enter(svc, up_bracket(), clock())
        assert r["code"] == "unknown"
        clock.t += 500
        book(md)
        await svc.tick(clock())
        assert store.select("trading_brackets")[0]["state"] == "unknown"  # the list may lag: no guess yet
        clock.t += 15_000
        book(md)
        await svc.tick(clock())
        row = store.select("trading_brackets")[0]
        assert row["state"] == "open" and row["filled"] == 6.0
        assert len([b for b in c.acct.creates() if b["intent"] == "ORDER_INTENT_BUY_LONG"]) == 1  # never resent
    run(go())


def test_kalshi_service_defaults_are_unchanged(store):
    svc = TradingService(store, {}, TradingConfig(), strategy="you")
    assert svc.modes == MODES and svc.mode_labels == MODE_LABELS and svc.mode == "sandbox"
    assert set(svc.gateways) == {"demo", "shadow", "prod"} and svc.venue == "Kalshi"


def test_prod_stop_right_after_a_down_entry_with_a_lagging_positions_list(store):
    """Penny test B2 in the service: the list shows nothing right after the Down buy; the take profit and the stop
    are still sent for the full holding and the bracket closes by its stop (not 'reconciled' to zero)."""
    async def go():
        clock, md, c, gw, idx, svc = prod_rig(store, bid=0.30, ask=0.31)
        await svc.reconcile("pm_prod", clock())
        svc.heartbeat(clock())
        c.acct.frozen = {}  # the positions list lags for the whole test
        idx.price = K - 60
        r = await enter(svc, up_bracket(side="DOWN", stop_index_below=None, stop_index_above=K + 10), clock())
        assert r["ok"] and r["bracket"]["held"] == 6.0 and r["bracket"]["tp_order_id"]
        assert c.acct.creates()[1]["quantity"] == 6  # the take profit went out for all 6
        idx.price = K + 10
        clock.t += 1_000
        book(md, bid=0.30, ask=0.31)
        await svc.tick(clock())
        assert c.acct.creates()[-1]["quantity"] == 6 and c.acct.pos[SLUG] == 0
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "stop"
    run(go())


# -- settlement results (Polymarket's own resolution) -------------------------------------------------------------
def test_resolution_reads_only_a_resolved_market_with_1_0_outcome_prices():
    from btc_signal_engine.adapters.polymarket_us.markets import resolution
    assert resolution({"status": "MARKET_STATUS_RESOLVED", "outcomePrices": '["1","0"]'})["result"] == "yes"
    assert resolution({"status": "MARKET_STATUS_RESOLVED", "outcomePrices": ["0", "1"]})["result"] == "no"
    for m in ({"status": "MARKET_STATUS_OPEN", "outcomePrices": '["1","0"]'},  # not resolved yet
              {"status": "MARKET_STATUS_RESOLVED", "outcomePrices": '["0.5","0.5"]'},  # not a clean result
              {"status": "MARKET_STATUS_RESOLVED", "outcomePrices": "not json"}, {}, None):
        r = resolution(m)
        assert r["final"] is False and r["result"] is None


def test_production_gateway_reads_the_markets_resolution():
    async def go():
        clock = Clock()
        md = PolymarketMarketData(clock_ms=clock)
        asked = []

        async def retrieve_by_slug(slug):
            asked.append(slug)
            return {"market": {"status": "MARKET_STATUS_RESOLVED", "outcomePrices": '["0","1"]'}}
        gw = PolymarketTrading(client(), quote_fn=md.quote, clock_ms=clock, sleep=_no_sleep,
                               markets_fn=retrieve_by_slug)
        got = await gw.market_result(SLUG)
        assert got == {"ticker": SLUG, "status": "MARKET_STATUS_RESOLVED", "result": "no", "final": True,
                       "source": "polymarket_market_resolution"} and asked == [SLUG]
    run(go())
