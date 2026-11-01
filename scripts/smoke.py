"""Run the deploy gate's smoke suite (tests/smoke, pytest + Playwright) against a running stack, READ-ONLY, so it runs
against staging and, right after a deploy, against production (docs/operations.md, "Staging and the deploy gate").

    docker compose -f deploy/dev/compose.yaml build dev     # the dev image the suite runs in (cached)
    python scripts/smoke.py --project settleview-staging

Standard library only. On the host it only asks Docker: `docker ps` and `docker inspect` of the project's containers
and `docker logs --since 30m` of the app and the recorders, written to a temporary folder. Then the suite runs in the
dev image (pytest, httpx, Playwright's Chromium) in a throwaway container that shares the app container's network
namespace (`--network container:<app>`): it reaches the app's own listeners at localhost:<port> with no port
published (an offline staging has none) and passes the Host check as this PC does. Mounted read-only: that folder, the
app's data volume (the PC secret: the suite signs a cookie and writes nothing) and the checkout; read-only root, no
capabilities. The suite sends GET/HEAD only (tests/smoke/conftest.py). Exit code: pytest's.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV_IMAGE = "settleview-dev:local"  # deploy/dev/compose.yaml's image (`docker compose ... build dev`)
APP_UID = "10001:10001"  # the app's user (Dockerfile): the PC secret file is readable by it only
LOG_WINDOW = "30m"


def docker_env() -> dict[str, str]:
    # Git Bash rewrites absolute container paths into Windows paths (docs/operations.md, "Deploying"): never here.
    return {**{k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")},
            "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}


def docker(*args: str, capture: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    exe = shutil.which("docker")
    if exe is None:
        raise SystemExit("smoke: docker is not on PATH")
    return subprocess.run([exe, *args], capture_output=capture, text=True, encoding="utf-8",  # noqa: S603
                          errors="replace", timeout=timeout, env=docker_env())


def containers(project: str) -> dict[str, dict]:
    """service -> `docker inspect` of its container (compose one-off `run` containers are not services)."""
    ps = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                "--filter", "label=com.docker.compose.oneoff=False", "--format", "{{.ID}}")
    if ps.returncode != 0:
        raise SystemExit(f"smoke: docker ps failed: {ps.stderr.strip()[:300]}")
    if not ps.stdout.split():
        return {}
    got = docker("inspect", *ps.stdout.split())
    if got.returncode != 0:
        raise SystemExit(f"smoke: docker inspect failed: {got.stderr.strip()[:300]}")
    return {c["Config"]["Labels"].get("com.docker.compose.service", c["Name"].lstrip("/")): c
            for c in json.loads(got.stdout)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The deploy gate's read-only smoke suite against a running stack.")
    ap.add_argument("--project", required=True, help="the compose project, e.g. settleview-staging")
    ap.add_argument("--image", default=DEV_IMAGE, help="the image the suite runs in")
    ap.add_argument("pytest_args", nargs="*", help="extra pytest arguments (after --)")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", a.project):
        ap.error(f"{a.project!r} is not a compose project name")
    services = containers(a.project)
    app = services.get("app")
    if app is None or not app["State"].get("Running"):
        print(f"smoke: the project {a.project} has no running app", file=sys.stderr)
        return 2
    data = next((m for m in app.get("Mounts") or [] if m.get("Destination") == "/data"), None)
    if data is None:
        print("smoke: the app container mounts nothing at /data", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="settleview-smoke-") as tmp:
        d = Path(tmp)
        d.chmod(0o755)  # readable by the suite's user (10001) on Linux; it holds no secret (settings and logs only)
        (d / "logs").mkdir(mode=0o755)
        (d / "project.json").write_text(json.dumps({"project": a.project}), encoding="utf-8")
        (d / "containers.json").write_text(json.dumps(services), encoding="utf-8")
        for name, c in services.items():
            if name == "app" or name.startswith("rec-"):
                logs = docker("logs", "--since", LOG_WINDOW, c["Id"])
                (d / "logs" / f"{name}.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        vol = (f"type=volume,source={data['Name']}" if data.get("Type") == "volume"
               else f"type=bind,source={data['Source']}")
        cmd = ["run", "--rm", "--network", f"container:{app['Id']}",
               "--mount", f"{vol},target=/data,readonly",
               "--mount", f"type=bind,source={d},target=/smoke,readonly",
               "--mount", f"type=bind,source={ROOT},target=/app,readonly", "-w", "/app",
               "--read-only", "--tmpfs", "/tmp:size=512m",  # noqa: S108 - the throwaway container's own tmpfs
               "--shm-size", "1g", "--user", APP_UID, "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "SETTLEVIEW_SMOKE_DIR=/smoke",
               "--label", f"settleview.smoke={a.project}",
               a.image, "python", "-m", "pytest", "-p", "no:cacheprovider", "tests/smoke", "-v", "-rs",
               *a.pytest_args]
        print(f"smoke: {a.project} (read-only)\n+ docker " + " ".join(cmd), flush=True)
        return docker(*cmd, capture=False, timeout=900).returncode


if __name__ == "__main__":
    sys.exit(main())
