"""compose.yaml and the Dockerfile (P0 #6 / #7, P2 hardening): every service's logs rotated, every secret a file (no
secret as an environment value, none interpolated into a service), the Python services hardened, the phone port's
host address a variable, the recorders' healthcheck a liveness check, tini as PID 1.

The checks read `docker compose config --format json` (the fully resolved file) run on a copy of compose.yaml in an
empty folder (never next to a real `.env` or `secrets/`); skipped where the docker CLI is missing. A few text checks
run everywhere."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
SECRET_SETTINGS = ("BTCFLOW_POLYMARKET_KEY_ID", "BTCFLOW_POLYMARKET_SECRET_KEY", "BTCFLOW_ANTHROPIC_API_KEY",
                   "BTCFLOW_TELEGRAM_BOT_TOKEN", "BTCFLOW_KALSHI_KEY_ID", "BTCFLOW_TRADING_DEMO_KEY_ID",
                   "BTCFLOW_TRADING_PROD_KEY_ID", "BTCFLOW_DATABASE_PASSWORD", "POSTGRES_PASSWORD", "PGPASSWORD")
PYTHON_SERVICES = ("app", "rec-pm-quotes", "rec-pm-settle")
ALL_SERVICES = ("postgres", "backup", "app", "rec-pm-quotes", "rec-pm-settle", "openobserve")


def _config(tmp_path: Path, **env: str) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    (tmp_path / "deploy" / "postgres").mkdir(parents=True)
    shutil.copy(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    for f in ("initdb.sh", "backup.sh"):
        shutil.copy(ROOT / "deploy" / "postgres" / f, tmp_path / "deploy" / "postgres" / f)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "openobserve.env").write_text("ZO_ROOT_USER_PASSWORD=dummy\n", encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BTCFLOW_", "COMPOSE_", "OTEL_"))}
    p = subprocess.run(["docker", "compose", "-p", "svp0dcfg", "--profile", "observability", "config", "--format",
                        "json"], cwd=tmp_path, capture_output=True, text=True, timeout=60, env={**clean, **env})
    if p.returncode != 0:
        pytest.fail(f"docker compose config failed: {p.stderr[-2000:]}")
    return json.loads(p.stdout)


def test_every_service_logs_with_the_local_driver_and_size_caps(tmp_path):
    services = _config(tmp_path)["services"]
    assert set(services) == set(ALL_SERVICES)
    for name, svc in services.items():
        assert svc.get("logging") == {"driver": "local", "options": {"max-size": "10m", "max-file": "5"}}, name


def test_no_secret_is_an_environment_value_or_interpolated(tmp_path):
    services = _config(tmp_path)["services"]
    for name, svc in services.items():
        env = svc.get("environment") or {}
        for key in SECRET_SETTINGS:
            assert key not in env, f"{name} sets {key} as a value"
    app_env = services["app"]["environment"]
    for key in SECRET_SETTINGS[:7]:
        assert app_env[key + "_FILE"] == "/run/secrets/" + key[len("BTCFLOW_"):].lower()
    q = services["rec-pm-quotes"]
    assert {k for k in q["environment"] if k.startswith("BTCFLOW_")} == {
        "BTCFLOW_DATABASE_URL", "BTCFLOW_DATABASE_PASSWORD_FILE", "BTCFLOW_POLYMARKET_KEY_ID_FILE",
        "BTCFLOW_POLYMARKET_SECRET_KEY_FILE"}
    assert {s["source"] for s in q["secrets"]} == {"pg_app_password", "otlp_headers", "polymarket_key_id",
                                                   "polymarket_secret_key"}
    assert {s["source"] for s in services["rec-pm-settle"]["secrets"]} == {"pg_app_password", "otlp_headers"}
    assert "${BTCFLOW_POLYMARKET" not in COMPOSE  # nothing from `.env` interpolated into a service


def test_the_python_services_are_hardened(tmp_path):
    services = _config(tmp_path)["services"]
    for name in PYTHON_SERVICES:
        svc = services[name]
        assert svc["read_only"] is True, name
        assert svc["cap_drop"] == ["ALL"] and "cap_add" not in svc, name
        assert svc["security_opt"] == ["no-new-privileges:true"], name
        assert {t.split(":")[0] for t in svc["tmpfs"]} == {"/tmp", "/home/app"}, name
        assert "init" not in svc  # tini is the image's PID 1
    pg = services["postgres"]
    assert pg["read_only"] is True and pg["cap_drop"] == ["ALL"]
    assert set(pg["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    for name in ("postgres", "backup", "openobserve"):
        assert services[name]["security_opt"] == ["no-new-privileges:true"], name


def test_the_phone_port_host_address_is_a_variable_and_the_pc_and_agent_ports_stay_loopback(tmp_path):
    def ports(cfg):
        return {p["target"]: p.get("host_ip") for p in cfg["services"]["app"]["ports"]}

    assert ports(_config(tmp_path / "a")) == {8000: "0.0.0.0", 8001: "127.0.0.1", 8002: "127.0.0.1"}
    assert ports(_config(tmp_path / "b", BTCFLOW_PHONE_BIND="192.168.1.20")) == {
        8000: "192.168.1.20", 8001: "127.0.0.1", 8002: "127.0.0.1"}


def test_the_recorders_healthcheck_is_process_local_liveness(tmp_path):
    services = _config(tmp_path)["services"]
    assert services["rec-pm-quotes"]["healthcheck"]["test"][:5] == ["CMD", "python", "-m", "btc_signal_engine.ops",
                                                                    "alive"]
    assert services["rec-pm-settle"]["healthcheck"]["test"][4:6] == ["alive", "pm-settle"]


def test_text_checks():
    assert "x-logging: &logging" in COMPOSE and COMPOSE.count("logging: *logging") == 4  # x-service + 3 services
    assert '"${BTCFLOW_PHONE_BIND:-0.0.0.0}:8000:8000"' in COMPOSE and '"127.0.0.1:8001:8001"' in COMPOSE
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in DOCKERFILE and "apt-get install -y --no-install-recommends tini" \
        in DOCKERFILE
    for setting in SECRET_SETTINGS[:7]:
        assert f"{setting}:" not in COMPOSE
