"""Ask Settleview's routes (adapters/http/copilot_routes.py): required mode and warnings, "off" refused, unknown keys
422, and the shown record."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.copilot_routes import register_copilot_routes
from tests.application.test_copilot import world
from tests.application.test_your_record import bracket
from tests.trading.helpers import NOW


def client(store):
    r, cp = world(store)
    app = FastAPI()
    register_copilot_routes(app, cp, lambda: NOW)
    return r, TestClient(app)


def test_now_needs_mode_and_warnings_and_refuses_unknown_keys(store):
    _, c = client(store)
    ok = c.get("/api/copilot/now", params={"venue": "kalshi", "span": "15m", "mode": "full", "warn": "late,against_btc"})
    assert ok.status_code == 200 and ok.json()["warnings_on"] == ["against_btc", "late"]
    assert c.get("/api/copilot/now", params={"venue": "kalshi", "span": "15m", "mode": "quiet", "warn": "none"}).status_code == 200
    for bad in ({"mode": "off", "warn": "none"}, {"mode": "full"}, {"warn": "none"}, {"mode": "full", "warn": "lucky"},
                {"mode": "full", "warn": ""}, {"mode": "full", "warn": "late,"}):
        assert c.get("/api/copilot/now", params={"venue": "kalshi", "span": "15m", **bad}).status_code == 422, bad
    for span in ("5m", "1h"):  # no Kalshi market for that span: 200 available false, as /api/play/now answers
        r = c.get("/api/copilot/now", params={"venue": "kalshi", "span": span, "mode": "full", "warn": "none"})
        assert r.status_code == 200 and r.json()["available"] is False and r.json()["reason"]


def test_shown(store):
    _, c = client(store)
    store.insert("trading_brackets", [{**bracket(1, state="open"), "env": "shadow"}])
    body = {"strategy": "you", "bracket_id": "b1",
            "shown": {"mode": "quiet", "side": "DOWN", "warning": {"keys": ["late"], "text": "This is late."},
                      "btc_gap": 12.0, "left_ms": 150_000}}
    assert c.post("/api/copilot/shown", json=body).json() == {"ok": True, "repeat": False}
    assert c.post("/api/copilot/shown", json={**body, "shown": {**body["shown"], "mode": "off"}}).status_code == 422
    bad = {**body, "shown": {**body["shown"], "warning": {"keys": ["lucky"], "text": "x"}}}
    assert c.post("/api/copilot/shown", json=bad).status_code == 422
