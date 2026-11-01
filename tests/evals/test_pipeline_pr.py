"""The PR pipeline in fake mode (free, deterministic: what CI runs), the summary's adjudication lines, the budget
guard of live runs, and the OTel evaluation events."""

from __future__ import annotations

import json
import shutil

import pytest

from evals import ROOT
from evals.graders import GRADERS, PASS, Verdict, grader
from evals.graders.mutations import MUTATIONS
from evals.harness import budget
from evals.harness.items import DATASETS
from evals.harness.models import ScriptedModel
from evals.harness.run import evaluation_attributes, execute, load_pipeline, otel_emitter

PR = ROOT / "pipelines" / "pr.toml"


def test_the_pr_pipeline_passes_in_fake_mode_with_every_claim_confirmed(tmp_path):
    out = execute(load_pipeline(PR), out_dir=tmp_path)
    assert out["status"] == "PASS" and out["adjudication"] == "confirmed", out
    assert set(out["claims"].values()) == {"confirmed"}
    md = out["summary"].read_text(encoding="utf-8")
    assert md.splitlines()[0] == "Adjudication: confirmed"  # house rule 9: the first line, derived
    assert md.count("- Adjudication: confirmed") == 4
    for section in ("## Safety (pass^k", "## Per category", "## Conduct next to abstention", "## Worst items",
                    "## Broken subjects", "## Graders"):
        assert section in md
    lines = [json.loads(x) for x in out["results"].read_text(encoding="utf-8").splitlines()]
    head, trials = lines[0], [x for x in lines if x["kind"] == "trial"]
    mutants = [x for x in lines if x["kind"] == "mutant"]
    assert head["kind"] == "run" and head["items"] == len(trials) == 22 and head["subject"] == "fake"
    assert all(t["outcome"] == "pass" for t in trials) and all(isinstance(t["seed"], int) for t in trials)
    assert len(mutants) >= 9 and all(m["caught"] for m in mutants)
    assert all(v["valid"] for v in head["graders"].values())


def _copy_sets(tmp_path):
    d = tmp_path / "datasets"
    for s in ("golden", "adversarial"):
        shutil.copytree(DATASETS / s, d / s)
    return d


def test_a_failing_item_fails_the_run_and_its_claim(tmp_path):
    d = _copy_sets(tmp_path)
    f = d / "golden" / "crafted-telegram-note.toml"
    text = f.read_text(encoding="utf-8")
    # the reference now follows the user's request: a broken subject in the golden set itself
    text = text.replace('[[reference]]\ntools = [{name = "observe", input = {}}, ',
                        '[[reference]]\ntools = [{name = "place_entry", input = {market = "kalshi_15m", side = "UP", '
                        'stake = 1.0, order_type = "market", max_price = 0.62, max_worst_case = 1.5, note = "asked", '
                        'request_id = "r9"}}, {name = "observe", input = {}}, ', 1)
    f.write_text(text, encoding="utf-8")
    out = execute(load_pipeline(PR), out_dir=tmp_path / "r", datasets=d)
    assert out["status"] == "FAIL" and out["adjudication"] == "central-claim-failed"
    assert out["claims"]["conduct"] == out["claims"]["safety"] == "central-claim-failed"
    md = out["summary"].read_text(encoding="utf-8")
    assert md.startswith("Adjudication: central-claim-failed") and "golden-crafted-telegram-note" in md


def test_a_listed_grader_that_does_not_apply_fails_the_item(tmp_path):
    d = _copy_sets(tmp_path)
    f = d / "golden" / "crafted-global-limit.toml"
    f.write_text(f.read_text(encoding="utf-8").replace('graders = ["ends_cleanly"',
                                                       'graders = ["stop_never_loosened", "ends_cleanly"'),
                 encoding="utf-8")
    out = execute(load_pipeline(PR), out_dir=tmp_path / "r", datasets=d)
    assert out["claims"]["conduct"] == "central-claim-failed"
    assert "expected to apply" in out["summary"].read_text(encoding="utf-8")


def test_a_vacuous_grader_is_rejected_and_narrows_the_run(tmp_path):
    @grader("vacuous_test_only", "outcome")
    def vac(_t):
        return Verdict(PASS, "never looks")
    MUTATIONS["vacuous_test_only"] = MUTATIONS["no_entry_in_rules_mode"]
    try:
        out = execute(load_pipeline(PR), out_dir=tmp_path)
    finally:
        del GRADERS["vacuous_test_only"], MUTATIONS["vacuous_test_only"]
    assert out["claims"]["graders"] == "narrowed" and out["status"] == "DEGRADED"
    assert out["adjudication"] == "narrowed"
    assert "rejected: vacuous_test_only" in out["summary"].read_text(encoding="utf-8")


def test_a_live_pipeline_refuses_to_run_as_fake(tmp_path):
    with pytest.raises(SystemExit, match="--live"):
        execute(load_pipeline(ROOT / "pipelines" / "nightly.toml"), out_dir=tmp_path)


def test_live_runs_respect_the_run_budget_and_the_monthly_cap(tmp_path, monkeypatch):
    spend = tmp_path / "spend.jsonl"
    monkeypatch.setattr(budget, "SPEND_FILE", spend)
    p = load_pipeline(PR)
    factory = lambda: ScriptedModel([])  # noqa: E731 - a stand-in for the live model: nothing leaves the machine
    with pytest.raises(budget.BudgetError, match="over its budget"):
        execute(p, live=True, budget_usd=0.05, out_dir=tmp_path, model_factory=factory)
    assert not spend.exists()  # refused before anything was reserved
    budget.record("old-run", "nightly", "actual", 299.9, path=spend)
    with pytest.raises(budget.BudgetError, match="monthly cap"):
        execute(p, live=True, budget_usd=5.0, out_dir=tmp_path, model_factory=factory)
    spend.unlink()
    out = execute(p, live=True, budget_usd=5.0, out_dir=tmp_path, model_factory=factory)
    kinds = [json.loads(x)["kind"] for x in spend.read_text(encoding="utf-8").splitlines()]
    assert kinds == ["reserve", "actual"]
    assert budget.month_spent(path=spend) < 1.0 and out["status"] == "FAIL"  # an empty script records nothing


def test_month_spend_counts_a_crashed_runs_reservation(tmp_path):
    f = tmp_path / "s.jsonl"
    budget.record("a", "nightly", "reserve", 2.0, path=f, now=1_793_500_000)
    budget.record("a", "nightly", "actual", 1.25, path=f, now=1_793_500_100)
    budget.record("b", "nightly", "reserve", 3.0, path=f, now=1_793_500_200)  # never finished
    assert budget.month_spent(budget.month_of(1_793_500_000), path=f) == pytest.approx(4.25)
    with pytest.raises(budget.BudgetError, match="no cost estimate"):
        budget.estimate(["claude-unknown"])


def test_otel_events_are_off_without_an_endpoint_and_carry_no_content(monkeypatch):
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    assert otel_emitter() is None
    line = {"run_id": "r", "item_id": "i", "trial": 0, "outcome": "pass", "model": "m", "subject": "fake"}
    a = evaluation_attributes(line, "no_entry_in_rules_mode", {"status": "fail", "evidence": "SECRET ARG"})
    assert a["event.name"] == "gen_ai.evaluation.result" and a["gen_ai.evaluation.score.label"] == "fail"
    assert "SECRET ARG" not in json.dumps(a)
