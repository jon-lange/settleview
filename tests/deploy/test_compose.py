"""compose.yaml and the Dockerfile (P0 #6 / #7, P2 hardening): every service's logs rotated, every secret a file (no
secret as an environment value, none interpolated into a service), the Python services hardened, the phone port's
host address a variable, the recorders' healthcheck a liveness check, tini as PID 1.

The checks read `docker compose config --format json` (the fully resolved file) run on a copy of compose.yaml in an
empty folder (never next to a real `.env` or `secrets/`); skipped where the docker CLI is missing. A few text checks
run everywhere."""

from __future__ import annotations

import ipaddress
import json
import os
import re
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
PYTHON_SERVICES = ("migrate", "app", "rec-pm-quotes", "rec-pm-settle")
ALL_SERVICES = ("postgres", "backup", "migrate", "app", "rec-pm-quotes", "rec-pm-settle", "otel-collector", "openobserve",
                "tailscale")
SERVE = json.loads((ROOT / "deploy" / "tailscale" / "serve.json").read_text(encoding="utf-8"))


def _config(tmp_path: Path, **env: str) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    (tmp_path / "deploy" / "postgres").mkdir(parents=True)
    (tmp_path / "deploy" / "tailscale").mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "tailscale" / "serve.json", tmp_path / "deploy" / "tailscale" / "serve.json")
    shutil.copy(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    for f in ("initdb.sh", "roles.sh", "audit.sql", "backup.sh"):
        shutil.copy(ROOT / "deploy" / "postgres" / f, tmp_path / "deploy" / "postgres" / f)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "openobserve.env").write_text("ZO_ROOT_USER_PASSWORD=dummy\n", encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BTCFLOW_", "COMPOSE_", "OTEL_"))}
    p = subprocess.run(["docker", "compose", "-p", "svp0dcfg", "--profile", "observability", "--profile", "tailscale",
                        "config", "--format", "json"], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                       env={**clean, **env})
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
    assert {s["source"] for s in q["secrets"]} == {"pg_recorder_password", "polymarket_key_id", "polymarket_secret_key"}
    assert {s["source"] for s in services["rec-pm-settle"]["secrets"]} == {"pg_recorder_password"}
    assert {s["source"] for s in services["migrate"]["secrets"]} == {"pg_app_password"}
    assert services["migrate"]["environment"]["OTEL_SERVICE_NAME"] == "settleview-migrate"  # migration_failed alert
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
    for name in ("postgres", "backup", "openobserve", "otel-collector"):
        assert services[name]["security_opt"] == ["no-new-privileges:true"], name


def test_the_collector_is_opt_in_pinned_unprivileged_and_alone_holds_the_backend_token(tmp_path):
    services = _config(tmp_path)["services"]
    c = services["otel-collector"]
    assert c["profiles"] == ["observability"] == services["openobserve"]["profiles"]
    assert re.fullmatch(r"otel/opentelemetry-collector-contrib:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", c["image"])
    assert c["read_only"] is True and c["cap_drop"] == ["ALL"] and "cap_add" not in c
    assert not c.get("ports")  # the services reach it as otel-collector:4318, inside the network only
    assert [s["source"] for s in c["secrets"]] == ["openobserve_ingest_token"]
    assert [x["target"] for x in c["configs"]] == ["/etc/otelcol-contrib/config.yaml"]
    assert c["healthcheck"]["test"][:3] == ["CMD", "/otelcol-contrib", "validate"]
    limits = c["deploy"]["resources"]["limits"]
    assert limits["memory"] and limits["cpus"]
    for name, svc in services.items():  # no other service holds a backend credential
        if name != "otel-collector":
            assert "openobserve_ingest_token" not in {s["source"] for s in svc.get("secrets") or ()}, name
            assert "OTEL_EXPORTER_OTLP_HEADERS_FILE" not in (svc.get("environment") or {}), name
    cfg = (ROOT / "deploy" / "otel-collector" / "config.yaml").read_text(encoding="utf-8")
    for part in ("memory_limiter:", "redaction:", "transform:", "batch:", "health_check:", "basicauth/openobserve:"):
        assert part in cfg, part


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
    assert "x-logging: &logging" in COMPOSE and COMPOSE.count("logging: *logging") == 6  # x-service + 5 services
    assert '"${BTCFLOW_PHONE_BIND:-0.0.0.0}:8000:8000"' in COMPOSE and '"127.0.0.1:8001:8001"' in COMPOSE
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in DOCKERFILE and "apt-get install -y --no-install-recommends tini" \
        in DOCKERFILE
    for setting in SECRET_SETTINGS[:7]:
        assert f"{setting}:" not in COMPOSE


def test_the_recorders_connect_as_the_recorder_role_and_the_app_as_the_app_role(tmp_path):
    """Least privilege (B4#4): the recorders' role owns only the schema research (deploy/postgres/roles.sh); only
    the app and the migrate job hold the app role's password."""
    services = _config(tmp_path)["services"]
    for name in ("rec-pm-quotes", "rec-pm-settle"):
        env = services[name]["environment"]
        assert env["BTCFLOW_DATABASE_URL"] == "postgresql://settleview_recorder@postgres:5432/settleview", name
        assert env["BTCFLOW_DATABASE_PASSWORD_FILE"] == "/run/secrets/pg_recorder_password", name
    holders = {n for n, svc in services.items() if "pg_app_password" in {x["source"] for x in svc.get("secrets", [])}}
    assert holders == {"postgres", "migrate", "app"}
    pg = services["postgres"]
    assert {x["source"] for x in pg["secrets"]} >= {"pg_recorder_password"}
    assert {c["target"] for c in pg["configs"]} == {"/docker-entrypoint-initdb.d/10-settleview.sh",
                                                     "/docker-entrypoint-initdb.d/20-roles.sh",
                                                     "/docker-entrypoint-initdb.d/30-audit.sql"}


def test_migrations_run_as_a_one_off_job_the_app_waits_for(tmp_path):
    services = _config(tmp_path)["services"]
    m = services["migrate"]
    assert m["command"] == ["python", "-m", "btc_signal_engine.ops", "migrate"] and m["restart"] == "no"
    assert m["deploy"]["restart_policy"]["condition"] == "on-failure"  # Swarm: a one-shot task
    app = services["app"]
    assert app["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert app["environment"]["BTCFLOW_DB_MIGRATE"] == "check"  # and never migrates itself
    for name in ("rec-pm-quotes", "rec-pm-settle"):
        assert "migrate" not in services[name]["depends_on"], name  # their own tables; trading's schema is not theirs


def test_the_python_services_run_a_local_build_or_the_digest_in_settleview_image(tmp_path):
    """P3 #4: deploy by digest. Unset, every Python service runs the local build; set, all of them run that image."""
    (tmp_path / "default").mkdir()
    services = _config(tmp_path / "default")["services"]
    assert {services[s]["image"] for s in PYTHON_SERVICES} == {"settleview:local"}
    ref = "ghcr.io/jon-lange/settleview@sha256:" + "e" * 64
    (tmp_path / "pinned").mkdir()
    services = _config(tmp_path / "pinned", SETTLEVIEW_IMAGE=ref)["services"]
    assert {services[s]["image"] for s in PYTHON_SERVICES} == {ref}
    assert services["postgres"]["image"] == services["backup"]["image"] == "postgres:17.10-bookworm"


def test_the_tailscale_sidecar_is_opt_in_pinned_and_unprivileged(tmp_path):
    """P3 #1: the official image by digest, profile `tailscale`, userspace networking (no tun device, no capability),
    a read-only root, the auth key only as a secret file, nothing published on the host."""
    ts = _config(tmp_path)["services"]["tailscale"]
    assert ts["profiles"] == ["tailscale"]
    image = ts["image"]
    assert image.startswith("tailscale/tailscale:v") and re.search(r"@sha256:[0-9a-f]{64}$", image), image
    assert ts["read_only"] is True and ts["cap_drop"] == ["ALL"] and "cap_add" not in ts
    assert ts["security_opt"] == ["no-new-privileges:true"]
    assert "devices" not in ts and "privileged" not in ts and "network_mode" not in ts and "ports" not in ts
    env = ts["environment"]
    assert env["TS_USERSPACE"] == "true" and env["TS_HOSTNAME"] == "settleview" and ts["hostname"] == "settleview"
    assert env["TS_AUTHKEY"] == "file:/run/secrets/ts_authkey" and env["TS_AUTH_ONCE"] == "true"
    assert env["TS_STATE_DIR"] == "/var/lib/tailscale" and env["TS_SERVE_CONFIG"] == "/config/serve.json"
    assert env["TS_LOCAL_ADDR_PORT"].startswith("127.0.0.1:")  # the health endpoint never leaves the container
    assert not any(k in env for k in ("TS_EXTRA_ARGS", "TS_TAILSCALED_EXTRA_ARGS", "TS_ROUTES", "TS_SOCKS5_SERVER",
                                      "TS_OUTBOUND_HTTP_PROXY_LISTEN"))
    assert [x["source"] for x in ts["secrets"]] == ["ts_authkey"]
    assert [(c["source"], c["target"]) for c in ts["configs"]] == [("ts_serve", "/config/serve.json")]
    assert {v["target"] for v in ts["volumes"]} == {"/var/lib/tailscale"}
    assert ts["logging"]["driver"] == "local" and ts["deploy"]["resources"]["limits"]["memory"]


def test_only_the_app_and_the_sidecar_share_the_internal_edge_network_at_a_fixed_address(tmp_path):
    cfg = _config(tmp_path)
    edge = cfg["networks"]["tailscale-edge"]
    assert edge["internal"] is True
    (pool,) = edge["ipam"]["config"]
    sidecar = cfg["services"]["tailscale"]["networks"]["tailscale-edge"]["ipv4_address"]
    subnet, dynamic = ipaddress.ip_network(pool["subnet"]), ipaddress.ip_network(pool["ip_range"])
    assert ipaddress.ip_address(sidecar) in subnet and ipaddress.ip_address(sidecar) not in dynamic  # never handed out
    assert ipaddress.ip_address(pool["gateway"]) not in dynamic and ipaddress.ip_address(pool["gateway"]) !=         ipaddress.ip_address(sidecar)
    on_edge = {n for n, svc in cfg["services"].items() if "tailscale-edge" in (svc.get("networks") or {})}
    assert on_edge == {"app", "tailscale"}
    on_egress = {n for n, svc in cfg["services"].items() if "tailscale-egress" in (svc.get("networks") or {})}
    assert on_egress == {"tailscale"}
    assert set(cfg["services"]["app"]["networks"]) == {"default", "tailscale-edge"}
    # proxy headers are opt-in: compose never sets the trusted address itself (the owner's .env does)
    assert "BTCFLOW_FORWARDED_ALLOW_IPS" not in cfg["services"]["app"]["environment"]
    assert f"BTCFLOW_FORWARDED_ALLOW_IPS={sidecar}" in COMPOSE  # the documented value is the sidecar's address


def test_the_serve_config_publishes_https_443_to_the_phone_port_and_never_funnel():
    assert SERVE["TCP"] == {"443": {"HTTPS": True}}
    assert SERVE["Web"] == {"${TS_CERT_DOMAIN}:443": {"Handlers": {"/": {"Proxy": "http://app:8000"}}}}
    assert not any(SERVE.get("AllowFunnel", {}).values())
    assert set(SERVE) <= {"TCP", "Web", "AllowFunnel"}  # no Services, no Foreground

