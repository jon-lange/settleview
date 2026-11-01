"""HTTP routes for Ask Settleview on the Play screen (application/copilot.py). Read only, except the record of what was
shown with an order (one audit row; it never touches an order).

GET /api/copilot/now?venue=&span=&mode=quiet|full&warn=<comma list>[&stake=]: the line, the lights, the per-side
warnings, the fill-in ticket and the hold note for that Play tab now. `mode` is required and "off" is not accepted
(off = the screen does not ask: nothing is computed). `warn` is required too: the toggled-on warning keys, comma
separated, or "none" (the page's request helper drops an empty parameter). An unknown key is 422, never dropped.

POST /api/copilot/shown {strategy, bracket_id, shown}: what the screen showed when the slide sent that order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from btc_signal_engine.application.copilot import Copilot

VENUE = "^(kalshi|polymarket)$"
SPAN = "^(5m|15m|1h)$"
Name = Field(max_length=32)


class LightShown(BaseModel):
    rule: str = Name
    state: Literal["grey", "amber", "green"]


class LineShown(BaseModel):
    kind: Literal["ready", "setup", "warning", "context", "hold"]
    text: str = Field(max_length=300)
    rule: str | None = Field(None, max_length=32)


class WarningShown(BaseModel):
    keys: list[str] = Field(max_length=8)
    text: str = Field(max_length=300)


class FilledShown(BaseModel):
    rule: str = Name
    side: Literal["UP", "DOWN"]
    max_price: float = Field(gt=0, lt=1)
    stop: float | None = Field(None, gt=0)


class Shown(BaseModel):
    mode: Literal["quiet", "full"]
    side: Literal["UP", "DOWN"]
    ask: float | None = Field(None, ge=0, le=1)
    lights: list[LightShown] = Field(default_factory=list, max_length=12)
    line: LineShown | None = None
    warning: WarningShown | None = None
    filled: FilledShown | None = None
    btc: float | None = None
    strike: float | None = None
    btc_gap: float | None = None
    left_ms: int | None = None


class ShownBody(BaseModel):
    strategy: str = Name
    bracket_id: str = Field(max_length=64)
    shown: Shown


def register_copilot_routes(app: FastAPI, copilot: Copilot, now_fn: Callable[[], int]) -> None:
    @app.get("/api/copilot/now")
    def copilot_now(venue: str = Query(..., pattern=VENUE), span: str = Query(..., pattern=SPAN),
                    mode: str = Query(..., pattern="^(quiet|full)$"),
                    warn: str = Query(..., min_length=1, max_length=200),
                    stake: float | None = Query(None, gt=0, le=10_000)) -> dict:
        keys = [] if warn == "none" else warn.split(",")
        try:
            return copilot.view(now_fn(), venue, span, mode=mode, warnings=keys, stake=stake)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/copilot/shown")
    def copilot_shown(body: ShownBody) -> dict:
        try:
            return copilot.record_shown(body.strategy, body.bracket_id, body.shown.model_dump(), now_fn())
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
