"""Who is asking: the PC (a paired browser), a phone (the PIN), or an external agent (its own port and token).

- The PC is a paired browser, never a network position (P0 #1, docs/plans/2026-10-31-1800-review-fix-plan.md). It
  holds the PC cookie (adapters/pc_credentials.py: HMAC under a secret file in the app's data folder, HttpOnly,
  SameSite=Strict, 400 days), set by /pair after the user enters a one-time code from
  `python -m btc_signal_engine.ops pair-pc`. A request is the PC (actor "pc") only with a valid PC cookie AND in the
  PC's place: on the PC listener (BTCFLOW_PC_PORT, published on the host's 127.0.0.1 only), or, when no PC listener
  is configured (a native install), from a loopback client (or one in BTCFLOW_LOCAL_CLIENTS) on the main port. Pairing
  works only in that same place. In the PC's place without the cookie, pages show "Pair this browser" and /api answers
  401: nothing else. So a sibling container that reaches app:8001 with a spoofed `Host: localhost:8001`, a local
  process, or the agent CLI on the container's loopback is never the PC.
- The agent listener (BTCFLOW_AGENT_PORT, optional) serves only the external agents' tool API (AGENT_PATHS:
  /api/agent/tools, /api/agent/call/{tool}, /api/agent/events/wait); its actor is "agent-api", never "pc" or a person,
  and every tool call needs the token of a live external session (agent_routes). Those paths answer 403 on the other
  listeners. Human-only actions (approve or skip a proposal, resume, limits, mode, the presence heartbeat) therefore
  need the PC cookie or a phone's PIN session, never just a network position or the absence of an agent header.
- /api/health (GET) is open to anyone who passes the Host check (the container healthcheck runs it on loopback): it
  answers only {status} without a PC or phone session, the full health with one.
- Until a PIN is set, every other client gets a 403 ("Phone access is off ...") page or JSON: no data at all.
- With a PIN set, other clients need a valid session cookie: pages redirect to /login, /api answers 401.
  /login (POST, form field `pin`; the body is read up to MAX_FORM_BYTES, a bigger one answers 413) checks the PIN
  against its stored salted hash (PBKDF2-HMAC-SHA256, PBKDF2_ITERATIONS,
  random 16-byte salt; compared in constant time). Success sets an HttpOnly, SameSite=Strict cookie signed with
  HMAC-SHA256 under a random per-process secret (a restart signs every phone out) and the PIN's session generation
  (setting, changing or clearing the PIN bumps it, so every old cookie stops working), valid for `session_hours`.
  Failures are rate limited per client (5 in 10 minutes -> 15-minute lockout) and across all clients (20 in
  10 minutes -> every login locked for 15 minutes, so rotating addresses or devices does not help), and logged
  without the PIN. A client is its IP plus, when it holds one, its DEVICE cookie (B1#9): behind Docker's port
  publishing every phone arrives from the bridge gateway's one address, so an IP-only key let five wrong PINs from any
  LAN device lock every phone out. The login page (GET /login, which a browser loads before it can post the form)
  sets `btcflow_device` (random, HMAC-signed under the per-process secret, HttpOnly, SameSite=Strict, path /login, a
  year); a POST with a valid one is counted under "<ip>#<device>", one without (a script, a cookie-less client) under
  the bare IP, and a POST never hands one out, so dropping the cookie only lands in the shared bucket and never frees
  another device's (rotating fresh cookies from the page still meets the cap across all clients). The cookie cannot be forged (signed) and no header is trusted for it:
  uvicorn's proxy-header rewriting is on only for the peers in BTCFLOW_FORWARDED_ALLOW_IPS (below; off by
  default), and this module never reads X-Forwarded-For itself. Logs name the device too.
  The PBKDF2 comparison runs in a worker thread so a login never blocks the event loop; logins from one address are
  checked one at a time, and each attempt counts as a (pending) failure from before its hash starts until it succeeds,
  so the cap stays exact when logins arrive in parallel.
- The PIN is set, changed or cleared only by the PC (/api/access/*, twice over: the middleware and the route), which
  also signs out every PC browser (/api/access/pc-signout). The plain PIN is never stored, logged or returned; only
  {salt, hash, iterations, generation, policy} is stored, in strategy_settings (strategy "access", key "phone_pin").
  PINs are 6-8 digits (`policy` 2, KTD6); a record without `policy` 2 was set under the old 4-digit rule and counts as
  no PIN: phones get RESET_TEXT until the owner sets a new one on the PC. The API docs (/api/docs,
  /api/openapi.json) are PC-only too (LOCAL_ONLY_PREFIXES).
- The middleware records who is asking on the request scope (KTD4): scope["state"]["actor"] is "pc",
  "phone:<first 8 characters of the session's nonce>", "agent-api" (the agent listener) or "public" (/api/health
  without a session). Routes read it through `from_pc` (fails closed: no actor is not the PC) and `human` to keep some
  changes on the PC (R10: switching to production, raising a limit, turning the kill switch off) or with a person. It
  also sets application.actor's context variable for the rest of the request (KTD5), so trading audit rows say who
  acted (R13); sync routes run in FastAPI's threadpool with a copy of that context. Never the full cookie.
- The request's `Host` must be on the allow-list (KTD4), from this computer too, or it gets 400: 127.0.0.1,
  localhost, [::1], this machine's own addresses and BTCFLOW_ALLOWED_HOSTS, each with the app's port (the PC listener:
  loopback names with its port only). That stops DNS rebinding (a web page whose name later resolves to this PC). A
  miss re-lists the machine's addresses (at most every 30 s) first, so a new DHCP lease or a VPN interface brought up
  after start still works.
- A state-changing request (not GET/HEAD/OPTIONS) that a browser sent from another site is refused with 403, from
  the PC too and on /login and /pair (before the PIN or code is checked, so it counts no failure): `Sec-Fetch-Site`
  other than same-origin/none; or, only when `Sec-Fetch-Site` is absent, an `Origin` that is `null` or whose host is
  not on the allow-list. Requests with neither (curl, scripts) pass. A WebSocket upgrade with an `Origin` that is not
  one of ours is refused the same way (browsers always send one on a WebSocket).
- Security headers on every response, including a Content-Security-Policy that runs only the app's own script
  files (plus the login keypad, by hash; the PC-only Swagger UI at /api/docs may also load cdn.jsdelivr.net and run
  its inline boot script); /api, /login and /pair are never cached.

- HTTPS (P3 #1, 2026-11-01): the app itself speaks plain HTTP; phones get HTTPS from the Tailscale sidecar (compose
  profile `tailscale`, Tailscale Serve on https://settleview.<tailnet>.ts.net -> app:8000). The proxy headers are
  uvicorn's own job (its ProxyHeadersMiddleware): on the phone listener only, and only for a peer in
  BTCFLOW_FORWARDED_ALLOW_IPS (uvicorn's forwarded_allow_ips; empty = proxy headers off), uvicorn sets the scheme
  from X-Forwarded-Proto and the client from X-Forwarded-For (each phone's tailnet address). This module reads only
  the result: an https/wss scheme makes the session and device cookies `Secure` and adds HSTS; the client address is
  the lockout key's address as always (plus the device cookie). The PC and agent listeners never trust proxy headers.
  The public HTTPS name (BTCFLOW_PUBLIC_URL) is on the Host allow-list without a port, so it is also an allowed Origin
  for the cross-site and WebSocket checks.

Without the sidecar there is no HTTPS: use the phone port only on the home LAN or over a VPN, and never port-forward
it to the internet. The client IP is the TCP peer address unless uvicorn rewrote it for a configured proxy; a header
from any other peer changes nothing (a header must never make a request "local").
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import logging
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from btc_signal_engine.adapters.pc_credentials import PC_COOKIE, PC_COOKIE_MS, PcCredentials
from btc_signal_engine.application.actor import current_actor
from btc_signal_engine.application.settings_store import get_setting, put_setting

log = logging.getLogger(__name__)

COOKIE = "btcflow_session"
DEVICE_COOKIE = "btcflow_device"  # the login's per-device lockout key (B1#9); signed, carries nothing else
DEVICE_COOKIE_S = 365 * 86_400
LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PIN_RE = re.compile(r"^[0-9]{6,8}$")
PIN_POLICY = 2  # the rule a stored PIN was set under (KTD6): 2 = 6-8 digits; older records must be set again
PBKDF2_ITERATIONS = 310_000
SETTING = {"strategy": "access", "key": "phone_pin"}
MAX_FAILURES = 5
FAILURE_WINDOW_MS = 10 * 60_000
LOCKOUT_MS = 15 * 60_000
GLOBAL_MAX_FAILURES = 20  # wrong PINs across all addresses within FAILURE_WINDOW_MS lock every login for LOCKOUT_MS
OPEN_PATHS = ("/login",)
PAIR_PATH = "/pair"
HEALTH_PATH = "/api/health"
# The external agents' tool API: served on the agent listener (BTCFLOW_AGENT_PORT) only.
AGENT_PATHS = ("/api/agent/call/", "/api/agent/tools", "/api/agent/events/wait")
MAX_FORM_BYTES = 2048  # /login and /pair read at most this much of a body; more answers 413
# home-screen install files and the logo (no data), fetched without the cookie
PUBLIC_ASSETS = ("/manifest.json", "/icon.svg", "/icons/favicon.svg", "/icons/favicon-32.png",
                 "/icons/apple-touch-icon.png", "/icons/icon-192.png", "/icons/icon-512.png")
LOCAL_ONLY_PREFIXES = ("/api/access", "/api/docs", "/api/openapi.json")
ONLY_PC = "Only from the PC itself."
PC = "pc"  # scope["state"]["actor"] of the paired PC browser; a phone's is "phone:<nonce prefix>"
PHONE_PREFIX = "phone:"
AGENT_API = "agent-api"  # the agent listener's actor: never the PC, never a person
PUBLIC = "public"  # /api/health without a session
PAIR_TEXT = ("Pair this browser first: run `python -m btc_signal_engine.ops pair-pc` on this PC (Docker: `docker "
             "compose exec app python -m btc_signal_engine.ops pair-pc`) and open the link it prints.")
AGENT_PORT_TEXT = "Agent tools are served on the agent port (BTCFLOW_AGENT_PORT) only."
OFF_TEXT = "Phone access is off — set a PIN on the PC (Phone access card)."
RESET_TEXT = "Phone access needs a new 6–8 digit PIN — set it on the PC."
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
RELIST_MS = 30_000  # a Host miss re-lists the machine's addresses at most this often
REFUSED_LOG_MS = 60_000  # each refused Host is logged at most once a minute
# The login page's keypad. It stays inline (a phone without a session is sent to /login for every other path, so a
# script file would never load); the CSP allows exactly this text by its hash.
_KEYPAD_JS = ("(function(){var i=document.getElementById('pin'),d=document.getElementById('dots');"
              "function draw(){var h='';for(var n=0;n<Math.max(6,i.value.length);n++){h+='<span'+(n<i.value.length?"
              "' class=\"on\"':'')+'></span>';}d.innerHTML=h;}"
              "Array.prototype.forEach.call(document.querySelectorAll('[data-k]'),function(b){b.addEventListener("
              "'click',function(){var k=b.getAttribute('data-k');if(k==='del'){i.value=i.value.slice(0,-1);}"
              "else if(i.value.length<8){i.value+=k;}draw();});});i.addEventListener('input',draw);draw();})();")
_KEYPAD_HASH = "sha256-" + base64.b64encode(hashlib.sha256(_KEYPAD_JS.encode("utf-8")).digest()).decode("ascii")
# Scripts load only from this origin, and nothing compiles code at runtime (no 'unsafe-eval', B6#6): the web app runs
# Alpine's CSP build (web/vendor/alpine-csp-*.min.js), whose directives are property paths into each view's
# registered component (tests/api/test_alpine_csp.py). 'unsafe-inline' for styles: the login page's <style> and the
# Play page's style attributes.
CSP = (f"default-src 'self'; script-src 'self' '{_KEYPAD_HASH}'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
# FastAPI's Swagger UI (PC-only) loads its files from cdn.jsdelivr.net and boots with an inline script; only these
# path gets a CSP that allows that. (FastAPI's OAuth2 redirect page is not under /api/docs and the app uses no OAuth2.)
DOCS_PATHS = frozenset({"/api/docs"})
_CDN = "https://cdn.jsdelivr.net"
DOCS_CSP = (f"default-src 'self'; script-src 'self' 'unsafe-inline' {_CDN}; style-src 'self' 'unsafe-inline' {_CDN}; "
            "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # same-origin: no referrer ever reaches other sites, and the login form's POST carries the real Origin (with
    # no-referrer browsers send `Origin: null`).
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": CSP,
}
NO_STORE = "no-store, max-age=0"
# HTTPS only (a trusted proxy said so): browsers keep to HTTPS for this name for a year. Never sent over plain HTTP
# (browsers ignore it there anyway, and the LAN port must stay usable).
HSTS = "max-age=31536000"


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in LOOPBACK_HOSTS or h.startswith("127.")


def lan_ipv4() -> str | None:
    """This PC's IPv4 on the default-route interface (a UDP "connect" picks the route; no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # TEST-NET-1: never routed anywhere, only used to choose the interface
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") or ip == "0.0.0.0" else ip  # noqa: S104 - a comparison, not a bind
    except OSError:
        return None
    finally:
        s.close()


def local_addresses() -> set[str]:
    """This machine's IPv4/IPv6 addresses (every interface the host name resolves to, VPN ones included, plus the
    default-route IPv4)."""
    out: set[str] = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        infos = []
    for info in infos:
        out.add(str(info[4][0]))
    ip = lan_ipv4()
    if ip:
        out.add(ip)
    return out


class HostAllowList:
    """The `Host` values this app answers to (KTD4): loopback names, this machine's addresses and the configured extra
    names (BTCFLOW_ALLOWED_HOSTS), each with the app's port. `clock_ms` drives the re-list and log throttles."""

    def __init__(self, port: int, extra: str | Iterable[str] = (), *, api_host: str = "127.0.0.1",
                 pc_port: int | None = None, agent_port: int | None = None, exact: Iterable[str] = (),
                 clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000) -> None:
        """`exact`: Host values taken as they are (the public HTTPS name without a port, as a browser sends it for
        https://name/ and in its Origin); main listener only."""
        self.port = port
        self.clock_ms = clock_ms
        names = list(extra.split(",") if isinstance(extra, str) else extra)
        names += LOOPBACK_HOSTS
        if api_host and api_host not in ("0.0.0.0", "::"):  # noqa: S104 - a comparison, not a bind
            names.append(api_host)
        self._fixed = (frozenset(v for n in names for v in self._forms(n))
                       | frozenset(e.strip().lower() for e in exact if e.strip()))
        # The PC listener answers to loopback names on its own port only (no LAN, VPN or extra names: only this
        # computer reaches it, and a rebinding page's own name is refused).
        self._pc = (frozenset(v for n in LOOPBACK_HOSTS for v in self._forms(n, pc_port)) if pc_port is not None
                    else frozenset())
        # The agent listener: the loopback names and the configured extra names with its port (an external agent on
        # this PC, or on the VPN name the user listed).
        self._names = [n for n in names if n.strip()]
        self.agent_port = agent_port
        self._agent = (frozenset(v for n in self._names for v in self._forms(n, agent_port))
                       if agent_port is not None else frozenset())
        self._machine: frozenset[str] = frozenset()
        self._listed_ms = 0
        self._logged: dict[str, int] = {}
        self._relist()

    def _forms(self, name: str, port: int | None = None) -> set[str]:
        """The Host values that reach us as `name`: "name:port", "[v6]:port"; also the bare name on port 80. An entry
        that already has a port ("vpn.example:8443") is taken as it is. `port` defaults to the app's port."""
        port = self.port if port is None else port
        n = name.strip().lower().split("%", 1)[0]  # an IPv6 zone id (fe80::1%12): a browser's Host never has one
        if not n:
            return set()
        if (n.startswith("[") and "]:" in n) or n.count(":") == 1:
            return {n}
        bare = n if n.startswith("[") else (f"[{n}]" if ":" in n else n)
        return {f"{bare}:{port}"} | ({bare} if port == 80 else set())

    def _set_machine(self, addrs: Iterable[str]) -> None:
        self._machine = frozenset(v for a in addrs for v in self._forms(a))

    def _relist(self) -> None:
        self._listed_ms = self.clock_ms()
        self._set_machine(local_addresses())

    async def _relist_off_loop(self) -> None:
        # Resolving the host name can take seconds on Windows (DNS / NetBIOS), and any LAN device can trigger a
        # re-list with an unknown Host: never on the loop thread.
        self._listed_ms = self.clock_ms()
        self._set_machine(await asyncio.to_thread(local_addresses))

    def known(self, host: str, *, listener: str = "main") -> bool:
        """Is `host` (a Host header, or an Origin's host[:port]) one of ours, without re-listing. On the PC listener
        only the loopback names with the PC port are; on the agent listener the loopback and extra names with the
        agent port."""
        h = (host or "").strip().lower()
        if listener == "pc":
            return h in self._pc
        if listener == "agent":
            return h in self._agent
        return h in self._fixed or h in self._machine

    async def allows(self, host: str, *, listener: str = "main") -> bool:
        """`known`, after re-listing the machine's addresses on a miss (at most every RELIST_MS; only for the main
        listener, the others' lists are fixed). A final miss is logged at WARNING, at most once a minute per Host."""
        if self.known(host, listener=listener):
            return True
        now = self.clock_ms()
        if listener == "main" and now - self._listed_ms >= RELIST_MS:
            await self._relist_off_loop()
            if self.known(host):
                return True
        key = (host or "")[:200]
        if now - self._logged.get(key, now - REFUSED_LOG_MS) >= REFUSED_LOG_MS:
            if len(self._logged) > 1000:
                self._logged.clear()
            self._logged[key] = now
            log.warning("access: refused a request for Host %r (not on the allow-list; a VPN name goes in "
                        "BTCFLOW_ALLOWED_HOSTS)", key)
        return False


def hash_pin(pin: str, salt: bytes, iterations: int | None = None) -> str:
    """PBKDF2-HMAC-SHA256 of the PIN; `iterations` defaults to PBKDF2_ITERATIONS as it is now (read at call time)."""
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt,
                               PBKDF2_ITERATIONS if iterations is None else iterations).hex()


class PinStore:
    """The stored PIN record in strategy_settings (or in memory without a repository, for tests)."""

    def __init__(self, repo: Any | None = None) -> None:
        self.repo = repo
        self._mem: dict | None = None

    def load(self) -> dict | None:
        if self.repo is None:
            return self._mem
        return get_setting(self.repo, SETTING)

    def save(self, value: dict, now: int) -> None:
        if self.repo is None:
            self._mem = dict(value)
            return
        put_setting(self.repo, SETTING, value, now)


class AccessGuard:
    """PIN (hashed), sessions and per-IP lockouts. `clock_ms` is injectable for tests."""

    def __init__(self, store: PinStore | None = None, *, session_hours: float = 12.0, secret: bytes | None = None,
                 clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000) -> None:
        self.store = store or PinStore()
        self._secret = secret or secrets.token_bytes(32)
        self.session_ms = int(session_hours * 3_600_000)
        self.clock_ms = clock_ms
        self._failures: dict[str, list[int]] = {}
        self._locked_until: dict[str, int] = {}
        self._global_failures: list[int] = []  # every address's recent failures (pending ones included)
        self._global_locked_until = 0
        self._sessions: dict[str, int] = {}  # nonce -> expiry of the cookies issued under the current generation
        self._record = self.store.load()
        # Guards the record and the failure/lockout state: logins update them on the loop, the PC's /api/access/*
        # routes (sync, so FastAPI runs them in its threadpool) from worker threads. Held only for short updates.
        self._lock = threading.Lock()
        self._address_locks: dict[str, tuple[asyncio.Lock, int]] = {}  # ip -> (lock, logins holding or waiting)

    # -- the PIN ---------------------------------------------------------------------------------------------
    @property
    def pin_set(self) -> bool:
        """A PIN that phones can sign in with: one set under the current rule (policy >= PIN_POLICY)."""
        return self._usable(self._record)

    @property
    def reset_needed(self) -> bool:
        """A PIN set under an older rule (4-digit, no policy): phone access is off until the PC sets a new one."""
        return bool(self._record and self._record.get("hash")) and not self.pin_set

    @staticmethod
    def _usable(record: dict | None) -> bool:
        r = record or {}
        return bool(r.get("hash")) and int(r.get("policy") or 0) >= PIN_POLICY

    def _generation(self) -> int:
        return int((self._record or {}).get("generation") or 0)

    def set_pin(self, pin: str, again: str) -> tuple[bool, str]:
        """Sets or changes the PIN (6-8 digits, entered twice). Every existing phone session ends."""
        if not PIN_RE.fullmatch(pin or ""):
            return False, "The PIN must be 6 to 8 digits."
        if not hmac.compare_digest((pin or "").encode(), (again or "").encode()):
            return False, "The two PINs do not match."
        salt = secrets.token_bytes(16)
        iterations = PBKDF2_ITERATIONS
        digest = hash_pin(pin, salt, iterations)  # outside the lock; the route runs in FastAPI's threadpool
        with self._lock:
            self._record = {"algo": "pbkdf2_sha256", "iterations": iterations, "salt": salt.hex(),
                            "hash": digest, "generation": self._generation() + 1, "policy": PIN_POLICY}
            self.store.save(self._record, self.clock_ms())
            self._sessions.clear()
            self._failures.clear()
            self._locked_until.clear()
            self._global_failures.clear()
            self._global_locked_until = 0
        log.warning("access: phone PIN %s; all phone sessions ended", "changed" if self._generation() > 1 else "set")
        return True, "PIN saved. Phones must sign in again."

    def clear_pin(self) -> None:
        with self._lock:
            self._record = {"generation": self._generation() + 1}
            self.store.save(self._record, self.clock_ms())
            self._sessions.clear()
        log.warning("access: phone PIN cleared; phone access is off")

    def sign_out_all(self) -> None:
        """Ends every phone session (the PIN stays)."""
        with self._lock:
            self._record = {**(self._record or {}), "generation": self._generation() + 1}
            self.store.save(self._record, self.clock_ms())
            self._sessions.clear()
        log.warning("access: all phones signed out")

    def signed_in(self) -> int:
        now = self.clock_ms()
        for k in [k for k, exp in self._sessions.items() if exp <= now]:
            del self._sessions[k]
        return len(self._sessions) if self.pin_set else 0

    @staticmethod
    def _pin_matches(record: dict | None, pin: str) -> bool:
        """The PBKDF2 comparison against `record` (blocking: logins call it in a worker thread)."""
        r = record or {}
        if not AccessGuard._usable(r):
            return False
        got = hash_pin(pin or "", bytes.fromhex(r["salt"]), int(r["iterations"]))
        return hmac.compare_digest(got, r["hash"])

    # -- sessions ---------------------------------------------------------------------------------------------
    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires, nonce = self.clock_ms() + self.session_ms, secrets.token_hex(12)
        self._sessions[nonce] = expires
        payload = f"{expires}.{self._generation()}.{nonce}"
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: str | None) -> bool:
        return self.session_id(token) is not None

    def session_id(self, token: str | None) -> str | None:
        """The nonce of a valid session cookie, else None."""
        if not token or not self.pin_set:
            return None
        try:
            expires, gen, nonce, sig = token.split(".")
            payload = f"{int(expires)}.{int(gen)}.{nonce}"
        except ValueError:
            return None
        if not hmac.compare_digest(self._sign(payload), sig):
            return None
        if (int(gen) == self._generation() and self.clock_ms() < int(expires)
                and self._sessions.get(nonce) == int(expires)):
            return nonce
        return None

    # -- devices (the lockout key behind a gateway) ----------------------------------------------------------
    def issue_device(self) -> str:
        nonce = secrets.token_hex(12)
        return f"{nonce}.{self._sign('device.' + nonce)[:32]}"

    def device_id(self, token: str | None) -> str | None:
        """The device id of a valid device cookie (signed by this process), else None."""
        if not token or token.count(".") != 1:
            return None
        nonce, sig = token.split(".")
        if not re.fullmatch(r"[0-9a-f]{24}", nonce) or not hmac.compare_digest(self._sign("device." + nonce)[:32],
                                                                                sig):
            return None
        return nonce

    @staticmethod
    def client_key(ip: str, device: str | None) -> str:
        """Whose failures these are: the IP, plus the device when the client holds a valid device cookie."""
        return f"{ip}#{device[:8]}" if device else ip

    # -- login and lockout ------------------------------------------------------------------------------------
    def locked_for_ms(self, ip: str) -> int:
        """How long logins from `ip` stay refused: its own lockout or the lock on every login, whichever is longer."""
        return max(0, self._locked_until.get(ip, 0) - self.clock_ms(), self._global_locked_until - self.clock_ms())

    async def check_pin(self, ip: str, pin: str) -> tuple[bool, str | None]:
        """(ok, refusal). Failures are counted per IP; the PIN is never logged. Logins from one IP are checked one
        at a time (the others wait their turn); only the hash leaves the loop."""
        entry = self._address_locks.setdefault(ip, (asyncio.Lock(), 0))
        self._address_locks[ip] = (entry[0], entry[1] + 1)
        try:
            async with entry[0]:
                return await self._check_one(ip, pin)
        finally:
            lock, users = self._address_locks[ip]
            if users <= 1:
                del self._address_locks[ip]
            else:
                self._address_locks[ip] = (lock, users - 1)

    async def _check_one(self, ip: str, pin: str) -> tuple[bool, str | None]:
        # 1. Under the lock: refuse while this address or every login is locked out, or while the attempts already
        #    counted across all addresses (pending ones included) reach the global cap; else record the attempt as a
        #    pending failure, for the address and globally. Addresses hash in parallel, so the global cap must count
        #    pending attempts here to stay exact.
        with self._lock:
            now = self.clock_ms()
            if self.locked_for_ms(ip):
                log.warning("access: login attempt from %s while locked out", ip)
                return False, f"Too many wrong PINs. Try again in {self.locked_for_ms(ip) // 60_000 + 1} minutes."
            self._global_failures = [t for t in self._global_failures if now - t < FAILURE_WINDOW_MS]
            if len(self._global_failures) >= GLOBAL_MAX_FAILURES:
                log.warning("access: login attempt from %s refused: %d PINs from all addresses wrong or being "
                            "checked in 10 minutes", ip, len(self._global_failures))
                return False, "Too many wrong PINs. Try again in a few minutes."
            self._failures[ip] = [t for t in self._failures.get(ip, []) if now - t < FAILURE_WINDOW_MS] + [now]
            self._global_failures.append(now)
            record = self._record
        # 2. Only the PBKDF2 comparison runs off the loop (KTD1).
        matched = await asyncio.to_thread(self._pin_matches, record, pin)
        # 3. Under the lock: a match clears the address's failures, the pending one included; a miss keeps it and
        #    applies the lockout. A match against a PIN that was changed meanwhile does not sign in.
        with self._lock:
            if matched and self._record is record:
                self._failures.pop(ip, None)
                if now in self._global_failures:  # (gone if the global lock cleared the list meanwhile)
                    self._global_failures.remove(now)
                log.info("access: login from %s", ip)
                return True, None
            recent = self._failures.get(ip, [])
            log.warning("access: wrong PIN from %s (%d in 10 minutes)", ip, len(recent))
            if len(self._global_failures) >= GLOBAL_MAX_FAILURES and self._global_locked_until <= now:
                self._global_locked_until = now + LOCKOUT_MS
                self._global_failures.clear()
                log.warning("access: %d wrong PINs from all addresses in 10 minutes; every login locked for %d "
                            "minutes", GLOBAL_MAX_FAILURES, LOCKOUT_MS // 60_000)
                if len(recent) >= MAX_FAILURES:
                    self._failures.pop(ip, None)
                    self._locked_until[ip] = now + LOCKOUT_MS
                return False, f"Too many wrong PINs. Every login is locked for {LOCKOUT_MS // 60_000} minutes."
            if len(recent) >= MAX_FAILURES:
                self._locked_until[ip] = now + LOCKOUT_MS
                self._failures.pop(ip, None)
                log.warning("access: %s locked out for %d minutes", ip, LOCKOUT_MS // 60_000)
                return False, f"Too many wrong PINs. Locked for {LOCKOUT_MS // 60_000} minutes."
            return False, "Wrong PIN."


_STYLE = ("<style>body{margin:0;background:#0b1014;color:#e4eee9;font:16px/1.4 Arial,sans-serif;display:flex;"
          "min-height:100vh;align-items:center;justify-content:center}form,main{width:min(340px,92vw);display:grid;"
          "gap:14px}input,button{font:inherit;padding:14px;border-radius:10px;border:1px solid #233038;"
          "background:#162128;color:#e4eee9;min-height:48px}button{background:#99edac;color:#102117;font-weight:700;"
          "border:0}.err{color:#ef8a8c;margin:0;text-align:center}small{color:#81958f;text-align:center}"
          "#pin{position:absolute;opacity:0;width:1px;height:1px}.sr{position:absolute;left:-999px}"
          ".dots{display:flex;gap:14px;justify-content:center;min-height:24px}.dots span{width:18px;height:18px;"
          "border-radius:50%;border:2px solid #81958f}.dots span.on{background:#99edac;border-color:#99edac}"
          ".pad{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.pad button{min-height:64px;font-size:24px;"
          "background:#162128;color:#e4eee9;border:1px solid #233038}.pad .ok{background:#99edac;color:#102117}"
          "h1{text-align:center}</style>")
_HEAD = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
         "content=\"width=device-width,initial-scale=1\"><title>BTC Coach</title>" + _STYLE + "</head><body>")


def _login_page(message: str | None = None, *, https: bool = False) -> str:
    msg = f'<p class="err" role="alert">{html.escape(message)}</p>' if message else ""
    keys = "".join(f'<button type="button" data-k="{k}" aria-label="{k}">{k}</button>' for k in "123456789")
    return (_HEAD + '<form method="post" action="/login" id="f"><h1>BTC Coach</h1>' + msg +
            '<label for="pin" class="sr">PIN</label><input id="pin" name="pin" type="password" inputmode="numeric" '
            'pattern="[0-9]*" maxlength="8" autocomplete="current-password" required>'
            '<div class="dots" id="dots" aria-hidden="true"></div>'
            '<div class="pad">' + keys + '<button type="button" data-k="del" aria-label="Delete">&#9003;</button>'
            '<button type="button" data-k="0" aria-label="0">0</button>'
            '<button type="submit" class="ok" aria-label="Sign in">OK</button></div>'
            "<small>Stays signed in for 12 hours. 5 wrong tries locks this phone out for 15 minutes.</small>"
            + ("<small>Encrypted (HTTPS) over your tailnet.</small></form>" if https else
               "<small>Home network / VPN only. The connection is not encrypted (no HTTPS).</small></form>") +
            "<script>" + _KEYPAD_JS + "</script></body></html>")


def _off_page(text: str = OFF_TEXT) -> str:
    return _HEAD + f"<main><h1>BTC Coach</h1><p>{html.escape(text)}</p></main></body></html>"


def _pair_page(message: str | None = None, code: str = "") -> str:
    """"Pair this browser": the only page the PC's place shows without the PC cookie. A plain form (no script): the
    code from `ops pair-pc`, prefilled from the link's ?code=. Pairing is a POST, never the GET of a link."""
    msg = f'<p class="err" role="alert">{html.escape(message)}</p>' if message else ""
    return (_HEAD + '<form method="post" action="/pair" id="f"><h1>Pair this browser</h1>' + msg +
            "<p>This browser is not paired with Settleview yet. On this PC run:</p>"
            "<p><code>python -m btc_signal_engine.ops pair-pc</code></p>"
            "<small>Docker: <code>docker compose exec app python -m btc_signal_engine.ops pair-pc</code>. It prints a "
            "one-time code (good for 5 minutes) and a link that fills it in.</small>"
            '<label for="code">Pairing code</label><input id="code" name="code" autocomplete="one-time-code" '
            'autocapitalize="characters" spellcheck="false" maxlength="20" required value="' + html.escape(code[:20]) +
            '"><button type="submit">Pair</button>'
            "<small>A paired browser is the PC: no PIN, PC-only changes allowed. Sign every PC browser out from the "
            "Phone access card (or <code>ops signout-pc</code>).</small></form></body></html>")


def _client_ip(scope: dict) -> str:
    client = scope.get("client")
    return client[0] if client else ""


def _https(scope: dict) -> bool:
    """Did this request arrive over HTTPS (uvicorn set the scheme from a configured proxy's X-Forwarded-Proto)?"""
    return scope.get("scheme") in ("https", "wss")


def _hsts_send(send):
    """`send` adding Strict-Transport-Security to the response (HTTPS requests only)."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"strict-transport-security"]
            headers.append((b"strict-transport-security", HSTS.encode()))
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


def _server_port(scope: dict) -> int | None:
    """The port of the socket this request arrived on (the server sets it from the socket, not from any header)."""
    server = scope.get("server")
    try:
        return int(server[1]) if server else None
    except (TypeError, ValueError, IndexError):
        return None


async def _as_actor(actor: str, app, scope, receive, send) -> None:
    """Run the rest of the request as `actor`: on the scope (routes) and in the context variable (audit rows)."""
    # A new dict: the server may hand every request a shallow copy of one shared lifespan state.
    scope["state"] = {**scope.get("state", {}), "actor": actor}
    token = current_actor.set(actor)
    try:
        await app(scope, receive, send)
    finally:
        current_actor.reset(token)


def _actor(request: HTTPConnection) -> str | None:
    return request.scope.get("state", {}).get("actor")


def from_pc(request: HTTPConnection) -> bool:
    """Is this the paired PC browser? Read from the actor the access middleware recorded (KTD4). Fails closed: a
    request without an actor (no middleware) is not the PC; route tests that need the PC set the actor explicitly."""
    return _actor(request) == PC


def human(request: HTTPConnection) -> bool:
    """Is a person asking: the paired PC browser or a phone signed in with the PIN (never an agent, the agent
    listener, an unpaired caller or a request without an actor)?"""
    a = _actor(request) or ""
    return a == PC or (a.startswith(PHONE_PREFIX) and len(a) > len(PHONE_PREFIX))


def only_pc() -> JSONResponse:
    return JSONResponse({"detail": ONLY_PC}, status_code=403)


def only_people() -> JSONResponse:
    return JSONResponse({"ok": False, "code": "person_required",
                         "reason": "Only you can do this (the paired PC or a phone signed in with the PIN)."},
                        status_code=403)


def _with_headers(resp: Response, path: str) -> Response:
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if path.startswith("/api") or path.startswith("/login") or path.startswith(PAIR_PATH):
        resp.headers["Cache-Control"] = NO_STORE
    return resp


class BodyTooLarge(Exception):
    pass


async def read_small_form(request: Request, limit: int = MAX_FORM_BYTES) -> dict[str, list[str]]:
    """An urlencoded form body of at most `limit` bytes, read from the stream (never the whole body first: an
    unauthenticated client could send gigabytes). BodyTooLarge beyond it."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.strip().isdigit() and int(declared) > limit:
        raise BodyTooLarge
    got = bytearray()
    async for chunk in request.stream():
        got += chunk
        if len(got) > limit:
            raise BodyTooLarge
    return parse_qs(bytes(got).decode("utf-8", "replace"))


def _too_large() -> JSONResponse:
    return JSONResponse({"detail": "Request body too large."}, status_code=413)


class AccessMiddleware:
    """Pure ASGI middleware (covers HTTP and WebSocket scopes). The gate, in order (KTD4): the Host; the listener (the
    agent listener serves only AGENT_PATHS as "agent-api"); in the PC's place the PC cookie (else only /pair), from
    elsewhere the PIN and the session; the cross-site check for every state-changing request and WebSocket upgrade."""

    def __init__(self, app, guard: AccessGuard, hosts: HostAllowList, pc: PcCredentials | None = None,
                 local_clients: frozenset[str] = LOOPBACK_CLIENTS, pc_port: int | None = None,
                 agent_port: int | None = None) -> None:
        self.app = app
        self.guard = guard
        self.hosts = hosts
        self.pc = pc or PcCredentials()
        self.local_clients = local_clients  # without a PC listener: client IPs that may pair and hold the PC cookie
        self.pc_port = pc_port  # the PC listener's port: the only place the PC cookie counts when it is set
        self.agent_port = agent_port  # the agent listener's port: the tool API only, actor "agent-api"

    def _listener(self, scope) -> str:
        port = _server_port(scope)
        if self.pc_port is not None and port == self.pc_port:
            return "pc"
        if self.agent_port is not None and port == self.agent_port:
            return "agent"
        return "main"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        listener = self._listener(scope)
        conn = HTTPConnection(scope)  # (Request() takes HTTP scopes only)
        # HTTPS (uvicorn set the scheme from a configured proxy's X-Forwarded-Proto): HSTS on every response.
        if _https(scope):
            send = _hsts_send(send)
        host_ok = await self.hosts.allows(conn.headers.get("host", ""), listener=listener)
        if listener == "agent":
            await self._agent(scope, receive, send, host_ok, path)
            return
        # The PC's place: the PC listener; without one (a native install), a loopback / listed client on the main port.
        pc_place = listener == "pc" or (self.pc_port is None and _client_ip(scope) in self.local_clients)
        if pc_place and self.pc.stale:
            await asyncio.to_thread(self.pc.reload_if_stale)
        paired = pc_place and self.pc.valid(conn.cookies.get(PC_COOKIE))
        if scope["type"] == "websocket":
            sid = None if pc_place else self.guard.session_id(conn.cookies.get(COOKIE))
            if host_ok and not self._ws_cross_site(conn, listener) and (paired or sid):
                await _as_actor(PC if paired else f"{PHONE_PREFIX}{sid[:8]}", self.app, scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope, receive)
        cross_site = request.method not in SAFE_METHODS and self._cross_site(request, listener)
        if not host_ok:
            resp = JSONResponse({"detail": "Unknown Host."}, status_code=400)
        elif path.startswith(AGENT_PATHS):
            resp = JSONResponse({"ok": False, "code": "agent_port", "reason": AGENT_PORT_TEXT}, status_code=403)
        elif pc_place:
            resp = await self._pc_place(request, scope, receive, send, path, paired, cross_site)
            if resp is None:
                return
        elif path == PAIR_PATH:
            resp = JSONResponse({"detail": "Pairing works only on the PC itself."}, status_code=403)
        elif path == HEALTH_PATH and request.method in ("GET", "HEAD") and not self._phone(request):
            await _as_actor(PUBLIC, self.app, scope, receive, self._headers_send(send, path))
            return
        elif path.startswith(LOCAL_ONLY_PREFIXES):
            resp = only_pc()
        elif not self.guard.pin_set:
            text = RESET_TEXT if self.guard.reset_needed else OFF_TEXT
            resp = (JSONResponse({"detail": text}, status_code=403) if path.startswith("/api")
                    else HTMLResponse(_off_page(text), status_code=403))
        elif path in OPEN_PATHS:
            resp = (JSONResponse({"detail": "Cross-site request refused."}, status_code=403) if cross_site
                    else await self._login(request))
        elif path in PUBLIC_ASSETS and request.method in ("GET", "HEAD"):
            await self.app(scope, receive, self._headers_send(send, path))
            return
        elif sid := self._phone(request):
            if cross_site:
                resp = JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
            else:
                await _as_actor(f"{PHONE_PREFIX}{sid[:8]}", self.app, scope, receive, self._headers_send(send, path))
                return
        elif path.startswith("/api"):
            resp = JSONResponse({"detail": "Sign in first (PIN)."}, status_code=401)
        else:
            resp = RedirectResponse("/login", status_code=303)
        await _with_headers(resp, path)(scope, receive, send)

    def _phone(self, request: Request) -> str | None:
        return self.guard.session_id(request.cookies.get(COOKIE))

    async def _pc_place(self, request: Request, scope, receive, send, path: str, paired: bool,
                        cross_site: bool) -> Response | None:
        """The PC listener (or, without one, a loopback client on the main port). None: the request was handed on."""
        if cross_site:
            return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
        if paired:
            if path in OPEN_PATHS or path == PAIR_PATH:
                return RedirectResponse("/#play", status_code=303)
            await _as_actor(PC, self.app, scope, receive, self._headers_send(send, path))
            return None
        if path == HEALTH_PATH and request.method in ("GET", "HEAD"):
            await _as_actor(PUBLIC, self.app, scope, receive, self._headers_send(send, path))
            return None
        if path in PUBLIC_ASSETS and request.method in ("GET", "HEAD"):
            await self.app(scope, receive, self._headers_send(send, path))
            return None
        if path == PAIR_PATH:
            return await self._pair(request)
        if path.startswith("/api"):
            return JSONResponse({"detail": PAIR_TEXT, "code": "pair_required"}, status_code=401)
        return HTMLResponse(_pair_page(), status_code=401)

    async def _agent(self, scope, receive, send, host_ok: bool, path: str) -> None:
        """The agent listener: the tool API only, as "agent-api" (each call proves its session with its token)."""
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope, receive)
        if not host_ok:
            resp = JSONResponse({"detail": "Unknown Host."}, status_code=400)
        elif not path.startswith(AGENT_PATHS):
            resp = JSONResponse({"detail": "Not found: this port serves only the agent tool API (/api/agent/tools, "
                                           "/api/agent/call/{tool})."}, status_code=404)
        elif request.method not in SAFE_METHODS and self._cross_site(request, "agent"):
            resp = JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
        else:
            await _as_actor(AGENT_API, self.app, scope, receive, self._headers_send(send, path))
            return
        await _with_headers(resp, path)(scope, receive, send)

    def _origin_foreign(self, origin: str, listener: str) -> bool:
        parts = urlsplit(origin.strip())
        return parts.scheme not in ("http", "https") or not self.hosts.known(parts.netloc, listener=listener)

    def _cross_site(self, request: Request, listener: str = "main") -> bool:
        """Did a browser send this from another site? `Sec-Fetch-Site`, when present, decides alone (a form POST can
        carry `Origin: null` with same-origin); without it the `Origin` decides (`null` is refused). Neither header
        (curl, scripts on the PC) means no."""
        site = request.headers.get("sec-fetch-site")
        if site is not None:
            return site.strip().lower() not in ("same-origin", "none")
        origin = request.headers.get("origin")
        if origin is None:
            return False
        return self._origin_foreign(origin, listener)

    def _ws_cross_site(self, conn: HTTPConnection, listener: str) -> bool:
        """A WebSocket upgrade from another site's page (B1#11): browsers send `Origin` on every WebSocket, so an
        Origin that is not one of ours (or `null`) is refused; without one (not a browser) the cookie still decides."""
        site = conn.headers.get("sec-fetch-site")
        if site is not None and site.strip().lower() not in ("same-origin", "none"):
            return True
        origin = conn.headers.get("origin")
        return origin is not None and self._origin_foreign(origin, listener)

    @staticmethod
    def _headers_send(send, path: str):
        extra = ({**SECURITY_HEADERS, "Content-Security-Policy": DOCS_CSP} if path in DOCS_PATHS
                 else SECURITY_HEADERS)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                for k, v in extra.items():
                    if k.lower().encode() not in names:
                        headers.append((k.lower().encode(), v.encode()))
                if path.startswith("/api"):
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", NO_STORE.encode()))
                message = {**message, "headers": headers}
            await send(message)
        return wrapped

    async def _login(self, request: Request) -> Response:
        if request.method != "POST":
            if self.guard.valid(request.cookies.get(COOKIE)):
                return RedirectResponse("/#play", status_code=303)
            return self._with_device(request, HTMLResponse(_login_page(https=_https(request.scope))))
        device = self.guard.device_id(request.cookies.get(DEVICE_COOKIE))
        key = AccessGuard.client_key(_client_ip(request.scope), device)
        try:
            form = await read_small_form(request)
        except BodyTooLarge:
            return _too_large()
        pin = (form.get("pin") or [""])[0]
        ok, refusal = await self.guard.check_pin(key, pin)
        if not ok:  # (no device cookie handed out here: a cookie-less client stays in its IP's bucket)
            return HTMLResponse(_login_page(refusal, https=_https(request.scope)),
                                status_code=429 if "Too many" in (refusal or "") else 401)
        resp = RedirectResponse("/#play", status_code=303)
        resp.set_cookie(COOKIE, self.guard.issue(), max_age=self.guard.session_ms // 1000, httponly=True,
                        samesite="strict", path="/", secure=_https(request.scope))
        return resp

    def _with_device(self, request: Request, resp: Response) -> Response:
        """Give the login page's browser its device cookie (the lockout key) unless it holds a valid one."""
        if self.guard.device_id(request.cookies.get(DEVICE_COOKIE)) is None:
            resp.set_cookie(DEVICE_COOKIE, self.guard.issue_device(), max_age=DEVICE_COOKIE_S, httponly=True,
                            samesite="strict", path="/login", secure=_https(request.scope))
        return resp

    async def _pair(self, request: Request) -> Response:
        """GET: the form (the link's ?code= prefilled). POST: redeem the code; success sets the PC cookie."""
        if request.method != "POST":
            return HTMLResponse(_pair_page(code=request.query_params.get("code", "")))
        try:
            form = await read_small_form(request)
        except BodyTooLarge:
            return _too_large()
        code = (form.get("code") or [""])[0]
        ok, message = await asyncio.to_thread(self.pc.redeem, code)
        if not ok:
            log.warning("access: a pairing attempt failed (%s)", message.split(".")[0])
            return HTMLResponse(_pair_page(message), status_code=429 if "Too many" in message else 401)
        log.warning("access: a browser was paired as this PC")
        resp = RedirectResponse("/#play", status_code=303)
        resp.set_cookie(PC_COOKIE, self.pc.issue(), max_age=PC_COOKIE_MS // 1000, httponly=True, samesite="strict",
                        path="/")
        return resp


class PinBody(BaseModel):
    pin: str = Field(max_length=16)
    pin_again: str = Field(max_length=16)


def in_container() -> bool:
    """Inside a Docker / Podman container (their marker files): the machine's own address is the container's, which
    no phone can reach."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def phone_url(port: int, *, public_url: str = "", allowed_hosts: str | Iterable[str] = (),
              lan_ip_fn: Callable[[], str | None] = lan_ipv4,
              container_fn: Callable[[], bool] = in_container) -> str | None:
    """The address a phone opens: BTCFLOW_PUBLIC_URL, else the first BTCFLOW_ALLOWED_HOSTS name (with the app's port
    unless it names one), else this machine's LAN address, except in a container (that address is the container's:
    None, and the card says to set BTCFLOW_PUBLIC_URL)."""
    if public_url.strip():
        return public_url.strip().rstrip("/") + "/#play"
    names = [n.strip() for n in (allowed_hosts.split(",") if isinstance(allowed_hosts, str) else allowed_hosts)
             if n and n.strip()]
    if names:
        n = names[0]
        has_port = (n.startswith("[") and "]:" in n) or n.count(":") == 1
        bare = n if has_port or n.startswith("[") or ":" not in n else f"[{n}]"
        return f"http://{bare}/#play" if has_port else f"http://{bare}:{port}/#play"
    if container_fn():
        return None
    ip = lan_ip_fn()
    return f"http://{ip}:{port}/#play" if ip else None


def register_access_routes(app: FastAPI, guard: AccessGuard, *, api_host: str = "127.0.0.1", port: int = 8000,
                           lan_ip_fn: Callable[[], str | None] = lan_ipv4, pc: PcCredentials | None = None,
                           public_url: str = "", allowed_hosts: str | Iterable[str] = (),
                           container_fn: Callable[[], bool] = in_container) -> None:
    """The Phone access card's routes. PC only (the middleware refuses others; checked here again)."""

    def local(request: Request) -> bool:
        return from_pc(request)

    def view() -> dict:
        in_box = container_fn()
        ip = None if in_box else lan_ip_fn()
        listening = not is_loopback_host(api_host)
        url = phone_url(port, public_url=public_url, allowed_hosts=allowed_hosts, lan_ip_fn=lambda: ip,
                        container_fn=lambda: in_box)
        return {"pin_set": guard.pin_set, "reset_needed": guard.reset_needed, "signed_in": guard.signed_in(),
                "listening_on_network": listening, "api_host": api_host, "port": port,
                "lan_ip": ip, "lan_url": url, "in_container": in_box,
                "session_hours": guard.session_ms / 3_600_000,
                "note": (None if listening else "This server listens on this PC only: set BTCFLOW_API_HOST=0.0.0.0 "
                                                "in .env and restart to reach it from the phone."),
                "how_to_find_ip": ("In Docker the app cannot see this PC's address: set BTCFLOW_PUBLIC_URL=http://"
                                   "<this PC's LAN address>:8000 in .env and restart." if in_box else
                                   "If the address is missing: run `ipconfig` (Windows) or `ip addr` (Linux) and use "
                                   "the IPv4 address of the adapter with a default gateway (Wi-Fi or Ethernet).")}

    @app.get("/api/access")
    def access_view(request: Request):
        return view() if local(request) else only_pc()

    @app.post("/api/access/pin")
    def access_set_pin(body: PinBody, request: Request):
        if not local(request):
            return only_pc()
        ok, reason = guard.set_pin(body.pin, body.pin_again)
        return {"ok": ok, "reason": reason, **view()}

    @app.post("/api/access/signout")
    def access_signout(request: Request):
        if not local(request):
            return only_pc()
        guard.sign_out_all()
        return {"ok": True, "reason": "Every phone is signed out.", **view()}

    @app.post("/api/access/clear")
    def access_clear(request: Request):
        if not local(request):
            return only_pc()
        guard.clear_pin()
        return {"ok": True, "reason": "PIN cleared: phone access is off.", **view()}

    @app.post("/api/access/pc-signout")
    def access_pc_signout(request: Request):
        """Every PC browser, this one included, is signed out (a new PC secret): each pairs again with
        `ops pair-pc`."""
        if not local(request):
            return only_pc()
        if pc is None:
            return JSONResponse({"ok": False, "reason": "PC pairing is not configured."}, status_code=409)
        pc.sign_out_all()
        log.warning("access: every PC browser was signed out")
        resp = JSONResponse({"ok": True, "reason": "Every PC browser is signed out. Run `ops pair-pc` to pair "
                                                   "this one again."})
        resp.delete_cookie(PC_COOKIE, path="/")
        return resp


def _is_loopback_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_loopback


def local_client_set(extra: str | Iterable[str] = ()) -> frozenset[str]:
    """Loopback plus BTCFLOW_LOCAL_CLIENTS (comma-separated client IPs): without a PC listener, the clients that may
    pair as this PC (and hold its cookie) on the main port. WARNING for each non-loopback entry."""
    items = extra.split(",") if isinstance(extra, str) else extra
    names = {n.strip() for n in items if n and n.strip()}
    for name in sorted(n for n in names if not _is_loopback_ip(n)):
        log.warning("BTCFLOW_LOCAL_CLIENTS: requests from %s may pair as this PC (they still need a pairing code "
                    "from `ops pair-pc`). Safe only while the port is published on 127.0.0.1 and no proxy forwards "
                    "to the app (docs/operations.md)", name)
    return LOOPBACK_CLIENTS | names


def public_hosts(public_url: str) -> list[str]:
    """The Host values a browser sends for BTCFLOW_PUBLIC_URL: "name:port", plus the bare name when the port is the
    scheme's default (https://settleview.example.ts.net -> "settleview.example.ts.net:443" and
    "settleview.example.ts.net", the form a browser puts in Host and Origin). Empty for no URL."""
    if not public_url.strip():
        return []
    parts = urlsplit(public_url.strip())
    host = (parts.hostname or "").lower()
    bare = f"[{host}]" if ":" in host else host
    default = 443 if parts.scheme == "https" else 80
    port = parts.port or default
    return [f"{bare}:{port}"] + ([bare] if port == default else [])


def install_access(app, guard: AccessGuard, *, allowed_hosts: str | Iterable[str] = (),
                   local_clients: str | Iterable[str] = (), pc_port: int | None = None,
                   agent_port: int | None = None, pc: PcCredentials | None = None, public_url: str = "",
                   **route_kw) -> PcCredentials:
    """The access routes plus the middleware around the whole app (pages, /api, static files). `allowed_hosts`: extra
    Host names (BTCFLOW_ALLOWED_HOSTS, comma-separated) on top of loopback and this machine's addresses (plus
    `public_url`'s host). `local_clients`: without a PC listener, extra client IPs that may pair as this PC
    (BTCFLOW_LOCAL_CLIENTS). `pc_port`: the PC listener's port (BTCFLOW_PC_PORT); `agent_port`: the agent listener's
    (BTCFLOW_AGENT_PORT); each must differ from the app's port and from each other. `pc`: the PC credential (the
    data folder's secret and pairing files; in memory when None). Returns `pc`."""
    port = route_kw.get("port", 8000)
    if pc_port is not None and pc_port == port:
        raise ValueError(f"the PC port ({pc_port}) must differ from the app's port")
    if agent_port is not None and agent_port in (port, pc_port):
        raise ValueError(f"the agent port ({agent_port}) must differ from the app's and the PC's ports")
    pc = pc or PcCredentials(clock_ms=guard.clock_ms)
    names = [n for n in (allowed_hosts.split(",") if isinstance(allowed_hosts, str) else allowed_hosts) if n]
    exact = public_hosts(public_url)
    if public_url.strip():
        names.append(exact[0])
    register_access_routes(app, guard, pc=pc, public_url=public_url, allowed_hosts=names, **route_kw)
    hosts = HostAllowList(port, names, api_host=route_kw.get("api_host", "127.0.0.1"), pc_port=pc_port,
                          agent_port=agent_port, exact=exact, clock_ms=guard.clock_ms)
    app.add_middleware(AccessMiddleware, guard=guard, hosts=hosts, pc=pc, local_clients=local_client_set(local_clients),
                       pc_port=pc_port, agent_port=agent_port)
    app.state.pc_credentials = pc
    return pc
