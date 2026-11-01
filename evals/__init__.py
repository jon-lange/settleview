"""Settleview agent evals: eval-as-code (docs/plans/2026-10-31-1900-agent-evals-plan.md).

Layout: `datasets/` (TOML items + JSONL frames), `graders/` (code predicates), `harness/` (frozen and simulated runs,
the fake and the live model, budget, reports), `pipelines/` (pr, nightly, promote), `export.py` (journal -> JSONL),
`review.py` (the error-analysis pack). See evals/README.md.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
