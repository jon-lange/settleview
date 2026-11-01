"""Test-only stand-ins for routes another worker is building (presets, Telegram), hosted by tests/e2e/server.py so the
Agent tab's browser tests can drive the page against the routes' contract. Each is added only when the app does not
already register the real route (then the real one is used and these stay out of the way).

Contract (as the page codes against it):
- GET /api/agent/presets -> {ok, presets: [{name, launch, created_ts}]}; POST /api/agent/presets {name, launch}
  (PC only); DELETE /api/agent/presets/{name} (PC only).
- GET /api/telegram/status -> {configured, running, paired, user_label, alerts: {proposals, trades, activity,
  pauses, session_end, settle_warnings, setups, data, daily}, daily_at}; POST /api/telegram/pair -> {code, expires_ts};
  POST /api/telegram/unpair; POST /api/telegram/alerts {kind: bool, daily_at?: "HH:MM"}.
`Stubs.off` holds the names ("presets", "telegram") answered 404, as an app without them answers."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request

from btc_signal_engine.adapters.http.access import from_pc, only_pc

ALERT_KINDS = ("proposals", "trades", "activity", "pauses", "session_end", "settle_warnings", "setups", "data",
               "daily")


class Stubs:
    def __init__(self, now_fn: Callable[[], int]) -> None:
        self.now = now_fn
        self.off: set[str] = set()
        self.presets: list[dict] = []
        self.posted: list[dict] = []
        self.telegram = {"configured": True, "running": True, "bot_username": "example_settleview_bot",
                         "paired": False, "user_label": None, "alerts": {k: True for k in ALERT_KINDS},
                         "daily_at": "22:00"}
        self.code = "482913"

    def _on(self, name: str) -> None:
        if name in self.off:
            raise HTTPException(404, "Not Found")

    def install(self, app: FastAPI) -> None:
        have = {getattr(r, "path", None) for r in app.routes}
        if "/api/agent/presets" not in have:
            self._presets(app)
        if "/api/telegram/status" not in have:
            self._telegram(app)

    def _presets(self, app: FastAPI) -> None:
        @app.get("/api/agent/presets")
        async def presets() -> dict:
            self._on("presets")
            return {"ok": True, "presets": self.presets}

        @app.post("/api/agent/presets")
        async def save(request: Request):
            self._on("presets")
            if not from_pc(request):
                return only_pc()
            body = await request.json()
            self.posted.append(body)
            self.presets = [p for p in self.presets if p["name"] != body["name"]] + [
                {"name": body["name"], "launch": body["launch"], "created_ts": self.now()}]
            return {"ok": True}

        @app.delete("/api/agent/presets/{name}")
        async def delete(name: str, request: Request):
            self._on("presets")
            if not from_pc(request):
                return only_pc()
            self.presets = [p for p in self.presets if p["name"] != name]
            return {"ok": True}

    def _telegram(self, app: FastAPI) -> None:
        @app.get("/api/telegram/status")
        async def status() -> dict:
            self._on("telegram")
            return self.telegram

        @app.post("/api/telegram/pair")
        async def pair(request: Request):
            self._on("telegram")
            if not from_pc(request):
                return only_pc()
            return {"code": self.code, "expires_ts": self.now() + 600_000}

        @app.post("/api/telegram/unpair")
        async def unpair(request: Request):
            self._on("telegram")
            if not from_pc(request):
                return only_pc()
            self.telegram = {**self.telegram, "paired": False, "user_label": None}
            return {"ok": True}

        @app.post("/api/telegram/alerts")
        async def alerts(request: Request):
            self._on("telegram")
            if not from_pc(request):
                return only_pc()
            body = await request.json()
            bad = [k for k in body if k not in ALERT_KINDS and k != "daily_at"]
            if bad:
                return {"ok": False, "code": "unknown_alert", "reason": f"Unknown alert {bad[0]!r}."}
            at = body.pop("daily_at", self.telegram["daily_at"])
            self.telegram = {**self.telegram, "alerts": {**self.telegram["alerts"], **body}, "daily_at": at}
            return {"ok": True, "alerts": self.telegram["alerts"], "daily_at": at}
