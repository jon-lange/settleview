"""Who acted (R13, KTD5): every trading_audit row written for a request through the real access middleware and route
carries "pc" or "phone:<first 8 characters of the session nonce>" (never the full cookie), also from sync routes that
FastAPI runs in its threadpool; rows written outside a request (reconcile, the monitor tick's settlement) say
"system"."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.access import COOKIE, AccessGuard, PinStore, install_access
from btc_signal_engine.adapters.http.play_routes import register_play_routes
from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from btc_signal_engine.application.play import PlayService, PlayVenue
from btc_signal_engine.application.play_limits import PlayLimits
from tests.trading.helpers import CLOSE, NOW, Exchange, Market, proposal, ready, service
from tests.pc import paired

PIN = "482916"


def _app(store):
    ex = Exchange(Market())
    svc = asyncio.run(ready(service(store, ex)))
    svc._test_market = ex.market
    lim = PlayLimits(store)
    app = FastAPI()
    register_trading_routes(app, {svc.strategy: svc}, lambda: NOW)
    register_play_routes(app, PlayService({"kalshi": PlayVenue(svc)}, evaluation_fn=lambda: {}), lambda: NOW,
                         limits=lim)
    install_access(app, AccessGuard(PinStore(store), session_hours=12, clock_ms=lambda: NOW), api_host="0.0.0.0",
                   port=8000, lan_ip_fn=lambda: "192.168.1.10", allowed_hosts="192.168.1.10")
    pc = paired(TestClient(app, client=("127.0.0.1", 1), follow_redirects=False, base_url="http://127.0.0.1:8000"))
    return app, svc, pc


def _phone(app, pc):
    assert pc.post("/api/access/pin", json={"pin": PIN, "pin_again": PIN}).json()["ok"]
    phone = TestClient(app, client=("192.168.1.50", 1), follow_redirects=False, base_url="http://192.168.1.10:8000")
    assert phone.post("/login", data={"pin": PIN}).status_code == 303
    token = phone.cookies.get(COOKIE)
    return phone, token, "phone:" + token.split(".")[2][:8]


def _rows(store, after: int = 0) -> list[dict]:
    return [r for r in store.select("trading_audit", order_by=["id"]) if r["id"] > after]


def _approve(c) -> None:
    prep = c.post("/api/trading/prepare", json=proposal(strategy="you")).json()
    assert prep["ok"], prep
    r = c.post("/api/trading/approve", json={"token": prep["token"], "decision": "yes", "ticket": prep["ticket"],
                                             "strategy": "you"}).json()
    assert r["ok"] and r["bracket"]["state"] == "open", r


def test_an_approve_from_the_pc_writes_pc_and_background_rows_write_system(store):
    app, svc, pc = _app(store)
    before = _rows(store)
    assert before and {r["actor"] for r in before} == {"system"}  # the reconcile at start-up
    mark = before[-1]["id"]
    _approve(pc)
    rows = _rows(store, mark)
    assert {"proposed", "approved", "sent"} <= {r["event"] for r in rows}
    assert {r["actor"] for r in rows} == {"pc"}
    mark = rows[-1]["id"]
    assert pc.post("/api/trading/kill").json()["killed"]  # async route
    assert pc.post("/api/trading/resume").json()["killed"] is False  # sync route: runs in the threadpool
    assert [(r["event"], r["actor"]) for r in _rows(store, mark)] == [("kill", "pc"), ("resume", "pc")]
    mark = _rows(store)[-1]["id"]
    svc._test_market.settle("yes")  # the official result is in at the close
    asyncio.run(svc.tick(CLOSE))  # the monitor tick, outside any request
    settled = [r for r in _rows(store, mark) if r["event"] == "settled"]
    assert settled and {r["actor"] for r in _rows(store, mark)} == {"system"}


def test_an_approve_from_a_signed_in_phone_writes_the_session_prefix_never_the_token(store):
    app, svc, pc = _app(store)
    phone, token, actor = _phone(app, pc)
    mark = _rows(store)[-1]["id"]
    _approve(phone)
    assert phone.post("/api/trading/kill").json()["killed"]
    rows = _rows(store, mark)
    assert {"proposed", "approved", "sent", "kill"} <= {r["event"] for r in rows}
    assert {r["actor"] for r in rows} == {actor}
    assert len(actor) == len("phone:") + 8 and token.split(".")[2] not in actor
    assert all(token not in str(r) for r in rows)


def test_limits_and_market_band_changes_record_the_actor(store):
    app, svc, pc = _app(store)
    assert pc.post("/api/play/limits", json={"per_trade": 25, "per_day": 100}).json()["ok"]
    assert pc.post("/api/play/market_band", json={"cents": 10}).json()["ok"]
    phone, _, actor = _phone(app, pc)
    assert phone.post("/api/play/limits", json={"per_trade": 10, "per_day": 100}).json()["ok"]
    rows = [(r["event"], r["actor"]) for r in _rows(store) if r["event"] in ("limits_changed", "market_band_changed")]
    assert rows == [("limits_changed", "pc"), ("market_band_changed", "pc"), ("limits_changed", actor)]
