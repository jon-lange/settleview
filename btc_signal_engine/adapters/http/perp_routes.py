"""HTTP routes for the Perps screen (perps plan U7; KTD11, KTD15, KTD16) and its server-sent-events push channel.

Every route sits under the PIN middleware like any /api path (access.py): the PC passes, a phone needs its session
cookie, the stream included (checked once, when it opens). Refusals are HTTP 200 {"ok": false, "code", "reason"} so the
screen shows the plain-English reason; a malformed body (missing field, wrong type, unknown action) is 422.

  GET  /api/perps/view            the whole View (application/perp_view.py)
  GET  /api/perps/stream          text/event-stream: the full View first, then `event: view` frames with the changed
                                  top-level fields (at most one per 100 ms), `event: ping` every 15 s
  POST /api/perps/prepare         entry / add ticket          -> {ok, token, ticket, expires_ms}
  POST /api/perps/prepare_exit    reduce / close ticket       -> {ok, token, ticket, expires_ms}
  POST /api/perps/prepare_trigger stop / take-profit ticket   -> {ok, token, ticket, expires_ms} (a key left out keeps
                                  that leg, an explicit null clears it, both null removes the trigger)
  POST /api/perps/approve         {token, shown}              -> {ok, outcome, view} | refusal
  POST /api/perps/adopt           {size_shown}                -> {ok, outcome, view} | refusal
  GET  /api/perps/record?last=N   your last N closed positions (realized P&L from fills), wins, losses, total
  POST /api/perps/mode            {mode, confirm}             -> {ok, mode, ready, view} | refusal; switching TO
                                  perp_prod needs the typed confirmation, from the PC or the phone (the user's call,
                                  2026-10-30, replacing R10's PC-only rule for perps)
Kill, resume and the presence heartbeat are the existing /api/trading routes (trading_routes.py `extra`, KTD16).

With the feature flag off (no service), the view answers {"enabled": false, "enabled_reason": "Perps are switched
off", ...} and every write route refuses with code `perps_off`.

Publishing (`PerpPublisher`): a change is noted with `notify()` (a margin-stream book or trade event, a service state
change after any route here, the shared controls, the monitor tick); one task then recomputes the View off the
caller's path, at most once per `min_gap_s`, and publishes the changed fields to the hub, which coalesces bursts to one
frame per 100 ms. A full recompute also runs every `refresh_s` so ages and the ticker channel's mark and funding stay
current. Nothing is computed while no stream is open.

Approvals are shielded (`Inflight`): the save-send-record step runs to its end even if the request is cancelled (a
dropped connection or the app shutting down), so a restart never finds an order sent but not recorded; shutdown
waits for them (`drain`) before the margin client closes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, Field

from btc_signal_engine.adapters.http.access import human, only_people
from btc_signal_engine.adapters.http.push import PushHub
from btc_signal_engine.application.perp_view import OFF_TEXT, PerpView

log = logging.getLogger(__name__)
T = TypeVar("T")

_MONEY = {"allow_inf_nan": False}


class PrepareBody(BaseModel):
    action: Literal["entry", "add"]
    side: Literal["long", "short"]
    collateral: float = Field(gt=0, **_MONEY)
    leverage: float = Field(gt=0, **_MONEY)
    # required keys (null = none), so a missing field is never read as "no stop"
    stop_loss: float | None = Field(..., gt=0, **_MONEY)
    take_profit: float | None = Field(..., gt=0, **_MONEY)
    fill: Literal["market", "all_or_nothing", "limit"]
    limit_price: float | None = Field(..., gt=0, **_MONEY)
    band: float | None = Field(..., ge=0, **_MONEY)  # the service refuses one wider than its max_band


class PrepareExitBody(BaseModel):
    action: Literal["reduce", "close"]
    count: float | None = Field(..., gt=0, **_MONEY)


class PrepareTriggerBody(BaseModel):
    # a key left out keeps that leg (the service's contract); null clears it
    stop_loss: float | None = Field(None, gt=0, **_MONEY)
    take_profit: float | None = Field(None, gt=0, **_MONEY)


class ApproveBody(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    shown: dict[str, Any] = Field(..., description="The ticket exactly as it was shown.")


class AdoptBody(BaseModel):
    size_shown: float = Field(..., **_MONEY)


class ModeBody(BaseModel):
    mode: Literal["perp_demo", "perp_prod", "off"]
    confirm: str | None = Field(None, max_length=200)


class Inflight:
    """Shielded approvals: each runs to its end even if the request awaiting it is cancelled; `drain` waits for them."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def run(self, coro: Awaitable[T]) -> T:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def drain(self, timeout_s: float) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=timeout_s)
        if pending:
            log.error("%d perps approval(s) still running %.0f s into shutdown: the next start resolves them from "
                      "their saved 'sending' state", len(pending), timeout_s)


class PerpPublisher:
    """Recomputes the View after `notify()` (off the caller's path) and publishes its changed fields to the hub."""

    def __init__(self, view: PerpView, hub: PushHub, now_fn: Callable[[], int], *, min_gap_s: float = 0.05,
                 refresh_s: float = 1.0) -> None:
        self.view = view
        self.hub = hub
        self.now_fn = now_fn
        self.min_gap_s = min_gap_s
        self.refresh_s = refresh_s
        self._wake = asyncio.Event()

    def notify(self) -> None:
        """Cheap and synchronous: safe from a stream callback on the event loop."""
        self._wake.set()

    def publish_now(self) -> None:
        if self.hub.subscribers:
            self.hub.publish(self.view.changes(self.now_fn()))

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self.refresh_s)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                self.publish_now()
            except Exception:
                log.warning("perps view publish failed", exc_info=True)
            await asyncio.sleep(self.min_gap_s)


def register_perp_routes(app: FastAPI, view: PerpView, hub: PushHub, now_fn: Callable[[], int], *,
                         notify: Callable[[], None] = lambda: None, inflight: Inflight | None = None) -> None:
    svc = view.service
    flight = inflight or Inflight()

    def off() -> dict:
        return {"ok": False, "code": "perps_off", "reason": f"{OFF_TEXT}: nothing was sent."}

    def done(out: dict) -> dict:
        notify()
        return out

    @app.get("/api/perps/view")
    async def perps_view() -> dict:  # on the loop, never beside a send in a worker thread
        return view.view(now_fn())

    @app.get("/api/perps/record")
    def perps_record(last: int = Query(10, ge=1, le=200)) -> dict:  # a small indexed read; a worker thread is fine
        if svc is None:
            return {"env": None, "reason": OFF_TEXT, "rows": [], "wins": 0, "losses": 0, "pnl": 0.0}
        return svc.record(last)

    @app.get("/api/perps/stream")
    def perps_stream():
        return hub.response(lambda: view.view(now_fn()))

    @app.post("/api/perps/prepare")
    async def perps_prepare(body: PrepareBody) -> dict:
        if svc is None:
            return off()
        return done(await svc.prepare(body.model_dump(), now_fn()))

    @app.post("/api/perps/prepare_exit")
    async def perps_prepare_exit(body: PrepareExitBody) -> dict:
        if svc is None:
            return off()
        return done(await svc.prepare_exit(body.model_dump(), now_fn()))

    @app.post("/api/perps/prepare_trigger")
    async def perps_prepare_trigger(body: PrepareTriggerBody) -> dict:
        if svc is None:
            return off()
        return done(await svc.prepare_trigger(body.model_dump(exclude_unset=True), now_fn()))

    @app.post("/api/perps/approve")
    async def perps_approve(body: ApproveBody) -> dict:
        if svc is None:
            return off()
        out = await flight.run(svc.approve(body.token, body.shown, now_fn()))
        if "view" in out:
            out["view"] = view.view(now_fn())
        return done(out)

    @app.post("/api/perps/adopt")
    async def perps_adopt(body: AdoptBody) -> dict:
        if svc is None:
            return off()
        out = await svc.adopt(body.size_shown, now_fn())
        if "view" in out:
            out["view"] = view.view(now_fn())
        return done(out)

    @app.post("/api/perps/mode")
    async def perps_mode(body: ModeBody, request: Request):
        # Production works from the phone too (the user's call, 2026-10-30): the typed confirmation is the guard.
        if not human(request):  # a person only: the paired PC or a phone's PIN session
            return only_people()
        if svc is None:
            return off()
        out = await svc.set_mode(body.mode, now_fn(), body.confirm)
        if out.get("ok"):
            out["view"] = view.view(now_fn())
        return done(out)
