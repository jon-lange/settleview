"""Trading HTTP routes: the full flow over HTTP, modes, status without key material, several Play tabs at once, and
no route of the retired auto / micro-live sessions."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from btc_signal_engine.application.trading import PROD_CONFIRM, TradingConfig, TradingService
from tests.trading.helpers import NOW, Exchange, Ids, Market, gateway, proposal, ready, service
from tests.pc import as_pc
from tests.trading.test_strategy_scope import svc_for, up


def client_for(svc):
    app = FastAPI()
    register_trading_routes(app, {svc.strategy: svc}, lambda: NOW)
    return TestClient(as_pc(app))


def test_prepare_approve_status_over_http(store):
    ex = Exchange(Market())
    svc = asyncio.run(ready(service(store, ex)))
    c = client_for(svc)
    assert c.post("/api/trading/heartbeat", json={"visible": True}).json()["present"]
    prep = c.post("/api/trading/prepare", json=proposal(strategy="you")).json()
    assert prep["ok"] and prep["ticket"]["limit_price"] == 0.58 and "hash" not in prep["ticket"]
    r = c.post("/api/trading/approve", json={"token": prep["token"], "decision": "yes", "ticket": prep["ticket"],
                                             "strategy": "you"}).json()
    assert r["ok"] and r["bracket"]["state"] == "open"
    again = c.post("/api/trading/approve", json={"token": prep["token"], "decision": "yes", "ticket": prep["ticket"],
                                                 "strategy": "you"})
    assert again.json()["code"] == "bad_token"
    st = c.get("/api/trading/status", params={"strategy": "you"}).json()
    assert st["mode"] == "prod" and st["open"] and st["exposure"]["open_positions"] == 1
    assert set(st["modes"]) == {"sandbox", "shadow", "prod"} and st["modes"]["prod"]["gated_on"]
    assert not {"session", "auto", "micro_gate", "lab"} & set(st)
    blob = json.dumps(st)
    assert "test-key" not in blob and "PRIVATE" not in blob
    assert c.post("/api/trading/prepare", json=proposal(strategy="you", side="SIDEWAYS")).status_code == 422
    assert c.post("/api/trading/kill").json()["killed"]
    assert c.post("/api/trading/prepare", json=proposal(strategy="you")).json()["code"] == "killed"
    assert c.post("/api/trading/resume").json()["killed"] is False
    for path in ("/api/trading/session", "/api/trading/auto", "/api/trading/audit", "/api/trading/trades"):
        assert c.get(path, params={"strategy": "you"}).status_code == 404, path
    for path in ("/api/trading/session/start", "/api/trading/auto/start"):
        assert c.post(path, json={}).status_code == 404, path


def test_switch_to_prod_needs_typed_confirmation_and_configuration(store):
    ex = Exchange(Market())
    svc = TradingService(store, {"demo": gateway(ex, "demo"), "prod": gateway(ex, "prod")},
                         TradingConfig(enabled=True), id_fn=Ids(), token_fn=Ids(), strategy="you")
    c = client_for(svc)
    assert svc.mode == "sandbox"
    assert c.post("/api/trading/mode", json={"mode": "prod", "strategy": "you"}).json()["code"] == "confirm"
    assert c.post("/api/trading/mode", json={"mode": "prod", "confirm": "yes", "strategy": "you"}).json()["code"] == "confirm"
    r = c.post("/api/trading/mode", json={"mode": "prod", "confirm": PROD_CONFIRM, "strategy": "you"}).json()
    assert r["ok"] and r["mode"] == "prod"
    assert store.select("trading_settings", {"key": "you.mode"})[0]["value"] == {"mode": "prod"}
    assert c.post("/api/trading/mode", json={"mode": "shadow", "strategy": "you"}).json()["mode"] == "shadow"
    off = TradingService(store, {"demo": gateway(ex, "demo"), "prod": None}, TradingConfig(enabled=True),
                         strategy="you")
    assert asyncio.run(off.set_mode("prod", NOW, PROD_CONFIRM))["code"] == "not_configured"
    assert ex.writes() == []


def test_prod_credential_rejection_falls_back_to_sandbox(store):
    async def go():
        import httpx

        class Unauthorized(Exchange):
            async def handler(self, request):
                if request.method == "POST":
                    return httpx.Response(401, json={"error": {"code": "unauthorized"}})
                return await super().handler(request)
        ex = Unauthorized(Market())
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW)
        assert r["code"] == "rejected" and svc.mode == "sandbox"
    asyncio.run(go())


def test_routes_pick_the_named_tab_and_refuse_unknown_strategies(store):
    ex = Exchange(Market())
    you = asyncio.run(up(svc_for(store, ex, "you", mode="shadow")))
    poly = asyncio.run(up(svc_for(store, Exchange(Market()), "you_poly", mode="shadow")))
    app = FastAPI()
    register_trading_routes(app, {"you": you, "you_poly": poly}, lambda: NOW)
    c = TestClient(as_pc(app))
    assert c.get("/api/trading/status", params={"strategy": "you_poly"}).json()["strategy"] == "you_poly"
    assert c.get("/api/trading/status", params={"strategy": "you"}).json()["strategy"] == "you"
    assert c.get("/api/trading/status").status_code == 422  # no strategy: refused, never some tab by default
    r = c.get("/api/trading/status", params={"strategy": "nope"}).json()
    assert r == {"ok": False, "code": "unknown_strategy",
                 "reason": "There is no strategy called 'nope' (known: you, you_poly)."}
    # the per-strategy mode switch keeps the production confirmation
    m = c.post("/api/trading/mode", json={"mode": "prod", "strategy": "you_poly"}).json()
    assert m["ok"] is False and m["code"] == "confirm"
    # one presence heartbeat counts for every tab's service
    c.post("/api/trading/heartbeat", json={"visible": True})
    assert you.present(NOW) and poly.present(NOW)
    # the kill switch reaches every service at once
    assert c.post("/api/trading/kill").json()["killed"]
    assert you.killed() and poly.killed()
    assert ex.writes() == []
