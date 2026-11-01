"""The smoke suite (tests/smoke/conftest.py): is a running Settleview stack whole? Read-only; skipped unless
scripts/smoke.py runs it against a stack."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.smoke.conftest import SAFE, ReadOnlyClient, env_of

# Production credentials' secret files: never mounted in a staging stack (deploy/staging/compose.staging.yaml).
PROD_SECRET_FILES = ("trading_prod_key_id", "polymarket_key_id", "polymarket_secret_key", "kalshi_key_id",
                     "telegram_bot_token")
# The OpenTelemetry SDK's own lines when an export fails (opentelemetry.exporter.otlp.proto.http and the SDK's batch
# processors).
OTLP_ERROR = re.compile(r"(?i)(failed to export|exception while exporting|transient error .* exporting)")
STRATEGIES = ("you", "you_poly", "you_poly5")  # the Play tabs' trading services (domain/markets.py)
VIEWS = ("play", "record", "agent", "perps")


# -- the stack as Docker sees it --------------------------------------------------------------------------------------
def test_the_migrate_job_exited_0(stack):
    st = stack["services"]["migrate"]["State"]
    assert not st["Running"] and st["ExitCode"] == 0, f"migrate: {st['Status']}, exit {st['ExitCode']}"


def test_every_long_running_service_is_healthy(stack):
    bad = {name: (c["State"].get("Health") or {}).get("Status", c["State"]["Status"])
           for name, c in stack["services"].items() if name != "migrate"}
    assert set(bad) >= {"postgres", "app"}
    assert {k: v for k, v in bad.items() if v != "healthy"} == {}


def test_no_otlp_export_error(stack):
    for name, c in stack["services"].items():
        if name == "app" or name.startswith("rec-"):
            if not env_of(c).get("OTEL_EXPORTER_OTLP_ENDPOINT"):
                continue  # no exporter configured
            bad = [ln for ln in stack["logs"].get(name, "").splitlines() if OTLP_ERROR.search(ln)]
            assert not bad, f"{name}: {len(bad)} export error line(s), e.g. {bad[0][:200]}"


def test_staging_mounts_and_names_no_production_key(stack, staging):
    if not staging:
        pytest.skip("not a staging stack")
    for name, c in stack["services"].items():
        mounted = {Path(m["Destination"]).name for m in c.get("Mounts") or []
                   if str(m.get("Destination", "")).startswith("/run/secrets/")}
        assert not mounted & set(PROD_SECRET_FILES), name
        named = [k for k in env_of(c) if k.removesuffix("_FILE").lower().removeprefix("btcflow_") in PROD_SECRET_FILES]
        assert not named, name


# -- the app's listeners ----------------------------------------------------------------------------------------------
def test_health_on_the_phone_listener_says_only_up(ports):
    c = ReadOnlyClient(f"http://localhost:{ports['api']}")
    try:
        r = c.get("/api/health")
        assert r.status_code == 200 and r.json() == {"status": "up"}
    finally:
        c.close()


def test_full_health_on_the_pc_listener(pc):
    r = pc.get("/api/health")
    assert r.status_code == 200
    h = r.json()
    # `degraded` parts (an offline staging has no venue feed) are reported, not failed: the app itself is up.
    assert h["status"] in ("live", "degraded") and h["loops"], h


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_trading_status_and_no_production_on_staging(pc, staging, strategy):
    t = pc.get("/api/trading/status", params={"strategy": strategy}).json()
    if t.get("code") == "unknown_strategy":
        pytest.skip(f"{strategy} is not wired on this server")
    assert "mode" in t
    if staging:
        assert t["enabled"] is False and t["mode"] != "prod"
        assert (t["modes"].get("prod") or {}).get("ready") is not True


def test_the_play_stream_sends_a_frame(pc):
    event = None
    with pc.stream("/api/play/stream", params={"venue": "kalshi", "span": "15m"}, timeout=20) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                assert event in ("view", "stale") and "seq" in json.loads(line[5:])
                return
    pytest.fail("the stream closed without a frame")


def test_the_agent_api_lists_sessions(pc):
    r = pc.get("/api/agent/sessions")
    if r.status_code == 404:
        pytest.skip("agent mode is off on this server")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and isinstance(body["sessions"], list)


def test_the_agent_listener_serves_its_tool_api_only(ports):
    if ports["agent"] is None:
        pytest.skip("no agent listener")
    c = ReadOnlyClient(f"http://localhost:{ports['agent']}")
    try:
        assert c.get("/api/health").status_code == 404
        assert c.get("/api/agent/tools").status_code in (200, 401, 403, 404)
    finally:
        c.close()


# -- the screens, in headless Chromium --------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def browser_ctx(ports, pc_cookie):
    from playwright import sync_api

    from btc_signal_engine.adapters.pc_credentials import PC_COOKIE
    base = f"http://localhost:{ports['pc']}"
    blocked: list[str] = []

    def guard(route) -> None:
        req = route.request
        if req.method in SAFE and req.url.startswith(base + "/"):
            route.continue_()
        else:
            blocked.append(f"{req.method} {req.url.removeprefix(base)}")
            route.abort()

    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(base_url=base)
        ctx.add_cookies([{"name": PC_COOKIE, "value": pc_cookie, "url": base, "httpOnly": True,
                          "sameSite": "Strict"}])
        ctx.route("**/*", guard)
        ctx.set_default_timeout(15_000)
        yield ctx, blocked
        ctx.close()
        browser.close()
    print(f"browser: {len(blocked)} non-GET or foreign request(s) aborted: {sorted({b.split('?')[0] for b in blocked})}")


@pytest.mark.parametrize("view", VIEWS)
def test_the_screen_loads(browser_ctx, view):
    ctx, _ = browser_ctx
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)[:200]))
    try:
        page.goto(f"/#{view}", wait_until="domcontentloaded")
        page.locator(f"#panel-{view}").wait_for(state="visible")
        page.wait_for_timeout(1_500)  # its first reads and the live stream
        assert not errors
    finally:
        page.close()
