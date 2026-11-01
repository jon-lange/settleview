"""HTTP routes the Play screens use from the Coach: the current window's recorded series (the odds pane) and the
pause switch (half of the Play kill switch). Nothing here places orders.

Un-pausing re-enables entries like /api/trading/resume, so it is PC-only (R10): a phone gets 403; pausing stays open."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from btc_signal_engine.adapters.http.access import from_pc, only_pc
from btc_signal_engine.application.coach import H_MS, CoachService


class PauseBody(BaseModel):
    paused: bool


def register_coach_routes(app: FastAPI, coach: CoachService, now_fn: Callable[[], int]) -> None:
    @app.get("/api/coach/pause")
    def coach_pause() -> dict:
        return coach.pause_state()

    @app.post("/api/coach/pause")
    def coach_set_pause(body: PauseBody, request: Request):
        if not body.paused and not from_pc(request):
            return only_pc()
        return coach.set_paused(body.paused, now_fn())

    @app.get("/api/coach/series")
    def coach_series(window: str = Query("current", description="current, or a window start in UTC ms.")) -> dict:
        now = now_fn()
        if window == "current":
            boundary = now - now % H_MS
        else:
            try:
                boundary = int(window)
            except ValueError:
                raise HTTPException(422, "window must be 'current' or a window start in UTC ms") from None
            if boundary % H_MS or boundary > now:
                raise HTTPException(422, "window must be the start of a past or current 15-minute window")
        return coach.series(boundary, now)
