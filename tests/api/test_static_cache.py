"""The web app's files must be revalidated on every load (no stale script after a deploy)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.app import WEB_DIR, RevalidatingStaticFiles


def test_web_files_are_sent_with_no_cache_and_revalidate():
    app = FastAPI()
    app.mount("/", RevalidatingStaticFiles(directory=WEB_DIR, html=True), name="web")
    c = TestClient(app)
    for path in ("/", "/views/play.js", "/style.css"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
        again = c.get(path, headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304, path
