"""deploy/openobserve/ingest_token.py (review B4#14): the collector's OpenObserve credential is the ingest-only
organization token, never the root login. Against a fake OpenObserve on a local port (checked against the real
v1.1.1 image on 2026-11-01: the token is accepted by /v1/logs and refused by /streams, /users and /_search)."""

from __future__ import annotations

import base64
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ingest_token", ROOT / "deploy" / "openobserve" / "ingest_token.py")
it = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(it)

ROOT_PW, TOKEN, NEW_TOKEN = "Root-Pass-1x", "o2oi_token_one", "o2oi_token_two"


class FakeO2:
    def __init__(self, reads_with_token: int = 401) -> None:
        self.token = TOKEN
        self.reads_with_token = reads_with_token
        o2 = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b""):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _who(self):
                raw = (self.headers.get("Authorization") or "").removeprefix("Basic ")
                return base64.b64decode(raw).decode().split(":", 1) if raw else ["", ""]

            def _passcode(self, rotate):
                user, pw = self._who()
                if (user, pw) != (it.ROOT_EMAIL, ROOT_PW):
                    return self._send(401)
                if rotate:
                    o2.token = NEW_TOKEN
                self._send(200, json.dumps({"data": {"passcode": o2.token, "user": user}}).encode())

            def do_GET(self):  # noqa: N802
                if self.path == "/api/default/passcode":
                    return self._passcode(False)
                if self.path == "/api/default/streams":
                    return self._send(o2.reads_with_token if self._who()[1] == o2.token else 401)
                self._send(404)

            def do_PUT(self):  # noqa: N802
                self._passcode(self.path == "/api/default/passcode")

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                ok = self.path == "/api/default/v1/logs" and self._who()[1] == o2.token
                self._send(200 if ok else 401)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def o2():
    f = FakeO2()
    yield f
    f.close()


def _secrets(tmp_path: Path, pw: str = ROOT_PW) -> Path:
    d = tmp_path / "secrets"
    d.mkdir()
    (d / "openobserve.env").write_text(f"ZO_ROOT_USER_PASSWORD={pw}\n", encoding="ascii")
    (d / it.INGEST_TOKEN).write_text("", encoding="ascii")  # make_secrets.py's empty file
    return d


def _token(d: Path) -> str:
    return (d / it.INGEST_TOKEN).read_text(encoding="ascii")


def test_the_ingest_token_is_written_for_the_collector_and_nothing_is_printed(o2, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("O2_ROOT_PASSWORD", raising=False)
    d = _secrets(tmp_path)
    assert it.main(["--url", o2.url, "--secrets", str(d)]) == 0
    assert _token(d) == TOKEN  # the token alone: the collector's basicauth password_file
    assert it.main(["--url", o2.url, "--secrets", str(d)]) == 0  # idempotent
    out = capsys.readouterr().out
    assert "kept" in out and "HTTP 401 (want 401" in out
    assert TOKEN not in out and ROOT_PW not in out
    assert it.main(["--url", o2.url, "--secrets", str(d), "--rotate"]) == 0
    assert _token(d) == NEW_TOKEN


def test_a_wrong_root_password_or_a_token_that_can_read_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("O2_ROOT_PASSWORD", raising=False)
    d = _secrets(tmp_path, pw="wrong")
    before = _token(d)
    f = FakeO2()
    try:
        assert it.main(["--url", f.url, "--secrets", str(d)]) == 1
    finally:
        f.close()
    monkeypatch.setenv("O2_ROOT_PASSWORD", ROOT_PW)  # the Swarm form: from the deploying shell
    f = FakeO2(reads_with_token=200)  # a backend where the token is not ingest-only
    try:
        assert it.main(["--url", f.url, "--secrets", str(d)]) == 1
    finally:
        f.close()
    assert _token(d) == before


def test_make_secrets_no_longer_derives_the_header_from_the_root_password(tmp_path):
    spec = importlib.util.spec_from_file_location("make_secrets", ROOT / "deploy" / "postgres" / "make_secrets.py")
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)
    assert ms.main(["make_secrets", str(tmp_path)]) == 0
    assert (tmp_path / "openobserve_ingest_token").read_text() == ""  # empty until ingest_token.py runs
    assert not (tmp_path / "otlp_headers").exists()  # the services hold no backend credential any more
    assert (tmp_path / "pg_recorder_password").stat().st_size > 20
    assert it.ROOT_EMAIL in (ROOT / "compose.yaml").read_text(encoding="utf-8")  # ZO_ROOT_USER_EMAIL's literal


def test_the_collector_sends_the_label_and_reads_the_file_this_script_writes():
    cfg = (ROOT / "deploy" / "otel-collector" / "config.yaml").read_text(encoding="utf-8")
    assert f"username: {it.INGEST_LABEL}" in cfg
    assert f"password_file: /run/secrets/{it.INGEST_TOKEN}" in cfg
    assert f"file: ./secrets/{it.INGEST_TOKEN}" in (ROOT / "compose.yaml").read_text(encoding="utf-8")
