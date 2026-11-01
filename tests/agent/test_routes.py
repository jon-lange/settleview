"""Agent routes and live stream (U7): launch / stop / resume / note, the prompt store, the tool endpoints the MCP server
uses, and the AG-UI event stream. A bare FastAPI app over a real AgentSessions / AgentTools / TradingService on the
shadow exchange (no network) and a scripted fake model; the access middleware where the phone matters."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.access import AGENT_API, AccessGuard, PinStore, install_access
from btc_signal_engine.adapters.http.agent_feed import AgUiFeed, FleetFeeds, ObservedTools, json_patch
from btc_signal_engine.adapters.http.agent_routes import register_agent_routes, wake_hook, wake_start_hook
from btc_signal_engine.adapters.http.push import FeedHub
from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from btc_signal_engine.application.agent_prompts import AgentPrompts
from btc_signal_engine.application.agent_fleet import AgentFleet
from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.play_limits import PlayLimits
from btc_signal_engine.application.ports import LlmInvalidKey, LlmUnavailable
from tests.agent.fake_harness import FakeHarness, text, thinking, tool_use, turn
from tests.agent.test_runner import Clock, _yield
from tests.agent.test_tools import HOUR, SID, Rig, entry
from tests.pc import as_actor, as_pc, paired
from tests.trading.helpers import NOW

PIN = "482916"
AGENT_PORT = 8002
DEFAULT = "DEFAULT SYSTEM PROMPT"


def launch_body(**kw):
    b = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 20.0, "per_trade": 10.0, "ends_ts": NOW + HOUR,
         "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto", "confirm": "START"}
    b.update(kw)
    return b


class BadKeyLlm(FakeHarness):
    async def check_key(self) -> None:
        raise LlmInvalidKey("The Anthropic API key was rejected: invalid x-api-key")


class World:
    def __init__(self, store, *script, llm=..., access=False):
        self.store = store
        self.r = r = Rig(store)
        self.prompts = AgentPrompts(store, DEFAULT)
        self.prompts.seed(NOW)
        self.hub = FeedHub(trimmed=lambda n: {"type": "CUSTOM", "name": "feed_trimmed", "value": {"dropped": n}})
        main = AgUiFeed(self.hub, lambda: {"session": r.ag.latest_view(NOW), "prompts": self.prompts.versions()},
                        decisions_fn=r.tools.decisions, now_fn=lambda: NOW)
        # as the app: one feed per session whose stream is open (GET /api/agent/sessions/{id}/stream)
        self.feed = FleetFeeds(main, lambda sid: AgUiFeed(
            FeedHub(trimmed=lambda n: {"type": "CUSTOM", "name": "feed_trimmed", "value": {"dropped": n}}),
            lambda: {"session": r.ag.view(sid, NOW), "prompts": self.prompts.versions()},
            decisions_fn=r.tools.decisions, now_fn=lambda: NOW), live_fn=lambda sid: r.ag.get(sid) is not None)
        r.ag._notify = self.feed.session_changed
        self.llm = FakeHarness(*script) if llm is ... else llm
        self.runner = self.fleet = None
        if self.llm is not None:
            self.clock = Clock()
            self.runner = self.make_runner()
            made = [self.runner]  # the first launch gets this runner (the tests drive it); later ones fresh ones
            self.fleet = AgentFleet(r.ag, r.play, lambda: made.pop() if made else self.make_runner(),
                                    clock=self.clock, tools=r.tools)
            r.tools._events_fn = self.fleet.events
        self.started: list[str] = []
        self.limits = PlayLimits(store)
        self.app = app = FastAPI()
        register_trading_routes(app, {"you": r.svc}, lambda: NOW)
        self.key = register_agent_routes(app, sessions=r.ag, tools=r.tools, prompts=self.prompts, feed=self.feed,
                                         now_fn=lambda: NOW, llm=self.llm, fleet=self.fleet,
                                         start_task=lambda name, factory: self.started.append(name),
                                         limits=self.limits)
        if access:  # the real middleware: the paired PC browser, the agent listener on its own port
            install_access(app, AccessGuard(PinStore(store), session_hours=12, clock_ms=lambda: NOW),
                           api_host="0.0.0.0", port=8000, lan_ip_fn=lambda: "192.168.1.10",
                           allowed_hosts="192.168.1.10", agent_port=AGENT_PORT)
            self.pc = paired(TestClient(app, client=("127.0.0.1", 1), follow_redirects=False,
                                        base_url="http://127.0.0.1:8000"))
            self.agent = TestClient(app, client=("127.0.0.1", 1), base_url=f"http://127.0.0.1:{AGENT_PORT}")
        else:  # route tests: the actor the middleware would record
            self.pc = TestClient(as_pc(app))
            self.agent = TestClient(as_actor(app, AGENT_API))

    def make_runner(self) -> AgentRunner:
        box: dict = {}
        box["r"] = AgentRunner(self.r.ag, ObservedTools(self.r.tools, self.feed), self.r.play, self.llm,
                               prompts_fn=self.prompts.prompts_fn(lambda: self.r.ag.get(box["r"].session_id)),
                               clock=self.clock, notify_fn=wake_hook(self.r.ag, self.feed), live_fn=self.feed.live,
                               start_fn=wake_start_hook(self.r.ag, self.feed), sleep=_yield)
        return box["r"]

    def phone(self) -> TestClient:
        assert self.pc.post("/api/access/pin", json={"pin": PIN, "pin_again": PIN}).json()["ok"]
        phone = TestClient(self.app, client=("192.168.1.50", 1), follow_redirects=False,
                           base_url="http://192.168.1.10:8000")
        assert phone.post("/login", data={"pin": PIN}).status_code == 303
        return phone

    def nothing_prepared(self) -> bool:
        return (self.r.svc._tickets == {} and self.r.buys() == [] and self.store.select("trading_brackets") == []
                and not [a for a in self.store.select("trading_audit") if a["event"] == "proposed"])


# -- launch ---------------------------------------------------------------------------------------------------------
def test_launch_needs_the_typed_START_then_checks_the_key_pins_prompts_and_starts_the_loop(store):
    w = World(store)
    for confirm in (None, "start", "yes"):
        out = w.pc.post("/api/agent/launch", json=launch_body(confirm=confirm)).json()
        assert out["code"] == "confirm_required" and "START" in out["reason"]
    assert w.r.ag.latest() is None and w.started == [] and w.llm.keys_checked == 0
    out = w.pc.post("/api/agent/launch", json=launch_body()).json()
    assert out["ok"] and out["session"]["state"] == "running" and "token" not in out
    assert out["session"]["prompt_versions"] == {"system": 1, "user": 0}
    assert w.llm.keys_checked == 1 and w.started == ["agent_fleet"]  # the fleet's one loop, with the first launch
    assert w.runner.session_id == SID and w.runner.events is not None  # a note can arrive before the first tick
    st = w.pc.get("/api/agent/status").json()
    assert st["in_app"] == {"available": True, "reason": None} and st["key"]["valid"] is True
    assert {m["id"] for m in st["models"]} >= {"claude-haiku-5-5"} and st["live_count"] == 1
    assert "session" not in st  # the deprecated single-session field is gone (the fleet names its session)
    assert "token_hash" not in json.dumps(st) and st["prompts"] == {"system": 1, "user": 0}
    assert st["server_ms"] == NOW and st["settle_warnings"] == []  # nothing settling too long
    k15 = st["venues"]["kalshi_15m"]
    assert {k: k15[k] for k in ("strategy", "mode")} == {"strategy": "you", "mode": "shadow"} and k15["label"]
    assert k15["also_traded_by"] == [{"id": SID, "name": "Kalshi 15m agent"}] and "H4" in k15["rules"]
    assert st["venues"]["polymarket_15m"]["strategy"] == "you_poly"
    assert st["venues"]["polymarket_15m"]["mode"] is None  # no such service in this app
    assert k15["next_agent"] == {"agent_id": None, "name": "Kalshi 15m agent 2"}  # what a nameless launch runs
    one = w.pc.get(f"/api/agent/sessions/{SID}").json()["session"]
    assert one["budget_parts"] == {"realized": 0.0, "at_risk": 0.0, "pending_settlement": 0.0}
    w.r.ag._id = lambda: "sess0002abcd"
    again = w.pc.post("/api/agent/launch", json=launch_body()).json()  # multi-agent: a second one launches
    assert again["ok"] and again["id"] == "sess0002abcd" and w.started == ["agent_fleet"]
    assert again["also_traded_by"] == [{"id": SID, "name": "Kalshi 15m agent"}]


def test_launch_is_refused_for_a_venue_in_production_an_unknown_model_or_sell_on_stop(store):
    w = World(store)
    w.r.svc.mode = "prod"
    out = w.pc.post("/api/agent/launch", json=launch_body()).json()
    assert out["code"] == "production_locked" and w.r.ag.latest() is None and w.started == []
    w.r.svc.mode = "shadow"
    assert w.pc.post("/api/agent/launch", json=launch_body(model="gpt-9")).json()["code"] == "unknown_model"
    assert w.pc.post("/api/agent/launch", json=launch_body(sell_on_stop=True)).json()["code"] == "not_available"
    assert w.pc.post("/api/agent/launch", json=launch_body(budget=0)).json()["code"] == "invalid"
    assert w.r.ag.latest() is None


def test_an_invalid_key_refuses_the_launch_and_status_says_so(store):
    w = World(store, llm=BadKeyLlm())
    out = w.pc.post("/api/agent/launch", json=launch_body()).json()
    assert out == {"ok": False, "code": "agent_unavailable", "reason": "agent unavailable: invalid key"}
    assert w.r.ag.latest() is None and w.started == []
    st = w.pc.get("/api/agent/status").json()
    assert st["in_app"]["available"] is False and st["in_app"]["reason"].startswith("agent unavailable: invalid key")
    assert st["key"]["present"] and st["key"]["valid"] is False


def test_without_a_key_in_app_is_unavailable_but_an_external_session_launches_and_gets_its_token_once(store):
    w = World(store, llm=None)
    st = w.pc.get("/api/agent/status").json()
    assert st["in_app"]["available"] is False and "no key" in st["in_app"]["reason"]
    assert st["key"] == {"present": False, "valid": None, "checked_ts": None, "reason": None}
    assert w.pc.post("/api/agent/launch", json=launch_body()).json()["code"] == "agent_unavailable"
    out = w.pc.post("/api/agent/launch", json=launch_body(kind="external", model=None, spend_cap=None)).json()
    assert out["ok"] and len(out["token"]) > 20 and w.started == []
    assert "token" not in json.dumps(w.pc.get(f"/api/agent/sessions/{out['id']}").json()).replace("token_", "")


# -- phone: launch within limits, stop without confirmation, resume with RESUME ----------------------------------------
def test_phone_launch_within_limits_stop_without_confirmation_resume_needs_RESUME(store):
    w = World(store, access=True)
    phone = w.phone()
    assert w.limits.set(5.0, None, None, NOW)["ok"]  # the user's per-trade limit: $5
    out = phone.post("/api/agent/launch", json=launch_body(per_trade=10.0)).json()
    assert out["code"] == "over_your_limit" and w.r.ag.latest() is None
    assert phone.post("/api/agent/launch", json=launch_body(per_trade=5.0)).json()["ok"]
    sid = w.r.ag.latest()["id"]
    w.r.ag.pause(sid, NOW, "The model is unavailable: overloaded")
    out = phone.post(f"/api/agent/sessions/{sid}/resume", json={}).json()
    assert out["code"] == "confirm_required" and w.r.ag.get(sid)["state"] == "paused"
    assert phone.post(f"/api/agent/sessions/{sid}/resume", json={"confirm": "RESUME"}).json()["session"]["state"] == \
        "running"
    assert phone.post("/api/agent/prompts/save", json={"layer": "user", "text": "x"}).status_code == 403
    out = phone.post(f"/api/agent/sessions/{sid}/stop").json()  # one tap, no confirmation (R18)
    assert out["ok"] and out["session"]["state"] == "ended" and out["session"]["end_reason"] == "stopped"
    assert phone.post(f"/api/agent/sessions/{sid}/stop").json()["code"] == "no_session"


def test_the_pc_resumes_without_a_phrase_and_the_kill_switch_blocks_it(store):
    w = World(store)
    assert w.pc.post("/api/agent/launch", json=launch_body()).json()["ok"]
    assert w.pc.post("/api/trading/kill").json()["killed"]
    w.r.ag.review(NOW)
    assert w.r.ag.latest()["state"] == "paused"
    assert w.pc.post(f"/api/agent/sessions/{SID}/resume").json()["code"] == "cannot_resume"
    w.pc.post("/api/trading/resume")
    assert w.pc.post(f"/api/agent/sessions/{SID}/resume").json()["session"]["state"] == "running"


# -- notes --------------------------------------------------------------------------------------------------------------
def test_a_note_is_queued_then_marked_delivered_when_a_wake_takes_it(store):
    w = World(store, turn(text("Noted.")))
    assert w.pc.post("/api/agent/launch", json=launch_body()).json()["ok"]
    out = w.pc.post(f"/api/agent/sessions/{SID}/note", json={"text": "Only trade UP today."}).json()
    assert out["ok"] and out["note"]["state"] == "queued" and out["note"]["by"] == "pc"
    asyncio.run(w.runner.tick())
    rows = w.pc.get(f"/api/agent/sessions/{SID}/journal").json()["rows"]
    note = next(r for r in rows if r["kind"] == "note")
    assert note["data"]["state"] == "delivered" and note["data"]["delivered_ts"] == NOW
    wake = next(r for r in rows if r["kind"] == "wake")
    assert wake["data"]["trigger"][0]["type"] == "human_note"
    assert w.pc.get(f"/api/agent/sessions/{SID}/journal", params={"after_id": wake["id"]}).json()["rows"] == [
        r for r in rows if r["id"] > wake["id"]]
    dec = w.pc.get(f"/api/agent/sessions/{SID}/decisions").json()
    assert dec["ok"] and dec["rows"]  # the runner's UNRECORDED row: this wake did not call record_decision


# -- tool endpoints (MCP) -----------------------------------------------------------------------------------------------
def test_a_tool_call_without_a_live_sessions_token_is_403_and_prepares_nothing(store):
    w = World(store)
    r = w.agent.post("/api/agent/call/place_entry", json=entry())
    assert r.status_code == 403 and r.json()["code"] == "no_agent_session"  # AE6
    out = w.pc.post("/api/agent/launch", json=launch_body(kind="external", model=None, spend_cap=None)).json()
    assert out["ok"]
    for headers in ({}, {"X-Agent-Token": "wrong"}):
        r = w.agent.post("/api/agent/call/place_entry", json=entry(), headers=headers)
        assert r.status_code == 403, headers
    assert w.agent.post("/api/agent/call/set_alarms", json={"alarms": []}).status_code == 403  # session tools too
    assert w.nothing_prepared()
    # reads need the token too: the agent listener is reachable by anything that reaches its port
    assert w.agent.post("/api/agent/call/observe", json={}).json()["code"] == "no_agent_session"
    obs = w.agent.post("/api/agent/call/observe", json={}, headers={"X-Agent-Token": out["token"]}).json()
    assert obs["ok"] and obs["markets"]
    # the routes check the listener themselves too: the PC's actor is not the agent listener's
    for path in ("/api/agent/call/observe", "/api/agent/tools"):
        r = w.pc.request("POST" if "call" in path else "GET", path, json={}, headers={"X-Agent-Token": out["token"]})
        assert r.status_code == 403 and r.json()["code"] == "agent_port", path
    listed = w.agent.get("/api/agent/tools").json()["tools"]
    kinds = {t["name"]: t["kind"] for t in listed}
    assert kinds["observe"] == "read" and kinds["place_entry"] == "write" and kinds["end_session"] == "session"
    assert [t["name"] for t in listed] == [t["name"] for t in w.r.tools.tools()]
    assert w.agent.get("/api/agent/events/wait", params={"timeout_s": 5}).json()["code"] == "not_available"
    assert w.agent.get("/api/agent/events/wait", params={"timeout_s": 60}).status_code == 422


def test_a_forged_actor_header_or_field_without_a_valid_token_is_403_and_prepares_nothing(store):
    w = World(store)
    assert w.pc.post("/api/agent/launch", json=launch_body(kind="external", model=None, spend_cap=None)).json()["ok"]
    forged = {"X-Actor": "agent:" + SID[:8], "X-Agent-Session": SID, "X-Agent-Token": ""}
    for body in (entry(), {**entry(), "actor": "agent:" + SID[:8]}, {**entry(), "session_id": SID}):
        r = w.agent.post("/api/agent/call/place_entry", json=body, headers=forged)
        assert r.status_code == 403
    assert w.nothing_prepared()


def test_tool_endpoints_are_on_the_agent_listener_only_and_never_reach_human_routes(store):
    w = World(store, access=True)
    phone = w.phone()
    for c in (phone, w.pc):  # the PC and a phone: the tool API is not on their listener
        assert c.get("/api/agent/tools").json()["code"] == "agent_port"
        assert c.post("/api/agent/call/observe", json={}).status_code == 403
    out = w.pc.post("/api/agent/launch", json=launch_body(kind="external", model=None, spend_cap=None)).json()
    h = {"X-Agent-Token": out["token"]}
    assert w.agent.get("/api/agent/tools").json()["ok"]
    assert w.agent.post("/api/agent/call/observe", json={}, headers=h).json()["ok"]
    # the agent listener never reaches a person's routes, with or without its token
    for path in ("/api/trading/resume", "/api/trading/heartbeat", "/api/agent/proposals/approve",
                 f"/api/agent/sessions/{out['id']}/resume", "/api/agent/launch", "/api/access/pin"):
        assert w.agent.post(path, json={}, headers=h).status_code == 404, path


def test_an_external_session_token_write_reaches_the_shadow_exchange_once(store):
    w = World(store, llm=None)
    out = w.pc.post("/api/agent/launch", json=launch_body(kind="external", model=None, spend_cap=None)).json()
    h = {"X-Agent-Token": out["token"]}
    first = w.agent.post("/api/agent/call/place_entry", json=entry(), headers=h).json()
    assert first["ok"], first
    again = w.agent.post("/api/agent/call/place_entry", json=entry(), headers=h).json()
    assert again == {**first, "repeat": True} and len(w.r.buys()) == 1  # the same request_id sends nothing again
    actors = {a["actor"] for a in store.select("trading_audit") if a["event"] in ("approved", "sent")}
    assert actors == {"agent:" + SID[:8]}
    assert w.pc.post(f"/api/agent/sessions/{out['id']}/stop").json()["session"]["state"] == "ended"
    r = w.agent.post("/api/agent/call/place_entry", json=entry(request_id="r2"), headers=h)
    assert r.status_code == 403 and len(w.r.buys()) == 1


# -- prompts ------------------------------------------------------------------------------------------------------------
def test_prompt_save_makes_a_version_reset_restores_the_default_and_a_running_session_keeps_its_versions(store):
    w = World(store)
    p = w.pc.get("/api/agent/prompts").json()
    assert p["system"]["version"] == 1 and p["system"]["text"] == DEFAULT and p["system"]["is_default"]
    assert p["user"] == {"layer": "user", "version": 0, "text": "", "created_ts": None, "is_default": True}
    assert w.pc.post("/api/agent/launch", json=launch_body()).json()["ok"]
    out = w.pc.post("/api/agent/prompts/save", json={"layer": "system", "text": "Trade less."}).json()
    assert out["changed"] and out["prompt"]["version"] == 2 and "session_versions" not in out
    assert w.pc.get(f"/api/agent/sessions/{SID}").json()["session"]["prompt_versions"] == {"system": 1, "user": 0}
    w.pc.post("/api/agent/prompts/save", json={"layer": "user", "text": "Be brief."})
    same = w.pc.post("/api/agent/prompts/save", json={"layer": "user", "text": "Be brief."}).json()
    assert same["changed"] is False and same["prompt"]["version"] == 1
    # the running session's wakes still read what it launched with
    assert w.runner._prompts() == (DEFAULT, "", {"system": 1, "user": 0})
    out = w.pc.post("/api/agent/prompts/reset", json={"layer": "system"}).json()
    assert out["prompt"]["version"] == 3 and out["prompt"]["text"] == DEFAULT and out["prompt"]["is_default"]
    out = w.pc.post("/api/agent/prompts/restore", json={"layer": "system", "version": 2}).json()
    assert out["prompt"]["version"] == 4 and out["prompt"]["text"] == "Trade less."
    assert [h["version"] for h in out["history"]["system"]] == [4, 3, 2, 1]
    assert [h["version"] for h in out["history"]["user"]] == [1, 0]
    assert w.pc.post("/api/agent/prompts/restore", json={"layer": "system", "version": 9}).json()["code"] == \
        "unknown_version"
    assert w.pc.post("/api/agent/prompts/save", json={"layer": "system", "text": " "}).json()["code"] == "empty"
    assert w.pc.post("/api/agent/prompts/save", json={"layer": "boss", "text": "x"}).status_code == 422
    assert w.pc.get("/api/agent/prompts/version", params={"layer": "system", "version": 2}).json()["text"] == \
        "Trade less."
    w.pc.post(f"/api/agent/sessions/{SID}/stop")
    w.r.ag._id = lambda: "sess0002abcd"
    nxt = w.pc.post("/api/agent/launch", json=launch_body()).json()
    assert nxt["session"]["prompt_versions"] == {"system": 4, "user": 1}  # the next session uses the edits
    assert w.runner._prompts() == ("Trade less.", "Be brief.", {"system": 4, "user": 1})


def test_the_old_session_and_auto_routes_stay_gone(store):
    w = World(store)
    for path in ("/api/trading/session", "/api/trading/auto"):
        assert w.pc.get(path).status_code == 404, path
        assert w.pc.post(path + "/start", json={}).status_code == 404, path


# -- the AG-UI stream ---------------------------------------------------------------------------------------------------
async def sse(app, during, *, until: str, quiet_s: float = 0.05, sid: str = SID) -> list[dict]:
    """Open GET /api/agent/sessions/{sid}/stream on the ASGI app (it never ends); `await during()` once the first
    frame arrived; return the events up to the first of type `until` (plus any within `quiet_s`)."""
    path = f"/api/agent/sessions/{sid}/stream"
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8000")], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}
    out, buf = [], [""]
    first, done, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def receive():
        await hold.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] != "http.response.body":
            return
        buf[0] += msg.get("body", b"").decode()
        while "\n\n" in buf[0]:
            frame, buf[0] = buf[0].split("\n\n", 1)
            if frame.startswith(":"):
                continue
            ev = json.loads(frame.removeprefix("data: "))
            out.append(ev)
            first.set()
            if ev["type"] == until:
                done.set()

    task = asyncio.ensure_future(app(scope, receive, send))
    await asyncio.wait_for(first.wait(), 3.0)
    await during()
    await asyncio.wait_for(done.wait(), 3.0)
    await asyncio.sleep(quiet_s)
    hold.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return out


def test_the_stream_sends_the_state_snapshot_then_every_event_of_a_scripted_wake_in_order(store):
    turn1 = turn(thinking("abc"), text("Looking."), tool_use("t1", "observe", {}),
                 deltas=(("thinking", "a"), ("thinking", "b"), ("thinking", "c"), ("text", "Looking."),
                         ("tool_use", "observe")))
    turn2 = turn(text("Nothing to do."), deltas=(("text", "Nothing "), ("text", "to do.")))
    w = World(store, turn1, turn2)
    assert w.r.ag.launch({k: v for k, v in launch_body().items() if k != "confirm"}, NOW,
                         prompt_versions={"system": 1, "user": 0})["ok"]
    assert w.runner.attach(SID)

    async def wake():
        w.runner.events.push(NOW, "human_note", value={"notes": [{"id": 1, "text": "go"}]})
        assert await w.runner.tick()

    events = asyncio.run(sse(w.app, wake, until="RUN_FINISHED"))
    types = [e["type"] for e in events]
    snap = events[0]
    assert snap["type"] == "STATE_SNAPSHOT" and snap["snapshot"]["session"]["id"] == SID
    assert snap["snapshot"]["prompts"] == {"system": 1, "user": 0}
    assert types[1:] == [
        "RUN_STARTED",
        "REASONING_START", "REASONING_MESSAGE_START", "REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_END", "REASONING_END",
        "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
        "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT",
        "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
        "CUSTOM", "RUN_FINISHED", "STATE_DELTA"]
    # three rapid deltas: three frames, none merged
    assert [e["delta"] for e in events if e["type"] == "REASONING_MESSAGE_CONTENT"] == ["a", "b", "c"]
    run = events[1]
    assert run["threadId"] == SID and run["runId"] == SID + ":1"
    assert run["trigger"] == ["human_note"] and run["server_ms"] == NOW
    call = events[types.index("TOOL_CALL_START")]
    assert call["toolCallName"] == "observe" and call["parentMessageId"] == events[types.index("TEXT_MESSAGE_START")]["messageId"]
    res = events[types.index("TOOL_CALL_RESULT")]
    assert res["toolCallId"] == call["toolCallId"] and res["role"] == "tool" and json.loads(res["content"])["ok"]
    assert events[types.index("REASONING_MESSAGE_START")]["role"] == "reasoning"
    assert events[types.index("CUSTOM")]["name"] == "decision"
    fin = events[types.index("RUN_FINISHED")]
    assert fin["runId"] == run["runId"] and fin["result"]["decision"] == "Nothing to do."
    assert fin["result"]["trigger"] == ["human_note"] and fin["result"]["status"] == "done"
    delta = events[-1]["delta"]
    assert any(op["path"].startswith("/session/usage") for op in delta)


def test_a_stopped_session_is_a_custom_event_and_a_state_delta(store):
    w = World(store)
    assert w.r.ag.launch({k: v for k, v in launch_body().items() if k != "confirm"}, NOW)["ok"]

    async def stop():
        w.r.ag.stop(w.r.ag.latest()["id"], NOW)

    events = asyncio.run(sse(w.app, stop, until="STATE_DELTA"))
    assert [e["type"] for e in events] == ["STATE_SNAPSHOT", "CUSTOM", "STATE_DELTA"]
    assert events[1]["name"] == "session_state" and events[1]["value"]["state"] == "ended"
    assert {"op": "replace", "path": "/session/state", "value": "ended"} in events[2]["delta"]
    assert "token_hash" not in json.dumps(events)


def test_a_slow_client_keeps_the_last_200_and_is_told_how_many_it_lost():
    async def go():
        hub = FeedHub(keep=200, trimmed=lambda n: {"type": "CUSTOM", "name": "feed_trimmed", "value": {"dropped": n}})
        frames = hub.stream(lambda: [{"type": "STATE_SNAPSHOT", "snapshot": {}}])
        first = await frames.__anext__()
        for i in range(250):
            hub.publish({"type": "CUSTOM", "name": "n", "value": i})
        got = [await asyncio.wait_for(frames.__anext__(), 1.0) for _ in range(201)]
        await frames.aclose()
        await hub.close()
        return first, got

    first, got = asyncio.run(go())
    assert json.loads(first[6:])["type"] == "STATE_SNAPSHOT"
    events = [json.loads(f[6:]) for f in got]
    assert events[0] == {"type": "CUSTOM", "name": "feed_trimmed", "value": {"dropped": 50}}
    assert [e["value"] for e in events[1:]] == list(range(50, 250))


def test_json_patch_is_rfc6902_shaped():
    old = {"a": 1, "b": {"c": 2, "d": [1]}, "x/y": 0}
    new = {"a": 1, "b": {"c": 3, "d": [1, 2]}, "e": None}
    assert json_patch(old, new) == [
        {"op": "remove", "path": "/x~1y"}, {"op": "replace", "path": "/b/c", "value": 3},
        {"op": "replace", "path": "/b/d", "value": [1, 2]}, {"op": "add", "path": "/e", "value": None}]
    assert json_patch(old, old) == []


def test_launch_takes_minutes_and_refuses_both_or_neither(store):
    w = World(store)
    both = w.pc.post("/api/agent/launch", json=launch_body(minutes=30)).json()
    neither = w.pc.post("/api/agent/launch", json=launch_body(ends_ts=None)).json()
    assert both["code"] == neither["code"] == "invalid" and w.r.ag.latest() is None
    out = w.pc.post("/api/agent/launch", json=launch_body(ends_ts=None, minutes=45)).json()
    assert out["ok"] and out["session"]["ends_ts"] == NOW + 45 * 60_000


def test_a_failed_wake_is_a_run_error_with_its_ids_and_the_pause_carries_its_code(store):
    w = World(store, LlmUnavailable("Overloaded (after retries)."))
    assert w.r.ag.launch({k: v for k, v in launch_body().items() if k != "confirm"}, NOW)["ok"]
    assert w.runner.attach(SID)

    async def wake():
        w.runner.events.push(NOW, "human_note", value={"notes": [{"id": 1, "text": "go"}]})
        await w.runner.tick()

    events = asyncio.run(sse(w.app, wake, until="RUN_ERROR"))
    err = next(e for e in events if e["type"] == "RUN_ERROR")
    assert err["threadId"] == SID and err["runId"] == SID + ":1" and err["code"] == "unavailable"
    state = next(e for e in events if e["type"] == "CUSTOM" and e["name"] == "session_state")
    assert state["value"]["state"] == "paused" and state["value"]["pause_code"] == "model_unavailable"
    assert w.pc.get(f"/api/agent/sessions/{SID}").json()["session"]["pause_code"] == "model_unavailable"


def test_a_note_is_delivered_when_the_wake_starts_not_when_it_ends(store):
    seen = {}

    def first(req):  # inside the wake: the note that woke it is already marked delivered
        note = next(j for j in store.select("agent_journal") if j["kind"] == "note")
        seen["state"] = note["data"]["state"]
        w.runner.clock.t = NOW + 5_000  # the wake takes 5 s
        return turn(text("Noted."))
    w = World(store, first)
    assert w.pc.post("/api/agent/launch", json=launch_body()).json()["ok"]
    assert w.pc.post(f"/api/agent/sessions/{SID}/note", json={"text": "Only trade UP today."}).json()["ok"]
    asyncio.run(w.runner.tick())
    assert seen["state"] == "delivered"
    note = next(r for r in w.pc.get(f"/api/agent/sessions/{SID}/journal").json()["rows"] if r["kind"] == "note")
    assert note["data"]["delivered_ts"] == NOW


def test_a_quiet_stream_sends_a_heartbeat_event_with_the_server_clock_besides_the_ping():
    async def go():
        hub = FeedHub(heartbeat_s=0.01)
        feed = AgUiFeed(hub, lambda: {"session": None}, now_fn=lambda: NOW)
        frames = hub.stream(feed.first, idle=feed.refresh, heartbeat=feed.heartbeat)
        got = [await asyncio.wait_for(frames.__anext__(), 1.0) for _ in range(5)]
        await frames.aclose()
        await hub.close()
        return got

    got = asyncio.run(go())
    assert json.loads(got[0][6:])["type"] == "STATE_SNAPSHOT"
    assert got[1] == got[3] == ": ping\n\n"
    for f in (got[2], got[4]):
        assert json.loads(f[6:]) == {"type": "CUSTOM", "name": "heartbeat", "value": {"server_ms": NOW}}


def test_the_single_session_wrappers_are_gone_every_call_names_its_session(store):
    w = World(store)
    assert w.pc.post("/api/agent/launch", json=launch_body()).json()["ok"]
    for path in ("/api/agent/stop", "/api/agent/resume", "/api/agent/note"):
        assert w.pc.post(path, json={"text": "x"}).status_code in (404, 405), path
    for path in ("/api/agent/journal", "/api/agent/decisions", "/api/agent/stream"):
        assert w.pc.get(path).status_code == 404, path
    assert w.r.ag.get(SID)["state"] == "running"  # nothing above touched it


def test_retire_archives_an_idle_agent_on_the_pc_only(store):
    w = World(store, access=True)
    assert w.pc.post("/api/agent/launch", json=launch_body()).json()["ok"]
    aid = w.r.ag.get(SID)["agent_id"]
    assert w.pc.post(f"/api/agent/agents/{aid}/archive").json()["code"] == "agent_busy"
    w.pc.post(f"/api/agent/sessions/{SID}/stop")
    assert w.phone().post(f"/api/agent/agents/{aid}/archive").status_code == 403
    out = w.pc.post(f"/api/agent/agents/{aid}/archive").json()
    assert out["ok"] and out["agent"]["archived"] is True
    assert w.pc.post(f"/api/agent/agents/{aid}/archive").json()["code"] == "agent_archived"
    assert w.pc.post("/api/agent/agents/nope/archive").status_code == 404
    assert [a["id"] for a in w.pc.get("/api/agent/agents").json()["agents"]] == []
    again = w.pc.post("/api/agent/launch", json={**launch_body(), "agent_id": aid}).json()
    assert again["code"] == "agent_archived"


def test_a_fleet_row_says_what_the_agent_holds_and_which_rule_opened_it(store):
    w = World(store)
    assert w.pc.post("/api/agent/launch", json=launch_body(rules=["CUSHION100"])).json()["ok"]
    store.insert("trading_brackets", [{
        "id": "held1", "env": "shadow", "strategy": "you", "session_id": SID, "kind": "entry", "ticker": "T",
        "side": "UP", "window_end": NOW + 600_000, "stake": 1.0, "contracts": 1.16, "limit_price": 0.9,
        "take_profit": 0.99, "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h", "state": "open", "filled": 1.16,
        "held": 1.16, "tp_filled": 0.0, "entry_avg": 0.86, "entry_cost": 1.0024, "proceeds": 0.0,
        "created_ts": NOW, "updated_ts": NOW}])
    w.r.ag.journal("rule", {"rule": "CUSHION100", "market": "kalshi_15m", "ok": True, "bracket_id": "held1"}, NOW,
                   session_id=SID)
    row = w.pc.get("/api/agent/sessions").json()["sessions"][0]
    assert row["chip"] == "Holding" and row["line"].startswith("Holding Up $1.00 · CUSHION100")
    assert row["holding"] == [{"bracket_id": "held1", "side": "UP", "cost": 1.0024, "rule": "CUSHION100"}]
