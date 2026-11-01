"""The Coach routes Play uses: /api/coach/series and /api/coach/pause (temporary SQLite store; rows are synthetic)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.coach_routes import register_coach_routes
from btc_signal_engine.application.coach import CoachService

from tests.coach.test_service import AT, T, live_quote, rising, seed_index, snap, tick
from tests.pc import paired


@pytest.fixture
def client(store):
    coach = CoachService(store, store, (60, 300, 900))
    seed_index(store, T - 1_920_000, AT + 1_000, rising)
    tick(coach, AT + 200, snap(), {}, live_quote(AT + 100))
    app = FastAPI()
    register_coach_routes(app, coach, lambda: AT + 400)
    return TestClient(app)


def test_series_current_and_by_window(client):
    cur = client.get("/api/coach/series", params={"window": "current"}).json()
    assert cur["boundary"] == T and cur["now"] == AT + 400 and cur["points"]
    assert client.get("/api/coach/series", params={"window": str(T)}).json()["boundary"] == T
    assert client.get("/api/coach/series", params={"window": "x"}).status_code == 422
    assert client.get("/api/coach/series", params={"window": str(T + 1)}).status_code == 422  # not a boundary
    assert client.get("/api/coach/series", params={"window": str(T + 900_000)}).status_code == 422  # future


def test_pause_get_and_set(client):
    assert client.get("/api/coach/pause").json()["paused"] is False
    assert client.post("/api/coach/pause", json={"paused": True}).json()["paused"] is True
    assert client.get("/api/coach/pause").json() == {**client.get("/api/coach/pause").json(), "paused": True,
                                                     "since": AT + 400}
    assert client.post("/api/coach/pause", json={}).status_code == 422


def test_retired_coach_routes_are_gone(client):
    for path in ("/api/coach", "/api/coach/history", "/api/coach/gate", "/api/coach/adverse", "/api/coach/money"):
        assert client.get(path, params={"horizon": 900}).status_code == 404


def test_a_phone_can_pause_but_only_the_pc_can_unpause(client):
    """Un-pausing is half of turning the kill switch off (entries become possible again): PC-only, like resume."""
    from btc_signal_engine.adapters.http.access import AccessGuard, PinStore, install_access
    app = client.app
    install_access(app, AccessGuard(PinStore(None), session_hours=12, clock_ms=lambda: AT + 400), api_host="0.0.0.0",
                   port=8000, lan_ip_fn=lambda: "192.168.1.10", allowed_hosts="192.168.1.10")
    pc = paired(TestClient(app, client=("127.0.0.1", 1), follow_redirects=False, base_url="http://127.0.0.1:8000"))
    assert pc.post("/api/access/pin", json={"pin": "482916", "pin_again": "482916"}).json()["ok"]
    phone = TestClient(app, client=("192.168.1.50", 1), follow_redirects=False, base_url="http://192.168.1.10:8000")
    phone.post("/login", data={"pin": "482916"})
    assert phone.post("/api/coach/pause", json={"paused": True}).json()["paused"] is True
    r = phone.post("/api/coach/pause", json={"paused": False})
    assert r.status_code == 403 and r.json() == {"detail": "Only from the PC itself."}
    assert phone.get("/api/coach/pause").json()["paused"] is True
    assert pc.post("/api/coach/pause", json={"paused": False}).json()["paused"] is False
