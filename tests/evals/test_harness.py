"""The harness on Inspect's model API: frozen replay (recorded results; an unrecorded call is `diverged`, never a
failure), the simulated world (the real runner, session gate and OrderGate on the shadow exchange), scripted mockllm
subjects, the dataset's strictness, and a broken-gate mutation check for the control graders (a deliberately broken
subject: the gates switched off)."""

from __future__ import annotations

import asyncio
import json

import pytest
from inspect_ai.model import ModelOutput, get_model

from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.agent_sessions import AgentSessions
from btc_signal_engine.application.trading import OK
from evals import dataset as ds
from evals import frozen, sim
from evals.graders import FAIL, PASS, grade
from evals.harness import InspectHarness, replay_turns, scripted_model
from evals.scorers import degraded_reason, problems
from evals.solver import script_for
from tests.evals.conftest import item


def _frozen(meta, model, replay=False):
    h = InspectHarness(model)
    return asyncio.run(frozen.run(meta, h, model_id=meta["model"], replay=replay)), h


def test_frozen_replay_of_a_recording_reproduces_its_calls_and_known_verdicts():
    meta = item("golden-real-837-settled-loss-duplicate")
    t, _ = _frozen(meta, script_for(meta, "reference"), replay=True)
    rec = meta["frame"]["recorded"]["tools"]
    assert [(c["name"], c["code"]) for c in t["tools"]] == [(c["name"], c["code"]) for c in rec]
    assert t["status"] == "done" and not t["diverged"]
    verdicts, probs = problems(meta, "reference", t)
    assert not probs, probs
    assert verdicts["single_record_decision"]["status"] == FAIL  # the recorded duplicate, kept as a known fail
    assert problems(meta, "model", t)[1]  # a live subject must beat the recording


def test_a_call_outside_the_recording_is_diverged_and_degraded_not_failed():
    meta = item("golden-real-255-rule-entry")
    model = scripted_model([{"tools": [{"name": "get_scoreboard", "input": {"market": "kalshi_15m"}}]},
                            {"text": "never reached"}])
    t, h = _frozen(meta, model)
    assert t["status"] == "diverged" and t["diverged"] == {"call": 0, "name": "get_scoreboard"}
    assert t["tools"][0]["code"] == "diverged"
    assert sum(m.role == "assistant" for m in h.messages) == 1  # the gate stopped the wake before the next turn
    assert degraded_reason(t) == "diverged"


def test_frozen_mode_sends_the_recorded_message_with_todays_prompt_and_tools():
    meta = item("golden-real-4098-self-stop")
    seen = {}

    def out(messages, tools, _choice, _config):
        seen.update(system=messages[0].text, prompt=messages[1].text, tools=[t.name for t in tools])
        return ModelOutput.from_content("mockllm/model", "done")
    _frozen(meta, get_model("mockllm/model", custom_outputs=out, memoize=False))
    assert seen["prompt"] == meta["frame"]["input"]["message"] and seen["prompt"].startswith("<memory>")
    assert "How this app talks to you" in seen["system"] and "record_decision" in seen["tools"]


def test_a_call_the_runtime_refused_is_reported_without_being_executed():
    ran = []

    async def execute(name, args):
        ran.append(name)
        return "{}", False
    m = scripted_model([{"tools": [{"name": "record_decision", "input": {}},
                                   {"name": "record_decision", "input": {}, "not_run": True}]}, {"text": "ok"}])
    out = asyncio.run(InspectHarness(m).run_wake("s", "p", frozen.specs(), execute, session_id="x", model="m",
                                                 effort="medium", max_turns=6, max_tokens=1, max_budget_usd=1.0))
    assert ran == ["record_decision"] and len(out.tool_calls) == 2 and out.status == "done" and out.turns == 2


def test_a_script_placeholder_without_a_value_is_an_error():
    m = scripted_model([{"tools": [{"name": "set_btc_stop", "input": {"bracket_id": "${bracket}"}}]}])

    async def execute(name, args):
        return "{}", False
    with pytest.raises(KeyError, match="bracket"):
        asyncio.run(InspectHarness(m).run_wake("s", "p", [], execute, session_id="x", model="m", effort="medium",
                                               max_turns=6, max_tokens=1, max_budget_usd=1.0))


def test_simulated_mode_runs_the_real_gates_and_isolates_each_trial():
    meta = item("golden-crafted-injection-note")
    mutant = {**meta, "mutant": meta["mutants"][0]}
    bad, _ = asyncio.run(sim.run(mutant, script_for(mutant, "mutant"), model_id=meta["model"]))
    assert [(c["name"], c["code"]) for c in bad["tools"]] == [("set_btc_stop", "rule_stop_locked"),
                                                               ("place_entry", "rules_own_entries")]
    assert bad["world"]["agent_entries"] == 0 and bad["world"]["stop_changes"] == []  # the controls held
    v = grade(bad)
    assert v["no_entry_in_rules_mode"]["status"] == FAIL and v["no_entry_in_rules_mode"]["layer"] == "control"
    good, h = asyncio.run(sim.run(meta, script_for(meta, "reference"), model_id=meta["model"]))  # a fresh world
    assert [c["name"] for c in good["tools"]] == ["observe", "update_notes", "record_decision"]
    assert not problems(meta, "reference", good)[1]
    assert h.messages[1].text.startswith("<memory>")  # the runner's own wake message reached the model


def test_control_graders_fail_when_the_gates_are_broken(monkeypatch):
    """The broken subject is the code: the session gate and the runner's pause check switched off. A write after the
    pause then really goes out, and no_write_when_paused must FAIL (with the gates on it passes the same script)."""
    meta = item("golden-crafted-paused-mid-wake")
    sells = {**meta, "mutant": next(m for m in meta["mutants"] if m["name"] == "sells_while_paused")}
    held, _ = asyncio.run(sim.run(sells, script_for(sells, "mutant"), model_id=meta["model"]))
    assert grade(held)["no_write_when_paused"]["status"] == PASS and held["world"]["writes_after_pause"] == 0
    monkeypatch.setattr(AgentSessions, "check", lambda self, *a, **k: OK)
    monkeypatch.setattr(AgentRunner, "_running", lambda self: True)
    broken, _ = asyncio.run(sim.run(sells, script_for(sells, "mutant"), model_id=meta["model"]))
    assert broken["world"]["writes_after_pause"] > 0
    v = grade(broken)
    assert v["no_write_when_paused"]["status"] == FAIL, v["no_write_when_paused"]


def test_replay_turns_keep_the_recorded_order_and_the_dropped_calls():
    turns = replay_turns([{"name": "observe", "args": {}}, {"name": "record_decision", "args": {"a": 1},
                                                            "code": "not_run"}], "bye")
    assert turns == [{"tools": [{"name": "observe", "input": {}, "not_run": False},
                                {"name": "record_decision", "input": {"a": 1}, "not_run": True}]}, {"text": "bye"}]


@pytest.mark.parametrize(("extra", "match"), [({"graders": ["no_such"]}, "unknown grader"),
                                              ({}, "reference"), ({"colour": 1}, "unknown key"),
                                              ({"mode": "live"}, "mode must be")])
def test_items_are_strict(extra, match):
    r = {"id": "x", "title": "t", "category": "c", "source": "crafted", "mode": "simulated", **extra}
    with pytest.raises(ds.ItemError, match=match):
        ds.check(r, "x")


def test_a_duplicate_id_across_sets_is_an_error(datasets_copy):
    line = (datasets_copy / "golden.jsonl").read_text(encoding="utf-8").splitlines()[0]
    with (datasets_copy / "adversarial.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    with pytest.raises(ds.ItemError, match="duplicate item ids"):
        ds.load(["golden", "adversarial"])


def test_golden_v0_is_twelve_real_and_eight_crafted():
    golden = [s.metadata for s in ds.load(["golden"])]
    assert len(golden) == 20
    assert sum(m["source"] == "real" for m in golden) == 12 and sum(m["source"] == "crafted" for m in golden) == 8
    assert all(m["frame"] for m in golden if m["source"] == "real")
    assert len(ds.load(["adversarial"])) == 2
    lines = [json.loads(x) for x in (ds.DATASETS / "golden.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(isinstance(r["frame"], str) for r in lines if "frame" in r)  # frames stay exported files


def test_a_script_that_runs_out_is_an_error_not_a_silent_end():
    async def execute(name, args):
        return "{}", False
    m = scripted_model([{"tools": [{"name": "observe", "input": {}}]}])
    with pytest.raises(RuntimeError, match="ran out"):
        asyncio.run(InspectHarness(m).run_wake("s", "p", frozen.specs(), execute, session_id="x", model="m",
                                               effort="medium", max_turns=6, max_tokens=1, max_budget_usd=1.0))


def test_the_turn_cap_ends_a_runaway_wake():
    async def execute(name, args):
        return "{}", False
    m = scripted_model([{"tools": [{"name": "observe", "input": {}}]}] * 3)
    out = asyncio.run(InspectHarness(m).run_wake("s", "p", frozen.specs(), execute, session_id="x", model="m",
                                                 effort="medium", max_turns=3, max_tokens=1, max_budget_usd=1.0))
    assert out.status == "turn_cap" and out.turns == 3


def test_frames_are_copies_not_shared_between_samples():
    a, b = item("golden-real-553-settled-loss"), item("golden-real-553-settled-loss")
    a["frame"]["recorded"]["tools"].clear()
    assert b["frame"]["recorded"]["tools"]  # each load reads its own frame


def _keys(x, out: set) -> set:
    if isinstance(x, dict):
        for k, v in x.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(x, list):
        for v in x:
            _keys(v, out)
    return out


def test_the_model_sees_the_registry_schemas_less_only_the_keywords_inspect_drops():
    """Inspect's tool schema model drops a few JSON Schema keywords; the registry still enforces them (a breaking
    call is answered `invalid`, as in production). The list is pinned so a new loss is noticed."""
    from evals.harness import tool_infos
    dropped = set()
    for spec, info in zip(frozen.specs(), tool_infos(frozen.specs()), strict=True):
        dropped |= _keys(spec["input_schema"], set()) - _keys(info.parameters.model_dump(exclude_none=True), set())
    assert dropped == {"exclusiveMaximum", "exclusiveMinimum", "maxItems"}
