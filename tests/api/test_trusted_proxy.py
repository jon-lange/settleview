"""Phones over HTTPS through the Tailscale sidecar (P3 #1, BTCFLOW_FORWARDED_ALLOW_IPS).

uvicorn's own ProxyHeadersMiddleware (what `proxy_headers=True, forwarded_allow_ips=...` installs on the phone
listener, bootstrap/app.py) applies X-Forwarded-Proto / X-Forwarded-For from the sidecar's address only. The access
middleware reads the result: an https scheme makes the session and device cookies `Secure` and adds HSTS, and the PIN
lockout counts the forwarded client (plus the device cookie). Everyone else's forwarded headers change nothing, and the
public HTTPS name is an allowed Host and Origin. The apps here are wrapped exactly as uvicorn wraps the main server."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from btc_signal_engine.adapters.http import access
from btc_signal_engine.adapters.http.access import COOKIE, AccessGuard, PinStore, install_access, public_hosts
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from btc_signal_engine.bootstrap.config import Settings

PIN = "482916"
PUBLIC = "https://settleview.example-tailnet.ts.net"
NAME = "settleview.example-tailnet.ts.net"
SIDECAR = ("10.89.250.2", 41000)  # the sidecar's fixed address on tailscale-edge
EDGE_OTHER = ("10.89.250.5", 41000)  # anything else on that network (never the sidecar)
LAN_PHONE = ("192.168.1.50", 50000)
PC_PORT = 8001


class Clock:
    def __init__(self, t: int = 1_000_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def make(*, trusted: str = "10.89.250.2"):
    app = FastAPI()

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict:
        return {"actor": request.scope["state"]["actor"], "scheme": request.url.scheme,
                "peer": request.client.host if request.client else None}

    @app.post("/api/thing")
    def thing() -> dict:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"actor": websocket.scope["state"]["actor"], "scheme": websocket.url.scheme})
        await websocket.close()

    guard = AccessGuard(PinStore(None), session_hours=12, clock_ms=Clock())
    install_access(app, guard, api_host="0.0.0.0", port=8000, lan_ip_fn=lambda: "192.168.1.10",
                   allowed_hosts="192.168.1.10", pc_port=PC_PORT, agent_port=None, public_url=PUBLIC)
    assert guard.set_pin(PIN, PIN)[0]
    # The main server as bootstrap/app.py builds it: uvicorn's proxy headers only when an address is configured.
    return (ProxyHeadersMiddleware(app, trusted_hosts=trusted) if trusted else app), guard


def via_sidecar(app, phone_ip: str | None = "100.101.102.103", proto: str | None = "https") -> TestClient:
    """A phone on the tailnet: Tailscale Serve connects from SIDECAR with the phone's Host and these headers."""
    headers = {}
    if proto is not None:
        headers["x-forwarded-proto"] = proto
    if phone_ip is not None:
        headers["x-forwarded-for"] = phone_ip
    return TestClient(app, client=SIDECAR, follow_redirects=False, base_url=f"http://{NAME}", headers=headers)


def lan(app, who=LAN_PHONE, **headers) -> TestClient:
    return TestClient(app, client=who, follow_redirects=False, base_url="http://192.168.1.10:8000", headers=headers)


def _session(c: TestClient) -> str:
    """Sign in (the device cookie first, as a browser does) and return the session cookie's value. httpx keeps a
    Secure cookie but never sends it over the test's plain-HTTP transport, so callers pass it on explicitly."""
    c.get("/login")
    r = c.post("/login", data={"pin": PIN}, headers=_cookie(_device_cookies(c)))
    assert r.status_code == 303, r.text
    return r.cookies[COOKIE]


def _cookie(values: dict) -> dict:
    """A Cookie header (httpx never sends a Secure cookie over the test's plain-HTTP transport)."""
    return {"cookie": "; ".join(f"{k}={v}" for k, v in values.items())} if values else {}


def _device_cookies(c: TestClient) -> dict:
    dev = c.cookies.get(access.DEVICE_COOKIE)
    return {access.DEVICE_COOKIE: dev} if dev else {}


# -- the setting ------------------------------------------------------------------------------------------------------

def test_the_setting_defaults_to_off_and_takes_addresses_and_networks(monkeypatch):
    monkeypatch.delenv("BTCFLOW_FORWARDED_ALLOW_IPS", raising=False)
    assert Settings(_env_file=None).forwarded_allow_ips == ""
    for good in ("10.89.250.2", "10.89.250.2, 10.89.250.0/29", "fd00::2"):
        monkeypatch.setenv("BTCFLOW_FORWARDED_ALLOW_IPS", good)
        assert Settings(_env_file=None).forwarded_allow_ips == good


@pytest.mark.parametrize("bad", ["tailscale", "10.89.250.300", "10.89.250.2/33", "*", "0.0.0.0/0", "::/0",
                                 "10.89.250.2;rm", "10.89.250.1/29"])
def test_an_invalid_forwarded_allow_ips_is_a_configuration_error(bad, monkeypatch):
    """uvicorn would keep a typo as a literal that never matches (or "*" as everyone): the start stops instead."""
    monkeypatch.setenv("BTCFLOW_FORWARDED_ALLOW_IPS", bad)
    with pytest.raises(ValueError, match="BTCFLOW_FORWARDED_ALLOW_IPS"):
        Settings(_env_file=None)


def test_public_hosts_add_the_bare_name_for_the_default_port():
    assert public_hosts(PUBLIC) == [f"{NAME}:443", NAME]
    assert public_hosts("http://192.168.1.10:8000") == ["192.168.1.10:8000"]
    assert public_hosts("https://pc.example:8443/") == ["pc.example:8443"]
    assert public_hosts("http://[fd00::1]") == ["[fd00::1]:80", "[fd00::1]"]
    assert public_hosts("") == []


# -- HTTPS through the trusted proxy ----------------------------------------------------------------------------------

def test_through_the_sidecar_cookies_are_secure_and_responses_carry_hsts():
    app, _ = make()
    c = via_sidecar(app)
    r = c.get("/login")
    assert r.status_code == 200 and "Encrypted (HTTPS)" in r.text and "not encrypted" not in r.text
    assert r.headers["strict-transport-security"] == access.HSTS
    device = r.headers["set-cookie"]
    assert access.DEVICE_COOKIE in device and "secure" in device.lower() and "Path=/login" in device
    r = c.post("/login", data={"pin": PIN}, headers=_cookie(_device_cookies(c)))
    assert r.status_code == 303 and r.headers["strict-transport-security"] == access.HSTS
    session = r.headers["set-cookie"]
    assert session.startswith(COOKIE + "=") and "secure" in session.lower() and "httponly" in session.lower()
    assert "samesite=strict" in session.lower()
    got = c.get("/api/whoami", headers=_cookie({COOKIE: r.cookies[COOKIE]}))
    assert got.status_code == 200 and got.headers["strict-transport-security"] == access.HSTS
    body = got.json()
    assert body["actor"].startswith("phone:") and body["scheme"] == "https"
    assert body["peer"] == "100.101.102.103"  # uvicorn's client: the phone's tailnet address
    assert c.get("/").headers["strict-transport-security"] == access.HSTS  # the redirect to /login too


def test_the_lan_port_stays_plain_http_whatever_a_phone_claims():
    app, _ = make()
    c = lan(app, **{"x-forwarded-proto": "https", "x-forwarded-for": "100.64.0.9"})
    r = c.get("/login")
    assert r.status_code == 200 and "not encrypted" in r.text
    assert "strict-transport-security" not in r.headers and "secure" not in r.headers["set-cookie"].lower()
    r = c.post("/login", data={"pin": PIN})
    assert r.status_code == 303 and "secure" not in r.headers["set-cookie"].lower()
    assert c.get("/api/whoami").json()["scheme"] == "http"


def test_another_peer_on_the_edge_network_is_not_trusted():
    app, _ = make()
    c = TestClient(app, client=EDGE_OTHER, follow_redirects=False, base_url=f"http://{NAME}",
                   headers={"x-forwarded-proto": "https", "x-forwarded-for": "100.64.0.9"})
    r = c.get("/login")
    assert "strict-transport-security" not in r.headers and "secure" not in r.headers["set-cookie"].lower()


def test_the_sidecar_over_plain_http_is_not_https():
    app, _ = make()
    r = via_sidecar(app, proto="http").get("/login")
    assert "strict-transport-security" not in r.headers and "secure" not in r.headers["set-cookie"].lower()
    r = via_sidecar(app, proto=None).get("/login")
    assert "strict-transport-security" not in r.headers


def test_without_the_setting_forwarded_headers_are_ignored():
    app, _ = make(trusted="")
    r = via_sidecar(app).get("/login")
    assert "strict-transport-security" not in r.headers and "secure" not in r.headers["set-cookie"].lower()


# -- the PIN lockout behind the sidecar ------------------------------------------------------------------------------

def test_each_tailnet_phone_has_its_own_lockout_bucket(caplog):
    """Every phone's TCP peer is the sidecar: the forwarded tailnet address (plus the device cookie) is the key."""
    app, _ = make()
    attacker, phone = via_sidecar(app, "100.64.0.66"), via_sidecar(app, "100.64.0.7")
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            assert attacker.post("/login", data={"pin": "111111"}).status_code == 401
        assert attacker.post("/login", data={"pin": "111111"}).status_code == 429
    assert attacker.post("/login", data={"pin": PIN}).status_code == 429  # its own address is locked
    assert any("100.64.0.66 locked out" in r.getMessage() for r in caplog.records)
    assert not any(SIDECAR[0] in r.getMessage() for r in caplog.records)
    assert phone.post("/login", data={"pin": PIN}).status_code == 303  # another phone signs in


def test_behind_one_forwarded_address_the_device_cookie_still_separates_phones():
    app, _ = make()
    attacker, phone = via_sidecar(app, "100.64.0.9"), via_sidecar(app, "100.64.0.9")
    attacker.get("/login"), phone.get("/login")
    for _ in range(5):
        attacker.post("/login", data={"pin": "111111"}, headers=_cookie(_device_cookies(attacker)))
    assert attacker.post("/login", data={"pin": PIN}, headers=_cookie(_device_cookies(attacker))).status_code == 429
    assert phone.post("/login", data={"pin": PIN}, headers=_cookie(_device_cookies(phone))).status_code == 303


def test_without_a_forwarded_address_the_sidecar_is_the_bucket_and_the_global_cap_holds():
    app, _ = make()
    script = via_sidecar(app, phone_ip=None)
    for _ in range(5):
        script.post("/login", data={"pin": "111111"})
    assert script.post("/login", data={"pin": PIN}).status_code == 429
    assert via_sidecar(app, "100.64.0.7").post("/login", data={"pin": PIN}).status_code == 303
    app, _ = make()  # rotating forwarded addresses (only the sidecar can) still meets the cap across all clients
    codes = [via_sidecar(app, f"100.64.1.{i}").post("/login", data={"pin": "111111"}).status_code
             for i in range(access.GLOBAL_MAX_FAILURES)]
    assert codes[-1] == 429
    assert via_sidecar(app, "100.64.2.1").post("/login", data={"pin": PIN}).status_code == 429


def test_a_lan_client_cannot_pick_its_lockout_bucket_with_forwarded_for():
    app, _ = make()
    c = lan(app)
    for i in range(5):
        c.post("/login", data={"pin": "111111"}, headers={"x-forwarded-for": f"100.64.0.{i}"})
    assert c.post("/login", data={"pin": PIN}, headers={"x-forwarded-for": "100.64.0.99"}).status_code == 429


# -- Host, Origin and WebSockets ---------------------------------------------------------------------------------------

def test_the_public_https_name_is_an_allowed_host_and_origin():
    app, _ = make()
    c = via_sidecar(app)
    sid = _session(c)
    ok = c.post("/api/thing", headers={**_cookie({COOKIE: sid}), "origin": PUBLIC})
    assert ok.status_code == 200
    assert c.post("/api/thing", headers={**_cookie({COOKIE: sid}), "origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/thing", headers={**_cookie({COOKIE: sid}),
                  "origin": "https://settleview.other-tailnet.ts.net"}).status_code == 403
    assert c.get("/api/whoami", headers={"host": "evil.example"}).status_code == 400
    assert c.get("/login", headers={"host": f"{NAME}:8000"}).status_code == 400  # only the name phones open


def test_a_websocket_through_the_sidecar_accepts_the_public_origin_only():
    app, _ = make()
    c = via_sidecar(app)
    sid = _session(c)
    with c.websocket_connect(f"ws://{NAME}/ws", headers={"origin": PUBLIC, "cookie": f"{COOKIE}={sid}"}) as s:
        got = s.receive_json()
    assert got["actor"].startswith("phone:") and got["scheme"] == "wss"
    for origin in ("https://evil.example", "null", f"http://{NAME}:8000"):
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(f"ws://{NAME}/ws", headers={"origin": origin, "cookie": f"{COOKIE}={sid}"}):
                pass
    with pytest.raises(WebSocketDisconnect):  # no session: refused as before
        with c.websocket_connect(f"ws://{NAME}/ws", headers={"origin": PUBLIC}):
            pass
