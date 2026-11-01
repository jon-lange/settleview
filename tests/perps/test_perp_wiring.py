"""Perps wiring (perps plan U8): the feature flag off builds nothing that talks to Kalshi and the view says so; the
startup check refuses trading with Kalshi's own reason (unknown subaccount, margin not enabled); perp stream events
never reach the window trading services; each mode signs only with its own key and talks only to its own hosts; and
KD7's one daily total reaches the existing phone services. Keys are generated here; nothing is read from .env and
nothing goes to the network (httpx MockTransport)."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.app import create_app
from btc_signal_engine.adapters.kalshi.margin import BASE_URLS
from btc_signal_engine.application.perp_trading import PerpConfig, PerpTradingService
from btc_signal_engine.application.play_limits import CommittedToday
from btc_signal_engine.application.trading import TradingConfig
from btc_signal_engine.bootstrap.app import Runtime, margin_from_settings, perps_access_check
from btc_signal_engine.bootstrap.config import Settings
from tests.perps.helpers import FakeMargin, FakeQuotes, Ids, entry
from tests.trading.helpers import NOW as T_NOW, Exchange, Market, proposal
from tests.trading.test_strategy_scope import svc_for, up
from tests.pc import paired

NOW = 1793350800000  # 2026-10-30 09:00:00 UTC
HOSTS = {env: httpx.URL(url).host for env, url in BASE_URLS.items()}
TICKERS = {"perp_demo": "KXBTCPERP1", "perp_prod": "KXBTCPERP"}


def run(coro):
    return asyncio.run(coro)


def key_file(tmp_path, name: str) -> tuple[ed25519.Ed25519PrivateKey, str]:
    """A throwaway Ed25519 key written for this test only."""
    key = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / f"{name}.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return key, str(path)


class KalshiPerps:
    """A MockTransport answering Kalshi's perps reads on BOTH hosts (by host), recording every request: host, path,
    key id and signature. `subaccounts` are what /margin/balance lists; `enabled` is /margin/enabled."""

    def __init__(self, *, subaccounts=(0, 64), enabled: bool = True) -> None:
        self.subaccounts = subaccounts
        self.enabled = enabled
        self.seen: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.seen.append({"host": req.url.host, "method": req.method, "path": req.url.path,
                          "key": req.headers.get("KALSHI-ACCESS-KEY"), "ts": req.headers.get("KALSHI-ACCESS-TIMESTAMP"),
                          "sig": req.headers.get("KALSHI-ACCESS-SIGNATURE")})
        env = next(e for e, h in HOSTS.items() if h == req.url.host)
        t = TICKERS[env]
        path = req.url.path.removeprefix("/trade-api/v2")
        body = {
            "/margin/enabled": {"enabled": self.enabled},
            "/margin/balance": {"settled_funds": "50.0000", "subaccount_balances": [
                {"subaccount": s, "account_equity": "50.0000" if s == 0 else "0", "available_balance": "50.0000"}
                for s in self.subaccounts]},
            "/margin/exchange/status": {"exchange_active": True, "trading_active": True},
            f"/margin/markets/{t}": {"market": {"ticker": t, "status": "active", "tick_size": "0.0001",
                                                "contract_size": "0.0001",
                                                "long_leverage_estimates": {"1000": 6.8473},
                                                "short_leverage_estimates": {"1000": 6.38}}},
            "/margin/risk_parameters": {"liquidation_margin_ratio_threshold": 1.0,
                                        "initial_margin_multiplier": {t: 1.3}},
            "/margin/fee_tiers": {"taker_fee_rates": {t: 0.0012}, "maker_fee_rates": {t: 0.0002}},
            "/margin/positions": {"positions": []},
            "/margin/risk": {"positions": []},
            "/margin/orders": {"orders": [], "cursor": ""},
            "/margin/funding_rates/estimate": {"market_ticker": t, "funding_rate": 0.0001, "mark_price": "7.7970",
                                               "next_funding_time": "2026-10-30T12:00:00Z"},
            "/margin/notional_risk_limit": {"default_notional_value_risk_limit": "",
                                            "notional_value_risk_limits_by_market_ticker": {t: "500.0000"},
                                            "current_usage_by_market_ticker": {}, "total_current_usage": "0"},
        }.get(path)
        if body is None:
            return httpx.Response(404, json={"code": "not_found", "path": path})
        return httpx.Response(200, json=body)


class Stale(FakeQuotes):
    """No stream in these tests: the quote cache says why (the service refuses entries with it)."""

    def __init__(self) -> None:
        super().__init__(FakeMargin())
        self.why = "not connected yet"


def perps_service(store, gateways: dict, mode: str) -> PerpTradingService:
    store.insert("trading_settings", [{"key": "perps.mode", "value": {"mode": mode}, "updated_ts": 0}],
                 ignore_conflicts=False)
    return PerpTradingService(store, gateways, PerpConfig(prod_enabled=True),
                              quotes={e: Stale() for e in gateways}, access_check=perps_access_check,
                              id_fn=Ids(), token_fn=Ids("tok"), now=NOW)


def settings(tmp_path, **kw) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "wiring.db", trading_stream=False, **kw)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY", "BTCFLOW_PERPS_ENABLED", "BTCFLOW_PERPS_SUBACCOUNT",
              "BTCFLOW_TRADING_ENABLED", "BTCFLOW_TRADING_DEMO_KEY_ID", "BTCFLOW_TRADING_DEMO_KEY_PATH",
              "BTCFLOW_TRADING_PROD_KEY_ID", "BTCFLOW_TRADING_PROD_KEY_PATH", "BTCFLOW_KALSHI_KEY_ID",
              "BTCFLOW_KALSHI_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)


# -- the feature flag ------------------------------------------------------------------------------------------------
def test_perps_switched_off_starts_no_margin_client_loop_or_stream_and_the_view_says_so(tmp_path):
    rt = Runtime(settings(tmp_path))
    try:
        assert rt.perps is None and rt.perp_clients == {} and rt.perp_streams == {}
        assert not {"perps", "perps_push"} & set(rt.loops())
        c = paired(TestClient(create_app(rt.services()), client=("127.0.0.1", 1), base_url="http://127.0.0.1:8000"),
                   rt.settings.data_dir)
        v = c.get("/api/perps/view").json()
        assert v["enabled"] is False and v["enabled_reason"] == "Perps are switched off"
        assert v["quote"] is None and v["can"]["entry"] is False and v["can"]["entry_reason"] == "Perps are switched off"
        assert c.post("/api/perps/prepare", json={**entry(), "band": None}).json() == {
            "ok": False, "code": "perps_off", "reason": "Perps are switched off: nothing was sent."}
        h = c.get("/api/health").json()
        assert h["perps"] == {"enabled": False, "mode": None, "ready": False,
                              "ready_reason": "Perps are switched off", "stream": None}
        assert "loop_lag" in h
        assert c.post("/api/trading/kill").status_code == 200  # the shared controls still work without perps
    finally:
        rt.writer.close()


# -- the startup check -----------------------------------------------------------------------------------------------
def test_an_unknown_subaccount_number_refuses_perps_trading_with_that_reason(store, tmp_path):
    kalshi = KalshiPerps(subaccounts=(0, 64))
    _, path = key_file(tmp_path, "prod")
    client, _, why = margin_from_settings("perp_prod", "prod-key-id", path, subaccount=5, max_notional=2_000,
                                          transport=kalshi.transport())
    assert why == ""
    svc = perps_service(store, {"perp_prod": client}, "perp_prod")
    res = run(svc.reconcile(NOW))
    reason = ("Kalshi lists no perps subaccount 5 on this account (it lists 0, 64): check BTCFLOW_PERPS_SUBACCOUNT; "
              "nothing can be sent.")
    assert res == {"ok": False, "reason": reason}
    v = svc.view(NOW)
    assert v["ready"] is False and v["ready_reason"] == reason
    svc.heartbeat(NOW)
    prep = run(svc.prepare(entry(), NOW))
    assert prep == {"ok": False, "code": "not_reconciled", "reason": reason}
    # the check ran first: nothing past the two reads it needs
    assert [r["path"] for r in kalshi.seen] == ["/trade-api/v2/margin/enabled", "/trade-api/v2/margin/balance",
                                                "/trade-api/v2/margin/balance"]  # (prepare re-reads the balance)


def test_margin_not_enabled_refuses_perps_trading_with_kalshis_answer(store, tmp_path):
    kalshi = KalshiPerps(enabled=False)
    _, path = key_file(tmp_path, "demo")
    client, _, _ = margin_from_settings("perp_demo", "demo-key-id", path, subaccount=0, max_notional=2_000,
                                        transport=kalshi.transport())
    svc = perps_service(store, {"perp_demo": client}, "perp_demo")
    assert run(svc.reconcile(NOW))["ok"] is False
    assert svc.view(NOW)["ready_reason"] == ("Kalshi says perps (margin) trading is not enabled on this perp_demo "
                                             "account; nothing can be sent.")


def test_a_mode_whose_key_is_missing_is_refused_with_a_stated_reason(store):
    client, auth, why = margin_from_settings("perp_prod", None, None, subaccount=0, max_notional=2_000)
    assert (client, auth) == (None, None)
    assert why == ("No production trading key is configured (BTCFLOW_TRADING_PROD_KEY_ID / _KEY_PATH): perps "
                   "production is unavailable.")
    svc = perps_service(store, {"perp_prod": None}, "off")
    assert run(svc.set_mode("perp_demo", NOW)) == {"ok": False, "code": "not_configured",
                                                   "reason": "No demo perps trading key is configured."}


# -- environments never cross ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["perp_demo", "perp_prod"])
def test_each_mode_signs_only_with_its_own_key_and_talks_only_to_its_own_host(store, tmp_path, mode):
    kalshi = KalshiPerps()
    keys = {}
    clients = {}
    for env, kid in (("perp_demo", "demo-key-id"), ("perp_prod", "prod-key-id")):
        key, path = key_file(tmp_path, env)
        keys[env] = (kid, key.public_key())
        clients[env], _, _ = margin_from_settings(env, kid, path, subaccount=0, max_notional=2_000,
                                                  transport=kalshi.transport())
    svc = perps_service(store, clients, mode)
    assert run(svc.reconcile(NOW))["ok"] is True
    run(svc.tick(NOW + 2_000))
    run(svc.prepare(entry(), NOW + 2_100))  # refused (no live price here), after its balance read
    kid, pub = keys[mode]
    assert len(kalshi.seen) >= 12
    for r in kalshi.seen:
        assert r["host"] == HOSTS[mode] and r["key"] == kid
        pub.verify(base64.b64decode(r["sig"]), (r["ts"] + r["method"] + r["path"]).encode())  # raises if not


def test_the_runtime_gives_each_env_its_own_client_signer_and_stream_host(tmp_path):
    _, demo = key_file(tmp_path, "demo")
    _, prod = key_file(tmp_path, "prod")
    rt = Runtime(settings(tmp_path, perps_enabled=True, trading_enabled=True, trading_demo_key_id="demo-key-id",
                          trading_demo_key_path=demo, trading_prod_key_id="prod-key-id", trading_prod_key_path=prod))
    try:
        for env in ("perp_demo", "perp_prod"):
            c, st = rt.perp_clients[env], rt.perp_streams[env]
            assert c.env == env and c.base_url == BASE_URLS[env] and c.subaccount == 0
            assert st.env == env and st._signer.env == env
            assert st.url.startswith("wss://external-api-margin-ws" + (".demo" if env == "perp_demo" else ""))
            assert rt.perps.gateways[env] is c
        assert float(rt.perp_clients["perp_prod"].max_notional) == 2_000.0  # perps_order_hard_ceiling
        assert rt.perp_view.modes_available() == ["off", "perp_demo", "perp_prod"]
    finally:
        rt.writer.close()


def test_production_perps_need_the_production_master_switch(tmp_path):
    _, prod = key_file(tmp_path, "prod")
    rt = Runtime(settings(tmp_path, perps_enabled=True, trading_prod_key_id="prod-key-id",
                          trading_prod_key_path=prod))
    try:
        assert rt.perp_clients == {}  # no prod client without trading_enabled; no demo key either
        v = rt.perp_view.view(NOW)
        assert v["modes_available"] == ["off"]
        assert v["modes_unavailable"] == {
            "perp_demo": "No demo trading key is configured (BTCFLOW_TRADING_DEMO_KEY_ID / _KEY_PATH): perps demo is "
                         "unavailable.",
            "perp_prod": "Production trading is turned off on this server (BTCFLOW_TRADING_ENABLED)."}
    finally:
        rt.writer.close()


# -- events and loops --------------------------------------------------------------------------------------------------
def test_perp_stream_events_never_reach_the_window_trading_services(tmp_path):
    _, demo = key_file(tmp_path, "demo")
    rt = Runtime(settings(tmp_path, perps_enabled=True, trading_demo_key_id="demo-key-id",
                          trading_demo_key_path=demo))
    try:
        assert rt.perps is not None and rt.perps not in rt.trading_services.values()
        assert {"perps", "perps_push"} <= set(rt.loops())
        window, perps = [], []
        for name, svc in rt.trading_services.items():
            async def spy(ev, now, name=name):
                window.append((name, ev))
            svc.on_stream_event = spy

        async def perp_spy(ev, now):
            perps.append(ev)
        rt.perps.on_stream_event = perp_spy
        fill = {"type": "fill", "env": "perp_demo", "trade_id": "t1", "order_id": "o1", "client_order_id": None,
                "ticker": "KXBTCPERP1", "is_taker": True, "side": "bid", "price": 7.8, "count": 1.0, "fee": 0.0094,
                "post_position": 1.0, "subaccount": 0, "order_source": "user", "ts_ms": NOW}

        async def go():
            rt._on_perps_event(fill)
            rt._on_perps_event({"type": "book_top", "env": "perp_demo", "ticker": "KXBTCPERP1", "bid": 7.79,
                                "ask": 7.8, "ts_ms": NOW})
            while not rt.perp_events.empty():
                await rt._perps_apply(rt.perp_events.get_nowait())
        run(go())
        assert perps == [fill]  # the book top only republishes the view
        assert window == []
    finally:
        rt.writer.close()


# -- KD7: one daily total ---------------------------------------------------------------------------------------------
def test_the_phone_services_and_perps_share_one_daily_total(tmp_path):
    _, demo = key_file(tmp_path, "demo")
    rt = Runtime(settings(tmp_path, perps_enabled=True, trading_demo_key_id="demo-key-id",
                          trading_demo_key_path=demo))
    try:
        phones = [s for s in rt.phone_trading.values() if s is not None]
        assert phones and all(s._committed_today_fn == rt.committed_today.total for s in phones)
        assert rt.perps._committed_today_fn == rt.committed_today.total
        rt.store.insert("perp_orders", [perp_order("a", "perp_prod", 7.0, NOW)])
        assert rt.committed_today.total("prod", NOW) == 7.0  # Kalshi event prod and perps prod: one pool
        assert rt.committed_today.total("perp_demo", NOW) == 0.0
    finally:
        rt.writer.close()


def perp_order(oid: str, env: str, collateral: float, created_ms: int) -> dict:
    return {"id": oid, "env": env, "position_id": "p", "client_order_id": f"svperp-{oid}", "order_id": f"o-{oid}",
            "role": "entry", "side": "bid", "price": 7.8, "count": 3.0, "collateral": collateral,
            "time_in_force": "immediate_or_cancel", "reduce_only": False, "filled_count": 3.0,
            "avg_fill_price": 7.8, "fees": 0.03, "outcome": "filled", "reason": None, "created_ms": created_ms,
            "updated_ms": created_ms}


def test_the_event_services_day_check_counts_perps_collateral(store):
    total = CommittedToday()
    perps = PerpTradingService(store, {}, PerpConfig())
    total.add_source(perps.committed_by_env)
    svc = svc_for(store, Exchange(Market()), "you", config=TradingConfig(enabled=True, max_dollars_per_day=10.0),
                  committed_today_fn=total.total)
    total.add_source(svc.committed_by_env)
    store.insert("perp_orders", [perp_order("a", "perp_prod", 7.0, T_NOW - 60_000)])

    async def go():
        await up(svc)
        return await svc.prepare(proposal(stake=5.0), T_NOW)
    r = run(go())
    assert r["ok"] is False and r["code"] == "over_day_limit"
    assert r["reason"].startswith("Today already has $7.00 in across event contracts and perps")
