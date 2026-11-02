"""The dev container (.devcontainer/devcontainer.json on deploy/dev/compose.yaml): its own compose project, never the
live `settleview`; no published or forwarded port; data/ read-only in every service; the research service's checkout
read-only with only its output folders writable; the test Postgres on tmpfs; no `.env` or secret read.

The compose checks read `docker compose config --format json` of the real file (it resolves paths, it creates
nothing); skipped where the docker CLI is missing. The devcontainer.json checks run everywhere."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEV_COMPOSE = ROOT / "deploy" / "dev" / "compose.yaml"
DEVCONTAINER = ROOT / ".devcontainer" / "devcontainer.json"
DEV_SERVICES = ("dev", "dev-pg", "research", "evals")


def _devcontainer() -> dict:
    # JSON with comments (the spec's format): this file keeps its comments on lines of their own.
    text = "\n".join(line for line in DEVCONTAINER.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("//"))
    return json.loads(text)


def _config() -> dict:
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("BTCFLOW_", "COMPOSE_", "SETTLEVIEW_", "ANTHROPIC_"))}
    p = subprocess.run(["docker", "compose", "-f", str(DEV_COMPOSE), "--profile", "*", "config", "--format", "json"],
                       capture_output=True, text=True, timeout=60, env=clean)
    if p.returncode != 0:
        pytest.fail(f"docker compose config failed: {p.stderr[-2000:]}")
    return json.loads(p.stdout)


def _mounts(svc: dict) -> dict[str, dict]:
    return {v["target"]: v for v in svc.get("volumes", [])}


def test_devcontainer_uses_the_dev_compose_service_and_forwards_no_port():
    dc = _devcontainer()
    assert dc["dockerComposeFile"] == ["../deploy/dev/compose.yaml", "compose.yaml"]  # the service, then the overlay
    assert dc["service"] == "dev" and dc["runServices"] == ["dev"] and dc["workspaceFolder"] == "/app"
    assert dc["overrideCommand"] is True  # the service's own command runs the suite once and exits
    assert dc["remoteUser"] == "dev" and dc["updateRemoteUserUID"] is True  # the host user's uid on Linux
    assert not {"forwardPorts", "appPort", "portsAttributes", "mounts", "runArgs"} & set(dc)
    assert dc["otherPortsAttributes"] == {"onAutoForward": "ignore"}


def test_the_overlay_only_makes_up_run_the_image_the_cli_built():
    """`updateRemoteUserUID` points `up` at an image with `dev` remapped to the host uid; the base file's
    `pull_policy: build` would rebuild the service under that name (uid 10001 again: CI could not write the checkout)."""
    overlay = (ROOT / ".devcontainer" / "compose.yaml").read_text(encoding="utf-8")
    body = [line for line in overlay.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert body == ["services:", "  dev:", "    pull_policy: never"]
    assert "pull_policy: build" in DEV_COMPOSE.read_text(encoding="utf-8")  # plain `docker compose run` builds first


def test_own_project_no_port_no_env_file():
    cfg = _config()
    assert cfg["name"] == "settleview-dev"
    assert set(cfg["services"]) == {"dev", "postgres", "dev-pg", "research", "evals"}
    for name, svc in cfg["services"].items():
        assert "ports" not in svc and "env_file" not in svc and "secrets" not in svc, name
        assert svc.get("network_mode") is None and not svc.get("privileged"), name
        for v in svc.get("volumes", []):
            assert "docker.sock" not in v.get("source", ""), name


def test_data_is_read_only_everywhere_and_research_writes_only_its_outputs():
    services = _config()["services"]
    for name in DEV_SERVICES:
        m = _mounts(services[name])
        assert m["/app/data"]["read_only"] is True, name
        assert Path(m["/app/data"]["source"]).name == "data", name
    for name in ("dev", "dev-pg", "evals"):
        assert not _mounts(services[name])["/app"].get("read_only"), name
    r = _mounts(services["research"])
    assert r["/app"]["read_only"] is True
    writable = {t for t, v in r.items() if v["type"] == "bind" and not v.get("read_only")}
    assert writable == {"/app/docs/research", "/app/data/research"}


def test_test_postgres_is_throwaway_and_only_dev_pg_sees_it():
    services = _config()["services"]
    pg = services["postgres"]
    assert pg["profiles"] == ["postgres"] and pg["image"].startswith("postgres:17.")
    assert "/var/lib/postgresql/data" in pg["tmpfs"] and not pg.get("volumes")
    for name in DEV_SERVICES:
        has = "BTCFLOW_TEST_DATABASE_URL" in services[name]["environment"]
        assert has == (name == "dev-pg"), name


def test_only_the_evals_service_passes_the_anthropic_key_and_by_name():
    services = _config()["services"]
    for name in DEV_SERVICES:
        env = services[name]["environment"]
        assert ("ANTHROPIC_API_KEY" in env) == (name == "evals"), name
    assert services["evals"]["environment"]["ANTHROPIC_API_KEY"] is None  # unset in the test env: no value
