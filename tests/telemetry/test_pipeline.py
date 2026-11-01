"""The OpenTelemetry pipeline end to end, in memory (tests/telemetry/conftest.py): what each instrumentation exports,
that the sampler keeps the order path and drops polls, streams, health and Telegram, that logs carry trace ids, that the
agent wake is a GenAI span with tool children and no content, that the app's gauges read the Runtime, and above all that
no token, key, password, cookie, signature or query secret reaches an exporter (`pipe` checks after every test)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from btc_signal_engine.adapters.agent_sdk import harness as harness_mod
from btc_signal_engine.application.agent_fleet import WakeScheduler
from btc_signal_engine.application.ports import LlmUnavailable, WakeResult
from btc_signal_engine.application.trading import GateResult
from btc_signal_engine.bootstrap import telemetry
from tests.telemetry.conftest import (ANTHROPIC_KEY, BEARER, COOKIE, DB_PASSWORD, QUERY_SECRET, SIGNATURE, TG_TOKEN)

tracer = trace.get_tracer("tests.telemetry")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", COOKIE)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


SECRET_HEADERS = {"Authorization": BEARER, "Cookie": COOKIE, "X-Api-Key": ANTHROPIC_KEY,
                  "KALSHI-ACCESS-SIGNATURE": SIGNATURE}


def _client_spans(pipe):
    return [s for s in pipe.spans() if s.kind == SpanKind.CLIENT and s.name.startswith("GET")]


# -- httpx ------------------------------------------------------------------------------------------------------------
def test_httpx_spans_never_carry_the_bot_token_headers_or_query_secrets(pipe, server):
    url = f"{server}/bot{TG_TOKEN}/getUpdates?offset=1&token={QUERY_SECRET}"
    with tracer.start_as_current_span("poll"):  # an internal root: its children are kept
        with httpx.Client() as c:
            assert c.get(url, headers=SECRET_HEADERS).status_code == 200

        async def go():
            async with httpx.AsyncClient() as ac:
                return await ac.get(url, headers=SECRET_HEADERS)
        assert asyncio.run(go()).status_code == 200
    spans = _client_spans(pipe)
    assert len(spans) == 2
    for s in spans:
        full = s.attributes.get("url.full") or s.attributes.get("http.url")
        assert full is not None and "/bot<redacted>/getUpdates" in full and "offset=<redacted>" in full
        assert s.attributes.get("http.request.header.authorization") == "<redacted>"
        assert s.attributes.get("http.response.header.set_cookie") == "<redacted>"


def test_a_failed_request_records_the_error_without_the_token(pipe):
    with tracer.start_as_current_span("poll"):
        with httpx.Client(timeout=1) as c, pytest.raises(httpx.HTTPError):
            c.get(f"http://127.0.0.1:9/bot{TG_TOKEN}/getMe?key={QUERY_SECRET}", headers=SECRET_HEADERS)
    (s,) = _client_spans(pipe)
    assert s.status.status_code == StatusCode.ERROR


def test_background_client_calls_are_sampled_and_telegram_is_never_traced(pipe, server):
    with httpx.Client() as c:
        c.get(f"{server}/margin/positions")  # a root client span: background ratio (0 in these tests)
    assert _client_spans(pipe) == []
    sampler = telemetry._sampler_class()(1.0)
    drop = sampler.should_sample(None, 1, "GET", SpanKind.CLIENT, {"server.address": "api.telegram.org"})
    keep = sampler.should_sample(None, 1, "GET", SpanKind.CLIENT, {"server.address": "api.kalshi.com",
                                                                   "http.request.method": "GET"})
    assert not drop.decision.is_sampled() and keep.decision.is_sampled()
    db = sampler.should_sample(None, 1, "INSERT settleview", SpanKind.CLIENT, {})  # db.* come after the start
    assert not db.decision.is_sampled()  # the writer flushes are metrics, not traces of their own


# -- FastAPI ----------------------------------------------------------------------------------------------------------
@pytest.fixture
def api(otel):
    app = FastAPI()

    @app.get("/api/play/now")
    def now():
        return {"ok": True}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/trading/prepare")
    def prepare(body: dict):
        with httpx.Client():  # nested client work joins the request's trace
            pass
        return {"ok": True}

    @app.post("/api/trading/heartbeat")
    def heartbeat():
        return {"ok": True}
    otel.tel.instrument_app(app)  # as Runtime.run does
    return TestClient(app)


def _server_spans(pipe):
    return [s for s in pipe.spans() if s.kind == SpanKind.SERVER]


def test_order_path_requests_are_traced_without_headers_or_query_values(pipe, api):
    r = api.post(f"/api/trading/prepare?pin=1234&token={QUERY_SECRET}", json={"stake": 1},
                 headers=SECRET_HEADERS)
    assert r.status_code == 200
    (s,) = _server_spans(pipe)
    assert s.name == "POST /api/trading/prepare"
    assert s.attributes.get("http.route") == "/api/trading/prepare"
    assert s.attributes.get("url.query") == "pin=<redacted>&token=<redacted>"
    assert s.attributes.get("http.request.header.authorization") == "<redacted>"
    assert s.attributes.get("http.request.header.cookie") == "<redacted>"
    assert not [x for x in pipe.spans() if x.name in ("http send", "http receive") or x.name.endswith((" send",
                                                                                                      " receive"))]


def test_polls_heartbeats_and_health_are_not_traced_but_counted(pipe, api):
    for _ in range(3):
        api.get("/api/play/now")
        api.post("/api/trading/heartbeat")
        api.get("/api/health")
    assert _server_spans(pipe) == []
    points = pipe.collect()
    routes = {p.attributes.get("http.route") for n, p in points if n.startswith("http.server")}
    assert "/api/play/now" in routes and "/api/trading/heartbeat" in routes
    assert "/api/health" not in routes  # excluded entirely (Docker's healthcheck every 30 s)


def test_sse_streams_are_never_traced():
    sampler = telemetry._sampler_class()(1.0)
    r = sampler.should_sample(None, 1, "GET /api/perps/stream", SpanKind.SERVER, {"http.route": "/api/perps/stream"})
    assert not r.decision.is_sampled()
    r = sampler.should_sample(None, 1, "GET /api/agent/sessions/{sid}/stream", SpanKind.SERVER,
                              {"http.method": "GET", "http.target": "/api/agent/sessions/abc/stream"})
    assert not r.decision.is_sampled()


# -- logs -------------------------------------------------------------------------------------------------------------
def test_logs_are_exported_scrubbed_and_carry_the_trace_id(pipe):
    lg = logging.getLogger("btc_signal_engine.test_otel")
    lg.setLevel(logging.INFO)
    with tracer.start_as_current_span("work") as span:
        lg.warning("calling https://api.telegram.org/bot%s/getUpdates with %s", TG_TOKEN, BEARER)
        lg.info("db password=%s key %s", DB_PASSWORD, ANTHROPIC_KEY)
        try:
            raise ConnectionError(f"https://api.telegram.org/bot{TG_TOKEN}/getMe failed")
        except ConnectionError:
            lg.exception("telegram failed")
    recs = [r for r in pipe.logs() if "test_otel" in str(dict(r.attributes or {})) or r.trace_id]
    mine = [r for r in recs if r.trace_id == span.get_span_context().trace_id]
    assert len(mine) == 3
    assert "bot<redacted>" in mine[0].body
    assert any("exception.stacktrace" in (r.attributes or {}) for r in mine)


# -- SQLAlchemy 2.1 ---------------------------------------------------------------------------------------------------
def test_sqlalchemy_statements_are_traced_without_parameter_values(pipe, tmp_path):
    from sqlalchemy import text

    from btc_signal_engine.adapters.sqlite.db import write_engine
    eng = write_engine(tmp_path / "q.db")  # every engine the adapters make is traced
    with tracer.start_as_current_span("query"):
        with eng.connect() as conn:
            conn.execute(text("select :p as x"), {"p": DB_PASSWORD}).fetchall()
    stmts = [s.attributes.get("db.query.text") or s.attributes.get("db.statement") for s in pipe.spans()]
    stmts = [x for x in stmts if x]
    assert stmts == ["select ? as x"]  # the statement, never the bound value
    eng.dispose()


# -- the writer -------------------------------------------------------------------------------------------------------
def test_writer_batches_are_measured(pipe, tmp_path):
    from btc_signal_engine.adapters.sqlite.db import migrate, read_engine, write_engine
    from btc_signal_engine.adapters.sqlite.store import SqliteStore
    from btc_signal_engine.adapters.sqlite.writer import BufferedStore
    db = tmp_path / "w.db"
    migrate(db)
    we = write_engine(db)
    w = BufferedStore(SqliteStore(we, read_engine(db)), we)
    w.insert("trading_audit", [{"ts": 1, "event": "x", "env": "shadow"}])
    assert asyncio.run(w.flush()) == 1
    w.close()
    names = {n for n, _ in pipe.collect()}
    assert {"settleview.writer.flush.duration", "settleview.writer.batch.size"} <= names


# -- the agent wake (GenAI) -------------------------------------------------------------------------------------------
SPECS = [{"name": "place_entry", "description": "Buy.", "kind": "write",
          "input_schema": {"type": "object", "properties": {}, "additionalProperties": True}},
         {"name": "observe", "description": "Read.", "kind": "read",
          "input_schema": {"type": "object", "properties": {}, "additionalProperties": True}}]
CONTENT = ("SV-SYSTEM-PROMPT-MARKER", "SV-WAKE-PROMPT-MARKER", "SV-ARG-MARKER", "SV-RESULT-MARKER",
           "SV-MODEL-TEXT-MARKER")


def _fake_run(tools, fail: Exception | None = None):
    async def run(self, opts, prompt):
        assert prompt == "SV-WAKE-PROMPT-MARKER"
        self.verified = True
        for name in tools:
            await self._handler(name)({"note": "SV-ARG-MARKER"})
        if fail is not None:
            raise fail
        return WakeResult(status="done", stop_reason="end_turn", text="SV-MODEL-TEXT-MARKER", thinking=(),
                          tool_calls=tuple({"id": f"t{i}", "name": n, "input": {"note": "SV-ARG-MARKER"}}
                                           for i, n in enumerate(tools)),
                          usage={"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 1000,
                                 "cache_creation_input_tokens": 20}, cost_usd=0.0123, turns=2,
                          detail={"models": ["claude-haiku-5-5"], "cli_version": "2.1.301",
                                  "terminal_reason": "completed", "subtype": "success"})
    return run


async def _execute(name, args):
    assert args == {"note": "SV-ARG-MARKER"}
    return json.dumps({"ok": name == "observe", "echo": "SV-RESULT-MARKER"}), name != "observe"


def test_an_agent_wake_is_a_genai_span_with_tool_children_and_no_content(pipe, tmp_path, monkeypatch):
    monkeypatch.setattr(harness_mod._Wake, "run", _fake_run(["observe", "place_entry"]))
    h = harness_mod.SdkHarness(ANTHROPIC_KEY, tmp_path, environ={})
    out = asyncio.run(h.run_wake("SV-SYSTEM-PROMPT-MARKER", "SV-WAKE-PROMPT-MARKER", SPECS, _execute,
                                 session_id="sess-1", model="claude-haiku-5-5", effort="low", max_turns=4,
                                 max_tokens=8000, max_budget_usd=1.0))
    assert out.status == "done"
    (wake,) = [s for s in pipe.spans() if s.name == "invoke_agent settleview"]
    a = wake.attributes
    assert a["gen_ai.operation.name"] == "invoke_agent" and a["gen_ai.system"] == "anthropic"
    assert a["gen_ai.provider.name"] == "anthropic" and a["gen_ai.request.model"] == "claude-haiku-5-5"
    assert a["gen_ai.usage.input_tokens"] == 1120 and a["gen_ai.usage.output_tokens"] == 40
    assert a["gen_ai.usage.cache_read.input_tokens"] == 1000 and a["gen_ai.usage.cost"] == pytest.approx(0.0123)
    assert a["gen_ai.response.finish_reasons"] == ("end_turn",) and a["gen_ai.conversation.id"] == "sess-1"
    tools = {s.attributes["gen_ai.tool.name"]: s for s in pipe.spans() if s.name.startswith("execute_tool")}
    assert set(tools) == {"observe", "place_entry"}
    for s in tools.values():
        assert s.parent.span_id == wake.context.span_id and s.context.trace_id == wake.context.trace_id
    assert tools["observe"].attributes["settleview.tool.result"] == "ok"
    assert tools["place_entry"].status.status_code == StatusCode.ERROR
    assert tools["place_entry"].attributes["settleview.tool.result"] == "error"
    points = pipe.collect()
    usage = {p.attributes["gen_ai.token.type"]: p.sum for n, p in points if n == "gen_ai.client.token.usage"}
    assert usage == {"input": 1120, "output": 40}
    calls = {(p.attributes["gen_ai.tool.name"], p.attributes["settleview.tool.result"]): p.value
             for n, p in points if n == "settleview.agent.tool_calls"}
    assert calls[("observe", "ok")] >= 1 and calls[("place_entry", "error")] >= 1
    pipe.assert_clean(CONTENT)  # no prompt, argument, result or model text left the process


def test_a_failed_wake_records_its_error_type_only(pipe, tmp_path, monkeypatch):
    monkeypatch.setattr(harness_mod._Wake, "run", _fake_run([], fail=LlmUnavailable("SV-MODEL-TEXT-MARKER boom")))
    h = harness_mod.SdkHarness(ANTHROPIC_KEY, tmp_path, environ={})
    with pytest.raises(LlmUnavailable):
        asyncio.run(h.run_wake("SV-SYSTEM-PROMPT-MARKER", "SV-WAKE-PROMPT-MARKER", SPECS, _execute,
                               session_id="s2", model="claude-haiku-5-5", effort="low", max_turns=4,
                               max_tokens=8000, max_budget_usd=1.0))
    (wake,) = [s for s in pipe.spans() if s.name == "invoke_agent settleview"]
    assert wake.status.status_code == StatusCode.ERROR and wake.attributes["error.type"] == "LlmUnavailable"
    assert not wake.events  # no exception event: its message could quote the model
    pipe.assert_clean(CONTENT)


# -- the app's gauges and hooks ---------------------------------------------------------------------------------------
class Gate:
    def __init__(self):
        self.results = [GateResult(True), GateResult(False, "price_moved", "moved")]

    def check(self, ticket, now, quote, **kw):
        return self.results.pop(0)


class _Coverage:
    def __init__(self) -> None:
        self.reads: list[int] = []

    def rolling_coverage(self, now):
        self.reads.append(now)
        return {"windows": 288, "recorded": 270, "settled": 280, "longest_gap_min": 45, "day_start": 0}


class _Sessions:
    def live(self):
        return [{"id": "s1", "name": "Scout", "limits": {"spend_cap": 2.0}, "usage": {"api_usd": 1.7}},
                {"id": "s2", "name": "NoCap", "limits": {}, "usage": {"api_usd": 9.0}}]

    def global_limits(self):
        return {"api_day": 10.0, "max_loss_day": None}

    def global_usage(self, now):
        return {"api_day": 4.0, "loss_day": 0.0}


def test_runtime_gauges_and_hooks(pipe, tmp_path):
    """The app's gauges and hooks, with the SLIs deploy/openobserve/alerts/ read (P3 item 2): the Coinbase feed, the
    perps book, agent spend vs budget, the recorders' 24 h coverage (read once per COVERAGE_TTL_S) and uptime. One
    test: the instruments live in the session's one meter provider, so a second bind_runtime would not rebind them."""
    from btc_signal_engine.adapters.sqlite.db import read_engine, write_engine
    from btc_signal_engine.bootstrap.stall import StallWatch
    db = tmp_path / "g.db"
    we, re_ = write_engine(db), read_engine(db) if db.exists() else None
    svc = SimpleNamespace(audit_observers=[], gate=Gate())
    sched = WakeScheduler(2)
    loop = asyncio.new_event_loop()
    watch = StallWatch(loop)
    rt = SimpleNamespace(
        stall_watch=watch, writer=SimpleNamespace(pending=3, failed_batches=1),
        store=SimpleNamespace(_write=we, _read=re_ or we), trading_services={"you_poly": svc},
        agent_fleet=SimpleNamespace(scheduler=sched, now=lambda: 10_000),
        feed=SimpleNamespace(snapshot=lambda: {"status": "stale", "lag_ms": 950}),
        recorder_watch=(cov := _Coverage()), agent_sessions=_Sessions(),
        health_parts=lambda: {
            "perps": {"enabled": True, "stream": {"env": "prod", "connected": True, "book_live": False}},
            "stalls_last_hour": 2, "loops": {"writer": {"last_beat_age_s": 0.3, "restarts": 1, "critical": True}},
            "streams": {"trading_pm_prod": {"venue": "polymarket", "env": "prod", "connected": True, "stale": True,
                                            "last_msg_age_ms": 4200}},
            "recorders": {"available": True, "recorders": {"pm-quotes": {"age_s": 2, "state": "ok"}}}})
    pipe.tel.bind_runtime(rt)
    watch.on_lag(12.5)
    for ev in ("stream_down", "stream_up", "stream_down", "fill"):
        for observe in svc.audit_observers:
            observe({"event": ev, "env": "pm_prod"})
    assert svc.gate.check({"kind": "entry"}, 0, None).ok
    assert not svc.gate.check({"kind": "entry"}, 0, None, token_error=None, exit_order=False).ok
    sched.offer("a", "urgent", 1_000)
    sched.offer("b", "normal", 2_000)
    sched.offer("c", "normal", 3_000)
    sched.take(5_000)
    points = pipe.collect()
    by = {}
    for n, p in points:
        by.setdefault(n, []).append(p)
    assert by["settleview.writer.pending"][0].value == 3
    assert by["settleview.stream.stale"][0].value == 1
    assert by["settleview.stream.last_message_age"][0].attributes["stream"] == "trading_pm_prod"
    assert by["settleview.loop.restarts"][0].value == 1 and by["settleview.event_loop.stalls"][0].value == 2
    assert by["settleview.recorder.age"][0].value == 2
    flaps = {p.attributes["direction"]: p.value for p in by["settleview.stream.flaps"]}
    assert flaps == {"down": 2, "up": 1}
    gate = {(p.attributes["stage"], p.attributes["result"], p.attributes["code"]): p.value
            for p in by["settleview.order_gate.decisions"]}
    assert gate == {("prepare", "ok", "ok"): 1, ("approve", "refused", "price_moved"): 1}
    assert by["settleview.agent.wake_queue.depth"][0].value == 1
    assert by["settleview.agent.wakes.running"][0].value == 2
    assert sum(p.count for p in by["settleview.agent.wake_queue.wait"]) == 2
    assert by["settleview.event_loop.lag"][0].count >= 1
    assert {p.attributes["pool"] for p in by["settleview.db.pool.checked_out"]} >= {"write"}
    assert by["settleview.feed.stale"][0].value == 1 and by["settleview.feed.stale"][0].attributes["feed"] == "coinbase"
    assert by["settleview.feed.lag"][0].value == 950
    assert by["settleview.perps.book_live"][0].value == 0 and by["settleview.perps.connected"][0].value == 1
    assert round(by["settleview.recorder.coverage"][0].value, 4) == round(270 / 288, 4)
    assert by["settleview.recorder.longest_gap"][0].value == 45
    spend = {(p.attributes["budget"], p.attributes["session"]): round(p.value, 3)
             for p in by["settleview.agent.spend_ratio"]}
    assert spend == {("session", "Scout"): 0.85, ("day", "all"): 0.4}
    assert by["process.uptime"][0].value >= 0
    pipe.collect()
    assert len(cov.reads) == 1  # the 24 h coverage is cached between exports
    loop.close()
    we.dispose()


def test_process_metrics_are_exported(pipe):
    names = {n for n, _ in pipe.collect()}
    assert "process.cpu.time" in names


from tests.agent.test_sdk_harness import CLI, SCRIPT as CLI_SCRIPT, SPECS as CLI_SPECS, needs_cli  # noqa: E402


@needs_cli
def test_real_cli_wake_spans_nest_tool_calls_and_carry_no_content(pipe, tmp_path):
    from tests.agent.fake_anthropic_api import FakeApi

    async def execute(name, args):
        return json.dumps({"ok": True, "echo": "SV-RESULT-MARKER"}), False

    with FakeApi(CLI_SCRIPT) as fake:
        h = harness_mod.SdkHarness(ANTHROPIC_KEY, tmp_path / "data", cli_path=CLI, base_url=fake.url)
        out = asyncio.run(h.run_wake("SV-SYSTEM-PROMPT-MARKER", "SV-WAKE-PROMPT-MARKER", CLI_SPECS, execute,
                                     session_id="sess-cli", model="claude-haiku-5-5", effort="low", max_turns=6,
                                     max_tokens=16_000, max_budget_usd=5.0))
    assert out.status == "done"
    (wake,) = [s for s in pipe.spans() if s.name == "invoke_agent settleview"]
    tools = [s for s in pipe.spans() if s.name.startswith("execute_tool")]
    assert [s.attributes["gen_ai.tool.name"] for s in tools] == ["observe", "place_entry"]
    assert all(s.parent.span_id == wake.context.span_id for s in tools)
    assert wake.attributes["gen_ai.usage.output_tokens"] > 0
    pipe.assert_clean(CONTENT + ("Checking the market first.", "Entry placed."))


def test_setup_never_imports_sqlalchemy_asyncio(otel):
    import sys
    # it needs greenlet, which the image does not have (the app would not start)
    assert "sqlalchemy.ext.asyncio" not in sys.modules

