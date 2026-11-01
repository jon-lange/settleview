"""FastAPI delivery adapter: the HTTP API (docs/api.md) plus the static Play app (web/)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from btc_signal_engine.adapters.http.access import human

WEB_DIR = Path(__file__).resolve().parents[3] / "web"


@dataclass
class Services:
    store: Any  # SqliteStore (reads)
    feed: Callable[[], dict]
    # The app's other health parts (bootstrap reads them): streams, writer_pending, writer_failed_batches, loops,
    # stalls_last_hour, uptime_s, memory_mb. Each stream is {"stale": bool, ...}.
    health: Callable[[], dict] | None = None
    extra_routes: Callable[[FastAPI], None] | None = None


def create_app(svc: Services) -> FastAPI:
    app = FastAPI(title="Settleview", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/api/health")
    def health(request: Request) -> dict:
        """Operations check (not used by Play; docs/operations.md): the Coinbase feed (`feed`, lag, clock offset), open
        data gaps, each venue stream, the writer backlog, each loop's heartbeat, stalls in the last hour, uptime and
        memory. `status` is `live`, or `degraded` when any feed or stream is not live or a failed writer batch waits
        for a retry; `degraded` names those parts. Without a PC or phone session (the container healthcheck, anyone
        who passes the Host check) only {status}: no details and no database query."""
        if not human(request):
            return {"status": "up"}
        feed = svc.feed()
        feed.pop("price", None)
        parts = svc.health() if svc.health is not None else {}
        bad = ([] if feed["status"] == "live" else ["coinbase"]) + sorted(
            name for name, st in parts.get("streams", {}).items() if st.get("stale")) + (
            ["writer"] if parts.get("writer_failed_batches") else [])
        try:  # the only database read here (bounded by the statement timeout); an outage is a part, never a 500
            gaps: int | None = len(svc.store.select("gaps", {"repaired": False}))
        except Exception:
            gaps = None
            bad.append("database")
        return {**feed, "feed": feed["status"], "gaps_open": gaps,
                **parts, "status": "degraded" if bad else "live", "degraded": bad}

    if svc.extra_routes:
        svc.extra_routes(app)
    if WEB_DIR.exists():
        app.mount("/", RevalidatingStaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


class RevalidatingStaticFiles(StaticFiles):
    """The web app's files, sent with `Cache-Control: no-cache`: a browser keeps its copy but asks every time (a cheap
    304 via the ETag when unchanged). Without it, browsers guessed a freshness time and a phone ran a stale script
    against a newer page after a deploy (2026-10-29: the page hung until opened in a private window)."""

    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
