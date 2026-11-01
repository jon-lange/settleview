"""The eval harness: frozen replay (recorded results; an unrecorded call is `diverged`, never a failure), the simulated
world (the real runner, session gate and OrderGate on the shadow exchange), the scripted model, and a broken-gate
mutation check for the control graders (a deliberately broken subject: the gates switched off)."""

from __future__ import annotations

import asyncio
import copy

import pytest

from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.agent_sessions import AgentSessions
from btc_signal_engine.application.trading import OK
from evals.graders import FAIL, PASS, grade
from evals.graders.mutations import validate_graders
from evals.harness import frozen, sim
from evals.harness.items import DATASETS, ItemError, load_item, load_set
from evals.harness.models import ScriptedModel, replay_turns
from evals.harness.run import judge

VALID = {g for g, (ok, _) in validate_graders().items() if ok}


def golden(name: str):
    return load_item(DATASETS / "golden" / f"{name}.toml")


def test_frozen_replay_of_a_recording_reproduces_its_calls_and_known_verdicts():
    item = golden("real-837-settled-loss-duplicate")
    t = asyncio.run(frozen.run(item, None))
    rec = item.frame["recorded"]["tools"]
    assert [(c["name"], c["code"]) for c in t["tools"]] == [(c["name"], c["code"]) for c in rec]
    assert t["status"] == "done" and not t["diverged"]
    j = judge(item, t, fake=True, valid=VALID)
    assert j["outcome"] == "pass", j["problems"]
    assert j["verdicts"]["single_record_decision"]["status"] == FAIL  # the recorded duplicate, kept as a known fail
    assert judge(item, t, fake=False, valid=VALID)["outcome"] == "fail"  # a live run must beat the recording


def test_a_call_outside_the_recording_is_diverged_not_failed():
    item = golden("real-255-rule-entry-supervision")
    model = ScriptedModel([{"tools": [{"name": "get_scoreboard", "input": {"market": "kalshi_15m"}}]},
                           {"text": "never reached"}])
    t = asyncio.run(frozen.run(item, model))
    assert t["status"] == "diverged" and t["diverged"] == {"call": 0, "name": "get_scoreboard"}
    assert t["tools"][0]["code"] == "diverged" and len(model.requests) == 1
    assert judge(item, t, fake=False, valid=VALID)["outcome"] == "diverged"


def test_frozen_mode_sends_the_recorded_message_with_todays_prompt_and_tools():
    item = golden("real-4098-self-stop-mode-conflict")
    model = ScriptedModel(replay_turns(item.frame["recorded"]["tools"], None))
    asyncio.run(frozen.run(item, model))
    (req,) = model.requests
    assert req["prompt"] == item.frame["input"]["message"] and req["prompt"].startswith("<memory>")
    assert "How this app talks to you" in req["system"] and "record_decision" in req["tools"]


def test_the_scripted_model_reports_a_not_run_call_without_executing_it():
    ran = []

    async def execute(name, args):
        ran.append(name)
        return "{}", False

    m = ScriptedModel([{"tools": [{"name": "record_decision", "input": {}},
                                  {"name": "record_decision", "input": {}, "not_run": True}]}])
    out = asyncio.run(m.run_wake("s", "p", [], execute, session_id="x", model="m", effort="e", max_turns=6,
                                 max_tokens=1, max_budget_usd=1.0))
    assert ran == ["record_decision"] and len(out.tool_calls) == 2 and out.status == "done"


def test_a_script_placeholder_without_a_value_is_an_error():
    m = ScriptedModel([{"tools": [{"name": "set_btc_stop", "input": {"bracket_id": "${bracket}"}}]}])

    async def execute(name, args):
        return "{}", False
    with pytest.raises(KeyError, match="bracket"):
        asyncio.run(m.run_wake("s", "p", [], execute, session_id="x", model="m", effort="e", max_turns=6,
                               max_tokens=1, max_budget_usd=1.0))


def test_simulated_mode_runs_the_real_gates_and_isolates_each_trial():
    item = golden("crafted-injection-in-note")
    mutant = item.mutants[0]["turns"]
    bad = sim.run(item, None, turns=mutant)
    assert [(c["name"], c["code"]) for c in bad["tools"]] == [("set_btc_stop", "rule_stop_locked"),
                                                               ("place_entry", "rules_own_entries")]
    assert bad["world"]["agent_entries"] == 0 and bad["world"]["stop_changes"] == []  # the controls held
    v = grade(bad)
    assert v["no_entry_in_rules_mode"]["status"] == FAIL and v["no_entry_in_rules_mode"]["layer"] == "control"
    good = sim.run(item, None)  # a fresh store and exchange: nothing from the mutant's trial is left
    assert [c["name"] for c in good["tools"]] == ["observe", "update_notes", "record_decision"]
    assert judge(item, good, fake=True, valid=VALID)["outcome"] == "pass"


def test_control_graders_fail_when_the_gates_are_broken(monkeypatch):
    """The broken subject is the code: the session gate and the runner's pause check switched off. A write after the
    pause then really goes out, and no_write_when_paused must FAIL (with the gates on it passes the same script)."""
    item = golden("crafted-paused-mid-wake")
    sells = next(m for m in item.mutants if m["name"] == "sells_while_paused")["turns"]
    held = sim.run(item, None, turns=copy.deepcopy(sells))
    assert grade(held)["no_write_when_paused"]["status"] == PASS and held["world"]["writes_after_pause"] == 0
    monkeypatch.setattr(AgentSessions, "check", lambda self, *a, **k: OK)
    monkeypatch.setattr(AgentRunner, "_running", lambda self: True)
    broken = sim.run(item, None, turns=copy.deepcopy(sells))
    assert broken["world"]["writes_after_pause"] > 0
    v = grade(broken)
    assert v["no_write_when_paused"]["status"] == FAIL, v["no_write_when_paused"]


def test_item_files_are_strict():
    with pytest.raises(ItemError, match="unknown grader"):
        _bad_item("graders = ['no_such']")
    with pytest.raises(ItemError, match="reference"):
        _bad_item("")


def _bad_item(extra: str):
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    p = d / "x.toml"
    p.write_text(f"id = 'x'\ntitle = 't'\ncategory = 'c'\nsource = 'crafted'\nmode = 'simulated'\n{extra}\n",
                 encoding="utf-8")
    return load_item(p)


def test_golden_v0_is_twelve_real_and_eight_crafted():
    items = load_set(DATASETS / "golden")
    assert len(items) == 20
    assert sum(i.source == "real" for i in items) == 12 and sum(i.source == "crafted" for i in items) == 8
    assert all(i.frame for i in items if i.source == "real")
    assert len(load_set(DATASETS / "adversarial")) == 2


def test_live_model_needs_a_key_and_never_reads_a_dotenv(tmp_path, monkeypatch):
    from evals.harness.models import live_model
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="Anthropic key"):
        live_model(tmp_path)
    key = tmp_path / "key.txt"
    key.write_text("sk-ant-test-dummy\n", encoding="utf-8")
    h = live_model(tmp_path / "cli", key_file=str(key))  # builds the harness; nothing is called
    assert h.live and h.name == "live"


def test_the_live_path_replays_a_frozen_item_through_the_real_sdk_against_a_fake_api(tmp_path):
    """The production harness (SDK + Claude Code CLI) on a local fake Anthropic API: the wiring a --live run uses,
    for free. Skipped where no CLI is bundled or configured (as tests/agent/test_sdk_harness.py)."""
    from btc_signal_engine.adapters.agent_sdk.harness import PREFIX, SdkHarness
    from tests.agent.fake_anthropic_api import FakeApi
    from tests.agent.test_sdk_harness import CLI
    if CLI is None:
        pytest.skip("no Claude Code CLI: set BTCFLOW_AGENT_CLI_PATH (or a platform wheel that bundles one)")
    item = golden("real-4098-self-stop-mode-conflict")
    rec = item.frame["recorded"]["tools"]
    script = [[("tool_use", PREFIX + c["name"], c["args"]) for c in rec], [("text", "Ended the session.")]]
    with FakeApi(script) as api:
        h = SdkHarness("sk-ant-test-dummy", tmp_path / "data", cli_path=CLI, base_url=api.url,
                       client_app="settleview/evals-test")
        t = asyncio.run(frozen.run(item, h))
    assert not t["diverged"], t["tools"]
    assert [c["name"] for c in t["tools"]] == [c["name"] for c in rec]
    assert t["status"] == "session_stopped" and t["turns"] >= 1
