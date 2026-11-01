"""HTTP routes for the Play screens' human-confirmed trading. Never returns key ids, key paths or key material.

Refusals (gate, limits, expired or altered approvals) come back as 200 {"ok": false, "code", "reason"} so the screen
can show the plain-English reason; malformed bodies are 422.

Strategies: `services` maps each phone strategy ("you", "you_poly", "you_poly5") to its own TradingService. The mode,
status and ticket prepare / approve / exit / take-profit / stop routes REQUIRE a `strategy` (body field or query
parameter; missing = 422, empty or unknown = code `unknown_strategy`, never some default); a ticket can only be
approved on the strategy that prepared it. The kill switch, resume and the presence heartbeat reach every service.

Shared controls (perps plan KTD16): `extra` services (the perps service) implement `Controlled` and also receive the
kill switch, resume and the heartbeat, AFTER every event service; their answer is never the one returned (the first
event service stays the primary). A failure there is logged and audited through its own `record_failure` and never
stops the event services from being killed or resumed: the switch itself is the one shared `killed` setting.

People only (P0 #1; `access.human`): the presence heartbeat and a mode change need the paired PC or a phone's PIN
session, never just a network position.

PC-only (R10; the access middleware's actor, `from_pc`): turning the kill switch off (/resume) answers a phone 403
"Only from the PC itself."; a phone can always turn it on. Switching TO production works from the phone too, behind
the typed real-money confirmation (the user's call, 2026-10-30, to match perps).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, Field

from btc_signal_engine.adapters.http.access import from_pc, human, only_pc, only_people
from btc_signal_engine.application.trading import TradingService

log = logging.getLogger(__name__)


class Controlled(Protocol):
    """KTD16: what the shared kill / resume / presence routes need from a non-event trading service."""

    def heartbeat(self, now: int, visible: bool = True) -> dict: ...

    async def kill(self, now: int) -> dict: ...

    def resume(self, now: int) -> dict: ...

    def record_failure(self, what: str, now: int, exc: Exception) -> None: ...


class ProposalBody(BaseModel):
    ticker: str = Field(max_length=64)
    side: Literal["UP", "DOWN"]
    stake: float = Field(gt=0, le=10_000)
    # "limit": max_price required (an IoC at it or less); "market": max_price omitted (an IoC at 99c sized at the live
    # ask; the ticket states the worst case). Absent = "limit" (then max_price is required).
    price_mode: Literal["limit", "market"] | None = None
    max_price: float | None = Field(None, gt=0, lt=1)
    take_profit: float = Field(gt=0, lt=1)
    stop: float | None = Field(None, gt=0, lt=1)
    stop_index_below: float | None = Field(None, gt=0)
    stop_index_above: float | None = Field(None, gt=0)
    window_end: int | None = None
    fair_up: float | None = None
    strategy: str = Field(max_length=32)


class ApproveBody(BaseModel):
    token: str = Field(max_length=200)
    decision: Literal["yes", "no"]
    ticket: dict[str, Any] = Field(default_factory=dict, description="The ticket exactly as it was shown.")
    strategy: str = Field(max_length=32)


class ExitBody(BaseModel):
    bracket_id: str = Field(max_length=64)
    strategy: str = Field(max_length=32)


class TakeProfitBody(BaseModel):
    bracket_id: str = Field(max_length=64)
    price: float = Field(gt=0, le=0.99)
    strategy: str = Field(max_length=32)


class IndexStopBody(BaseModel):
    bracket_id: str = Field(max_length=64)
    level: float | None = Field(None, gt=0)  # None removes the stop
    strategy: str = Field(max_length=32)


class HeartbeatBody(BaseModel):
    visible: bool = True


class ModeBody(BaseModel):
    mode: Literal["sandbox", "shadow", "prod"]
    confirm: str | None = Field(None, max_length=200)
    strategy: str = Field(max_length=32)


def _control_failed(what: str, x: Controlled, now: int, exc: Exception) -> None:
    """A shared control that failed on an `extra` service: logged, then audited by that service (its own failure to
    audit is logged too); it never stops the other services."""
    log.error("%s on %r failed", what, x, exc_info=exc)
    try:
        x.record_failure(what, now, exc)
    except Exception:
        log.error("auditing the failed %s on %r failed too", what, x, exc_info=True)


def register_trading_routes(app: FastAPI, services: dict[str, TradingService], now_fn: Callable[[], int], *,
                            extra: Sequence[Controlled] = (), on_control: Callable[[], None] | None = None) -> None:
    """`extra`: further services reached by heartbeat, kill and resume (KTD16). `on_control()` runs after each of
    those three (the Perps screen's push channel republishes its view)."""
    by_name = dict(services)
    extras = tuple(extra)
    if not by_name:
        raise ValueError("register_trading_routes needs at least one trading service")
    # The service whose answer the shared routes (heartbeat, kill, resume) return: the first one.
    primary = next(iter(by_name.values()))

    def fan_out(what: str, call: Callable[[Controlled], Any], now: int) -> None:
        for x in extras:
            try:
                call(x)
            except Exception as exc:
                _control_failed(what, x, now, exc)

    async def fan_out_kill(now: int) -> None:
        for x in extras:
            try:
                await x.kill(now)
            except Exception as exc:
                _control_failed("kill", x, now, exc)

    def changed() -> None:
        if on_control is not None:
            try:
                on_control()
            except Exception:
                log.warning("after-control hook failed", exc_info=True)

    def pick(strategy: str) -> tuple[TradingService | None, dict | None]:
        # No default: an empty or unknown name is refused, never quietly read as another strategy.
        if strategy not in by_name:
            return None, {"ok": False, "code": "unknown_strategy",
                          "reason": f"There is no strategy called {strategy!r} (known: {', '.join(sorted(by_name))})."}
        return by_name[strategy], None

    @app.post("/api/trading/prepare")
    async def trading_prepare(body: ProposalBody) -> dict:
        s, refused = pick(body.strategy)
        return refused or await s.prepare(body.model_dump(exclude={"strategy"}), now_fn())

    @app.post("/api/trading/prepare_exit")
    async def trading_prepare_exit(body: ExitBody) -> dict:
        s, refused = pick(body.strategy)
        return refused or await s.prepare_exit(body.bracket_id, now_fn())

    @app.post("/api/trading/approve")
    async def trading_approve(body: ApproveBody) -> dict:
        s, refused = pick(body.strategy)
        return refused or await s.approve(body.token, body.decision, body.ticket, now_fn())

    @app.post("/api/trading/take_profit")
    async def trading_take_profit(body: TakeProfitBody) -> dict:
        """Move an open bracket's resting take profit (the phone's auto-sell; 0.99 = ride). The tap is the Yes."""
        s, refused = pick(body.strategy)
        return refused or await s.set_take_profit(body.bracket_id, body.price, now_fn())

    @app.post("/api/trading/stop")
    async def trading_index_stop(body: IndexStopBody) -> dict:
        """Set, move or remove an open bracket's BTC stop. The tap is the Yes."""
        s, refused = pick(body.strategy)
        return refused or await s.set_index_stop(body.bracket_id, body.level, now_fn())

    @app.post("/api/trading/heartbeat")
    def trading_heartbeat(body: HeartbeatBody, request: Request):
        if not human(request):  # "a person is at the screen": the paired PC or a phone's PIN session only
            return only_people()
        now = now_fn()
        for other in by_name.values():
            if other is not primary:
                other.heartbeat(now, body.visible)
        out = primary.heartbeat(now, body.visible)
        fan_out("heartbeat", lambda x: x.heartbeat(now, body.visible), now)
        changed()
        return out

    @app.post("/api/trading/kill")
    async def trading_kill() -> dict:
        now = now_fn()
        for other in by_name.values():  # the switch is shared; every service audits it
            if other is not primary:
                await other.kill(now)
        out = await primary.kill(now)
        await fan_out_kill(now)  # after every event service: a failure here never stops them being killed
        changed()
        return out

    @app.post("/api/trading/resume")
    def trading_resume(request: Request):
        if not from_pc(request):
            return only_pc()
        now = now_fn()
        for other in by_name.values():  # the switch is shared, so turning it off reaches every service too
            if other is not primary:
                other.resume(now)
        out = primary.resume(now)
        fan_out("resume", lambda x: x.resume(now), now)
        changed()
        return out

    @app.post("/api/trading/mode")
    async def trading_mode(body: ModeBody, request: Request):
        if not human(request):
            return only_people()
        s, refused = pick(body.strategy)
        return refused or await s.set_mode(body.mode, now_fn(), body.confirm)

    @app.get("/api/trading/status")
    def trading_status(strategy: str = Query(..., max_length=32)) -> dict:
        s, refused = pick(strategy)
        return refused or s.status(now_fn())
