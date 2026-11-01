"""HTTP route for the web app's Record tab: your record over today, the last week or everything, from the database,
computed exactly as the Telegram daily summary computes it (application/record_summary.py). Read only.

GET /api/record/summary?range=today|week|all -> {ok, range, start_ms, now_ms, net, trades, wins,
    you {net, trades, wins}, agent {net, trades, wins}, api {usd, wakes, sessions}, by_rule [{rule, trades, wins, net}]}
`range` is required: missing or unknown is 422 (never a default range)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import tzinfo

from fastapi import FastAPI, Query

from btc_signal_engine.application import record_summary
from btc_signal_engine.application.ports import Repository

RANGE = "^(" + "|".join(record_summary.RANGES) + ")$"


def register_record_routes(app: FastAPI, repo: Repository, now_fn: Callable[[], int],
                           tz: tzinfo | None = None) -> None:
    @app.get("/api/record/summary")
    def record_summary_get(range: str = Query(..., pattern=RANGE)) -> dict:  # noqa: A002 (the query name)
        return record_summary.summary(repo, now_fn(), range, tz=tz)
