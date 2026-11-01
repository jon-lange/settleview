"""HTTP routes for the phone "Play" screens (strategies "you" on Kalshi and "you_poly" on Polymarket US). Read only,
except the phone's own trade limits: orders go through the existing /api/trading/prepare, /approve and /prepare_exit
routes with the venue's strategy. `venue` and `span` are required (missing or unknown = 422): a screen always says
which tab it is showing.

/api/play/stream (GET, text/event-stream; same `venue`, `span`, `stake` as /api/play/now): the Play screen's live
view pushed once a second (adapters/http/push.py TickStreams; review P1, B6#1-5): the whole /api/play/now payload
first, then its changed top-level fields; `event: stale` when a read fails. The screen opens one per tab and amount
(a new amount reopens it) and falls back to nothing: while the stream is down its numbers are shown as stale and its
slide is off. More than TickStreams.max_streams open streams answer 503 {"detail"}.

/api/play/limits (GET, POST): the per-trade and per-day limits shared by every phone strategy (PlayLimits). Like
every /api route it is open to the PC and to a signed-in phone (the PIN middleware); it is not loopback-only. POST
needs both fields (a dollar amount or null = no limit); raising or removing one needs `confirm` == LIMITS_CONFIRM.
Refusals come back as 200 {"ok": false, "code", "reason"} (codes "invalid", "confirm_required"), like the trading
routes; a malformed body (missing field, non-number, zero or negative) is 422.

/api/play/market_band (POST {"cents": 5 | 10 | 15 | 25 | null}): the market protection (how far over the ask a Market
buy may fill; null = up to 99c), shared by every phone strategy; GET /api/play/limits carries it as `market_band`.

A phone trades within the limits set on the PC (R10; the access middleware's actor, `from_pc`): a phone may lower a
limit or narrow the band, but raising or removing a limit, or widening the band (more cents or null), answers it 403
"Only from the PC itself." (so does any change while the stored value is unreadable: it cannot be shown to lower)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from btc_signal_engine.adapters.http.access import from_pc, human, only_pc, only_people
from btc_signal_engine.adapters.http.push import TickStreams
from btc_signal_engine.application.play import PlayService
from btc_signal_engine.application.play_limits import LimitsUnreadable, PlayLimits

VENUE = "^(kalshi|polymarket)$"
SPAN = "^(5m|15m|1h)$"


class BandBody(BaseModel):
    cents: int | None = Field(...)  # required: null = up to 99c, never read from a missing field


class LimitsBody(BaseModel):
    # Both required (null = no limit), so a missing field is never read as "no limit".
    per_trade: float | None = Field(..., gt=0, allow_inf_nan=False)
    per_day: float | None = Field(..., gt=0, allow_inf_nan=False)
    confirm: str | None = Field(None, max_length=100)


def register_play_routes(app: FastAPI, play: PlayService, now_fn: Callable[[], int],
                         limits: PlayLimits | None = None, streams: TickStreams | None = None) -> None:
    hub = streams if streams is not None else TickStreams()

    @app.get("/api/play/now")
    def play_now(venue: str = Query(..., pattern=VENUE), span: str = Query(..., pattern=SPAN),
                 stake: float | None = Query(None, gt=0, le=10_000)) -> dict:
        return play.now_view(now_fn(), venue, span=span, stake=stake)

    @app.get("/api/play/stream")
    def play_stream(venue: str = Query(..., pattern=VENUE), span: str = Query(..., pattern=SPAN),
                    stake: float | None = Query(None, gt=0, le=10_000)):
        if hub.full():
            return JSONResponse({"detail": "Too many live Play screens are open; try again shortly."}, status_code=503)
        return hub.response(lambda: play.now_view(now_fn(), venue, span=span, stake=stake))

    @app.get("/api/play/scoreboard")
    def play_scoreboard(venue: str = Query(..., pattern=VENUE), span: str = Query(..., pattern=SPAN),
                        mode: str | None = Query(None, pattern="^(prod|shadow|sandbox|all)$"),
                        last: int | None = Query(None, ge=1, le=500), both: bool = False) -> dict:
        return play.scoreboard(now_fn(), venue, span=span, mode=mode, last=last, both=both)

    if limits is None:
        return

    def _widens(cents: int | None) -> bool:
        """Would this band let a Market buy fill at a worse price than the current one (None = up to 99c)?"""
        try:
            old = limits.market_band_cents()
        except LimitsUnreadable:
            return True
        if old is None:
            return False
        return cents is None or cents > old

    def _view(now: int) -> dict:
        return {**limits.view(), "balance_cap": play.balance_caps(now), "history": limits.history(),
                "market_band": limits.band_view()}

    @app.get("/api/play/limits")
    def play_limits_get() -> dict:
        return _view(now_fn())

    @app.post("/api/play/limits")
    def play_limits_set(body: LimitsBody, request: Request):
        if not human(request):
            return only_people()
        now = now_fn()
        # A phone's change goes through without the phrase: PlayLimits refuses a raise or removal as confirm_required.
        pc = from_pc(request)
        res = limits.set(body.per_trade, body.per_day, body.confirm if pc else None, now)
        if not pc and res.get("code") == "confirm_required":
            return only_pc()
        if not res["ok"]:
            return res
        return {**res, **_view(now)}

    @app.post("/api/play/market_band")
    def play_market_band_set(body: BandBody, request: Request):
        if not human(request):
            return only_people()
        now = now_fn()
        if not from_pc(request) and _widens(body.cents):
            return only_pc()
        res = limits.set_market_band(body.cents, now)
        if not res["ok"]:
            return res
        return {**res, **_view(now)}
