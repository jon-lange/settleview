"""End to end through the OpenTelemetry Collector (deploy/otel-collector/config.yaml) into OpenObserve: what the
services emit carrying planted secrets (a Telegram bot token in a URL, the span name and status, an API key and a
private key file's path in an exception, an Authorization header, a signature, a password=, a query secret, a phone's
IP address) arrives in OpenObserve redacted, while the service names and the data the SLO alerts read arrive as before:
every alert in deploy/openobserve/alerts applies (alerts.py) and the alerts on per-service data see each service.

Runs only beside a throwaway collector and OpenObserve (tests/telemetry/collector_e2e.compose.yaml has the steps;
compose.yaml's own services, with the real ingest-only token); skipped anywhere else. Each service is a subprocess
running the app's own telemetry.setup with the real OTLP exporters (the SDK's providers are set once per process), so
nothing here goes around what the services do."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.telemetry.conftest import (ANTHROPIC_KEY, BEARER, COOKIE, DB_PASSWORD, KEY_PATH, QUERY_SECRET, SIGNATURE,
                                      TG_TOKEN)

OTLP = os.environ.get("SETTLEVIEW_E2E_OTLP", "")
O2 = os.environ.get("SETTLEVIEW_E2E_O2", "")
pytestmark = [pytest.mark.collector_e2e,
              pytest.mark.skipif(not (OTLP and O2), reason="needs the throwaway collector and OpenObserve "
                                                           "(tests/telemetry/collector_e2e.compose.yaml)")]

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("settleview-app", "settleview-rec-pm-quotes", "settleview-rec-pm-settle", "settleview-migrate")
PHONE_IP = "192.168.1.23"
# Every planted value, as it would have to appear if it leaked (the token's secret half alone too).
LEAKS = (TG_TOKEN, TG_TOKEN.split(":")[1], ANTHROPIC_KEY, KEY_PATH, "kalshi-prod-planted.pem", BEARER.split()[1],
         COOKIE.split("=")[1], SIGNATURE, QUERY_SECRET, DB_PASSWORD, PHONE_IP)

EMIT = textwrap.dedent("""
    import logging, sys
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, StatusCode
    from btc_signal_engine.bootstrap import telemetry
    from btc_signal_engine.bootstrap.logs import log_to_stderr
    from tests.telemetry.conftest import (ANTHROPIC_KEY, BEARER, COOKIE, DB_PASSWORD, KEY_PATH, QUERY_SECRET,
                                          SIGNATURE, TG_TOKEN)
    service, run = sys.argv[1], sys.argv[2]
    log_to_stderr()
    logging.getLogger().handlers.clear()  # nothing on this test's stderr: the export alone is checked
    tel = telemetry.setup(service)
    assert tel.enabled
    lg = logging.getLogger("btc_signal_engine.e2e")
    tracer = trace.get_tracer("e2e")
    with tracer.start_as_current_span(f"GET /bot{TG_TOKEN}/getUpdates", kind=SpanKind.INTERNAL) as span:
        span.set_attribute("e2e.run", run)
        span.set_attribute("url.full", f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset=1&t={QUERY_SECRET}")
        span.set_attribute("url.query", f"pin=1234&t={QUERY_SECRET}")
        span.set_attribute("http.request.header.authorization", BEARER)
        span.set_attribute("http.request.header.cookie", COOKIE)
        span.set_attribute("kalshi.request.signature", SIGNATURE)
        span.set_attribute("settleview.key_file", KEY_PATH)
        span.set_attribute("client.address", "192.168.1.23")
        span.add_event("exception", {"exception.message": f"cannot read {KEY_PATH} with {ANTHROPIC_KEY}"})
        span.set_status(StatusCode.ERROR, f"bot{TG_TOKEN} refused, key {ANTHROPIC_KEY}")
        lg.warning("e2e %s calling https://api.telegram.org/bot%s/getUpdates key file %s with %s password=%s sig %s",
                   run, TG_TOKEN, KEY_PATH, BEARER, DB_PASSWORD, SIGNATURE)
        try:
            raise ConnectionError(f"e2e {run}: {ANTHROPIC_KEY} refused for {KEY_PATH}")
        except ConnectionError:
            lg.exception("e2e %s failed", run)
    if service == "settleview-migrate":
        logging.getLogger("btc_signal_engine.migrate").error("migration failed (e2e %s)", run)
    tel.force_flush(10_000)
    tel.shutdown()
""")


def _auth() -> str:
    pw = os.environ["SETTLEVIEW_E2E_O2_PASSWORD"]
    return "Basic " + base64.b64encode(f"admin@settleview.local:{pw}".encode()).decode("ascii")


def _search(stream_type: str, sql: str, minutes: int = 30) -> list[dict]:
    now = int(time.time() * 1_000_000)
    body = json.dumps({"query": {"sql": sql, "start_time": now - minutes * 60_000_000, "end_time": now + 60_000_000,
                                 "from": 0, "size": 500}}).encode()
    url = f"{O2}/api/default/_search?type={stream_type}"
    headers = {"Authorization": _auth(), "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)  # noqa: S310 - the throwaway's http URL
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - the throwaway OpenObserve's http URL
            return json.loads(r.read())["hits"]
    except urllib.error.HTTPError as e:
        raise AssertionError(f"search {stream_type} failed: HTTP {e.code} {e.read()[:300]!r}") from None


def _wait(stream_type: str, sql: str, want: int = 1, timeout_s: float = 90) -> list[dict]:
    deadline, hits = time.monotonic() + timeout_s, []
    while time.monotonic() < deadline:
        try:
            hits = _search(stream_type, sql)
        except AssertionError:  # the stream does not exist until the first batch lands
            hits = []
        if len(hits) >= want:
            return hits
        time.sleep(2)
    raise AssertionError(f"{stream_type}: {len(hits)} of {want} rows after {timeout_s:.0f} s: {sql}")


@pytest.fixture(scope="module")
def run() -> str:
    """Every service exports once, as itself, through the collector."""
    run = uuid.uuid4().hex[:12]
    env = {**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": OTLP, "OTEL_TRACES_SAMPLER": "always_on",
           "OTEL_METRIC_EXPORT_INTERVAL": "1000", "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=e2e"}
    for service in SERVICES:
        p = subprocess.run([sys.executable, "-c", EMIT, service, run], cwd=ROOT, env=env, capture_output=True,
                           text=True, timeout=120)
        assert p.returncode == 0, p.stderr[-2000:]
    return run


def test_the_collector_reports_healthy():
    with urllib.request.urlopen(os.environ["SETTLEVIEW_E2E_COLLECTOR_HEALTH"], timeout=10) as r:  # noqa: S310 - throwaway
        assert r.status == 200


def test_spans_arrive_redacted_and_named_by_service(run):
    hits = _wait("traces", f"SELECT * FROM \"default\" WHERE e2e_run = '{run}'", want=len(SERVICES))
    assert {h["service_name"] for h in hits} == set(SERVICES)
    text = json.dumps(hits)
    for leak in LEAKS:
        assert leak not in text, leak
    h = hits[0]
    assert h["operation_name"] == "GET /bot****/getUpdates"
    # every query value, also of keys that name nothing secret (the transform processor)
    assert h["url_full"] == "https://api.telegram.org/bot****/getUpdates?offset=****&t=****"
    assert h["url_query"].endswith("&t=****") and "1234" not in h["url_query"]
    assert h["http_request_header_authorization"] == "****" and h["kalshi_request_signature"] == "****"
    assert "client_address" not in h  # deleted (personal data)
    assert h["status_message"] == "bot**** refused, key ****"  # the transform processor
    assert "cannot read **** with ****" in h["events"]  # a span event's attributes


def test_logs_arrive_redacted_and_named_by_service(run):
    hits = _wait("logs", f"SELECT * FROM \"default\" WHERE body LIKE '%e2e {run}%'", want=2 * len(SERVICES))
    assert {h["service_name"] for h in hits} == set(SERVICES)
    text = json.dumps(hits)
    for leak in LEAKS:
        assert leak not in text, leak
    warning = next(h for h in hits if "calling" in h["body"])
    assert "bot****/getUpdates" in warning["body"] and "key file ****" in warning["body"]
    failed = next(h for h in hits if "failed" in h["body"] and h.get("exception_stacktrace"))
    assert "****" in failed["exception_stacktrace"]


def test_metrics_arrive_named_by_service(run):
    hits = _wait("metrics", "SELECT DISTINCT service_name FROM \"process_memory_usage\" "
                            "WHERE deployment_environment_name = 'e2e'", want=len(SERVICES))
    assert {h["service_name"] for h in hits} == set(SERVICES)


def test_the_alerts_apply_and_see_every_service(run, tmp_path, monkeypatch, capsys):
    """alerts.py applies every alert whose stream exists (the rest are reported pending: perps off, no agent); the
    alerts on per-service data, evaluated as OpenObserve does (their own SQL), name each service."""
    def alert(name: str) -> dict:
        return json.loads((ROOT / "deploy" / "openobserve" / "alerts" / f"settleview_{name}.json").read_text("utf-8"))

    _wait("metrics", "SELECT service_name FROM \"process_uptime\"", want=1)
    silent = _search("metrics", alert("service_silent")["query_condition"]["sql"])
    assert silent == []  # every service reported: the critical alert would not fire
    restarted = _search("metrics", alert("service_restart")["query_condition"]["sql"])
    assert set(SERVICES) <= {h["service_name"] for h in restarted}  # each just started: up for under 5 min
    migration = _search("logs", alert("migration_failed")["query_condition"]["sql"])
    assert migration and migration[0]["n"] >= 1  # settleview-migrate's ERROR record, by its service name

    spec = importlib.util.spec_from_file_location("o2_alerts", ROOT / "deploy" / "openobserve" / "alerts.py")
    alerts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alerts)
    # Every alert as committed but disabled, so OpenObserve never evaluates one and never calls Telegram (its SSRF
    # guard accepts only a public destination, so the destination keeps Telegram's URL, with a fake token).
    off = tmp_path / "alerts"
    off.mkdir()
    for f in alerts.ALERTS_DIR.glob("*.json"):
        (off / f.name).write_text(json.dumps({**json.loads(f.read_text("utf-8")), "enabled": False}), "utf-8")
    monkeypatch.setattr(alerts, "ALERTS_DIR", off)
    monkeypatch.setenv("O2_ROOT_PASSWORD", os.environ["SETTLEVIEW_E2E_O2_PASSWORD"])
    monkeypatch.setenv("ALERTS_TELEGRAM_BOT_TOKEN", "0:e2e-not-a-token")
    monkeypatch.setenv("ALERTS_TELEGRAM_CHAT_ID", "1")
    code = alerts.main(["apply", "--url", O2, "--secrets", str(tmp_path)])
    out = capsys.readouterr().out
    assert code in (0, 2), out  # 2: applied, with alerts on streams nothing here exports (perps, the agent) pending
    for name in ("service_silent", "service_restart", "migration_failed"):
        assert f"ok      alert settleview_{name} created" in out or f"settleview_{name} updated" in out, name
