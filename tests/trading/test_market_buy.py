"""Kalshi-style MARKET buys for the phone's Play strategies (price_mode "market"): an immediate-or-cancel limit at the
market ceiling (99c) sized at the live ask, whose worst case (every contract at 99c plus the venue's fee) is the
ticket's max_cost and is checked against the per-trade / window / day limits and the gateway's order cap. The gate
does not refuse it for the ask moving, but every other check (kill switch, presence, cutoff, no / stale quote, spread
ceiling) stays. Request bodies per venue (Kalshi V2 through the SDK model, Polymarket US through its SDK) are checked
as sent. Simulated exchanges / fake SDK clients only: nothing reaches a venue."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from btc_signal_engine.adapters.http.trading_routes import ProposalBody
from btc_signal_engine.adapters.kalshi.backend import _model
from btc_signal_engine.adapters.polymarket_us.market_data import PolymarketMarketData
from btc_signal_engine.adapters.polymarket_us.markets import SERIES as PM_SERIES
from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
from btc_signal_engine.application.trading import MARKET_CEILING, TradingConfig, TradingService
from btc_signal_engine.domain.settlement.fees import POLYMARKET_FEE_RATE, kalshi_fee, polymarket_fee
from kalshi_python_async.models import CreateOrderV2Request
from tests.polymarket_us.fakes import CLOSE as PM_CLOSE, SLUG, Clock, client, full_book
from tests.trading.helpers import CLOSE, NOW, TICKER, Exchange, Market, gateway
from tests.trading.test_strategy_scope import Seq, svc_for, up

ORDERS = "/trade-api/v2/portfolio/events/orders"


def run(coro):
    return asyncio.run(coro)


def market(**kw) -> dict:
    """What the phone sends for a market buy: price_mode "market" and NO max_price."""
    p = {"ticker": TICKER, "side": "UP", "stake": 2.0, "price_mode": "market", "take_profit": 0.99, "stop": None,
         "window_end": CLOSE}
    p.update(kw)
    return p


def kalshi_worst(n: float) -> float:
    return round(n * 0.99 + kalshi_fee(n, 0.99, 0.07), 4)


def entry_posts(ex: Exchange) -> list[dict]:
    return [r["body"] for r in ex.seen if r["method"] == "POST" and r["path"] == ORDERS
            and "-entry" in r["body"]["client_order_id"]]


async def you(store, m: Market | None = None, **kw):
    ex = Exchange(m or Market())  # YES 55/58c, NO 42/45c
    svc = await up(svc_for(store, ex, "you", **kw))
    return ex, svc


# -- sizing, worst case and the request body (Kalshi) ----------------------------------------------------------------
@pytest.mark.parametrize("side,ask,book_side,yes_price", [("UP", 0.58, "bid", "0.9900"), ("DOWN", 0.45, "ask", "0.0100")])
def test_kalshi_market_ticket_states_the_worst_case_and_sends_an_ioc_at_99c(store, side, ask, book_side, yes_price):
    async def go():
        ex, svc = await you(store)
        prep = await svc.prepare(market(side=side), NOW)
        assert prep["ok"], prep
        t = prep["ticket"]
        n = svc.size(2.0, ask)
        assert n > 0 and t["contracts"] == n and t["price_mode"] == "market" and t["sized_at"] == ask
        assert t["limit_price"] == MARKET_CEILING == 0.99
        assert t["max_cost"] == kalshi_worst(n) == t["worst_case"]["cost"]
        assert t["worst_case"] == {"max_price": 0.99, "contracts": n, "cost": kalshi_worst(n)}
        assert t["cost_at_sized"] == round(n * ask + kalshi_fee(n, ask, 0.07), 4) <= 2.0  # spend at the ask ~ stake
        assert "at the market" in t["sentence"] and "99c" in t["sentence"]
        assert svc.market_terms(2.0, ask) == {"price_mode": "market", "max_price": 0.99, "sized_at": ask,
                                             "contracts": n, "cost_at_ask": t["cost_at_sized"],
                                             "worst_case_cost": t["max_cost"], "fits": True, "why": None}
        r = await svc.approve(prep["token"], "yes", t, NOW + 500)
        assert r["ok"] and r["bracket"]["state"] == "open", r
        body = entry_posts(ex)[0]
        assert (body["side"], body["price"], body["time_in_force"]) == (book_side, yes_price, "immediate_or_cancel")
        assert float(body["count"]) == n
        # the SDK's own request model accepts exactly these fields (nothing dropped or added: _model would refuse)
        model = _model(CreateOrderV2Request, **body)
        assert model.to_dict() == body
        # fill reporting unchanged: the fill price and fees come from the fills (the simulated book's ask)
        b = r["bracket"]
        assert b["entry_avg"] == ask and b["filled"] == n
        row = store.select("trading_brackets")[0]
        assert row["entry_fees"] == kalshi_fee(n, ask, 0.07) and row["max_cost"] == kalshi_worst(n)
        sent = [a for a in svc.audit(50) if a["event"] == "sent"][0]
        assert sent["detail"]["price_mode"] == "market" and sent["detail"]["sized_at"] == ask
    run(go())


def test_market_buy_is_not_refused_when_the_ask_moves_but_a_limit_buy_is(store):
    async def go():
        m = Market()
        ex, svc = await you(store, m)
        lim = await svc.prepare(market(price_mode="limit", max_price=0.59), NOW)
        mkt = await svc.prepare(market(), NOW + 1)
        assert lim["ok"] and mkt["ok"]
        m.set(0.58, 0.39)  # YES ask 58c -> 61c before the Yes
        r = await svc.approve(lim["token"], "yes", lim["ticket"], NOW + 400)
        assert not r["ok"] and r["code"] == "price_moved"
        r = await svc.approve(mkt["token"], "yes", mkt["ticket"], NOW + 500)
        assert r["ok"] and r["bracket"]["entry_avg"] == 0.61, r  # filled at the new ask, inside the 99c ceiling
        assert len(entry_posts(ex)) == 1
    run(go())


# -- limits: the worst case is what is checked -----------------------------------------------------------------------
def test_worst_case_over_the_per_trade_limit_is_refused_visibly(store):
    async def go():
        ex, svc = await you(store)  # per-trade limit $5; $4 at 58c = 6.67 contracts -> worst ~ $6.60
        prep = await svc.prepare(market(stake=4.0), NOW)
        assert not prep["ok"] and prep["code"] == "over_trade_limit"
        assert "up to $6.6" in prep["reason"] and "per-trade limit" in prep["reason"]
        assert svc.market_terms(4.0, 0.58)["fits"] is False
        assert entry_posts(ex) == []
    run(go())


def test_worst_case_over_the_day_and_window_limits_is_refused(store):
    async def go():
        cfg = TradingConfig(enabled=True, max_stake_per_trade=50.0, max_dollars_per_window=50.0,
                            max_dollars_per_day=5.0)
        ex, svc = await you(store, config=cfg)
        prep = await svc.prepare(market(stake=4.0), NOW)  # $4 at the ask, but up to ~$6.60 at 99c
        assert not prep["ok"] and prep["code"] == "over_day_limit"
        lim = await svc.prepare(market(stake=4.0, price_mode="limit", max_price=0.59), NOW)
        assert lim["ok"]  # the same stake as a limit fits: only the market's worst case is over
    run(go())


def test_worst_case_over_the_gateways_order_cap_is_refused_before_sending(store):
    async def go():
        ex = Exchange(Market())
        store.insert("trading_settings", [{"key": "you.mode", "value": {"mode": "prod"}, "updated_ts": 0}],
                     ignore_conflicts=False)
        svc = TradingService(store, {"prod": gateway(ex, "prod", max_order_cost=5.0)},
                             TradingConfig(enabled=True, max_stake_per_trade=50.0, max_dollars_per_window=50.0),
                             id_fn=Seq("y-"), token_fn=Seq("t-"), strategy="you")
        await up(svc)
        prep = await svc.prepare(market(stake=4.0), NOW)
        assert not prep["ok"] and prep["code"] == "over_order_cap" and "$5.00 cap" in prep["reason"]
        assert entry_posts(ex) == []
    run(go())


def test_a_gateway_that_does_not_state_its_cap_refuses_market_buys(store):
    async def go():
        ex, svc = await you(store)
        gw = svc.gateways["prod"]

        class NoCap:  # the same gateway without max_order_cost
            def __getattr__(self, name):
                if name == "max_order_cost":
                    raise AttributeError(name)
                return getattr(gw, name)

        svc.gateways["prod"] = NoCap()
        prep = await svc.prepare(market(), NOW)
        assert not prep["ok"] and prep["code"] == "no_order_cap"
    run(go())


# -- every other gate still refuses -----------------------------------------------------------------------------------
def test_kill_switch_cutoff_presence_and_no_quote_still_refuse_market_buys(store):
    async def go():
        ex, svc = await you(store)
        prep = await svc.prepare(market(), NOW)
        await svc.kill(NOW + 100)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 200)
        assert not r["ok"] and r["code"] == "killed", r
        assert (await svc.prepare(market(), NOW + 300))["code"] == "killed"
        svc.resume(NOW + 400)
        gone = await svc.prepare(market(), NOW + 300_000)  # nobody at the screen for > 3 minutes, 5 min left
        assert not gone["ok"] and gone["code"] == "not_present"
        svc.heartbeat(CLOSE - 60_000)
        late = await svc.prepare(market(), CLOSE - 60_000)  # 1 minute left: inside the 2-minute cutoff
        assert not late["ok"] and late["code"] == "too_late"
        svc.heartbeat(NOW + 500)

        async def no_quote(ticker):
            raise RuntimeError("stale")  # the quote cannot be read: nothing to size at, nothing sent

        svc.gateways["prod"].quote = no_quote
        r = await svc.prepare(market(), NOW + 600)
        assert not r["ok"] and r["code"] == "no_quote"
        assert entry_posts(ex) == []
    run(go())


def test_a_quote_lost_between_prepare_and_yes_refuses(store):
    async def go():
        ex, svc = await you(store)
        prep = await svc.prepare(market(), NOW)

        async def no_quote(ticker):
            return None

        svc.gateways["prod"].quote = no_quote
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 300)
        assert not r["ok"] and r["code"] == "no_quote"
        assert entry_posts(ex) == []
    run(go())


def test_market_spread_ceiling_is_10c_not_none(store):
    async def go():
        ex, svc = await you(store, Market(yes_bid=0.50, no_bid=0.42))  # YES 50/58: 8c wide
        assert (await svc.prepare(market(price_mode="limit", max_price=0.59), NOW))["code"] == "thin_market"
        assert (await svc.prepare(market(), NOW))["ok"]  # a market buy accepts up to 10c
    run(go())


def test_market_spread_over_the_ceiling_is_refused(store):
    async def go():
        ex, svc = await you(store, Market(yes_bid=0.46, no_bid=0.42))  # YES 46/58: 12c wide
        r = await svc.prepare(market(), NOW)
        assert not r["ok"] and r["code"] == "thin_market"
    run(go())


# -- the request contract ---------------------------------------------------------------------------------------------
def test_price_mode_is_explicit_and_market_has_no_max_price(store):
    async def go():
        ex, svc = await you(store)
        r = await svc.prepare(market(max_price=0.60), NOW)
        assert not r["ok"] and r["code"] == "invalid" and "no max_price" in r["reason"]
        r = await svc.prepare(market(price_mode=None), NOW)  # neither a mode nor a max_price
        assert not r["ok"] and r["code"] == "invalid" and "price_mode" in r["reason"]
        r = await svc.prepare(market(price_mode="best"), NOW)
        assert not r["ok"] and r["code"] == "invalid"
        r = await svc.prepare(market(price_mode=None, max_price=0.59), NOW)  # today's callers: a limit
        assert r["ok"] and r["ticket"]["price_mode"] == "limit" and r["ticket"]["limit_price"] == 0.59
        assert r["ticket"]["worst_case"]["max_price"] == 0.59
    run(go())


def test_the_price_mode_is_bound_to_the_approval(store):
    async def go():
        ex, svc = await you(store)
        prep = await svc.prepare(market(), NOW)
        r = await svc.approve(prep["token"], "yes", {**prep["ticket"], "price_mode": "limit"}, NOW + 300)
        assert not r["ok"] and r["code"] == "altered"
        assert entry_posts(ex) == []
    run(go())


def test_http_body_takes_price_mode_and_an_optional_max_price():
    base = {"ticker": TICKER, "side": "UP", "stake": 2.0, "take_profit": 0.99, "strategy": "you"}
    body = ProposalBody(**base, price_mode="market").model_dump(exclude={"strategy"})
    assert body["price_mode"] == "market" and body["max_price"] is None
    assert ProposalBody(**base, max_price=0.5).model_dump()["price_mode"] is None  # service reads it as a limit
    with pytest.raises(ValidationError):
        ProposalBody(**base, price_mode="best")


# -- Polymarket US (through its SDK; fee rounded to the cent) ---------------------------------------------------------
PM_MODES = {"shadow": "pm_shadow", "prod": "pm_prod"}


async def _no_sleep(_):
    return None


def pm_rig(store, bid=0.69, ask=0.70):
    clock = Clock()
    md = PolymarketMarketData(clock_ms=clock)
    md.set_connected(True)
    md.on_message(full_book(SLUG, [(bid, 50), (round(bid - 0.01, 2), 50)], [(ask, 50), (round(ask + 0.01, 2), 50)]))
    c = client(bid=bid, ask=ask)
    gw = PolymarketTrading(c, quote_fn=md.quote, clock_ms=clock, sleep=_no_sleep, max_order_cost=25.0)
    store.insert("trading_settings", [{"key": "you_poly.mode", "value": {"mode": "prod"}, "updated_ts": 0}],
                 ignore_conflicts=False)
    cfg = TradingConfig(enabled=True, fee_rate=POLYMARKET_FEE_RATE, series=PM_SERIES, whole_contracts=True,
                        max_stake_per_trade=10.0)
    svc = TradingService(store, {"pm_prod": gw}, cfg, id_fn=Seq("p"), token_fn=Seq("t"),
                         strategy="you_poly", modes=PM_MODES, venue="Polymarket US", fee_fn=polymarket_fee,
                         now=clock())
    return clock, c, svc


@pytest.mark.parametrize("side,bid,ask,intent,yes_price,fill", [
    ("UP", 0.69, 0.70, "ORDER_INTENT_BUY_LONG", "0.99", 0.70),
    ("DOWN", 0.30, 0.31, "ORDER_INTENT_BUY_SHORT", "0.01", 0.70)])  # NO ask = 1 - 0.30
def test_polymarket_market_buy_is_whole_contracts_ioc_at_the_ceiling(store, side, bid, ask, intent, yes_price, fill):
    async def go():
        clock, c, svc = pm_rig(store, bid, ask)
        await svc.reconcile("pm_prod", clock())
        svc.heartbeat(clock())
        prep = await svc.prepare(market(ticker=SLUG, side=side, stake=5.0, window_end=PM_CLOSE), clock())
        assert prep["ok"], prep
        t = prep["ticket"]
        assert t["contracts"] == 6.0 and t["sized_at"] == 0.70  # whole contracts whose cost at 70c fits $5
        assert t["max_cost"] == round(6 * 0.99 + polymarket_fee(6, 0.99), 4) == t["worst_case"]["cost"]
        assert t["cost_at_sized"] == round(6 * 0.70 + polymarket_fee(6, 0.70), 4) <= 5.0
        r = await svc.approve(prep["token"], "yes", t, clock() + 200)
        assert r["ok"] and r["bracket"]["entry_avg"] == fill, r
        entry = c.acct.creates()[0]
        assert (entry["intent"], entry["type"], entry["price"]["value"], entry["quantity"], entry["tif"]) == (
            intent, "ORDER_TYPE_LIMIT", yes_price, 6, "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL")
        assert "cashOrderQty" not in entry
        assert store.select("trading_brackets")[0]["entry_fees"] == polymarket_fee(6, fill)
    run(go())


def test_polymarket_worst_case_uses_its_own_fee_against_the_limit(store):
    async def go():
        clock, c, svc = pm_rig(store)
        await svc.reconcile("pm_prod", clock())
        svc.heartbeat(clock())
        terms = svc.market_terms(9.0, 0.70)  # 12 contracts: worst 12 x 99c + fee = $11.89 > $10
        assert terms["contracts"] == 12.0 and terms["worst_case_cost"] == round(11.88 + polymarket_fee(12, 0.99), 4)
        assert terms["fits"] is False
        r = await svc.prepare(market(ticker=SLUG, stake=9.0, window_end=PM_CLOSE), clock())
        assert not r["ok"] and r["code"] == "over_trade_limit"
        assert c.acct.creates() == []
    run(go())


def test_polymarket_stale_websocket_book_refuses_a_market_buy(store):
    async def go():
        clock, c, svc = pm_rig(store)
        await svc.reconcile("pm_prod", clock())
        clock.t += 2_500  # no market data for 2.5 s: the quote is stale (None), never sized or sent
        svc.heartbeat(clock())
        r = await svc.prepare(market(ticker=SLUG, window_end=PM_CLOSE), clock())
        assert not r["ok"] and r["code"] == "no_quote"
        assert c.acct.creates() == []
    run(go())


# -- market protection (the limit = the ask + the user's band, not 99c) --------------------------------------------
def test_market_protection_caps_the_limit_and_the_worst_case(store):
    async def go():
        ex, svc = await you(store, market_band_fn=lambda: 0.15)  # YES ask 58c
        n = svc.size(2.0, 0.58)
        terms = svc.market_terms(2.0, 0.58)
        worst = round(n * 0.73 + kalshi_fee(n, 0.73, 0.07), 4)
        assert terms["max_price"] == 0.73 and terms["worst_case_cost"] == worst < kalshi_worst(n)
        prep = await svc.prepare(market(), NOW)
        assert prep["ok"], prep
        t = prep["ticket"]
        assert t["limit_price"] == 0.73 and t["max_cost"] == worst and t["worst_case"]["max_price"] == 0.73
        r = await svc.approve(prep["token"], "yes", t, NOW + 500)
        assert r["ok"], r
        assert entry_posts(ex)[0]["price"] == "0.7300"
    run(go())


def test_market_protection_never_above_99c_and_none_means_99c(store):
    async def go():
        _, svc = await you(store, market_band_fn=lambda: 0.25)
        assert svc.market_limit(0.90) == (0.99, svc.market_limit(0.90)[1]) and svc.market_limit(0.90)[1].ok
        svc._market_band_fn = lambda: None
        assert svc.market_limit(0.30)[0] == 0.99
    run(go())


def test_unreadable_market_protection_refuses_market_buys(store):
    def bad():
        raise ValueError("market protection is 7")

    async def go():
        _, svc = await you(store, market_band_fn=bad)
        prep = await svc.prepare(market(), NOW)
        assert not prep["ok"] and prep["code"] == "band_unreadable"
        terms = svc.market_terms(2.0, 0.58)
        assert terms["fits"] is False and "Market protection setting unreadable" in terms["why"]
    run(go())
