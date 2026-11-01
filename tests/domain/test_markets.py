"""The market table (domain/markets.py): lookups raise on anything unknown, every phone strategy has a gate profile
with the table's entry cutoff, the Play tabs and the adapters' allow-lists come from it, and both gateways refuse a
series that is not tradable in it (polymarket_1h: recognised, never traded) before anything is sent."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.adapters.kalshi.trading import KalshiTrading, TradingViolation as KalshiViolation
from btc_signal_engine.adapters.polymarket_us import markets as pm
from btc_signal_engine.adapters.polymarket_us.trading import TradingViolation as PmViolation
from btc_signal_engine.application import play
from btc_signal_engine.application.trading import STRATEGY_CUTOFFS, TradingConfig
from btc_signal_engine.domain import markets as M
from tests.polymarket_us.fakes import W0, client
from tests.polymarket_us.test_adapter import gw as pm_gateway
from tests.trading.helpers import TICKER, Exchange, Market, gateway as kalshi_gateway


def run(coro):
    return asyncio.run(coro)


# -- the table ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("call", [lambda: M.market("kalshi_1h"), lambda: M.market(None), lambda: M.venue("binance"),
                                  lambda: M.by_venue_span("kalshi", "5m"), lambda: M.by_venue_span("x", "15m"),
                                  lambda: M.by_phone_strategy("pnw_poly"), lambda: M.phone_markets("x"),
                                  lambda: M.tradable_spans("x"), lambda: M.allowed_series("x"),
                                  lambda: pm.series_for("2m")])
def test_unknown_keys_raise(call):
    with pytest.raises(M.UnknownMarket):
        call()


def test_rows():
    assert [m.key for m in M.MARKETS] == ["kalshi_15m", "polymarket_15m", "polymarket_5m", "polymarket_1h"]
    assert M.by_venue_span("polymarket", "5m") is M.market("polymarket_5m")
    assert M.by_phone_strategy("you_poly5").series == "cpc-btc-updown-5m"
    assert all(m.kind == "updown" and m.window_ms > 0 for m in M.MARKETS)
    assert M.allowed_series("kalshi") == {"KXBTC15M"}
    assert M.allowed_series("polymarket") == {"cpc-btc-updown-15m", "cpc-btc-updown-5m"}
    assert all(m.series.startswith(pm.FAMILY + "-") for m in M.venue_markets("polymarket"))  # the shadow's prefix


def test_polymarket_1h_is_recognised_but_not_tradable():
    h = M.market("polymarket_1h")
    assert not h.tradable and h.phone_strategy is None and h not in M.phone_markets()
    assert "1h" not in M.tradable_spans("polymarket") and "1h" not in pm.LIVE_SPANS
    slug = pm.slug_for(W0, "1h")
    assert pm.span_of(slug) == "1h" and pm.window_of(slug) == (W0, W0 + 3_600_000)
    assert all("-1h-" not in s for s in pm.current_slugs(W0))  # the stream never subscribes it


def test_every_phone_strategy_has_the_tables_cutoff():
    for m in M.phone_markets():
        assert STRATEGY_CUTOFFS[m.phone_strategy] == m.entry_cutoff_ms, m.key
    assert set(STRATEGY_CUTOFFS) == {m.phone_strategy for m in M.phone_markets()}  # no other strategy trades
    assert M.market("polymarket_5m").entry_cutoff_ms == 60_000
    assert TradingConfig().series == M.market("kalshi_15m").series


def test_play_tabs_come_from_the_table():
    assert play.VENUES == ("kalshi", "polymarket")
    assert play.SPANS == {"kalshi": ("15m",), "polymarket": ("15m", "5m")}
    assert play.VENUE_NAMES == {"kalshi": "Kalshi", "polymarket": "Polymarket"}
    assert play.FEE_ROUNDING == {"kalshi": "up_0.0001", "polymarket": "nearest_cent"}
    assert [play.venue_key(m.venue, m.span) for m in M.phone_markets()] == ["kalshi", "polymarket", "polymarket_5m"]
    assert play.venue_key("kalshi", "5m") is None and play.venue_key("polymarket", "1h") is None


def test_polymarket_market_constants_come_from_the_table():
    assert pm.SPANS_MS == {"15m": 900_000, "5m": 300_000, "1h": 3_600_000}
    assert pm.LIVE_SPANS == ("15m", "5m") and pm.SERIES == "cpc-btc-updown-15m"


# -- the gateways refuse anything the table does not trade ---------------------------------------------------------
@pytest.mark.parametrize("ticker", ["KXBTC1H-26OCT281400", "KXETH15M-26OCT281315-15", "cpc-btc-updown-15m-x"])
def test_kalshi_gateway_refuses_a_series_not_in_the_table(ticker):
    ex = Exchange(Market())
    g = kalshi_gateway(ex)
    with pytest.raises(KalshiViolation) as e:
        run(g.place_limit(ticker=ticker, side="yes", action="buy", count=1.0, price=0.5, client_order_id="c1"))
    assert str(e.value).startswith("only KXBTC15M markets may be traded")
    for call in (g.cancel("o1", ticker), g.quote(ticker)):
        with pytest.raises(KalshiViolation):
            run(call)
    assert ex.seen == []


def test_kalshi_gateway_accepts_a_series_set():
    g = KalshiTrading(kalshi_gateway(Exchange(Market()))._backend.client.auth, env="demo", series={"KXBTC15M", "KXBTC1H"})
    g._series_only(TICKER)
    g._series_only("KXBTC1H-26OCT281400")
    with pytest.raises(KalshiViolation, match="only KXBTC15M/KXBTC1H markets may be traded"):
        g._series_only("KXETH15M-26OCT281315-15")


@pytest.mark.parametrize("ticker", [pm.slug_for(W0, "1h"), "cpc-btc-updown-2m-2026-10-29-1200z", TICKER])
def test_polymarket_gateway_refuses_a_series_not_in_the_table(ticker):
    c = client()
    with pytest.raises(PmViolation) as e:
        run(pm_gateway(c).place_limit(ticker=ticker, side="yes", action="buy", count=1, price=0.70,
                                      client_order_id="x"))
    assert e.value.definitive and "only BTC Up/Down 15m/5m markets may be traded" in str(e.value)
    assert c.acct.calls == []


# -- perpetuals (KTD12: their own table, never a window market) ---------------------------------------------------
def test_perp_table():
    p = M.perp("kalshi_btc_perp")
    assert p.ticker("perp_prod") == "KXBTCPERP" and p.ticker("perp_demo") == "KXBTCPERP1"
    assert p.contract_btc == 0.0001 and p.tick == 0.0001 and p.count_step == 1.0
    assert M.allowed_perp_tickers("perp_demo") == frozenset({"KXBTCPERP1"})
    assert M.allowed_perp_tickers("perp_prod") == frozenset({"KXBTCPERP"})


@pytest.mark.parametrize("call", [lambda: M.perp("kalshi_eth_perp"), lambda: M.perp(None),
                                  lambda: M.perp("kalshi_btc_perp").ticker("prod"),
                                  lambda: M.allowed_perp_tickers("demo"), lambda: M.market("kalshi_btc_perp")])
def test_unknown_perp_keys_raise(call):
    with pytest.raises(M.UnknownMarket):
        call()


def test_window_lookups_never_return_the_perp():
    assert all("PERP" not in m.series for m in M.MARKETS)
    assert "KXBTCPERP" not in M.allowed_series("kalshi")
