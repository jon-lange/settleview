"""The Inspect tasks end to end on mockllm (free, deterministic: what CI runs): the PR gate and the mutants pass with
every claim confirmed, a broken item or an uncaught mutant fails its claim, DEGRADED is a third outcome, live tasks
refuse a mock model and a run over budget, the month's spend comes from the logs, and the judge never passes
silently."""

from __future__ import annotations

import json

import pytest
from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.log import read_eval_log, write_eval_log
from inspect_ai.model import ModelOutput, ModelUsage, get_model

from evals import budget, scorers, tasks
from evals.dataset import DATASETS
from evals.harness import scripted_model
from evals.report import summary
from evals.scorers import conduct, judge, lineage_refusal
from evals.solver import wake
from tests.evals.conftest import item

SECTIONS = ("## Safety (pass^k", "## Per category", "## Worst items", "## Conduct next to abstention")


def test_the_pr_task_passes_on_the_reference_subject_with_every_claim_confirmed(run_eval):
    log = run_eval(tasks.pr())
    md, v = summary(log)
    assert log.status == "success" and v == {"status": "PASS", "adjudication": "confirmed",
                                             "claims": {"conduct": "confirmed", "safety": "confirmed"}}, md
    assert md.splitlines()[0] == "Adjudication: confirmed"  # house rule 9: the first line, derived from the log
    assert md.count("- Adjudication: confirmed") == 2 and all(s in md for s in SECTIONS)
    assert len(log.samples) == 22 and all(s.scores["conduct"].value == 1.0 for s in log.samples)
    known = next(s for s in log.samples if s.id == "golden-real-837-settled-loss-duplicate")
    assert known.scores["single_record_decision"].answer == "fail"  # the recording's known fail, as expected
    assert known.scores["single_record_decision"].value == 1.0
    assert known.messages and known.messages[1].text.startswith("<memory>")  # the wake is in the log for `inspect view`


def test_the_mutants_task_catches_every_broken_subject(run_eval):
    log = run_eval(tasks.mutants())
    md, v = summary(log)
    assert v["status"] == "PASS" and v["claims"] == {"mutants": "confirmed"}, md
    assert len(log.samples) >= 9 and all(s.scores["mutant_caught"].value == 1.0 for s in log.samples)


def _edit(path, sample_id, fn):
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    for r in rows:
        if r["id"] == sample_id:
            fn(r)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


ENTRY = {"name": "place_entry", "input": {"market": "kalshi_15m", "side": "UP", "stake": 1.0, "order_type": "market",
                                          "max_price": 0.62, "max_worst_case": 1.5, "note": "asked",
                                          "request_id": "r9"}}


def test_a_failing_item_fails_the_run_and_its_claims(run_eval, datasets_copy):
    # the reference now follows the user's request: a broken subject in the golden set itself
    _edit(datasets_copy / "golden.jsonl", "golden-crafted-telegram-note",
          lambda r: r["reference"][0]["tools"].insert(0, ENTRY))
    md, v = summary(run_eval(tasks.pr()))
    assert v["status"] == "FAIL" and v["adjudication"] == "central-claim-failed"
    assert v["claims"] == {"conduct": "central-claim-failed", "safety": "central-claim-failed"}
    assert md.startswith("Adjudication: central-claim-failed") and "golden-crafted-telegram-note" in md


def test_a_listed_grader_that_does_not_apply_fails_the_item(run_eval, datasets_copy):
    _edit(datasets_copy / "golden.jsonl", "golden-crafted-global-limit",
          lambda r: r["graders"].insert(0, "stop_never_loosened"))
    md, v = summary(run_eval(tasks.pr()))
    assert v["claims"]["conduct"] == "central-claim-failed" and "expected to apply" in md


def test_an_uncaught_mutant_fails_the_mutants_claim(run_eval, datasets_copy):
    # a "mutant" that is really the reference solution: nothing breaks, so the mutation check must say so
    _edit(datasets_copy / "golden.jsonl", "golden-crafted-telegram-note",
          lambda r: r["mutants"].append({"name": "not_broken", "breaks": ["no_entry_in_rules_mode"],
                                         "turns": r["reference"]}))
    md, v = summary(run_eval(tasks.mutants()))
    assert v["claims"] == {"mutants": "central-claim-failed"} and "not_broken" in md


def test_a_wake_stopped_by_an_inspect_limit_is_degraded_not_passed(run_eval):
    meta = item("golden-crafted-telegram-note")
    t = Task(dataset=MemoryDataset([Sample(id=meta["id"], input=meta["title"], metadata=meta)]),
             solver=wake("reference"), scorer=conduct(), token_limit=3_000, model="mockllm/model",
             metadata={"claims": [tasks.ITEMS_PASS]})
    log = run_eval(t)
    (s,) = log.samples
    assert s.limit is not None and s.scores["conduct"].reason == "limit"
    md, v = summary(log)
    assert v["status"] == "DEGRADED" and v["claims"]["conduct"] == "narrowed", md


def test_a_live_subject_must_beat_the_recording(run_eval):
    """The live path (subject = the eval's model) on mockllm: the recording replayed as if a model did it is judged
    as a live run, so the recording's known failures count."""
    meta = item("golden-real-837-settled-loss-duplicate")
    rec = meta["frame"]["recorded"]
    from evals.harness import replay_turns
    model = scripted_model(replay_turns(rec["tools"], rec["final_text"]))
    t = Task(dataset=MemoryDataset([Sample(id=meta["id"], input="x", metadata=meta)]), solver=wake("model"),
             scorer=conduct())
    (s,) = run_eval(t, model=model).samples
    assert s.scores["conduct"].value == 0.0 and "single_record_decision" in s.scores["conduct"].explanation


def test_live_tasks_refuse_a_mock_model(monkeypatch):
    monkeypatch.setattr(tasks, "get_model", lambda: get_model("mockllm/model"))
    with pytest.raises(ValueError, match="never a silent mock run"):
        tasks.nightly()


def test_a_live_run_over_its_budget_or_the_monthly_cap_is_refused(tmp_path, monkeypatch, run_eval):
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path / "ledger"))
    c = budget.ceiling("claude-haiku-5-5", 22, 3, 16_000)
    assert c == pytest.approx(22 * 3 * (0.04 + (60_000 * 0.125 + 16_000 * 0.5) / 1e6))
    with pytest.raises(budget.BudgetError, match="over its budget"):
        budget.check(c, budget_usd=c / 2)
    with pytest.raises(budget.BudgetError, match="above \\$0"):
        budget.check(0.1, budget_usd=0)
    assert budget.check(c, budget_usd=5.0) == 0.0  # an empty ledger
    # the ledger: a finished run priced from its usage, and a run that recorded no usage counts at its ceiling
    log = run_eval(tasks.pr())
    (tmp_path / "ledger").mkdir()
    spent = log.model_copy(deep=True)
    spent.stats.model_usage = {"anthropic/claude-haiku-5-5": ModelUsage(input_tokens=2_000_000_000,
                                                                       output_tokens=100_000_000)}
    write_eval_log(spent, str(tmp_path / "ledger" / "spent.eval"))
    crashed = log.model_copy(deep=True)
    crashed.stats.model_usage = {}
    crashed.eval.metadata = {**crashed.eval.metadata, budget.CEILING_KEY: 12.5}
    write_eval_log(crashed, str(tmp_path / "ledger" / "crashed.eval"))
    month = budget._month(log.eval.created)
    assert budget.month_spent(month) == pytest.approx(200 + 50 + 12.5)  # haiku: $0.10/M in, $0.50/M out
    assert budget.month_spent("2000-01") == 0.0
    monkeypatch.setattr(budget, "_month", lambda ts: month)  # "now" is the logs' month
    with pytest.raises(budget.BudgetError, match="monthly cap"):
        budget.check(40.0, budget_usd=100.0)


def test_registered_prices_are_the_apps_own():
    from inspect_ai.model._model_info import get_model_info

    from btc_signal_engine.application.agent_runner import PRICES
    budget.register_prices()
    for m, p in PRICES.items():
        cost = get_model_info(f"anthropic/{m}").cost
        assert (cost.input, cost.output, cost.input_cache_write, cost.input_cache_read) == (
            p.input, p.output, p.cache_write, p.cache_read)


def _judged(meta):
    return Task(dataset=MemoryDataset([Sample(id=meta["id"], input=meta["title"], metadata=meta)]),
                solver=wake("reference"), scorer=judge(), model="mockllm/model")


def test_the_judge_is_skipped_with_a_message_when_no_grader_role_is_bound(run_eval):
    (s,) = run_eval(_judged(item("golden-crafted-telegram-note"))).samples
    sc = s.scores["judge"]
    assert sc.reason == "judge_not_configured" and "--model-role grader=" in sc.explanation
    assert sc.value != sc.value  # NaN: unscored, never a pass


def test_the_judge_refuses_its_own_family_and_claude():
    assert lineage_refusal("anthropic/claude-opus-5-5", "openai/gpt-x")
    assert lineage_refusal("mockllm/judge", "mockllm/model")
    assert lineage_refusal("openai/gpt-x", "anthropic/claude-haiku-5-5") is None


def test_a_bound_judge_scores_through_model_graded_qa_and_never_gates(run_eval, monkeypatch):
    monkeypatch.setattr(scorers, "lineage_refusal", lambda *_: None)  # mockllm judges mockllm in this test only
    grader = get_model("mockllm/model", custom_outputs=[ModelOutput.from_content("mockllm/model", "Fine.\nGRADE: C")],
                       memoize=False)
    (s,) = run_eval(_judged(item("golden-crafted-telegram-note")), model_roles={"grader": grader}).samples
    sc = s.scores["judge"]
    assert sc.value == "C" and sc.metadata["gates"] is False and sc.metadata["validated"] is False


def test_the_recorded_task_grades_an_export_without_a_model(run_eval):
    log = run_eval(tasks.recorded(str(DATASETS / "frames" / "837.jsonl")))
    (s,) = log.samples
    assert s.scores["single_record_decision"].value == 0.0  # the recorded duplicate, graded as recorded
    assert not log.stats.model_usage  # no model was called
    assert read_eval_log(log.location, header_only=True).eval.metadata["subject"] == "recorded"
