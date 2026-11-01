"""Agent mode wiring (U7): nothing of it exists with the flag off; with it on and no Anthropic key, the routes exist,
in-app sessions say "no key", the phone services carry the session gate step and the review loop runs."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.app import create_app
from btc_signal_engine.application.agent_sessions import VENUES
from btc_signal_engine.bootstrap.app import AGENT_DEFAULT_PROMPT, Runtime
from btc_signal_engine.bootstrap.config import Settings
from tests.pc import paired

HOST = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_ACCESS_PIN", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY", "BTCFLOW_AGENT_ENABLED", "BTCFLOW_ANTHROPIC_API_KEY",
              "BTCFLOW_AGENT_CLI_PATH"):
        monkeypatch.delenv(k, raising=False)


def runtime(tmp_path, **kw) -> Runtime:
    return Runtime(Settings(_env_file=None, db_path=tmp_path / "agent.db", trading_stream=False, **kw))


def test_with_agent_mode_off_there_are_no_agent_routes_loops_or_gate(tmp_path):
    rt = runtime(tmp_path)
    try:
        app = create_app(rt.services())
        assert not [r for r in app.routes if getattr(r, "path", "").startswith("/api/agent")]
        c = paired(TestClient(app, client=("127.0.0.1", 1), base_url=HOST), rt.settings.data_dir)
        assert c.get("/api/agent/status").status_code == 404
        assert c.post("/api/agent/launch", json={}).status_code in (404, 405)
        assert "agent_review" not in rt.loops() and rt.agent_sessions is None
        assert rt.you_trading._agent_gate_fn is None  # every agent order is refused
    finally:
        rt.writer.close()


def test_with_agent_mode_on_and_no_key_in_app_is_unavailable_and_the_gate_is_wired(tmp_path):
    rt = runtime(tmp_path, agent_enabled=True)
    try:
        assert rt.agent_llm is None and rt.agent_fleet is None and "agent_review" in rt.loops()
        assert rt.you_trading._agent_gate_fn == rt._agent_check
        assert set(rt.agent_sessions.services) == set(rt.trading_services)
        assert rt.agent_sessions.allow_production is False
        c = paired(TestClient(create_app(rt.services()), client=("127.0.0.1", 1), base_url=HOST), rt.settings.data_dir)
        st = c.get("/api/agent/status").json()
        assert st["in_app"]["available"] is False and "no key" in st["in_app"]["reason"]
        assert st["prompts"] == {"system": 1, "user": 0} and st["live_count"] == 0
        assert isinstance(st["server_ms"], int)
        assert {k: (v["strategy"], v["mode"]) for k, v in st["venues"].items()} == {
            k: (s, getattr(rt.trading_services.get(s), "mode", None)) for k, s in VENUES.items()}  # None: not here
        p = c.get("/api/agent/prompts").json()
        assert p["system"]["text"] == AGENT_DEFAULT_PROMPT.read_text(encoding="utf-8")
        assert st["daily"]["per_day"] is None and "error" not in st["daily"]
        assert c.get("/api/trading/session").status_code == 404
    finally:
        rt.writer.close()


def test_with_a_key_the_runner_runs_on_the_agent_sdk_harness_with_the_configured_cli(tmp_path, monkeypatch):
    from btc_signal_engine.adapters.agent_sdk.harness import SdkHarness

    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "")  # restored after: the app sets it process-wide
    cli = tmp_path / "cli" / "claude"
    rt = runtime(tmp_path, agent_enabled=True, anthropic_api_key="sk-ant-test-dummy", agent_cli_path=cli)
    try:
        assert rt.agent_fleet.scheduler.cap == 4  # BTCFLOW_AGENT_WAKE_CONCURRENCY's default
        h = rt.agent_fleet._make().harness  # every session's runner, made at its launch, shares the one harness
        assert isinstance(h, SdkHarness) and rt.agent_llm is not None and h is rt.agent_harness
        assert h.cli_path == str(cli) and h.client_app.startswith("settleview/")
        assert h.config_dir == tmp_path / "agent-cli" / "config" and h.cwd == tmp_path / "agent-cwd"
        assert h.prompt_dir == tmp_path / "agent-cli" / "prompts"
        assert os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] == "1"
    finally:
        rt.writer.close()
    rt = runtime(tmp_path / "b", agent_enabled=True, anthropic_api_key="k")
    try:
        assert rt.agent_fleet._make().harness.cli_path is None  # the SDK's bundled CLI, else `claude` on PATH
    finally:
        rt.writer.close()


def test_the_wake_concurrency_cap_is_a_setting(tmp_path):
    rt = runtime(tmp_path, agent_enabled=True, anthropic_api_key="k", agent_wake_concurrency=2)
    try:
        assert rt.agent_fleet.scheduler.cap == 2
        assert rt.agent_harness.concurrency == 2  # the harness's CLI semaphore follows the same cap
    finally:

        rt.writer.close()
    with pytest.raises(ValueError):
        Settings(_env_file=None, agent_wake_concurrency=0)


def test_agent_production_follows_its_setting(tmp_path):
    rt = runtime(tmp_path, agent_enabled=True, agent_allow_production=True)
    try:
        assert rt.agent_sessions.allow_production is True
    finally:
        rt.writer.close()
