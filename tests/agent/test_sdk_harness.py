"""The Claude Agent SDK harness (adapters/agent_sdk): its sandbox options, the CLI's environment, the stream mapping and
the init checks without a subprocess; then one integration run of the real SDK + Claude Code CLI against a local fake
Anthropic API (never the real one), skipped when no CLI is configured (BTCFLOW_AGENT_CLI_PATH) or bundled."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import claude_agent_sdk
from btc_signal_engine.adapters.agent_sdk.harness import (BLANK, DISALLOWED, PREFIX, SdkHarness, _Wake, child_env,
                                                           env_pass)
from btc_signal_engine.application.ports import LlmMisconfigured, TurnUsage, WakeResult

from btc_signal_engine.bootstrap.config import Settings

KEY = "sk-ant-test-dummy"
SPECS = [
    {"name": "observe", "description": "Read the market.", "kind": "read",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "place_entry", "description": "Buy (a write).", "kind": "write",
     "input_schema": {"type": "object", "properties": {"side": {"type": "string", "enum": ["UP", "DOWN"]},
                                                       "stake": {"type": "number"}},
                      "required": ["side", "stake"], "additionalProperties": False}},
]
NAMES = {PREFIX + t["name"] for t in SPECS}


def harness(tmp_path, **kw) -> SdkHarness:
    return SdkHarness(KEY, tmp_path, **kw)


# -- without a subprocess -------------------------------------------------------------------------------------------
def test_child_env_passes_only_the_key_blanks_other_credentials_and_refuses_an_inherited_base_url(tmp_path):
    inherited = {"PATH": "/usr/bin", "BTCFLOW_TRADING_PROD_KEY_PATH": "x", "ANTHROPIC_AUTH_TOKEN": "x",
                 "CLAUDE_CODE_OAUTH_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "x", "MY_API_TOKEN": "x",
                 "CLAUDE_CODE_SESSION_ID": "x", "OTEL_EXPORTER_OTLP_HEADERS": "x", "HOME": "/home/u"}
    env = child_env(KEY, config_dir=tmp_path / "cfg", max_tokens=16_000, client_app="settleview/test",
                    inherited=inherited)
    assert env["ANTHROPIC_API_KEY"] == KEY and env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    for k in ("BTCFLOW_TRADING_PROD_KEY_PATH", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
              "AWS_SECRET_ACCESS_KEY", "MY_API_TOKEN", "CLAUDE_CODE_SESSION_ID", "OTEL_EXPORTER_OTLP_HEADERS", *BLANK):
        assert env[k] == "", k
    assert "PATH" not in env and "HOME" not in env  # the CLI still needs these from the parent
    assert "ANTHROPIC_BASE_URL" not in env  # never set in production
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000" and env["CLAUDE_CODE_MAX_RETRIES"] == "2"
    for k in ("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
              "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"):
        assert env[k] == "1"
    assert env["ENABLE_TOOL_SEARCH"] == "false" and env["CLAUDE_AGENT_SDK_CLIENT_APP"] == "settleview/test"
    with pytest.raises(LlmMisconfigured, match="ANTHROPIC_BASE_URL"):
        child_env(KEY, config_dir=tmp_path, max_tokens=1, client_app="x",
                  inherited={"ANTHROPIC_BASE_URL": "https://elsewhere.example"})
    with pytest.raises(ValueError):
        SdkHarness("  ", tmp_path)


def test_options_sandbox_the_cli_to_our_tools_with_verbatim_prompts_and_no_bare_mode(tmp_path):
    h = harness(tmp_path, cli_path=tmp_path / "claude", environ={})
    w = _Wake(SPECS, None, on_delta=None, on_turn=None, gate=None)
    o = h.options(w, system_file=h.prompt_file("sess/1", "SYSTEM"), model="claude-haiku-5-5", effort="medium",
                  max_turns=6, max_tokens=16_000, max_budget_usd=4.5)
    assert o.verbatim_prompts is True and "bare" not in o.extra_args  # --bare silently turns SDK hooks off
    assert o.extra_args == {"no-session-persistence": None, "permission-prompts": "none"}
    assert o.tools == [] and set(DISALLOWED) <= set(o.disallowed_tools) and o.allowed_tools == [PREFIX + "*"]
    assert o.permission_mode == "dontAsk" and o.setting_sources == [] and o.strict_mcp_config and o.skills == []
    assert list(o.mcp_servers) == ["settleview"] and o.cwd == str(tmp_path / "agent-cwd")
    assert o.system_prompt == {"type": "file", "path": str(tmp_path / "agent-cli" / "prompts" / "sess_1.md")}
    assert (tmp_path / "agent-cli" / "prompts" / "sess_1.md").read_text(encoding="utf-8") == "SYSTEM"
    assert o.max_turns == 6 and o.max_budget_usd == 4.5 and o.effort == "medium" and o.include_partial_messages
    assert o.thinking == {"type": "adaptive", "display": "summarized"} and o.cli_path == str(tmp_path / "claude")
    # every tool call passes the gate: ours, a failed one, and one that is not ours (denied in PreToolUse)
    assert set(o.hooks) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
    assert all(m.matcher is None for ms in o.hooks.values() for m in ms)

    assert o.env["ANTHROPIC_API_KEY"] == KEY and "ANTHROPIC_BASE_URL" not in o.env


def test_the_stream_maps_to_deltas_and_settled_turns_and_the_total_is_reconciled():
    seen, turns = [], []
    w = _Wake(SPECS, None, on_delta=lambda k, t: seen.append((k, t)), on_turn=turns.append, gate=None)
    for e in ({"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 1,
                                                                             "cache_read_input_tokens": 5}}},
              {"type": "content_block_start", "content_block": {"type": "thinking"}},
              {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Hm"}},
              {"type": "content_block_delta", "delta": {"type": "signature_delta", "signature": "x"}},
              {"type": "content_block_start", "content_block": {"type": "text"}},
              {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Ok"}},
              {"type": "content_block_start", "content_block": {"type": "tool_use", "name": PREFIX + "observe"}},
              {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{}"}},
              {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}},
              {"type": "message_stop"}):
        w._stream(e)
    assert seen == [("thinking", "Hm"), ("text", "Ok"), ("tool_use", "observe")]
    assert turns == [TurnUsage(usage={"input_tokens": 10, "output_tokens": 40, "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 5}, stop_reason="tool_use", thinking="Hm",
                               text="Ok", message_id="msg_1")]
    assert w.context_tokens == 55
    w._reconcile({"input_tokens": 30, "output_tokens": 40, "cache_read_input_tokens": 5})
    assert turns[-1] == TurnUsage(usage={"input_tokens": 20, "output_tokens": 0, "cache_creation_input_tokens": 0,
                                         "cache_read_input_tokens": 0}, unstreamed=True)


def test_the_init_report_must_show_the_app_key_exactly_our_tools_and_a_new_enough_cli():
    good = {"apiKeySource": "ANTHROPIC_API_KEY", "tools": sorted(NAMES), "claude_code_version": "2.1.301"}
    w = _Wake(SPECS, None, on_delta=None, on_turn=None, gate=None)
    w._check_init(good)
    assert w.verified
    for bad, why in (({"apiKeySource": "none"}, "credential source"),
                     ({"apiKeySource": "/login managed key"}, "credential source"),
                     ({"tools": [*NAMES, "Bash"]}, "Bash"),
                     ({"tools": [PREFIX + "observe"]}, "place_entry"),
                     ({"claude_code_version": "2.1.200"}, "older than 2.1.248")):
        w = _Wake(SPECS, None, on_delta=None, on_turn=None, gate=None)
        with pytest.raises(LlmMisconfigured, match=why):
            w._check_init({**good, **bad})
        assert not w.verified


def test_the_cli_env_is_an_allowlist_everything_else_inherited_is_blanked(tmp_path):
    inherited = {"PATH": "/usr/bin", "Path": "C:/bin", "HOME": "/home/u", "TMPDIR": "/tmp", "LANG": "C.UTF-8",
                 "LC_ALL": "C.UTF-8", "NODE_OPTIONS": "--require /x.js", "HTTPS_PROXY": "http://u:p@proxy:8080",
                 "http_proxy": "http://proxy", "NODE_EXTRA_CA_CERTS": "/evil.pem", "DATABASE_URL": "postgres://x",
                 "PGPASSWORD": "x", "SOME_SETTING": "x"}
    env = child_env(KEY, config_dir=tmp_path, max_tokens=1, client_app="x", inherited=inherited)
    for k in ("NODE_OPTIONS", "HTTPS_PROXY", "http_proxy", "NODE_EXTRA_CA_CERTS", "DATABASE_URL", "PGPASSWORD",
              "SOME_SETTING"):
        assert env[k] == "", k  # main: only secret-looking / known-prefix names were blanked
    for k in ("PATH", "Path", "HOME", "TMPDIR", "LANG", "LC_ALL"):
        assert k not in env, k  # kept from the parent
    kept = child_env(KEY, config_dir=tmp_path, max_tokens=1, client_app="x", inherited=inherited,
                     pass_env=env_pass("HTTPS_PROXY, node_extra_ca_certs"))
    assert "HTTPS_PROXY" not in kept and "NODE_EXTRA_CA_CERTS" not in kept and kept["NODE_OPTIONS"] == ""
    for bad in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "BTCFLOW_TRADING_PROD_KEY_PATH", "A B"):
        with pytest.raises(ValueError):
            env_pass(bad)
    assert harness(tmp_path, environ={}, pass_env="HTTPS_PROXY").pass_env == ("HTTPS_PROXY",)


def test_each_wake_gets_its_own_config_dir_and_at_most_concurrency_clis_run(tmp_path, monkeypatch):
    seen, running, peak, release = [], [0], [0], asyncio.Event()

    async def fake_run(self, opts, prompt):
        d = Path(opts.env["CLAUDE_CONFIG_DIR"])
        seen.append(d)
        assert d.is_dir() and d.parent == tmp_path / "agent-cli" / "config"
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await release.wait()
        running[0] -= 1
        return WakeResult(status="done", stop_reason="end_turn", text=None, tool_calls=(), turns=0, cost_usd=0.0,
                          detail={})
    monkeypatch.setattr(_Wake, "run", fake_run)
    waits = []

    async def sleep(s):
        waits.append(s)
    h = harness(tmp_path, environ={}, concurrency=2, jitter_s=0.5, rand=lambda: 0.5, sleep=sleep)

    async def main():
        async def one(i):
            return await h.run_wake("S", "go", SPECS, None, session_id=f"s{i}", model="claude-haiku-5-5",
                                    effort="low", max_turns=2, max_tokens=100, max_budget_usd=1.0)
        tasks = [asyncio.create_task(one(i)) for i in range(3)]
        for _ in range(10):
            await asyncio.sleep(0)
        assert running[0] == 2 and h.running == 2  # the third waits for a slot
        release.set()
        await asyncio.gather(*tasks)
    asyncio.run(main())
    assert peak[0] == 2 and len(set(seen)) == 3  # main: one shared config dir, no cap
    assert not any(d.exists() for d in seen)  # removed after each wake
    assert waits == [0.25] * 3  # jitter_s * rand(): wakes due at one boundary spawn apart
    with pytest.raises(ValueError):
        harness(tmp_path, concurrency=0)


def test_a_failed_or_denied_tool_call_still_passes_the_spend_gate():
    asked = []

    def gate(ctx, chars):
        asked.append((ctx, chars))
        return "spend_cap"
    w = _Wake(SPECS, None, on_delta=None, on_turn=None, gate=gate)
    w.verified = True
    out = asyncio.run(w.post_tool_failure({"tool_name": PREFIX + "place_entry", "error": "boom"}, "t1", None))
    assert out == {"continue_": False, "stopReason": "spend_cap"} and len(asked) == 1
    w = _Wake(SPECS, None, on_delta=None, on_turn=None, gate=gate)
    w.verified = True
    out = asyncio.run(w.pre_tool({"tool_name": "Bash"}, "t2", None))  # not ours: denied, and the gate still asked
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["continue_"] is False and out["stopReason"] == "spend_cap"  # main: no gate on a denied call
    assert w.hooks["denied"] == 1
    ok = _Wake(SPECS, None, on_delta=None, on_turn=None, gate=lambda c, n: None)
    ok.verified = True
    assert asyncio.run(ok.pre_tool({"tool_name": PREFIX + "observe"}, "t3", None)) == {}  # ours: allowed, no gate


def test_a_missing_cli_is_a_misconfiguration_and_nothing_runs(tmp_path):
    ran = []

    async def execute(name, args):
        ran.append(name)
        return "{}", False
    with pytest.raises(LlmMisconfigured, match="CLI was not found"):
        asyncio.run(harness(tmp_path, cli_path=tmp_path / "no-such-claude", environ={}).run_wake(
            "SYSTEM", "go", SPECS, execute, session_id="s1", model="claude-haiku-5-5", effort="low", max_turns=2,
            max_tokens=1000, max_budget_usd=1.0))
    assert ran == []


# -- the real SDK and CLI against a local fake API ------------------------------------------------------------------
def _cli() -> Path | None:
    p = Settings(_env_file=None).agent_cli_path  # BTCFLOW_AGENT_CLI_PATH
    if p is not None:
        return Path(p) if Path(p).is_file() else None
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled"
    found = [f for f in bundled.glob("claude*") if f.is_file()] if bundled.is_dir() else []
    return found[0] if found else None


CLI = _cli()
needs_cli = pytest.mark.skipif(CLI is None, reason="no Claude Code CLI: set BTCFLOW_AGENT_CLI_PATH (or install a "
                                                    "platform wheel of claude-agent-sdk that bundles one)")
SCRIPT = [[("thinking", "Checking the market first."), ("text", "Observing."), ("tool_use", PREFIX + "observe", {})],
          [("tool_use", PREFIX + "place_entry", {"side": "UP", "stake": 1})],
          [("text", "Entry placed. Done for this wake.")]]


@needs_cli
def test_real_cli_offers_only_our_tools_sends_the_prompt_verbatim_with_the_key_and_the_gate_stops_it(tmp_path):
    from tests.agent.fake_anthropic_api import FakeApi

    secret = tmp_path / "notes.txt"
    secret.write_text("SV-LOCAL-FILE-MARKER", encoding="utf-8")
    prompt = (r"<memory>{}</memory> Notes from the user: see @C:\Windows\win.ini and @" + str(secret)
              + " for details. Decide and act.")
    ran, deltas, turns, gates = [], [], [], []

    async def execute(name, args):
        ran.append((name, args))
        return json.dumps({"ok": True, "tool": name}), False

    def gate(context_tokens, new_chars):
        gates.append((context_tokens, new_chars))
        return "spend_cap"  # the next turn does not fit

    with FakeApi(SCRIPT) as api:
        h = harness(tmp_path / "data", cli_path=CLI, base_url=api.url, client_app="settleview/test")
        out = asyncio.run(h.run_wake(
            "SV-SYSTEM-PROMPT-MARKER", prompt, SPECS, execute, session_id="sess1", model="claude-haiku-5-5",
            effort="low", max_turns=6, max_tokens=16_000, max_budget_usd=5.0,
            on_delta=lambda k, t: deltas.append((k, t)), on_turn=turns.append, gate=gate))

    msgs = api.messages()
    assert len(msgs) == 1  # the gate stopped the wake before a second model request
    assert [r["path"] for r in api.requests] == ["/v1/messages"]  # nothing else was called
    req = msgs[0]
    assert req["headers"]["x-api-key"] == KEY and "authorization" not in req["headers"]
    assert {t["name"] for t in req["body"]["tools"]} == NAMES  # only our tools are offered
    body = json.dumps(req["body"])
    assert "SV-SYSTEM-PROMPT-MARKER" in json.dumps(req["body"]["system"])
    assert "@C:\\\\Windows\\\\win.ini" in body and "SV-LOCAL-FILE-MARKER" not in body  # verbatim: no @file reads
    assert "[fonts]" not in body.lower() and "16-bit" not in body
    assert req["body"]["model"] == "claude-haiku-5-5" and req["body"]["max_tokens"] == 16_000
    assert ran == [("observe", {})] and len(gates) == 1 and gates[0][1] == len(json.dumps({"ok": True,
                                                                                        "tool": "observe"}))
    assert out.status == "spend_cap" and out.detail["terminal_reason"] == "hook_stopped"
    assert out.detail["hooks"]["pre"] >= 1 and out.detail["hooks"]["post"] >= 1
    assert out.detail["models"] == ["claude-haiku-5-5"] and out.cost_usd is not None
    assert [t.stop_reason for t in turns] == ["tool_use"] and turns[0].usage["output_tokens"] == 250
    assert ("tool_use", "observe") in deltas and ("text", "Observing. ") in deltas
    assert [c["name"] for c in out.tool_calls] == ["observe"]


@needs_cli
def test_real_cli_a_cancelled_wake_lets_the_write_in_flight_finish_once(tmp_path):
    from tests.agent.fake_anthropic_api import FakeApi

    done, started = [], None

    async def main():
        nonlocal started
        started, release = asyncio.Event(), asyncio.Event()

        async def execute(name, args):
            if name == "place_entry":
                started.set()
                await release.wait()
                done.append(name)
            return json.dumps({"ok": True}), False

        h = harness(tmp_path / "data", cli_path=CLI, base_url=api.url)
        task = asyncio.create_task(h.run_wake(
            "SYSTEM", "go", SPECS, execute, session_id="sess1", model="claude-haiku-5-5", effort="low",
            max_turns=6, max_tokens=16_000, max_budget_usd=5.0))
        await asyncio.wait_for(started.wait(), 30)
        task.cancel()
        await asyncio.sleep(0.5)
        assert not task.done()  # the cancelled wake waits for the order in flight
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)

    with FakeApi(SCRIPT) as api:
        asyncio.run(main())
    assert done == ["place_entry"]  # finished, exactly once
    assert len(api.messages()) == 2  # no model request after the Stop
