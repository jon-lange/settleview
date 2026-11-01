"""Perps routes and push channel (perps plan U7; KTD11, KTD16): the View with a stale quote, the PIN middleware on the
stream, coalesced pushes, the shared kill switch reaching perps, request validation (422) versus refusals (200), the
View's cost, and shielded approvals. In-memory Kalshi fakes only (tests/perps/helpers.py); no network."""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.access import COOKIE, AccessGuard, PinStore, install_access
from btc_signal_engine.adapters.http.perp_routes import Inflight, PerpPublisher, register_perp_routes
from btc_signal_engine.adapters.http.push import PushHub
from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from btc_signal_engine.application.perp_view import PerpView
from btc_signal_engine.application.play_limits import CommittedToday
from tests.api.test_access import LOCAL, PIN, REMOTE, Clock, client
from tests.perps.helpers import NOW, FakeMargin, FakeQuotes, Limits, open_long, ready, service
from tests.trading.helpers import Exchange, Market
from tests.trading.test_strategy_scope import svc_for
from tests.pc import as_pc, paired

SETTINGS = {"band": 0.005, "amber": 0.05, "red": 0.02}
HOST = "192.168.1.10:8000"


def run(coro):
    return asyncio.run(coro)


class Rig:
    def __init__(self, store, *, pin: bool = False, quotes: FakeQuotes | None = None, limits=None,
                 committed_today_fn=None) -> None:
        self.fx = FakeMargin()
        self.quotes = quotes or FakeQuotes(self.fx)
        self.svc = service(store, self.fx, quotes=self.quotes, limits=limits, committed_today_fn=committed_today_fn)
        self.you = svc_for(store, Exchange(Market()), "you")
        self.view = PerpView(self.svc, settings=SETTINGS)
        self.hub = PushHub(min_interval_s=0.1, heartbeat_s=15.0)
        self.now = lambda: NOW
        self.pub = PerpPublisher(self.view, self.hub, self.now)
        self.app = FastAPI()
        register_trading_routes(self.app, {"you": self.you}, self.now, extra=[self.svc], on_control=self.pub.notify)
        register_perp_routes(self.app, self.view, self.hub, self.now, notify=self.pub.notify)
        self.guard = None
        if pin:
            self.guard = AccessGuard(PinStore(store), session_hours=12, clock_ms=Clock())
            install_access(self.app, self.guard, api_host="0.0.0.0", port=8000, lan_ip_fn=lambda: "192.168.1.10",
                           allowed_hosts="192.168.1.10")
            assert client(self.app, LOCAL).post("/api/access/pin", json={"pin": PIN, "pin_again": PIN}).json()["ok"]

    def local(self) -> TestClient:
        """The PC: the paired browser behind the access middleware, or (no middleware) the actor set as the PC."""
        if self.guard is None:
            return TestClient(as_pc(self.app), client=("127.0.0.1", 1), base_url="http://127.0.0.1:8000")
        return paired(TestClient(self.app, client=("127.0.0.1", 1), base_url="http://127.0.0.1:8000"))


async def sse(app, *, cookie: str | None = None, who=REMOTE, during=None, frames: int = 2,
              quiet_s: float = 0.0) -> tuple[int, list[tuple[str, dict]]]:
    """Open GET /api/perps/stream straight on the ASGI app (the response never ends, so no buffering client fits);
    returns (status, [(event, data), ...]) after `frames` frames, plus any arriving within `quiet_s` more.
    `during(n)` runs after the n-th frame arrives."""
    headers = [(b"host", HOST.encode())]
    if cookie:
        headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/api/perps/stream", "raw_path": b"/api/perps/stream", "query_string": b"", "root_path": "",
             "headers": headers, "client": who, "server": ("192.168.1.10", 8000)}
    status, out, buf = [None], [], [""]
    enough = asyncio.Event()
    hold = asyncio.Event()

    async def receive():
        await hold.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status[0] = msg["status"]
        elif msg["type"] == "http.response.body":
            buf[0] += msg.get("body", b"").decode()
            while "\n\n" in buf[0]:
                frame, buf[0] = buf[0].split("\n\n", 1)
                lines = dict(line.split(": ", 1) for line in frame.split("\n"))
                out.append((lines["event"], json.loads(lines["data"])))
                if during is not None:
                    during(len(out))
                if len(out) >= frames:
                    enough.set()
            if not msg.get("more_body", False):
                enough.set()

    task = asyncio.ensure_future(app(scope, receive, send))
    await asyncio.wait_for(enough.wait(), 3.0)
    if quiet_s:
        await asyncio.sleep(quiet_s)
    hold.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return status[0], out


# -- the View --------------------------------------------------------------------------------------------------------
def test_the_view_with_a_stale_quote_has_no_entry_readiness_and_states_the_quote_age(store):
    r = Rig(store)
    run(ready(r.svc))
    r.quotes.stale(3_400)
    v = r.local().get("/api/perps/view").json()
    assert v["enabled"] is True and v["quote"] is None
    assert v["quote_reason"] == "No fresh Kalshi perps price (quote 3.4 s old)."
    assert v["can"]["entry"] is False and "quote 3.4 s old" in v["can"]["entry_reason"]
    assert any(b["code"] == "stale" and "quote 3.4 s old" in b["text"] for b in v["banners"])
    # the rest of the contract is there, with Kalshi's figures (FakeMargin: taker 0.12%, 6.8473x long at $1k)
    m = v["market"]
    assert (m["ticker"], m["taker_fee"], m["max_leverage_long"], m["max_leverage_short"]) == (
        "KXBTCPERP", 0.0012, 6.8473, 6.38)
    assert v["settings"] == SETTINGS and v["warnings_note"] == "Warnings show only while this screen is open."
    assert v["funding"] == {"rate": 0.0001, "next_ms": NOW - NOW % 86_400_000 + 12 * 3_600_000}  # the REST estimate
    assert v["modes_available"] == ["off", "perp_prod"] and v["exchange"]["trading_active"] is True


def test_a_fresh_quote_is_shown_with_its_age_and_the_ticker_channels_mark(store):
    r = Rig(store)
    run(ready(r.svc))
    v = r.local().get("/api/perps/view").json()
    assert v["quote"] == {"bid": 7.7969, "ask": 7.7971, "bid_size": 100.0, "ask_size": 100.0, "last": 7.797,
                          "mark": 7.797, "reference": 7.7965, "age_ms": 120}
    assert v["quote_reason"] is None and v["can"]["entry"] is True


# -- the push channel ------------------------------------------------------------------------------------------------
def test_the_stream_needs_a_pin_session_and_a_signed_in_phone_gets_the_snapshot_then_a_changed_quote(store):
    r = Rig(store, pin=True)
    run(ready(r.svc))

    async def go():
        status, frames = await sse(r.app, who=REMOTE, frames=1)
        assert status == 401 and frames == []
        r.view.changes(NOW)  # the publisher's baseline

        def during(n):
            if n == 1:
                r.fx.bid = Decimal("7.7950")
                r.pub.publish_now()
        return await sse(r.app, cookie=r.guard.issue(), who=REMOTE, during=during, frames=2)

    status, frames = run(go())
    assert status == 200
    (e1, snap), (e2, change) = frames
    assert e1 == "view" and snap["enabled"] is True and snap["quote"]["bid"] == 7.7969 and "can" in snap
    assert e2 == "view" and change["quote"]["bid"] == 7.795 and "mode" not in change


def test_fifty_quote_changes_inside_100_ms_are_one_frame_with_the_newest_values(store):
    r = Rig(store)
    run(ready(r.svc))

    async def go():
        r.view.changes(NOW)
        pub = asyncio.ensure_future(r.pub.run())
        t0 = []

        def during(n):
            if n == 1:
                t0.append(time.monotonic())
                for i in range(50):
                    r.fx.bid = Decimal("7.7900") + Decimal(i) / 10_000
                    r.pub.notify()
        try:
            return await sse(r.app, who=LOCAL, during=during, frames=2, quiet_s=0.3), t0
        finally:
            pub.cancel()

    (status, frames), _ = run(go())
    assert status == 200 and len(frames) == 2  # the snapshot, then ONE frame (nothing more within 0.3 s)
    assert frames[1][0] == "view" and frames[1][1]["quote"]["bid"] == 7.7949


# -- shared controls (KTD16) -----------------------------------------------------------------------------------------
def test_kill_from_the_existing_route_sets_the_shared_switch_and_the_perps_view_reports_it(store):
    r = Rig(store)
    run(ready(r.svc))
    c = r.local()
    assert c.get("/api/perps/view").json()["killed"] is False
    assert c.post("/api/trading/kill").status_code == 200
    assert store.select("trading_settings", {"key": "killed"})[0]["value"] == {"on": True}
    v = c.get("/api/perps/view").json()
    assert v["killed"] is True and v["can"]["entry"] is False
    assert v["can"]["entry_reason"] == "The kill switch is on: no new perps entries or adds."
    assert v["can"]["exit_reason"] is None or "kill" not in v["can"]["exit_reason"]
    assert [a["event"] for a in store.select("trading_audit", {"strategy": "perps", "event": "kill"})] == ["kill"]
    c.post("/api/trading/resume")
    assert c.get("/api/perps/view").json()["killed"] is False


def test_a_perps_failure_during_kill_is_audited_and_never_stops_the_event_services(store):
    r = Rig(store)
    run(ready(r.svc))

    async def boom(now):
        raise RuntimeError("perps database locked")
    r.svc.kill = boom
    res = r.local().post("/api/trading/kill")
    assert res.status_code == 200 and res.json()["killed"] is True  # the event service's answer
    assert r.you.killed() is True
    failed = store.select("trading_audit", {"strategy": "perps", "code": "kill_failed"})
    assert len(failed) == 1 and "perps database locked" in failed[0]["reason"]


def test_the_heartbeat_reaches_the_perps_service(store):
    r = Rig(store)
    run(ready(r.svc))
    r.svc._last_seen = None
    r.local().post("/api/trading/heartbeat", json={"visible": True})
    assert r.svc.present(NOW) is True


# -- requests ------------------------------------------------------------------------------------------------------
ENTRY = {"action": "entry", "side": "long", "collateral": 10.0, "leverage": 3.0, "stop_loss": None,
         "take_profit": None, "fill": "market", "limit_price": None, "band": None}


def test_a_malformed_prepare_is_422_and_a_refused_one_is_200_with_code_and_reason(store):
    r = Rig(store)
    run(ready(r.svc))
    c = r.local()
    assert c.post("/api/perps/prepare", json={**ENTRY, "side": "sideways"}).status_code == 422
    assert c.post("/api/perps/prepare", json={k: v for k, v in ENTRY.items() if k != "stop_loss"}).status_code == 422
    assert c.post("/api/perps/prepare", json={**ENTRY, "collateral": "ten"}).status_code == 422
    c.post("/api/trading/kill")
    res = c.post("/api/perps/prepare", json=ENTRY)
    assert res.status_code == 200
    assert res.json() == {"ok": False, "code": "killed",
                          "reason": "The kill switch is on: no new perps entries or adds."}


def test_prepare_then_approve_through_the_routes_returns_the_outcome_and_the_whole_view(store):
    r = Rig(store)
    run(ready(r.svc))
    c = r.local()
    prep = c.post("/api/perps/prepare", json=ENTRY).json()
    assert prep["ok"] and prep["ticket"]["count"] == 3.0
    out = c.post("/api/perps/approve", json={"token": prep["token"], "shown": prep["ticket"]}).json()
    assert out["ok"] and out["outcome"]["text"] == "Long 3 @ $7.7971 (fee $0.0281)."
    assert out["view"]["position"]["size"] == 3.0 and out["view"]["enabled"] is True and "quote" in out["view"]
    rec = c.get("/api/perps/record", params={"last": 5}).json()
    assert rec["rows"] == [] and (rec["wins"], rec["losses"], rec["pnl"]) == (0, 0, 0.0)  # open, so not on the record
    assert c.get("/api/perps/record", params={"last": 0}).status_code == 422


def test_a_phone_can_switch_perps_to_production_only_with_the_typed_confirmation(store):
    r = Rig(store, pin=True)
    run(ready(r.svc))
    phone = client(r.app, REMOTE)
    phone.cookies.set(COOKIE, r.guard.issue())
    assert phone.post("/api/perps/mode", json={"mode": "off", "confirm": None}).json()["ok"] is True
    bad = phone.post("/api/perps/mode", json={"mode": "perp_prod", "confirm": "yes"})
    assert bad.status_code == 200 and bad.json()["ok"] is False
    res = phone.post("/api/perps/mode", json={"mode": "perp_prod", "confirm": "I understand this uses real money"}).json()
    assert res["ok"] is True and res["view"]["mode"] == "perp_prod"


# -- cost and shutdown -------------------------------------------------------------------------------------------------
def test_computing_the_view_with_an_open_position_takes_under_5_ms(store):
    total = CommittedToday()
    r = Rig(store, limits=Limits(per_trade=100.0, per_day=500.0), committed_today_fn=total.total)
    total.add_source(r.svc.committed_by_env)
    total.add_source(r.you.committed_by_env)

    async def setup():
        await ready(r.svc)
        await open_long(r.svc, r.fx)
        for i in range(30):  # a busy day: 30 more entries' orders on record
            store.insert("perp_orders", [{"id": f"old{i}", "env": "perp_prod", "position_id": "old",
                                          "client_order_id": f"svperp-old{i}", "order_id": f"o{i}",
                                          "role": "entry", "side": "bid", "price": 7.8, "count": 3.0,
                                          "collateral": 10.0, "time_in_force": "immediate_or_cancel",
                                          "reduce_only": False, "filled_count": 3.0, "avg_fill_price": 7.8,
                                          "fees": 0.03, "outcome": "filled", "reason": None,
                                          "created_ms": NOW - 1_000 * i, "updated_ms": NOW}])
    run(setup())
    v = r.view.view(NOW)
    assert v["position"]["size"] == 3.0 and v["limits"]["committed_today"] == 310.0
    n = 50
    t0 = time.perf_counter()
    for _ in range(n):
        r.view.view(NOW)
    per_ms = (time.perf_counter() - t0) / n * 1000
    assert per_ms < 5.0, f"the View took {per_ms:.2f} ms"


def test_an_approval_finishes_even_when_its_request_is_cancelled_and_shutdown_waits_for_it():
    async def go():
        flight = Inflight()
        done = []

        async def save_send_record():
            await asyncio.sleep(0.05)
            done.append("recorded")
            return {"ok": True}
        req = asyncio.ensure_future(flight.run(save_send_record()))
        await asyncio.sleep(0.01)
        req.cancel()  # the request dies (dropped connection, shutdown)
        try:
            await req
        except asyncio.CancelledError:
            pass
        assert done == [] and flight.pending == 1
        await flight.drain(1.0)
        return done, flight.pending

    assert run(go()) == (["recorded"], 0)
