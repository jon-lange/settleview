"""The eval API budget: $300 a calendar month (user, 2026-10-31), on Inspect's own cost features.

- Prices: the app's one price table (`agent_runner.PRICES`) is registered with Inspect (`set_model_cost`), so every
  model call in a log carries its cost (`ModelUsage.total_cost`) and Inspect's per-sample `cost_limit` works.
- Per sample: `cost_limit` (Inspect stops the sample once it is spent; it is checked after each model call, so a
  sample can overrun it by at most one turn) and `token_limit`.
- Per run: a live task's ceiling is samples x epochs x (cost limit + one worst-case turn). The task refuses to start
  when the ceiling is over its `budget_usd`, or when this month's spend plus the ceiling is over the cap.
- The month's spend is read from the Inspect logs in the log directory (the ledger: keep them): each log's model
  cost (Inspect's, from the same table); a log with no usage yet (a run that crashed or is still running) counts at the
  ceiling it recorded. Mock-model runs cost nothing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from btc_signal_engine.application.agent_runner import PRICES, worst_case_usd
from evals import ROOT
from evals.graders import DEFAULT_MAX_USD

MONTH_CAP_USD = 300.0
TURN_MARGIN_TOKENS = 60_000  # the context a worst-case extra turn is priced at (the wake's prompt is ~15-30K)
CEILING_KEY = "settleview_cost_ceiling_usd"


class BudgetError(RuntimeError):
    """The run would break its own budget or the month's cap; nothing was spent."""


def log_dir() -> Path:
    return Path(os.environ.get("INSPECT_LOG_DIR") or ROOT / "logs")


def register_prices() -> None:
    """The app's prices into Inspect's model database (anthropic/<model>), so logs and cost limits see them."""
    from inspect_ai.model import ModelCost, set_model_cost
    for m, p in PRICES.items():
        set_model_cost(f"anthropic/{m}", ModelCost(input=p.input, output=p.output, input_cache_write=p.cache_write,
                                                   input_cache_read=p.cache_read))


def sample_cost_limit(model: str) -> float:
    """A sample's cost limit: twice the cap `bounded_turns_and_cost` fails at, so the grader, not the limit, judges."""
    if model not in DEFAULT_MAX_USD:
        raise BudgetError(f"no cost cap for model {model!r}; known: {', '.join(DEFAULT_MAX_USD)}")
    return 2 * DEFAULT_MAX_USD[model]


def ceiling(model: str, samples: int, epochs: int, max_tokens: int) -> float:
    per = sample_cost_limit(model) + worst_case_usd(model, TURN_MARGIN_TOKENS, max_tokens)
    return round(samples * epochs * per, 6)


def _month(ts: str | datetime) -> str:
    d = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    return d.astimezone(UTC).strftime("%Y-%m")


def log_cost(log) -> float | None:
    """A log's spend: Inspect's own per-call cost (`total_cost`, from the registered prices) where it has one, else
    the usage at the same base prices; None when the log recorded no usage. (Haiku's long-prompt rate, over 100K
    tokens a request, is not modelled: a wake's prompt is far below it.)"""
    usage = (log.stats.model_usage if log.stats else None) or {}
    if not usage:
        return None
    total = 0.0
    for name, u in usage.items():
        p = PRICES.get(name.split("/", 1)[-1])
        if u.total_cost is not None:
            total += u.total_cost
        elif p is not None:  # mockllm and other unpriced models cost nothing here
            total += (u.input_tokens * p.input + u.output_tokens * p.output
                      + (u.input_tokens_cache_write or 0) * p.cache_write
                      + (u.input_tokens_cache_read or 0) * p.cache_read) / 1e6
    return total


def month_spent(month: str | None = None, path: Path | None = None) -> float:
    from inspect_ai.log import list_eval_logs, read_eval_log
    month = month or _month(datetime.now(UTC))
    d = path or log_dir()
    if not d.exists():
        return 0.0
    spent = 0.0
    for info in list_eval_logs(str(d)):
        log = read_eval_log(info, header_only=True)
        if _month(log.eval.created) != month:
            continue
        cost = log_cost(log)
        if cost is None:
            cost = float((log.eval.metadata or {}).get(CEILING_KEY) or 0.0)
        spent += cost
    return round(spent, 6)


def check(ceiling_usd: float, budget_usd: float, *, cap: float = MONTH_CAP_USD, path: Path | None = None) -> float:
    """Refuse a run that does not fit; returns the month's spend so far."""
    if budget_usd <= 0:
        raise BudgetError("a live run needs budget_usd above $0 (-T budget_usd=N)")
    if ceiling_usd > budget_usd + 1e-9:
        raise BudgetError(f"the run's ceiling ${ceiling_usd:.2f} is over its budget ${budget_usd:.2f} "
                          "(fewer samples or epochs, or a higher -T budget_usd)")
    spent = month_spent(path=path)
    if spent + ceiling_usd > cap + 1e-9:
        raise BudgetError(f"the month's eval spend ${spent:.2f} + this run's ceiling ${ceiling_usd:.2f} is over the "
                          f"${cap:.0f} monthly cap")
    return spent
