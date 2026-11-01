"""Phone access: local requests pass; with no PIN every remote request is refused; the PIN is set only from the PC and
stored hashed; login, lockout, cookie expiry and PIN changes ending old sessions."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import threading
import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from btc_signal_engine.adapters.http import access
from btc_signal_engine.adapters.http.access import (COOKIE, MAX_FAILURES, OFF_TEXT, PBKDF2_ITERATIONS, RESET_TEXT,
                                                    SETTING, AccessGuard, PinStore, hash_pin, install_access)
from btc_signal_engine.adapters.http.app import WEB_DIR
from btc_signal_engine.adapters.pc_credentials import (MAX_PAIR_FAILURES, PAIRING_FILE, PC_COOKIE, RELOAD_MS,
                                                       SECRET_FILE, PcCredentials, create_pairing_code,
                                                       rotate_secret_file)
from tests.pc import as_actor, paired

PIN = "482916"
REMOTE = ("192.168.1.50", 50000)
LOCAL = ("127.0.0.1", 50000)


class Clock:
    def __init__(self, t: int = 1_000_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def make(repo=None, pin: str | None = PIN, clock: Clock | None = None):
    clock = clock or Clock()
    app = FastAPI()

    @app.get("/api/thing")
    def thing() -> dict:
        return {"ok": True}

    @app.post("/api/thing")
    def post_thing() -> dict:
        return {"posted": True}

    @app.get("/")
    def page():
        return {"page": True}

    guard = AccessGuard(PinStore(repo), session_hours=12, clock_ms=clock)
    install_access(app, guard, api_host="0.0.0.0", port=8000, lan_ip_fn=lambda: "192.168.1.10",
                   allowed_hosts="192.168.1.10, phone.vpn.example")
    if pin is not None:
        assert client(app, LOCAL).post("/api/access/pin", json={"pin": pin, "pin_again": pin}).json()["ok"]
    return app, guard, clock


def client(app, who=REMOTE, base_url: str | None = None, *, pair: bool = True):
    """The PC addresses the app as 127.0.0.1:8000, a phone by the PC's LAN address (on the allow-list via
    allowed_hosts above, as BTCFLOW_ALLOWED_HOSTS would put it). A loopback client is the paired PC browser (it holds
    the PC cookie, as after `ops pair-pc`) unless `pair` is False: loopback alone is never the PC."""
    loopback = who[0] in ("127.0.0.1", "::1")
    base = base_url or ("http://127.0.0.1:8000" if loopback else "http://192.168.1.10:8000")
    c = TestClient(app, client=who, follow_redirects=False, base_url=base)
    return paired(c) if loopback and pair else c


def test_local_requests_pass_unchanged_with_security_headers():
    app, _, _ = make(pin=None)
    c = client(app, LOCAL)
    r = c.get("/api/thing")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["cache-control"] == "no-store, max-age=0" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert c.post("/api/thing").json() == {"posted": True}
    assert client(app, ("::1", 1)).get("/api/thing").status_code == 200


def test_remote_requests_are_blocked_while_no_pin_is_set():
    app, guard, _ = make(pin=None)
    c = client(app)
    r = c.get("/api/thing")
    assert r.status_code == 403 and r.json() == {"detail": OFF_TEXT} and "ok" not in r.text
    page = c.get("/")
    assert page.status_code == 403 and "Phone access is off" in page.text
    login = c.post("/login", data={"pin": "1234"})
    assert login.status_code == 403 and COOKIE not in login.headers.get("set-cookie", "")
    assert c.post("/api/thing").status_code == 403
    c.cookies.set(COOKIE, guard.issue())  # even a correctly signed cookie means nothing without a PIN
    assert c.get("/api/thing").status_code == 403


def test_setting_the_pin_is_refused_from_a_remote_client_even_signed_in():
    app, guard, _ = make()
    c = client(app)
    assert c.post("/api/access/pin", json={"pin": "1111", "pin_again": "1111"}).status_code == 403
    c.post("/login", data={"pin": PIN})
    assert c.get("/api/thing").status_code == 200  # signed in ...
    assert c.post("/api/access/pin", json={"pin": "1111", "pin_again": "1111"}).status_code == 403  # ... still no
    assert c.post("/api/access/clear").status_code == 403
    assert c.get("/api/access").status_code == 403
    assert asyncio.run(guard.check_pin("10.0.0.1", PIN))[0]  # unchanged


def test_pin_rules_and_the_card_view_from_the_pc():
    app, _, _ = make(pin=None)
    c = client(app, LOCAL)
    v = c.get("/api/access").json()
    assert v["pin_set"] is False and v["lan_url"] == "http://192.168.1.10:8000/#play" and v["listening_on_network"]
    assert c.post("/api/access/pin", json={"pin": "12a456", "pin_again": "12a456"}).json()["ok"] is False
    assert c.post("/api/access/pin", json={"pin": "1234", "pin_again": "1234"}).json()["ok"] is False
    r = c.post("/api/access/pin", json={"pin": "12345", "pin_again": "12345"}).json()
    assert r["ok"] is False and r["reason"] == "The PIN must be 6 to 8 digits." and r["pin_set"] is False
    assert c.post("/api/access/pin", json={"pin": "123456789", "pin_again": "123456789"}).json()["ok"] is False
    r = c.post("/api/access/pin", json={"pin": "135792", "pin_again": "135729"}).json()
    assert r["ok"] is False and "match" in r["reason"]
    r = c.post("/api/access/pin", json={"pin": "135792", "pin_again": "135792"}).json()
    assert r["ok"] and r["pin_set"] and r["reset_needed"] is False and "135792" not in str(r)
    assert c.post("/api/access/pin", json={"pin": "13579246", "pin_again": "13579246"}).json()["ok"]
    r = c.post("/api/access/clear").json()
    assert r["ok"] and r["pin_set"] is False


def test_only_a_salted_hash_is_stored(store, caplog):
    with caplog.at_level(logging.INFO):
        make(repo=store)
    rows = store.select("strategy_settings", SETTING)
    assert len(rows) == 1
    rec = rows[0]["value"]
    assert PIN not in str(rows) and PIN not in caplog.text
    # the stored record names its own work factor (tests hash with fewer iterations: tests/conftest.py); the
    # production work factor stays at least 200,000
    assert rec["algo"] == "pbkdf2_sha256" and rec["iterations"] == access.PBKDF2_ITERATIONS and rec["policy"] == 2
    assert PBKDF2_ITERATIONS >= 200_000
    assert len(bytes.fromhex(rec["salt"])) == 16 and len(rec["hash"]) == 64
    other = make(repo=None, pin=PIN)[1]._record  # the same PIN with a fresh salt hashes differently
    assert other["hash"] != rec["hash"]
    # the stored hash survives a restart
    again = AccessGuard(PinStore(store))
    assert again.pin_set and asyncio.run(again.check_pin("10.0.0.2", PIN))[0]


def test_login_works_once_a_pin_is_set(caplog):
    app, _, _ = make()
    c = client(app)
    assert c.get("/api/thing").status_code == 401
    page = c.get("/")
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert 'name="pin"' in c.get("/login").text
    with caplog.at_level(logging.INFO):
        r = c.post("/login", data={"pin": PIN})
    assert r.status_code == 303 and r.headers["location"] == "/#play"
    sc = r.headers["set-cookie"].lower()
    assert COOKIE in sc and "httponly" in sc and "samesite=strict" in sc and "max-age=43200" in sc
    assert c.get("/api/thing").json() == {"ok": True}
    assert c.post("/api/thing").json() == {"posted": True}
    assert c.post("/api/thing", headers={"Origin": "http://evil.example"}).status_code == 403
    assert PIN not in caplog.text
    assert c.get("/api/thing", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 200


def test_wrong_pin_and_forged_cookies(caplog):
    app, _, _ = make()
    c = client(app)
    with caplog.at_level(logging.WARNING):
        r = c.post("/login", data={"pin": "000000"})
    assert r.status_code == 401 and "Wrong PIN" in r.text and COOKIE not in r.headers.get("set-cookie", "")
    assert "wrong PIN from 192.168.1.50" in caplog.text and "000000" not in caplog.text
    c.cookies.set(COOKIE, "99999999999999.1.abc.deadbeef")
    assert c.get("/api/thing").status_code == 401
    c.cookies.set(COOKIE, AccessGuard().issue())  # another process's secret
    assert c.get("/api/thing").status_code == 401
    assert c.get("/api/thing", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401


def test_a_pin_change_ends_every_old_session():
    app, _, _ = make()
    c = client(app)
    c.post("/login", data={"pin": PIN})
    assert c.get("/api/thing").status_code == 200
    assert client(app, LOCAL).post("/api/access/pin", json={"pin": "777777", "pin_again": "777777"}).json()["ok"]
    assert c.get("/api/thing").status_code == 401
    assert c.post("/login", data={"pin": PIN}).status_code == 401  # the old PIN is gone
    assert c.post("/login", data={"pin": "777777"}).status_code == 303
    assert c.get("/api/thing").status_code == 200
    client(app, LOCAL).post("/api/access/clear")
    assert c.get("/api/thing").status_code == 403  # cleared: phone access is off


def test_five_failures_lock_the_ip_out_for_fifteen_minutes():
    app, _, clock = make()
    c = client(app)
    for _ in range(4):
        assert c.post("/login", data={"pin": "111111"}).status_code == 401
    r = c.post("/login", data={"pin": "111111"})
    assert r.status_code == 429 and "Locked for 15 minutes" in r.text
    assert c.post("/login", data={"pin": PIN}).status_code == 429  # even the right PIN while locked
    assert client(app, ("192.168.1.51", 1)).post("/login", data={"pin": PIN}).status_code == 303
    clock.t += 15 * 60_000 + 1
    assert c.post("/login", data={"pin": PIN}).status_code == 303


def test_failures_older_than_ten_minutes_do_not_count():
    app, _, clock = make()
    c = client(app)
    for _ in range(4):
        c.post("/login", data={"pin": "111111"})
    clock.t += 10 * 60_000 + 1
    assert c.post("/login", data={"pin": "111111"}).status_code == 401


def test_cookie_expires_after_the_session_hours():
    app, _, clock = make()
    c = client(app)
    c.post("/login", data={"pin": PIN})
    clock.t += 12 * 3_600_000 - 1
    assert c.get("/api/thing").status_code == 200
    clock.t += 2
    assert c.get("/api/thing").status_code == 401


def test_signed_in_count_and_sign_out_all_from_the_pc_only():
    app, _, _ = make()
    phone, pc = client(app), client(app, LOCAL)
    phone.post("/login", data={"pin": PIN})
    client(app, ("192.168.1.77", 1)).post("/login", data={"pin": PIN})
    assert pc.get("/api/access").json()["signed_in"] == 2
    assert phone.post("/api/access/signout").status_code == 403
    r = pc.post("/api/access/signout").json()
    assert r["ok"] and r["signed_in"] == 0 and r["pin_set"]
    assert phone.get("/api/thing").status_code == 401
    assert phone.post("/login", data={"pin": PIN}).status_code == 303  # same PIN still works
    login = client(app).get("/login").text
    assert "5 wrong tries locks this phone out for 15 minutes" in login and 'data-k="0"' in login


def test_install_files_are_public_once_a_pin_is_set_but_nothing_else():
    app, _, _ = make()

    @app.get("/manifest.json")
    def manifest() -> dict:
        return {"start_url": "/#play"}

    c = client(app)
    assert c.get("/manifest.json").json() == {"start_url": "/#play"}
    assert c.get("/api/thing").status_code == 401
    off, _, _ = make(pin=None)
    assert client(off).get("/manifest.json").status_code == 403


class SlowHash:
    """Wraps the real hash_pin: sleeps `delay` s per call and records calls and the most calls running at once."""

    def __init__(self, real, delay: float) -> None:
        self.real, self.delay = real, delay
        self.calls = self.running = self.most = 0
        self._lock = threading.Lock()

    def __call__(self, *a, **kw):
        with self._lock:
            self.calls += 1
            self.running += 1
            self.most = max(self.most, self.running)
        try:
            time.sleep(self.delay)
            return self.real(*a, **kw)
        finally:
            with self._lock:
                self.running -= 1


def test_concurrent_wrong_pins_from_one_address_hash_one_at_a_time_and_lock_after_exactly_five(monkeypatch):
    _, guard, _ = make()
    slow = SlowHash(access.hash_pin, 0.02)
    monkeypatch.setattr(access, "hash_pin", slow)

    async def run():
        return await asyncio.gather(*(guard.check_pin("192.168.1.50", "111111") for _ in range(10)))

    results = asyncio.run(run())
    assert slow.most == 1 and slow.calls == 5
    assert [ok for ok, _ in results] == [False] * 10
    reasons = [r for _, r in results]
    assert reasons.count("Wrong PIN.") == 4 and reasons.count("Too many wrong PINs. Locked for 15 minutes.") == 1
    assert sum(r.startswith("Too many wrong PINs. Try again in") for r in reasons) == 5
    assert guard.locked_for_ms("192.168.1.50") == 15 * 60_000
    assert asyncio.run(guard.check_pin("192.168.1.51", PIN))[0]  # another address is unaffected


def test_the_loop_keeps_running_while_a_pin_is_hashed(monkeypatch):
    _, guard, _ = make()
    monkeypatch.setattr(access, "hash_pin", SlowHash(access.hash_pin, 0.3))

    async def run():
        ticks = 0
        login = asyncio.ensure_future(guard.check_pin("192.168.1.50", PIN))
        while not login.done():
            ticks += 1
            await asyncio.sleep(0.01)
        return ticks, login.result()

    ticks, (ok, refusal) = asyncio.run(run())
    assert ok and refusal is None
    assert ticks >= 10  # ~30 expected; with the hash on the loop the other coroutine runs once or twice


# -- U8: Host allow-list, cross-site check, CSP -------------------------------------------------------------------
def test_an_unknown_host_is_refused_even_from_loopback_and_logged(caplog):
    app, _, _ = make()
    with caplog.at_level(logging.WARNING):
        r = client(app, LOCAL, base_url="http://evil.example:8000").get("/api/thing")
    assert r.status_code == 400 and "ok" not in r.text
    assert "evil.example:8000" in caplog.text
    assert client(app, LOCAL, base_url="http://localhost:8000").get("/api/thing").status_code == 200
    assert client(app, ("::1", 1), base_url="http://[::1]:8000").get("/api/thing").status_code == 200
    assert client(app, LOCAL, base_url="http://127.0.0.1:9999").get("/api/thing").status_code == 400  # wrong port
    assert client(app, LOCAL, base_url="http://phone.vpn.example:8000").get("/api/thing").status_code == 200
    phone = client(app, base_url="http://evil.example:8000")
    assert phone.post("/login", data={"pin": PIN}).status_code == 400


def test_refused_hosts_are_logged_at_most_once_a_minute(caplog):
    app, _, clock = make()
    c = client(app, LOCAL, base_url="http://evil.example:8000")
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert c.get("/api/thing").status_code == 400
        clock.t += 61_000
        c.get("/api/thing")
    assert caplog.text.count("evil.example:8000") == 2


def test_a_websocket_with_an_unknown_host_is_refused():
    app, _, _ = make()

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text("hi")
        await socket.close()

    pc = client(app, LOCAL)
    with pc.websocket_connect("ws://127.0.0.1:8000/ws") as s:  # (a relative URL would send Host: testserver)
        assert s.receive_text() == "hi"
    try:
        with pc.websocket_connect("ws://evil.example:8000/ws"):
            raise AssertionError("connected")
    except WebSocketDisconnect as e:
        assert e.code == 1008


def test_a_new_local_address_is_accepted_after_a_relist(monkeypatch):
    addresses = {"10.8.0.2"}
    monkeypatch.setattr(access, "local_addresses", lambda: set(addresses))
    app, _, clock = make()
    assert client(app, LOCAL, base_url="http://10.8.0.2:8000").get("/api/thing").status_code == 200
    addresses.add("10.9.0.7")  # a VPN interface comes up after start
    new = client(app, LOCAL, base_url="http://10.9.0.7:8000")
    assert new.get("/api/thing").status_code == 400  # listed at start under 30 s ago: no re-list yet
    clock.t += 30_001
    assert new.get("/api/thing").status_code == 200
    addresses.add("10.9.0.8")
    assert client(app, LOCAL, base_url="http://10.9.0.8:8000").get("/api/thing").status_code == 400  # just re-listed
    monkeypatch.setattr(access, "local_addresses", lambda: {"fe80::1%12", "2001:db8::5"})
    clock.t += 30_001
    assert client(app, LOCAL, base_url="http://[2001:db8::5]:8000").get("/api/thing").status_code == 200
    assert client(app, LOCAL, base_url="http://[fe80::1]:8000").get("/api/thing").status_code == 200


def test_a_cross_site_post_from_loopback_is_refused():
    app, _, _ = make(pin=None)
    resume_calls = []

    @app.post("/api/trading/resume")
    def resume() -> dict:
        resume_calls.append(1)
        return {"ok": True}

    pc = client(app, LOCAL)
    assert pc.post("/api/trading/resume", headers={"Origin": "http://evil.example"}).status_code == 403
    assert pc.post("/api/trading/resume", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert pc.post("/api/trading/resume", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    # Origin: null is refused unless Sec-Fetch-Site says same-origin (then that header alone decides)
    assert pc.post("/api/trading/resume", headers={"Origin": "null"}).status_code == 403
    assert pc.post("/api/trading/resume", headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    forged = {"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "cross-site"}  # Sec-Fetch-Site wins over Origin
    assert pc.post("/api/trading/resume", headers=forged).status_code == 403
    assert resume_calls == []
    assert pc.post("/api/trading/resume").json() == {"ok": True}  # scripts on the PC: no Origin, no Sec-Fetch
    same = {"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"}
    assert pc.post("/api/trading/resume", headers=same).json() == {"ok": True}
    assert pc.post("/api/trading/resume", headers={"Sec-Fetch-Site": "none"}).json() == {"ok": True}
    assert pc.get("/api/thing", headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
                  ).status_code == 200  # reads are not state changes
    assert len(resume_calls) == 3


def test_a_phone_post_with_a_matching_origin_passes():
    app, _, _ = make()
    c = client(app)
    same = {"Origin": "http://192.168.1.10:8000", "Sec-Fetch-Site": "same-origin"}
    assert c.post("/login", data={"pin": PIN}, headers=same).status_code == 303
    assert c.post("/api/thing", headers=same).json() == {"posted": True}
    assert c.post("/api/thing", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_a_phone_login_form_with_a_null_origin_but_same_origin_fetch_site_signs_in():
    """The login form's POST as a browser sends it from a no-referrer page: Origin null, Sec-Fetch-Site same-origin."""
    app, _, _ = make()
    c = client(app)
    r = c.post("/login", data={"pin": PIN}, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 303 and r.headers["location"] == "/#play" and COOKIE in r.headers["set-cookie"]
    assert c.get("/api/thing").json() == {"ok": True}


def test_a_cross_site_post_with_a_forged_same_origin_origin_is_refused():
    app, _, _ = make()
    c = client(app)
    forged = {"Origin": "http://192.168.1.10:8000", "Sec-Fetch-Site": "cross-site"}
    assert c.post("/login", data={"pin": PIN}, headers=forged).status_code == 403
    c.post("/login", data={"pin": PIN})
    assert c.post("/api/thing", headers=forged).status_code == 403


def test_pages_send_the_origin_to_the_app_itself_and_no_referrer_elsewhere():
    app, _, _ = make()
    for r in (client(app).get("/login"), client(app, LOCAL).get("/api/thing")):
        assert r.headers["referrer-policy"] == "same-origin"


def test_a_cross_site_login_post_is_refused_and_counts_no_failure():
    app, guard, _ = make()
    c = client(app)
    for _ in range(MAX_FAILURES + 1):
        r = c.post("/login", data={"pin": "111111"}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403 and COOKIE not in r.headers.get("set-cookie", "")
    assert c.post("/login", data={"pin": PIN}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert guard.locked_for_ms(REMOTE[0]) == 0
    for _ in range(MAX_FAILURES - 1):  # all five tries are still there: four misses do not lock
        assert c.post("/login", data={"pin": "111111"}).status_code == 401
    assert c.post("/login", data={"pin": PIN}).status_code == 303


def _csp(r) -> dict[str, list[str]]:
    out = {}
    for part in r.headers["content-security-policy"].split(";"):
        name, *values = part.split()
        out[name] = values
    return out


def test_every_response_carries_the_csp(tmp_path):
    app, _, _ = make()
    (tmp_path / "x.js").write_text("1")
    app.mount("/static", StaticFiles(directory=tmp_path), name="static")
    pc, phone = client(app, LOCAL), client(app)
    for r in (pc.get("/api/thing"), pc.get("/static/x.js"), phone.get("/login"), phone.get("/api/thing"),
              client(app, LOCAL, base_url="http://evil.example:8000").get("/")):
        csp = _csp(r)
        assert csp["default-src"] == ["'self'"] and csp["connect-src"] == ["'self'"]
        assert csp["script-src"][:2] == ["'self'", "'unsafe-eval'"]
        assert csp["style-src"] == ["'self'", "'unsafe-inline'"] and csp["img-src"] == ["'self'", "data:"]
        assert csp["frame-ancestors"] == ["'none'"]
        assert "'unsafe-inline'" not in csp["script-src"]


def test_the_login_csp_carries_the_hash_of_the_keypad_script_it_serves():
    app, _, _ = make()
    r = client(app).get("/login")
    scripts = re.findall(r"<script>(.*?)</script>", r.text, re.S)
    assert len(scripts) == 1 and "data-k" in scripts[0]
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode()
    assert f"'sha256-{digest}'" in _csp(r)["script-src"]
    wrong = client(app).post("/login", data={"pin": "000000"})  # the page with an error message: same script
    assert re.findall(r"<script>(.*?)</script>", wrong.text, re.S) == scripts


def test_the_play_page_has_no_inline_script_the_csp_would_block():
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", page) == []  # every script is a file from our origin
    assert not re.search(r"""\son[a-z]+\s*=\s*["']""", page, re.I)  # no inline event handler attributes
    assert "javascript:" not in page.lower()
    srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', page)
    assert all("//" not in s and (WEB_DIR / s).exists() for s in srcs), srcs
    theme = page.index('src="theme.js"')
    assert theme < page.index('<link rel="stylesheet"') and theme < page.index("</head>")  # before first paint
    assert "window.BTCTheme" in (WEB_DIR / "theme.js").read_text(encoding="utf-8")


def test_the_middleware_records_the_actor_on_the_scope():
    app, _, _ = make()

    @app.get("/api/actor")
    def actor(request: Request) -> dict:
        return {"actor": request.scope["state"]["actor"]}

    assert client(app, LOCAL).get("/api/actor").json() == {"actor": "pc"}
    phone = client(app)
    phone.post("/login", data={"pin": PIN})
    nonce = phone.cookies.get(COOKIE).split(".")[2]
    assert phone.get("/api/actor").json() == {"actor": "phone:" + nonce[:8]}


def test_the_api_docs_are_pc_only():
    app = FastAPI(docs_url="/api/docs", openapi_url="/api/openapi.json")
    guard = AccessGuard(PinStore(None), session_hours=12, clock_ms=Clock())
    install_access(app, guard, api_host="0.0.0.0", port=8000, lan_ip_fn=lambda: "192.168.1.10",
                   allowed_hosts="192.168.1.10")
    pc = client(app, LOCAL)
    assert pc.post("/api/access/pin", json={"pin": PIN, "pin_again": PIN}).json()["ok"]
    assert pc.get("/api/docs").status_code == 200 and pc.get("/api/openapi.json").status_code == 200
    phone = client(app)
    phone.post("/login", data={"pin": PIN})
    for path in ("/api/docs", "/api/openapi.json"):
        r = phone.get(path)
        assert r.status_code == 403 and r.json() == {"detail": "Only from the PC itself."}, path


def test_only_the_api_docs_may_load_swagger_from_the_cdn():
    """Swagger UI loads its JS/CSS from cdn.jsdelivr.net and boots with an inline script: allowed on /api/docs only."""
    app = FastAPI(docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/api/play/now")
    def now() -> dict:
        return {"ok": True}

    install_access(app, AccessGuard(PinStore(None), clock_ms=Clock()), port=8000)
    pc = client(app, LOCAL)
    r = pc.get("/api/docs")
    assert r.status_code == 200 and "cdn.jsdelivr.net" in r.text
    csp = _csp(r)
    assert "https://cdn.jsdelivr.net" in csp["script-src"] and "'unsafe-inline'" in csp["script-src"]
    assert "https://cdn.jsdelivr.net" in csp["style-src"]
    assert csp["frame-ancestors"] == ["'none'"] and csp["default-src"] == ["'self'"]
    for r in (pc.get("/api/play/now"), pc.get("/api/openapi.json"), pc.get("/api/docsx")):
        assert "cdn.jsdelivr.net" not in r.headers["content-security-policy"]
        assert "'unsafe-inline'" not in _csp(r)["script-src"]


# -- U10: PIN policy v2 and a global failure cap ------------------------------------------------------------------
def test_a_pin_set_under_the_old_rule_must_be_set_again(store):
    salt = bytes(16)  # a record as the 4-digit rule stored it: no policy field
    old = {"algo": "pbkdf2_sha256", "iterations": 1_000, "salt": salt.hex(), "hash": hash_pin("1234", salt, 1_000),
           "generation": 3}
    store.insert("strategy_settings", [{**SETTING, "value": old, "updated_ts": 1}], ignore_conflicts=False)
    app, guard, _ = make(repo=store, pin=None)
    phone = client(app)
    r = phone.get("/api/thing")
    assert r.status_code == 403 and r.json() == {"detail": RESET_TEXT}
    assert RESET_TEXT == "Phone access needs a new 6–8 digit PIN — set it on the PC."
    page = phone.get("/")
    assert page.status_code == 403 and "needs a new 6–8 digit PIN" in page.text
    login = phone.post("/login", data={"pin": "1234"})  # the old PIN no longer signs in
    assert login.status_code == 403 and COOKIE not in login.headers.get("set-cookie", "")
    assert not asyncio.run(guard.check_pin("10.0.0.3", "1234"))[0]
    pc = client(app, LOCAL)
    v = pc.get("/api/access").json()
    assert v["pin_set"] is False and v["reset_needed"] is True and v["signed_in"] == 0
    assert pc.post("/api/access/pin", json={"pin": PIN, "pin_again": PIN}).json()["reset_needed"] is False
    assert store.select("strategy_settings", SETTING)[0]["value"]["generation"] == 4
    assert phone.post("/login", data={"pin": PIN}).status_code == 303
    assert phone.get("/api/thing").json() == {"ok": True}


def test_with_no_pin_ever_set_no_reset_is_reported():
    app, _, _ = make(pin=None)
    v = client(app, LOCAL).get("/api/access").json()
    assert v["pin_set"] is False and v["reset_needed"] is False
    assert client(app).get("/api/thing").json() == {"detail": OFF_TEXT}


def test_twenty_wrong_pins_across_addresses_lock_every_login_for_fifteen_minutes(caplog):
    app, guard, clock = make()
    with caplog.at_level(logging.WARNING):
        for n in range(19):
            assert client(app, (f"10.1.0.{n}", 1)).post("/login", data={"pin": "111111"}).status_code == 401
        r = client(app, ("10.1.0.19", 1)).post("/login", data={"pin": "111111"})
    assert r.status_code == 429
    assert "every login locked for 15 minutes" in caplog.text
    late = client(app, ("10.1.0.20", 1))
    assert late.post("/login", data={"pin": PIN}).status_code == 429  # the right PIN, from a fresh address
    clock.t += 15 * 60_000 - 1
    assert late.post("/login", data={"pin": PIN}).status_code == 429
    clock.t += 2
    assert late.post("/login", data={"pin": PIN}).status_code == 303
    assert guard.locked_for_ms("10.1.0.20") == 0


def test_a_correct_login_under_the_global_count_works_and_does_not_count():
    app, _, _ = make()
    for n in range(19):
        assert client(app, (f"10.2.0.{n}", 1)).post("/login", data={"pin": "111111"}).status_code == 401
    assert client(app, ("10.2.0.100", 1)).post("/login", data={"pin": PIN}).status_code == 303
    assert client(app, ("10.2.0.101", 1)).post("/login", data={"pin": PIN}).status_code == 303
    assert client(app, ("10.2.0.19", 1)).post("/login", data={"pin": "111111"}).status_code == 429  # the 20th miss
    assert client(app, ("10.2.0.102", 1)).post("/login", data={"pin": PIN}).status_code == 429


def test_forty_concurrent_wrong_pins_from_many_addresses_lock_after_exactly_twenty(monkeypatch):
    _, guard, _ = make()
    slow = SlowHash(access.hash_pin, 0.02)
    monkeypatch.setattr(access, "hash_pin", slow)

    async def run():
        return await asyncio.gather(*(guard.check_pin(f"10.3.0.{n}", "111111") for n in range(40)))

    results = asyncio.run(run())
    assert [ok for ok, _ in results] == [False] * 40
    assert slow.calls == 20  # only 20 attempts are checked; the other 20 are refused before hashing
    reasons = [r for _, r in results]
    assert sum(r == "Wrong PIN." for r in reasons) == 19
    assert sum(r.startswith("Too many wrong PINs") for r in reasons) == 21
    ok, refusal = asyncio.run(guard.check_pin("10.3.0.200", PIN))
    assert not ok and refusal.startswith("Too many wrong PINs") and slow.calls == 20


# -- U15: BTCFLOW_LOCAL_CLIENTS (without a PC listener: clients that may pair as the PC) ---------------------------
def test_a_listed_local_client_may_pair_as_the_pc_and_an_unlisted_one_is_a_phone(caplog):
    app = FastAPI()

    @app.get("/api/actor")
    def actor(request: Request) -> dict:
        return {"actor": request.scope["state"]["actor"]}

    guard = AccessGuard(PinStore(None), session_hours=12, clock_ms=Clock())
    with caplog.at_level(logging.WARNING):
        install_access(app, guard, api_host="0.0.0.0", port=8000, lan_ip_fn=lambda: "192.168.1.10",
                       allowed_hosts="192.168.1.10", local_clients="172.30.0.1")
    assert "172.30.0.1" in caplog.text and "BTCFLOW_LOCAL_CLIENTS" in caplog.text
    gateway = client(app, ("172.30.0.1", 50000), base_url="http://127.0.0.1:8000")
    r = gateway.get("/api/actor")
    assert r.status_code == 401 and r.json()["code"] == "pair_required"  # listed, but not paired: not the PC
    paired(gateway)
    assert gateway.get("/api/actor").json() == {"actor": "pc"}
    assert gateway.post("/api/access/pin", json={"pin": PIN, "pin_again": PIN}).json()["ok"]
    phone = paired(client(app, ("172.30.0.2", 50000), base_url="http://127.0.0.1:8000"))  # a stolen cookie: no
    assert phone.get("/api/actor").status_code == 401
    assert phone.get("/api/access").status_code == 403
    phone.post("/login", data={"pin": PIN})
    assert phone.get("/api/actor").json()["actor"].startswith("phone:")


def test_loopback_local_clients_log_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        install_access(FastAPI(), AccessGuard(PinStore(None), clock_ms=Clock()), port=8000,
                       local_clients="127.0.0.1, ::1")
    assert "BTCFLOW_LOCAL_CLIENTS" not in caplog.text


# -- P0 #1: the PC is a paired browser, never a network position -----------------------------------------------------
PC_PORT = 8001
AGENT_PORT = 8002
DOCKER_GW = ("172.17.0.1", 50000)  # in Docker every request (the PC's and every phone's) arrives from the gateway
SIBLING = ("172.18.0.5", 40000)  # another container on the compose network (openobserve, a recorder)
PC_BASE = f"http://localhost:{PC_PORT}"


def actor_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/actor")
    def actor(request: Request) -> dict:
        return {"actor": request.scope["state"]["actor"]}

    @app.post("/api/actor")
    def post_actor(request: Request) -> dict:
        return {"actor": request.scope["state"]["actor"]}

    @app.get("/api/agent/tools")
    def tools(request: Request) -> dict:
        return {"actor": request.scope["state"]["actor"]}

    @app.post("/api/agent/call/{tool}")
    def call(tool: str, request: Request) -> dict:
        return {"actor": request.scope["state"]["actor"], "tool": tool}

    @app.get("/api/health")
    def health(request: Request) -> dict:
        return {"actor": request.scope["state"]["actor"]}

    @app.get("/")
    def page(request: Request) -> dict:
        return {"page": request.scope["state"]["actor"]}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"actor": websocket.scope["state"]["actor"]})
        await websocket.close()

    return app


def make_listeners(pin: str | None = PIN, clock: Clock | None = None, pc=None):
    """The Docker layout: the main port 8000 (phones), the PC listener 8001, the agent listener 8002."""
    app = actor_app()
    guard = AccessGuard(PinStore(None), session_hours=12, clock_ms=clock or Clock())
    creds = install_access(app, guard, api_host="0.0.0.0", port=8000, lan_ip_fn=lambda: "192.168.1.10",
                           allowed_hosts="192.168.1.10", pc_port=PC_PORT, agent_port=AGENT_PORT, pc=pc)
    if pin is not None:
        assert pc_client(app).post("/api/access/pin", json={"pin": pin, "pin_again": pin}).json()["ok"]
    return app, guard, creds


def pc_client(app, who=DOCKER_GW, base_url: str = PC_BASE, *, pair: bool = True) -> TestClient:
    c = TestClient(app, client=who, follow_redirects=False, base_url=base_url)
    return paired(c) if pair else c


def test_on_the_pc_listener_the_paired_browser_is_the_pc_for_every_loopback_name():
    app, _, _ = make_listeners()
    for base in (PC_BASE, f"http://127.0.0.1:{PC_PORT}", f"http://[::1]:{PC_PORT}"):
        pc = pc_client(app, base_url=base)  # (the TestClient's scope["server"] port is the URL's)
        assert pc.get("/api/actor").json() == {"actor": "pc"}
        assert pc.post("/api/actor").json() == {"actor": "pc"}
        assert pc.get("/api/access").status_code == 200  # PC-only routes work
    assert pc_client(app).get("/login").status_code == 303
    assert pc_client(app).get("/pair").status_code == 303  # already paired


def test_the_pc_listener_without_the_cookie_shows_only_the_pairing_page():
    app, _, _ = make_listeners()
    c = pc_client(app, pair=False)
    for path in ("/", "/login", "/anything"):
        r = c.get(path)
        assert r.status_code == 401 and "Pair this browser" in r.text and "ops pair-pc" in r.text, path
        assert r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    for method, path in (("GET", "/api/actor"), ("POST", "/api/actor"), ("GET", "/api/access"),
                         ("POST", "/api/access/pin"), ("POST", "/api/trading/resume"), ("GET", "/api/docs")):
        r = c.request(method, path)
        assert r.status_code == 401 and r.json()["code"] == "pair_required" and "actor" not in r.text, path
    assert c.get("/pair").status_code == 200 and "Pairing code" in c.get("/pair").text
    assert c.get("/manifest.json").status_code != 401  # install files stay public (no data)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"ws://localhost:{PC_PORT}/ws"):
            pass


def test_a_sibling_container_with_a_spoofed_localhost_host_is_not_the_pc():
    """B1#1: openobserve or a recorder calling http://app:8001 with `Host: localhost:8001` (no cookie)."""
    app, _, _ = make_listeners()
    for host in (f"localhost:{PC_PORT}", f"127.0.0.1:{PC_PORT}"):
        c = TestClient(app, client=SIBLING, follow_redirects=False, base_url=f"http://app:{PC_PORT}")
        r = c.get("/api/actor", headers={"host": host})
        assert r.status_code == 401 and "actor" not in r.text, host
        for path in ("/api/trading/resume", "/api/trading/heartbeat", "/api/agent/proposals/approve",
                     "/api/play/limits", "/api/trading/mode"):
            assert c.post(path, json={}, headers={"host": host}).status_code == 401, path
    # its own name is not on the PC listener's list at all
    assert TestClient(app, client=SIBLING, base_url=f"http://app:{PC_PORT}").get("/api/actor").status_code == 400


def test_loopback_inside_the_container_is_not_the_pc():
    """B4#11: the agent CLI child (or any process) on the container's loopback, on the main port or the PC port."""
    app, _, _ = make_listeners()
    main = client(app, LOCAL, base_url="http://127.0.0.1:8000", pair=False)
    assert main.get("/api/actor").status_code == 401  # the phone rules: a PIN session needed
    assert main.get("/api/access").status_code == 403
    assert main.get("/pair").status_code == 403  # pairing only on the PC listener
    assert pc_client(app, LOCAL, pair=False).get("/api/actor").status_code == 401
    # a PC cookie on the main port means nothing (the PC listener is the only place it counts)
    assert paired(main).get("/api/actor").status_code == 401


def test_the_main_port_needs_the_pin_from_anyone():
    app, _, _ = make_listeners()
    phone = client(app, DOCKER_GW, base_url="http://192.168.1.10:8000")
    assert phone.get("/api/actor").status_code == 401
    assert phone.get("/api/access").status_code == 403
    phone.post("/login", data={"pin": PIN})
    assert phone.get("/api/actor").json()["actor"].startswith("phone:")
    assert phone.get("/api/access").status_code == 403  # a phone is never the PC


def test_a_spoofed_localhost_host_on_the_main_port_is_not_the_pc():
    app, _, _ = make_listeners()
    for host in ("localhost:8000", f"localhost:{PC_PORT}", "127.0.0.1:8000", f"127.0.0.1:{PC_PORT}"):
        c = paired(TestClient(app, client=DOCKER_GW, follow_redirects=False, base_url="http://192.168.1.10:8000"))
        r = c.get("/api/actor", headers={"host": host})
        assert r.status_code in (400, 401) and "actor" not in r.text, host
        assert c.get("/api/access", headers={"host": host}).status_code in (400, 403)


def test_the_pc_listener_answers_only_to_loopback_names_on_its_port_and_checks_cross_site():
    app, _, _ = make_listeners()
    for host in ("192.168.1.10:8001", "evil.example:8001", "localhost:8000", "localhost", f"localhost:{AGENT_PORT}"):
        assert pc_client(app).get("/api/actor", headers={"host": host}).status_code == 400, host
    pc = pc_client(app)
    assert pc.post("/api/actor", headers={"origin": "http://evil.example"}).status_code == 403
    assert pc.post("/api/actor", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert pc.post("/api/actor", headers={"sec-fetch-site": "same-site"}).status_code == 403  # localhost:5080's pages
    assert pc.post("/api/actor", headers={"origin": PC_BASE}).json() == {"actor": "pc"}
    assert pc.post("/api/actor", headers={"origin": "http://192.168.1.10:8000"}).status_code == 403


def test_websockets_need_the_pc_cookie_or_a_session_and_a_foreign_origin_is_refused():
    """B1#11: the Origin check applies to WebSocket upgrades too."""
    app, _, _ = make_listeners()
    pc = pc_client(app)
    with pc.websocket_connect(f"ws://localhost:{PC_PORT}/ws") as s:
        assert s.receive_json() == {"actor": "pc"}
    with pc.websocket_connect(f"ws://localhost:{PC_PORT}/ws", headers={"origin": PC_BASE}) as s:
        assert s.receive_json() == {"actor": "pc"}
    for bad in ({"origin": "http://evil.example"}, {"origin": "null"}, {"origin": "http://192.168.1.10:8000"},
                {"sec-fetch-site": "cross-site"}):
        with pytest.raises(WebSocketDisconnect):
            with pc.websocket_connect(f"ws://localhost:{PC_PORT}/ws", headers=bad):
                pass
    phone = client(app, DOCKER_GW, base_url="http://192.168.1.10:8000")
    with pytest.raises(WebSocketDisconnect):
        with phone.websocket_connect("ws://192.168.1.10:8000/ws"):
            pass
    with pytest.raises(WebSocketDisconnect):  # a spoofed Host on the main port is not the PC
        with phone.websocket_connect("ws://192.168.1.10:8000/ws", headers={"host": f"localhost:{PC_PORT}"}):
            pass
    phone.post("/login", data={"pin": PIN})
    with phone.websocket_connect("ws://192.168.1.10:8000/ws", headers={"origin": "http://192.168.1.10:8000"}) as s:
        assert s.receive_json()["actor"].startswith("phone:")
    with pytest.raises(WebSocketDisconnect):  # signed in, but the page is another site's
        with phone.websocket_connect("ws://192.168.1.10:8000/ws", headers={"origin": "http://evil.example"}):
            pass


# -- pairing ----------------------------------------------------------------------------------------------------------
def pair(c: TestClient, code: str, **headers) -> object:
    return c.post("/pair", data={"code": code}, headers=headers)


def test_a_pairing_code_pairs_one_browser_once_and_sets_a_strict_httponly_cookie(caplog):
    app, _, creds = make_listeners()
    code, _ = creds.new_code()
    first = pc_client(app, pair=False)
    form = first.get(f"/pair?code={code}")
    assert form.status_code == 200 and code in form.text and PC_COOKIE not in form.headers.get("set-cookie", "")
    with caplog.at_level(logging.INFO):
        r = pair(first, code.lower().replace("-", " "))  # case, spaces and dashes do not matter
    assert r.status_code == 303 and r.headers["location"] == "/#play"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{PC_COOKIE}=") and "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert "Max-Age=34560000" in cookie and r.headers["cache-control"] == "no-store, max-age=0"
    assert first.get("/api/actor").json() == {"actor": "pc"}
    assert code not in caplog.text and code.replace("-", "") not in caplog.text
    second = pc_client(app, pair=False)  # the same code again: used up
    r = pair(second, code)
    assert r.status_code == 401 and "No pairing code" in r.text and PC_COOKIE not in r.headers.get("set-cookie", "")
    assert second.get("/api/actor").status_code == 401


def test_a_pairing_code_expires_after_five_minutes():
    clock = Clock()
    app, _, creds = make_listeners(clock=clock)
    code, expires = creds.new_code()
    assert expires == clock.t + 5 * 60_000
    clock.t = expires
    r = pair(pc_client(app, pair=False), code)
    assert r.status_code == 401 and "expired" in r.text


def test_five_wrong_codes_cancel_the_pending_code():
    app, _, creds = make_listeners()
    code, _ = creds.new_code()
    c = pc_client(app, pair=False)
    for i in range(MAX_PAIR_FAILURES - 1):
        r = pair(c, "AAAA-AAAA-AAAA")
        assert r.status_code == 401 and "Wrong pairing code" in r.text, i
    r = pair(c, "")
    assert r.status_code == 429 and "cancelled" in r.text
    assert pair(c, code).status_code == 401  # the right code no longer works
    code2, _ = creds.new_code()  # a new `pair-pc` starts over
    assert pair(c, code2).status_code == 303


def test_pairing_works_only_in_the_pcs_place_and_never_cross_site():
    app, _, creds = make_listeners()
    code, _ = creds.new_code()
    phone = client(app, DOCKER_GW, base_url="http://192.168.1.10:8000")
    assert pair(phone, code).status_code == 403 and phone.get("/pair").status_code == 403
    agent = TestClient(app, client=LOCAL, base_url=f"http://localhost:{AGENT_PORT}")
    assert pair(agent, code).status_code == 404
    c = pc_client(app, pair=False)
    assert pair(c, code, **{"sec-fetch-site": "cross-site"}).status_code == 403
    assert pair(c, code, origin="http://evil.example").status_code == 403
    assert pair(c, code).status_code == 303  # the refused attempts used nothing up


def test_without_a_pc_listener_a_loopback_client_pairs_on_the_main_port():
    """A native install (no BTCFLOW_PC_PORT): the PC's place is a loopback client on the main port."""
    app, _, _ = make(pin=None)
    creds = app.state.pc_credentials
    c = client(app, LOCAL, pair=False)
    assert c.get("/api/thing").status_code == 401 and "Pair this browser" in c.get("/").text
    code, _ = creds.new_code()
    assert pair(c, code).status_code == 303
    assert c.get("/api/thing").json() == {"ok": True}
    phone = client(app)  # a LAN client cannot pair, even with a code
    code, _ = creds.new_code()
    assert pair(phone, code).status_code == 403


def test_a_tampered_forged_or_expired_pc_cookie_is_refused():
    clock = Clock()
    app, _, creds = make_listeners(clock=clock)
    good = creds.issue()
    issued, nonce, sig = good.split(".")
    other = PcCredentials(clock_ms=clock).issue()  # signed under another secret
    for bad in (f"{issued}.{nonce}.{'0' * 64}", f"{int(issued) + 1}.{nonce}.{sig}", f"{issued}.x{nonce}.{sig}",
                other, "garbage", "", "a.b.c.d", good + "0", "1." * 300):
        c = pc_client(app, pair=False)
        c.cookies.set(PC_COOKIE, bad)
        assert c.get("/api/actor").status_code == 401, bad[:40]
    c = pc_client(app, pair=False)
    c.cookies.set(PC_COOKIE, good)
    assert c.get("/api/actor").json() == {"actor": "pc"}
    clock.t += 401 * 86_400_000
    assert c.get("/api/actor").status_code == 401  # past its 400 days


def test_signing_out_pc_browsers_ends_every_pc_cookie_and_only_the_pc_may():
    app, _, _ = make_listeners()
    pc, other = pc_client(app), pc_client(app)
    phone = client(app, DOCKER_GW, base_url="http://192.168.1.10:8000")
    phone.post("/login", data={"pin": PIN})
    assert phone.post("/api/access/pc-signout").status_code == 403
    r = pc.post("/api/access/pc-signout")
    assert r.json()["ok"] and PC_COOKIE in r.headers["set-cookie"]  # this browser's cookie is cleared too
    assert other.get("/api/actor").status_code == 401
    assert phone.get("/api/actor").json()["actor"].startswith("phone:")  # phones are untouched


def test_the_secret_file_is_shared_with_ops_and_a_signout_from_ops_applies_within_seconds(tmp_path):
    clock = Clock()
    creds = PcCredentials(tmp_path, clock_ms=clock)
    assert (tmp_path / SECRET_FILE).exists()
    app, _, _ = make_listeners(clock=clock, pc=creds)
    c = pc_client(app)
    assert c.get("/api/actor").json() == {"actor": "pc"}
    assert PcCredentials(tmp_path, clock_ms=clock).valid(c.cookies.get(PC_COOKIE))  # a restart keeps the browser
    rotate_secret_file(tmp_path)  # `ops signout-pc` in another process
    assert c.get("/api/actor").json() == {"actor": "pc"}  # not re-read yet
    clock.t += RELOAD_MS
    assert c.get("/api/actor").status_code == 401
    code, _ = create_pairing_code(tmp_path, clock.t)  # `ops pair-pc` writes only the code's hash
    assert code.replace("-", "") not in (tmp_path / PAIRING_FILE).read_text() and code not in (
        tmp_path / PAIRING_FILE).read_text()
    assert pair(pc_client(app, pair=False), code).status_code == 303
    assert not (tmp_path / PAIRING_FILE).exists()


def test_ops_pair_pc_prints_a_code_and_link_and_signout_pc_rotates(tmp_path, capsys):
    from btc_signal_engine.bootstrap.ops import main
    assert main(["pair-pc", "--data-dir", str(tmp_path), "--port", "8001"]) == 0
    out = capsys.readouterr().out
    code = re.search(r"Pairing code: ([A-Z0-9-]{14})", out).group(1)
    assert f"http://localhost:8001/pair?code={code}" in out
    creds = PcCredentials(tmp_path)
    assert creds.redeem(code) == (True, "This browser is paired: it is the PC.")
    token = creds.issue()
    secret = (tmp_path / SECRET_FILE).read_text()
    assert main(["signout-pc", "--data-dir", str(tmp_path)]) == 0
    assert (tmp_path / SECRET_FILE).read_text() != secret
    assert not PcCredentials(tmp_path).valid(token)


# -- the agent listener (BTCFLOW_AGENT_PORT) ---------------------------------------------------------------------------
def test_the_agent_listener_serves_only_the_tool_api_and_is_never_the_pc():
    app, _, _ = make_listeners()
    for who in (LOCAL, DOCKER_GW):
        a = paired(TestClient(app, client=who, follow_redirects=False, base_url=f"http://localhost:{AGENT_PORT}"))
        assert a.get("/api/agent/tools").json() == {"actor": "agent-api"}  # a PC cookie changes nothing here
        assert a.post("/api/agent/call/observe", json={}).json() == {"actor": "agent-api", "tool": "observe"}
        for method, path in (("GET", "/api/actor"), ("GET", "/"), ("POST", "/api/trading/resume"),
                             ("GET", "/api/access"), ("POST", "/login"), ("GET", "/api/health")):
            assert a.request(method, path).status_code == 404, path
        assert a.post("/api/agent/call/observe", json={}, headers={"origin": "http://evil.example"}).status_code == 403
        assert a.get("/api/agent/tools", headers={"host": "evil.example:8002"}).status_code == 400
        with pytest.raises(WebSocketDisconnect):
            with a.websocket_connect(f"ws://localhost:{AGENT_PORT}/ws"):
                pass


def test_the_tool_api_is_refused_on_the_pc_and_main_listeners():
    app, _, _ = make_listeners()
    phone = client(app, DOCKER_GW, base_url="http://192.168.1.10:8000")
    phone.post("/login", data={"pin": PIN})
    for c in (pc_client(app), phone):
        for method, path in (("GET", "/api/agent/tools"), ("POST", "/api/agent/call/observe"),
                             ("GET", "/api/agent/events/wait")):
            r = c.request(method, path)
            assert r.status_code == 403 and r.json()["code"] == "agent_port", path


def test_listener_ports_must_differ():
    with pytest.raises(ValueError, match="must differ"):
        install_access(FastAPI(), AccessGuard(PinStore(None), clock_ms=Clock()), port=8000, pc_port=8000)
    for agent in (8000, 8001):
        with pytest.raises(ValueError, match="agent port"):
            install_access(FastAPI(), AccessGuard(PinStore(None), clock_ms=Clock()), port=8000, pc_port=8001,
                           agent_port=agent)


# -- the healthcheck, fail-closed actors, the login body cap ----------------------------------------------------------
def test_health_is_open_with_a_public_actor_and_full_for_a_session():
    app, _, _ = make_listeners()
    for c in (client(app, LOCAL, base_url="http://127.0.0.1:8000", pair=False), pc_client(app, pair=False),
              client(app, SIBLING, base_url="http://192.168.1.10:8000")):
        assert c.get("/api/health").json() == {"actor": "public"}
    assert pc_client(app).get("/api/health").json() == {"actor": "pc"}
    assert client(app, LOCAL, base_url="http://evil.example:8000", pair=False).get("/api/health").status_code == 400


def test_from_pc_fails_closed_without_an_actor():
    app = FastAPI()

    @app.get("/who")
    def who(request: Request) -> dict:
        return {"pc": access.from_pc(request), "human": access.human(request)}

    assert TestClient(app).get("/who").json() == {"pc": False, "human": False}
    assert TestClient(as_actor(app, "pc")).get("/who").json() == {"pc": True, "human": True}
    assert TestClient(as_actor(app, "phone:abcdef12")).get("/who").json() == {"pc": False, "human": True}
    for a in ("agent-api", "agent:abcdef12", "public", "phone:", "system"):
        assert TestClient(as_actor(app, a)).get("/who").json() == {"pc": False, "human": False}, a


def test_login_reads_at_most_2_kb_and_answers_413_beyond(caplog):
    app, guard, _ = make()
    c = client(app)
    big = "pin=" + "1" * 3000
    r = c.post("/login", content=big, headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    # a body without a Content-Length (chunked) is cut off while it streams
    r = c.post("/login", content=iter([b"pin=", b"1" * 1500, b"1" * 1500]),
               headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert guard._failures == {}  # never reached the PIN check
    assert c.post("/login", data={"pin": PIN}).status_code == 303


def test_the_pairing_form_has_the_same_body_cap():
    app, _, creds = make_listeners()
    code, _ = creds.new_code()
    c = pc_client(app, pair=False)
    r = c.post("/pair", content="code=" + "A" * 3000, headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert pair(c, code).status_code == 303


# -- the Phone access card's phone URL ---------------------------------------------------------------------------------
def test_the_phone_url_prefers_the_public_url_then_the_allowed_host_and_never_a_container_address():
    assert access.phone_url(8000, public_url="http://192.168.1.10:8000/", allowed_hosts="x.vpn",
                            lan_ip_fn=lambda: "172.17.0.2") == "http://192.168.1.10:8000/#play"
    assert access.phone_url(8000, allowed_hosts="pc.tailnet.example, 192.168.1.10",
                            lan_ip_fn=lambda: "172.17.0.2") == "http://pc.tailnet.example:8000/#play"
    assert access.phone_url(8000, allowed_hosts="vpn.example:8443") == "http://vpn.example:8443/#play"
    assert access.phone_url(8000, allowed_hosts="fd00::5") == "http://[fd00::5]:8000/#play"
    assert access.phone_url(8000, lan_ip_fn=lambda: "172.17.0.2", container_fn=lambda: True) is None
    assert access.phone_url(8000, lan_ip_fn=lambda: "192.168.1.10",
                            container_fn=lambda: False) == "http://192.168.1.10:8000/#play"


def test_the_card_in_a_container_shows_the_public_url_and_its_host_is_allowed():
    app = actor_app()
    guard = AccessGuard(PinStore(None), session_hours=12, clock_ms=Clock())
    install_access(app, guard, api_host="0.0.0.0", port=8000, lan_ip_fn=lambda: "172.17.0.2",
                   container_fn=lambda: True, pc_port=PC_PORT, public_url="http://192.168.1.10:8000")
    v = pc_client(app).get("/api/access").json()
    assert v["lan_url"] == "http://192.168.1.10:8000/#play" and v["lan_ip"] is None and v["in_container"] is True
    assert pc_client(app).post("/api/access/pin", json={"pin": PIN, "pin_again": PIN}).json()["ok"]
    phone = client(app, DOCKER_GW, base_url="http://192.168.1.10:8000")
    assert phone.post("/login", data={"pin": PIN}).status_code == 303  # the public URL's host is on the list
    app2 = actor_app()
    install_access(app2, AccessGuard(PinStore(None), clock_ms=Clock()), api_host="0.0.0.0", port=8000,
                   lan_ip_fn=lambda: "172.17.0.2", container_fn=lambda: True, pc_port=PC_PORT)
    v = pc_client(app2).get("/api/access").json()
    assert v["lan_url"] is None and "BTCFLOW_PUBLIC_URL" in v["how_to_find_ip"]
