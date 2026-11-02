"""Settleview agent evals on Inspect AI (docs/plans/2026-10-31-1900-agent-evals-plan.md; evals/README.md).

`tasks.py` (the Inspect tasks), `dataset.py` (JSONL items -> Samples), `solver.py` + `harness.py` (one agent wake on
Inspect's model API: frozen replay in `frozen.py`, the simulated world in `sim.py`), `graders/` (the code rules),
`scorers.py` (the rules as Inspect scorers), `budget.py` ($300/month on Inspect's cost features), `report.py`
(Adjudication and PASS / DEGRADED / FAIL from a log), `export.py` (journal -> JSONL), `review.py` (the
error-analysis pack). `python -m evals ...` is the Inspect CLI behind a .env guard.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DATASETS = ROOT / "datasets"
