"""The dev container: run the tests and the quality gates in Linux, as CI and production do, from any host.

    python scripts/dev.py test                      # the whole suite (e2e included, Playwright's Chromium)
    python scripts/dev.py test tests/kalshi -x      # any pytest arguments
    python scripts/dev.py postgres-tests            # the Postgres-only tests (none may skip) on a throwaway Postgres 17
    python scripts/dev.py postgres-tests --full     # ... then the whole suite with the store on that Postgres
    python scripts/dev.py quality                   # scripts/quality.py: ruff in the container, the rest in theirs
    python scripts/dev.py quality --only ruff       # any scripts/quality.py arguments
    python scripts/dev.py shell                     # a bash shell in the container (the checkout at /app)
    python scripts/dev.py run python -m ...         # any command in the container
    python scripts/dev.py build                     # rebuild the image (every command does this first; cached)
    python scripts/dev.py down                      # remove the dev project's containers, network and volumes

Needs Docker (Docker Desktop or Docker Engine) and any Python 3 on the host: standard library only, no venv. The
container is deploy/dev/compose.yaml's `dev` service: the production base image (Python 3.13, Debian trixie), the
project's dependencies at pyproject.toml's pins, and this checkout bind-mounted at /app, so an edit needs no rebuild and
a pyproject.toml change rebuilds only the dependency layer. Its compose project is `settleview-dev`, never the live
`settleview` stack: no shared container, volume, network or port, and nothing here publishes a port.

The quality gates: ruff runs in the dev container (the pinned ruff); hadolint, ShellCheck and Trivy already run in their
own pinned containers (scripts/quality.py), so they start from the host's Docker, as in CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "dev" / "compose.yaml"
PROJECT = "settleview-dev"  # never `settleview` (the live stack)
PYTEST = ["python", "-m", "pytest", "-p", "no:cacheprovider"]
IN_CONTAINER_TOOLS = {"ruff"}  # quality.py tools that need the project's Python environment


def say(msg: str) -> None:
    print(f"dev: {msg}", flush=True)


def compose(*args: str) -> list[str]:
    exe = shutil.which("docker")
    if exe is None:
        raise SystemExit("dev: docker is not on PATH (install Docker Desktop or Docker Engine)")
    return [exe, "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args]


def env() -> dict[str, str]:
    # No COMPOSE_* setting from the shell (COMPOSE_PROJECT_NAME, COMPOSE_FILE, COMPOSE_PROFILES) may redirect a command
    # to another project, and no app setting reaches the build.
    return {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "BTCFLOW_"))}


def run(cmd: list[str]) -> int:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, env=env()).returncode  # noqa: S603 - argv built here


def user() -> list[str]:
    # Linux / macOS: the host user, so files written into the checkout (e2e screenshots) are theirs. Docker Desktop on
    # Windows maps ownership itself; the image's non-root `dev` user is used.
    if hasattr(os, "getuid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


def build() -> int:
    return run(compose("build", "dev"))


def in_container(cmd: list[str], service: str = "dev", tty: bool = False) -> int:
    return run(compose("run", "--rm", *([] if tty else ["-T"]), *user(), service, *cmd))


def postgres_tests(args: list[str]) -> int:
    full = "--full" in args
    args = [a for a in args if a != "--full"]
    try:
        if args:
            return in_container([*PYTEST, *args], service="dev-pg")
        # As CI's postgres job: the Postgres-only tests, none may be skipped, then (--full) the whole suite on Postgres.
        # (-rs lists every skip; a skipped Postgres test means the server was not reached.)
        script = " ".join(PYTEST) + " -m postgres -rs | tee /tmp/pg.txt; status=$?; " \
            'if grep -q SKIPPED /tmp/pg.txt; then echo "dev: Postgres tests were skipped"; exit 1; fi; exit $status'
        code = in_container(["bash", "-o", "pipefail", "-c", script], service="dev-pg")
        if code == 0 and full:
            code = in_container(PYTEST, service="dev-pg")
        return code
    finally:
        run(compose("--profile", "postgres", "rm", "--stop", "--force", "postgres"))


def quality(args: list[str]) -> int:
    only = None
    rest: list[str] = []
    it = iter(args)
    for a in it:
        if a == "--only":
            only = next(it, "")
        elif a.startswith("--only="):
            only = a.split("=", 1)[1]
        else:
            rest.append(a)
    names = [n.strip() for n in (only or "all").split(",") if n.strip()]
    inside = [n for n in names if n in IN_CONTAINER_TOOLS or n == "all"]
    outside = [n for n in names if n not in IN_CONTAINER_TOOLS]
    codes = []
    if inside:
        codes.append(in_container(["python", "scripts/quality.py", "--only", "ruff"]))
    if outside:
        host = ["hadolint", "shellcheck", "trivy"] if outside == ["all"] else outside
        codes.append(run([sys.executable, str(ROOT / "scripts" / "quality.py"), "--only", ",".join(host), *rest]))
    return 0 if all(c == 0 for c in codes) else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    commands = ("build", "test", "postgres-tests", "quality", "shell", "run", "down")
    if cmd not in commands:
        say(f"unknown command {cmd!r} (one of: {', '.join(commands)})")
        return 2
    start = time.monotonic()
    try:
        if cmd == "down":
            return run(compose("--profile", "postgres", "down", "--volumes", "--remove-orphans"))
        code = build()
        if code != 0 or cmd == "build":
            return code
        if cmd == "test":
            return in_container([*PYTEST, *args])
        if cmd == "postgres-tests":
            return postgres_tests(args)
        if cmd == "quality":
            return quality(args)
        if cmd == "shell":
            return in_container(["bash"], tty=True)
        if not args:
            say("run: name a command, e.g. `python scripts/dev.py run python -m pip list`")
            return 2
        return in_container(args)
    finally:
        say(f"{cmd}: {time.monotonic() - start:.0f} s")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
