"""Bootstrap wiring of the Polymarket Play tabs (you_poly: 15m, you_poly5: 5m): built only with Polymarket keys (fake
here; nothing connects), one TradingService per tab on pm_shadow / pm_prod with Polymarket's fee and whole contracts,
the phone's user-set limits and cash backstop, the shared kill switch, streams by environment, the gateway's id
lookups and position seed covering every Polymarket strategy (incl. the retired pnw_poly's past orders)."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.adapters.kalshi.shadow import ShadowStream
from btc_signal_engine.adapters.polymarket_us.markets import slug_for
from btc_signal_engine.adapters.polymarket_us.stream import PolymarketTradingStream
from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading
from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import Settings


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_POLYMARKET_KEY_ID", "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "pm.db", trading_stream=True,
                         polymarket_key_id="00000000-0000-0000-0000-000000000000",
                         polymarket_secret_key="dGVzdC1vbmx5LW5vdC1hLWtleQ=="))  # gitleaks:allow - fake key (base64 of test text)
    yield r
    r.writer.close()


def test_no_keys_no_polymarket_service(tmp_path, monkeypatch):
    for k in ("BTCFLOW_POLYMARKET_KEY_ID", "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "n.db"))
    try:
        assert set(r.trading_services) == {"you"} and r.pm_gw is None and r.pm_market_stream is None
        assert r.phone_trading == {"kalshi_15m": r.you_trading, "polymarket_15m": None, "polymarket_5m": None}
        v = r.play.now_view(r.clock.now_ms(), "polymarket", span="15m")
        assert v["available"] is False and "not connected" in v["reason"]
    finally:
        r.writer.close()


def test_you_poly_is_wired(rt):
    svc = rt.trading_services["you_poly"]
    assert svc is rt.you_poly_trading and svc.strategy == "you_poly" and svc.mode == "shadow"
    assert svc.modes == {"shadow": "pm_shadow", "prod": "pm_prod"} and svc.venue == "Polymarket US"
    assert set(svc.gateways) == {"pm_shadow", "pm_prod"} and isinstance(svc.gateways["pm_prod"], PolymarketTrading)
    assert svc.config.fee_rate == 0.0695 and svc.config.series == "cpc-btc-updown-15m" and svc.config.whole_contracts
    # the phone's own user-set limits (read live; default none / none) and the cash backstop
    assert svc._limits_fn == rt.play_limits.for_trading and svc.cash_backstop
    assert svc.limits()[0] == {"per_trade": None, "per_window": None, "per_day": None}
    assert rt.pm_gw.max_order_cost == rt.settings.trading_order_hard_ceiling
    assert svc._index_price_fn == rt.live_price
    assert "secret" not in repr(svc.gateways["pm_prod"]).lower()
    assert rt.play.venues["polymarket"].svc is svc and rt.play.venues["polymarket"].yes_only
    # the kill switch is shared with the Kalshi tab
    run(rt.you_trading.kill(rt.clock.now_ms()))
    assert svc.killed() and rt.you_poly5_trading.killed()


def test_first_start_the_5m_tab_follows_the_15m_tabs_mode_then_keeps_its_own(rt):
    # the 15m tab starts on the service's safe default (shadow); the 5m tab follows it and stores it at once
    assert rt.you_poly_trading.mode == rt.you_poly5_trading.mode == "shadow"
    assert rt.store.select("trading_settings", {"key": "you_poly.mode"}) == []
    assert rt.store.select("trading_settings", {"key": "you_poly5.mode"})[0]["value"] == {"mode": "shadow"}


def test_streams_by_environment(rt):
    put = lambda e: None  # noqa: E731
    assert isinstance(rt._make_stream("pm_shadow", put), ShadowStream)
    st = rt._make_stream("pm_prod", put)
    assert isinstance(st, PolymarketTradingStream) and st.env == "pm_prod"
    st.close()
    assert rt._make_stream("pm_shadow", put).env == "pm_shadow"


def test_polymarket_services_know_each_others_and_the_retired_bots_orders(rt):
    slug = slug_for(1793206800000)
    rows = [{"bracket_id": f"b-{s}", "role": "entry", "ticker": slug, "side": "yes", "action": "buy", "count": 2.0,
             "price": 0.5, "tif": "ioc", "client_order_id": f"c-{s}", "order_id": f"x-{s}", "status": "executed",
             "created_ts": 1, "updated_ts": 1, "strategy": s} for s in ("pnw_poly", "you_poly", "you_poly5")]
    rt.store.insert("trading_orders", rows)
    assert rt.you_poly_trading._known_order_ids_fn() == {"c-pnw_poly", "x-pnw_poly", "c-you_poly5", "x-you_poly5"}
    assert rt.you_poly5_trading._known_order_ids_fn() == {"c-pnw_poly", "x-pnw_poly", "c-you_poly", "x-you_poly"}
    assert rt.you_poly_trading._known_order_ids_fn({"x-pnw_poly", "c-you_poly", "x-you_poly5"}) == {
        "x-pnw_poly", "x-you_poly5"}
    assert rt._pm_order_field("client_order_id", "c-you_poly", "order_id") == "x-you_poly"
    assert rt._pm_order_field("order_id", "x-pnw_poly", "client_order_id") == "c-pnw_poly"


def test_gateway_position_notes_and_seed_are_wired(rt):
    gw = rt.pm_gw
    assert gw._seed_fn == rt._pm_bracket_positions and gw._note_fn == rt._pm_position_note
    rt.store.insert("trading_brackets", [{
        "id": "b1", "env": "pm_prod", "strategy": "you_poly", "kind": "entry",
        "ticker": "cpc-btc-updown-15m-2026-10-29-1200z", "side": "DOWN", "window_end": 1, "stake": 1.0,
        "contracts": 1.0, "limit_price": 0.7, "take_profit": 0.99, "stop": 0.0, "max_cost": 0.71, "ticket_hash": "h",
        "state": "open", "filled": 1.0, "held": 1.0, "tp_filled": 0.0, "proceeds": 0.0, "exit_fees": 0.0,
        "exit_filled": 0.0, "created_ts": 1, "updated_ts": 1}])
    assert rt._pm_bracket_positions() == {"cpc-btc-updown-15m-2026-10-29-1200z": -1.0}
    rt._pm_position_note({"ts": 5, "env": "pm_prod", "slug": "s", "code": "position_mismatch", "reason": "r",
                          "detail": {"local": 1}})
    row = rt.store.select("trading_audit", {"strategy": "you_poly", "event": "position_check"})[0]
    assert row["code"] == "position_mismatch" and row["detail"]["ticker"] == "s"


def test_play_polymarket_reads_only_the_websocket_cache(rt):
    now = rt.clock.now_ms()
    now = now - now % 900_000 + 60_000  # a minute into a window (not in the last 2 minutes: "too late")
    assert rt._play_pm_quote(now, "15m") is None  # nothing streamed: no price, never a REST fallback
    v = rt.play.now_view(now, "polymarket", span="15m")
    assert v["available"] and v["strategy"] == "you_poly" and v["ready"] is False
    assert v["reason"].startswith("No live Polymarket price")


def test_you_poly5_trades_only_five_minute_windows_with_its_own_record(rt):
    svc, poly = rt.trading_services["you_poly5"], rt.you_poly_trading
    assert svc is rt.you_poly5_trading and svc.strategy == "you_poly5"
    assert svc.config.series == "cpc-btc-updown-5m" and svc.config.window_ms == 300_000
    assert svc.min_left_ms() == 60_000  # entries close at 1:00 left
    assert svc.gateways == poly.gateways and svc.modes == poly.modes and svc.mode == poly.mode
    assert svc._limits_fn == rt.play_limits.for_trading and svc.cash_backstop
    assert set(rt._other_pm_order_ids("you_poly5")) == set()
    pv = rt.play.venues["polymarket_5m"]
    assert pv.svc is svc and pv.yes_only and pv.window_ms == 300_000 and pv.strike_fn is not None


def test_five_minute_play_view_has_its_own_window_and_strike(rt):
    now = 1793206800000 + 65_000  # 12:01:05Z: inside the 12:00 5-minute window
    slug = slug_for(now, "5m")
    assert slug.startswith("cpc-btc-updown-5m-")
    v = rt.play.now_view(now, "polymarket", span="5m")
    assert v["available"] and v["strategy"] == "you_poly5" and v["span"] == "5m"
    assert v["window"]["end"] - v["window"]["start"] == 300_000 and v["window"]["entry_cutoff_ms"] == 60_000
    assert v["spans"]["polymarket"] == ["15m", "5m"] and v["spans"]["kalshi"] == ["15m"]
    assert v["ready"] is False  # no price to beat and no live price yet
    rt.pm_terms._strike[slug] = 80_000.0
    assert rt._pm_span_strike(now - now % 300_000, "5m") == 80_000.0
    assert rt.play.now_view(now, "kalshi", span="5m")["available"] is False  # Kalshi has no 5-minute tab
