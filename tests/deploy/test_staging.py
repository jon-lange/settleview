"""Staging (P3 #3, deploy/staging/): the production image with no real-money path, by construction at three layers.

1. The settings: BTCFLOW_STAGING refuses every production credential and switch (bootstrap/config.py STAGING_REFUSED),
   from an environment variable or a *_FILE secret alike.
2. The composition root: with staging on, no production gateway exists even if the settings' check were bypassed, so
   production is never "configured": the mode switch, the perps tab and agent sessions cannot reach it.
3. The compose override: no `.env`, no production key file mounted or named, every secret from staging's own folder,
   loopback-only ports, the Polymarket recorder (production keys) off. `docker compose config` resolves it as compose
   does (skipped where the docker CLI is missing; the dev container has it).

The smoke suite checks the running stack again (scripts/smoke.py); here, its read-only HTTP client too."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from btc_signal_engine.application.trading import PROD_CONFIRM
from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import STAGING_REFUSED, Settings

ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "deploy" / "staging" / "compose.staging.yaml"
PROD_SECRETS = ("trading_prod_key_id", "polymarket_key_id", "polymarket_secret_key", "kalshi_key_id",
                "telegram_bot_token")
VALUES = {"trading_enabled": True, "agent_allow_production": True}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("BTCFLOW_"):
            monkeypatch.delenv(k)


# -- 1. the settings ----------------------------------------------------------------------------------------------
def test_staging_alone_starts_and_production_is_off_by_default():
    s = Settings(_env_file=None, staging=True)
    assert s.staging and not s.trading_enabled and s.trading_prod_key_id is None


@pytest.mark.parametrize("name", STAGING_REFUSED)
def test_staging_refuses_every_production_credential_and_switch(name):
    with pytest.raises(ValueError, match=f"BTCFLOW_{name.upper()}"):
        Settings(_env_file=None, staging=True, **{name: VALUES.get(name, "x")})
    Settings(_env_file=None, **{name: VALUES.get(name, "x")})  # without staging: allowed, as before


def test_staging_refuses_a_production_key_from_a_secret_file_as_compose_mounts_it(monkeypatch, tmp_path):
    f = tmp_path / "trading_prod_key_id"
    f.write_text("a-key-id\n", encoding="utf-8")
    monkeypatch.setenv("BTCFLOW_STAGING", "true")
    monkeypatch.setenv("BTCFLOW_TRADING_PROD_KEY_ID_FILE", str(f))
    with pytest.raises(ValueError, match="BTCFLOW_TRADING_PROD_KEY_ID"):
        Settings(_env_file=None)
    f.write_text("", encoding="utf-8")  # an empty file is "not configured" (make_secrets.py writes them empty)
    assert Settings(_env_file=None).staging


def test_the_error_names_settings_never_values():
    with pytest.raises(ValueError) as e:
        Settings(_env_file=None, staging=True, polymarket_secret_key="s3cr3t-value", trading_prod_key_id="id-123")
    assert "s3cr3t-value" not in str(e.value) and "id-123" not in str(e.value)


def test_the_staging_override_sets_staging_on():
    assert 'BTCFLOW_STAGING: "true"' in STAGING.read_text(encoding="utf-8")


# -- 2. the composition root --------------------------------------------------------------------------------------
def _pem(tmp_path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "prod-test-key.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return p


def test_with_staging_on_no_production_gateway_exists_even_past_the_settings_check(tmp_path):
    # A production key and the master switch, as in production; then staging forced on past the validator (as if a
    # future setting slipped through it): the composition root still wires no production gateway.
    s = Settings(_env_file=None, db_path=tmp_path / "s.db", trading_stream=False, trading_enabled=True,
                 trading_prod_key_id="test-id", trading_prod_key_path=_pem(tmp_path), perps_enabled=True)
    prod = Runtime(s)
    try:
        assert prod.trading_gws["prod"] is not None  # the control: without staging it is wired
    finally:
        prod.writer.close()
    object.__setattr__(s, "staging", True)
    rt = Runtime(s)
    try:
        assert rt.trading_gws["prod"] is None
        svc = rt.you_trading
        assert svc.mode != "prod" and not svc._prod_configured()
        out = asyncio.run(svc.set_mode("prod", 1_700_000_000_000, PROD_CONFIRM))
        assert out["ok"] is False and out["code"] == "not_configured"
        assert svc.mode_view(1_700_000_000_000)["modes"]["prod"]["ready"] is False
        assert "perp_prod" not in rt.perp_clients
        assert "staging" in rt.perp_view.modes_unavailable["perp_prod"]
    finally:
        rt.writer.close()


# -- 3. the compose override --------------------------------------------------------------------------------------
def _staging_config(tmp_path: Path, *extra: str) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    (tmp_path / "deploy" / "postgres").mkdir(parents=True)
    (tmp_path / "deploy" / "staging").mkdir(parents=True)
    shutil.copy(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    for f in ("initdb.sh", "roles.sh", "backup.sh"):
        shutil.copy(ROOT / "deploy" / "postgres" / f, tmp_path / "deploy" / "postgres" / f)
    for f in ("compose.staging.yaml", "compose.offline.yaml", "staging.env"):
        shutil.copy(ROOT / "deploy" / "staging" / f, tmp_path / "deploy" / "staging" / f)
    # A production `.env` and production secrets next to it, as on the live host: staging must use neither.
    (tmp_path / ".env").write_text("BTCFLOW_TRADING_ENABLED=true\nOTEL_EXPORTER_OTLP_ENDPOINT=http://prod:4318\n",
                                   encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "openobserve.env").write_text("ZO_ROOT_USER_PASSWORD=dummy\n", encoding="utf-8")
    (tmp_path / "deploy" / "staging" / "secrets").mkdir()
    (tmp_path / "deploy" / "staging" / "secrets" / "openobserve.env").write_text("ZO_ROOT_USER_PASSWORD=dummy\n",
                                                                                 encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BTCFLOW_", "COMPOSE_", "OTEL_", "SETTLEVIEW_"))}
    files = ["-f", "compose.yaml", "-f", "deploy/staging/compose.staging.yaml", *extra]
    p = subprocess.run(["docker", "compose", "-p", "svstgcfg", "--env-file", "deploy/staging/staging.env", *files,
                        "--profile", "observability", "config", "--format", "json"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60, env=clean)
    if p.returncode != 0:
        pytest.fail(f"docker compose config failed: {p.stderr[-2000:]}")
    return json.loads(p.stdout)


def test_the_staging_app_reads_no_env_file_and_names_no_production_key(tmp_path):
    cfg = _staging_config(tmp_path)
    app = cfg["services"]["app"]
    assert not app.get("env_file")
    env = app["environment"]
    assert env["BTCFLOW_STAGING"] == "true"
    assert "BTCFLOW_TRADING_ENABLED" not in env and env["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
    for name in PROD_SECRETS:
        assert f"BTCFLOW_{name.upper()}_FILE" not in env, name
    assert {s["source"] for s in app["secrets"]} == {"pg_app_password", "otlp_headers", "anthropic_api_key",
                                                     "trading_demo_key_id"}
    for svc in ("migrate", "rec-pm-settle"):
        assert cfg["services"][svc]["environment"]["BTCFLOW_STAGING"] == "true", svc


def test_every_staging_secret_comes_from_staging_s_own_folder(tmp_path):
    cfg = _staging_config(tmp_path)
    own = (tmp_path / "deploy" / "staging" / "secrets").resolve()
    for name, s in cfg["secrets"].items():
        assert Path(s["file"]).resolve().parent == own, name
    for name, svc in cfg["services"].items():
        mounted = {s["source"] for s in svc.get("secrets") or []}
        assert not mounted & set(PROD_SECRETS), name
        for f in svc.get("env_file") or []:
            assert Path(f["path"]).resolve().parent == own, name


def test_staging_publishes_loopback_only_and_runs_no_polymarket_recorder(tmp_path):
    cfg = _staging_config(tmp_path)
    assert "rec-pm-quotes" not in cfg["services"]  # profile polymarket-recorder only
    ports = {(p["host_ip"], p["published"], p["target"]) for p in cfg["services"]["app"]["ports"]}
    assert ports == {("127.0.0.1", str(n), n) for n in (18000, 18001, 18002)}
    env = cfg["services"]["app"]["environment"]
    assert (env["BTCFLOW_API_PORT"], env["BTCFLOW_PC_PORT"], env["BTCFLOW_AGENT_PORT"]) == ("18000", "18001", "18002")
    assert {p["host_ip"] for p in cfg["services"]["openobserve"]["ports"]} == {"127.0.0.1"}
    assert cfg["name"] == "svstgcfg"


def test_the_offline_override_cuts_the_network_off(tmp_path):
    cfg = _staging_config(tmp_path, "-f", "deploy/staging/compose.offline.yaml")
    assert cfg["networks"]["default"]["internal"] is True


def test_the_image_is_a_variable_for_the_deploy_gate(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    cfg = _staging_config(tmp_path)
    assert cfg["services"]["app"]["image"] == "settleview:local"
    assert "image: ${SETTLEVIEW_IMAGE:-settleview:local}" in (ROOT / "compose.yaml").read_text(encoding="utf-8")


# -- the smoke suite's read-only client ---------------------------------------------------------------------------
def _smoke():
    import importlib.util
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_smoke_client_refuses_anything_but_get_before_sending(method):
    c = _smoke().ReadOnlyClient("http://127.0.0.1:9")  # nothing listens: a sent request would be a ConnectError
    try:
        with pytest.raises(PermissionError, match="read-only"):
            c.request(method, "/api/trading/mode", json={"mode": "prod"})
    finally:
        c.close()


def test_the_smoke_cookie_is_read_only_and_valid_for_the_app(tmp_path):
    from btc_signal_engine.adapters.pc_credentials import PcCredentials, cookie_from_secret_file
    with pytest.raises(FileNotFoundError):
        cookie_from_secret_file(tmp_path)  # no secret yet: none is made
    assert not any(tmp_path.iterdir())
    pc = PcCredentials(tmp_path)  # the app makes its secret
    before = sorted((p.name, p.stat().st_mtime_ns) for p in tmp_path.iterdir())
    assert pc.valid(cookie_from_secret_file(tmp_path))
    assert sorted((p.name, p.stat().st_mtime_ns) for p in tmp_path.iterdir()) == before


def test_the_smoke_finds_otlp_export_errors():
    rx = _smoke().OTLP_ERROR
    assert rx.search("WARNING opentelemetry.exporter.otlp.proto.http.trace_exporter Failed to export span batch "
                     "code: 401, reason: Unauthorized")
    assert rx.search("Transient error ConnectError encountered while exporting metrics to openobserve:5080")
    assert not rx.search("INFO btc_signal_engine telemetry: exporting traces, metrics and logs to OTLP")
