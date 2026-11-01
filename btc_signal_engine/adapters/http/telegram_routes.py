"""HTTP routes for the Telegram remote and the saved launch presets (U11). Never returns the bot token.

Refusals are HTTP 200 {"ok": false, "code", "reason"}; a malformed body is 422; PC-only actions answer a phone 403
"Only from the PC itself." (access.from_pc / only_pc, as the agent routes do).

  GET    /api/telegram/status        {ok, configured (a token is set), running (polling now), bot_username (the bot's
                                     @username without the @: BTCFLOW_TELEGRAM_BOT_USERNAME, else Telegram's getMe
                                     once the bot started; null = not known), paired, user_label,
                                     alerts {proposals, trades, activity, pauses, session_end, settle_warnings,
                                     setups, data, daily}, daily_at ("HH:MM", the daily summary's local time)}
  POST   /api/telegram/pair          (PC only) -> {ok, code, expires_ts}: a one-time code for /start <code>, 10 min;
                                     replaces any pending code
  POST   /api/telegram/unpair        (PC only) -> {ok, was_paired}
  POST   /api/telegram/mute          {session_id, muted: bool} (PC or phone) -> {ok, muted: [session ids]}: that
                                     agent's trade and activity alerts off / on (its pauses, end and proposals
                                     still come); an unknown session is refused (code not_found)
  POST   /api/telegram/alerts        {kind: bool, ..., daily_at?: "HH:MM"} (PC only) -> {ok, alerts, daily_at}; an
                                     unknown kind is refused (code unknown_alert), never dropped; a malformed
                                     daily_at is refused (code invalid)
  GET    /api/agent/presets          {ok, presets: [{name, launch, modes, created_ts}]}
  POST   /api/agent/presets          {name, launch} (PC only) -> {ok, preset, replaced} or the launch check's refusal
  DELETE /api/agent/presets/{name}   (PC only) -> {ok}
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, Request
from pydantic import BaseModel

from btc_signal_engine.adapters.http.access import from_pc, only_pc
from btc_signal_engine.application.agent_presets import AgentPresets
from btc_signal_engine.application.notifier import RemoteControl


class MuteBody(BaseModel):
    session_id: Any = None  # checked by RemoteControl.set_muted
    muted: Any = None


class PresetBody(BaseModel):
    name: Any = None  # checked by AgentPresets (1-40 characters)
    launch: Any = None  # the launch body without its confirm phrase; checked by launch's own validation


def register_telegram_routes(app: FastAPI, control: RemoteControl, now_fn: Callable[[], int]) -> None:
    @app.get("/api/telegram/status")
    async def telegram_status() -> dict:
        return control.status()

    @app.post("/api/telegram/pair")
    async def telegram_pair(request: Request):
        if not from_pc(request):
            return only_pc()
        if not control.configured:
            return {"ok": False, "code": "not_configured",
                    "reason": "No Telegram bot token is set (BTCFLOW_TELEGRAM_BOT_TOKEN in .env, then restart)."}
        return control.new_code(now_fn())

    @app.post("/api/telegram/unpair")
    async def telegram_unpair(request: Request):
        if not from_pc(request):
            return only_pc()
        return control.unpair(now_fn())

    @app.post("/api/telegram/mute")
    async def telegram_mute(body: MuteBody):
        return control.set_muted(body.session_id, body.muted, now_fn())

    @app.post("/api/telegram/alerts")
    async def telegram_alerts(request: Request, body: dict[str, Any] = Body(...)):
        if not from_pc(request):
            return only_pc()
        return control.set_alerts(body, now_fn())


def register_preset_routes(app: FastAPI, presets: AgentPresets, now_fn: Callable[[], int]) -> None:
    @app.get("/api/agent/presets")
    async def presets_list() -> dict:
        return {"ok": True, "presets": presets.all()}

    @app.post("/api/agent/presets")
    async def presets_save(body: PresetBody, request: Request):
        if not from_pc(request):
            return only_pc()
        return presets.save(body.name, body.launch, now_fn())

    @app.delete("/api/agent/presets/{name}")
    async def presets_delete(name: str, request: Request):
        if not from_pc(request):
            return only_pc()
        return presets.delete(name, now_fn())
