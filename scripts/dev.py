"""The dev container: tests, quality gates, research scripts and evals in Linux, as CI and production run, from any host.

    python scripts/dev.py test                      # the whole suite (e2e included, Playwright's Chromium)
    python scripts/dev.py test tests/kalshi -x      # any pytest arguments
    python scripts/dev.py postgres-tests            # the Postgres-only tests (none may skip) on a throwaway Postgres 17
    python scripts/dev.py postgres-tests --full     # ... then the whole suite with the store on that Postgres
    python scripts/dev.py quality                   # scripts/quality.py: ruff + import contracts in the container,
                                                    # hadolint, shellcheck and trivy in their own pinned containers
    python scripts/dev.py quality --only imports    # any scripts/quality.py arguments (lint-imports alone here)
    python scripts/dev.py experiments report        # scripts/experiments.py, the research data mounted read-only
    python scripts/dev.py script scripts/x.py ...   # any research script the same way
    python scripts/dev.py evals run pipelines/pr.toml   # python -m evals ... (also: evals test | export | review)
    python scripts/dev.py smoke --project settleview-staging   # the deploy gate's READ-ONLY smoke suite against a
                                                    # running stack (scripts/smoke.py: tests/smoke in pytest)
    python scripts/dev.py shell                     # a bash shell in the container (the checkout at /app)
    python scripts/dev.py run python -m ...         # any command in the container
    python scripts/dev.py build                     # rebuild the image (every command does this first; cached)
    python scripts/dev.py down                      # remove the dev project's containers, network and volumes

    python scripts/dev.py --data ../other-checkout/data experiments selftest   # research data from another folder

Needs Docker (Docker Desktop or Docker Engine) and any Python 3 on the host: standard library only, no venv. Without a
system Python, uv runs it with its own interpreter and no project environment: `uv run --no-project python
scripts/dev.py test`. The container is deploy/dev/compose.yaml's `dev` service: the production base image (Python
3.13, Debian trixie), the project's dependencies at pyproject.toml's pins, and this checkout bind-mounted at /app, so
an edit needs no rebuild and a pyproject.toml change rebuilds only the dependency layer. Its compose project is
`settleview-dev`, never the live `settleview` stack: no shared container, volume, network or port, and nothing here
publishes a port. SETTLEVIEW_DEV_PROJECT=settleview-dev-<name> gives a second checkout (a worktree) its own project,
so two `postgres-tests` runs never share or remove each other's Postgres.

Research data (`script`, `experiments`; `evals` too, read-only throughout). data/ is git-ignored and exists only on
the machine that recorded it: --data DIR (or SETTLEVIEW_DEV_DATA) points at it, default <checkout>/data. The scripts
open every database with SQLite `mode=ro`; the mounts enforce it as well:
    /app                       the checkout, READ-ONLY (a script cannot change code, tests or settings)
    /app/docs/research         read-write: write-ups
    /app/data                  READ-ONLY (btcflow.db, backups, logs, ...)
    /app/data/research         read-write: new output files, and appending to experiments_results.csv
    /app/data/research/**/*.db READ-ONLY, each one with its -wal / -shm / -journal files
A script that tries to write a database fails ("Read-only file system"); one that builds a new database file under
data/research/ can. Every other command (test, shell, run, evals, ...) has the checkout read-write and data/ read-only
as a whole; `evals` honours --data too.

The quality gates: ruff and lint-imports run in the dev container (the pinned versions); hadolint, ShellCheck and Trivy
already run in their own pinned containers (scripts/quality.py), so they start from the host's Docker, as in CI.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "dev" / "compose.yaml"
DEFAULT_PROJECT = "settleview-dev"  # never `settleview` (the live stack)
PYTEST = ["python", "-m", "pytest", "-p", "no:cacheprovider"]
IN_CONTAINER_TOOLS = {"ruff", "imports"}  # quality.py tools that need the project's Python environment
SQLITE_FILES = re.compile(r".*\.db(-wal|-shm|-journal)?$")
KEY_IN_CONTAINER = "/run/evals/api-key"  # where `evals ... --api-key-file F` sees F (mounted read-only)
COMMANDS = ("build", "test", "postgres-tests", "quality", "experiments", "script", "evals", "smoke", "shell", "run",
            "down")


def say(msg: str) -> None:
    print(f"dev: {msg}", flush=True)


def project() -> str:
    name = os.environ.get("SETTLEVIEW_DEV_PROJECT", DEFAULT_PROJECT)
    if not re.fullmatch(r"settleview-dev(-[a-z0-9][a-z0-9-]*)?", name):
        raise SystemExit(f"dev: SETTLEVIEW_DEV_PROJECT={name!r} must be settleview-dev or settleview-dev-<name> "
                         "(lowercase letters, digits, dashes): never the live `settleview` project")
    return name


def compose(*args: str) -> list[str]:
    exe = shutil.which("docker")
    if exe is None:
        raise SystemExit("dev: docker is not on PATH (install Docker Desktop or Docker Engine)")
    return [exe, "compose", "-p", project(), "-f", str(COMPOSE_FILE), *args]


def env() -> dict[str, str]:
    # No COMPOSE_* setting from the shell (COMPOSE_PROJECT_NAME, COMPOSE_FILE, COMPOSE_PROFILES) may redirect a command
    # to another project, and no app setting reaches the build.
    return {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "BTCFLOW_"))}


def run(cmd: list[str]) -> int:
    # The echo never shows where a key file lives (`evals --api-key-file`): only that one is mounted.
    shown = [f"<key file>:{KEY_IN_CONTAINER}:ro" if a.endswith(f":{KEY_IN_CONTAINER}:ro") else a for a in cmd]
    print("+ " + " ".join(shown), flush=True)
    return subprocess.run(cmd, cwd=ROOT, env=env()).returncode  # noqa: S603 - argv built here


def user() -> list[str]:
    # Linux / macOS: the host user, so files written into the checkout (e2e screenshots) are theirs. Docker Desktop on
    # Windows maps ownership itself; the image's non-root `dev` user is used.
    if hasattr(os, "getuid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


def build() -> int:
    return run(compose("build", "dev"))


def bind(source: Path, target: str, readonly: bool) -> list[str]:
    # The short syntax (`docker compose run` takes no other); compose reads a Windows drive letter in the source.
    return ["-v", f"{source}:{target}{':ro' if readonly else ''}"]


def in_container(cmd: list[str], service: str = "dev", tty: bool = False, extra: list[str] | None = None) -> int:
    extra = list(extra or [])
    # The checkout's data/ (git-ignored; the recorded research data, where it exists) is read-only in every dev
    # container: a test, a shell or a stray command cannot change it. `script` / `experiments` mount it themselves.
    if service != "research" and not any(":/app/data" in e for e in extra) and (ROOT / "data").is_dir():
        extra += bind(ROOT / "data", "/app/data", readonly=True)
    return run(compose("run", "--rm", *([] if tty else ["-T"]), *user(), *extra, service, *cmd))


def data_dir(explicit: str | None) -> Path | None:
    """The research data folder: --data, else SETTLEVIEW_DEV_DATA, else <checkout>/data. A folder that was named and
    does not exist is an error; the default may be absent (a checkout without recorded data)."""
    named = explicit or os.environ.get("SETTLEVIEW_DEV_DATA")
    if named:
        p = Path(named).expanduser().resolve()
        if not p.is_dir():
            raise SystemExit(f"dev: the data folder {p} does not exist")
        return p
    p = ROOT / "data"
    return p if p.is_dir() else None


def mountpoint(rel: str) -> None:
    # Docker cannot create a mount point inside a read-only bind mount: make it in the checkout first (git-ignored).
    (ROOT / rel).mkdir(parents=True, exist_ok=True)


def research_mounts(data: Path | None) -> list[str]:
    """data/ read-only, data/research/ writable for new outputs, every SQLite file in it read-only again."""
    if data is None:
        say("no data folder (data/ is git-ignored and exists only where it was recorded; --data DIR names one): "
            "scripts that read data/ will fail")
        return []
    mountpoint("data")
    out = bind(data, "/app/data", readonly=True)
    research = data / "research"
    if research.is_dir():
        out += bind(research, "/app/data/research", readonly=False)
        for f in sorted(research.rglob("*")):
            if f.is_file() and SQLITE_FILES.fullmatch(f.name):
                out += bind(f, f"/app/data/research/{f.relative_to(research).as_posix()}", readonly=True)
    say(f"data: {data} read-only; {research.name}/ writable for new outputs, its databases read-only")
    return out


def in_checkout(path: str) -> str:
    p = Path(path)
    full = (p if p.is_absolute() else Path.cwd() / p).resolve()
    if not full.is_file():
        full = (ROOT / p).resolve()
    try:
        rel = full.relative_to(ROOT)
    except ValueError:
        raise SystemExit(f"dev: {path} is not in this checkout (the container sees only {ROOT})") from None
    if not full.is_file():
        raise SystemExit(f"dev: no such script: {path}")
    return rel.as_posix()


def script(args: list[str], data: Path | None) -> int:
    if not args:
        say("script: name a script, e.g. `python scripts/dev.py script scripts/test_human_rules.py --help`")
        return 2
    return in_container(["python", in_checkout(args[0]), *args[1:]], service="research",
                        extra=research_mounts(data))


def evals(args: list[str], data: Path | None) -> int:
    """`evals test [pytest args]`: the eval tests (what CI runs); `evals export|review ...`: python -m evals.<that>;
    anything else: python -m evals ... (run, graders, grade)."""
    extra: list[str] = []
    if data is not None:
        extra += bind(data, "/app/data", readonly=True)  # `--source data/btcflow.db` (opened with mode=ro)
    if "ANTHROPIC_API_KEY" in os.environ:  # a live run's key: passed by name, its value never on a command line
        extra += ["-e", "ANTHROPIC_API_KEY"]
    out: list[str] = []
    it = iter(args)
    for a in it:
        if a == "--api-key-file" or a.startswith("--api-key-file="):
            f = a.split("=", 1)[1] if "=" in a else next(it, "")
            key = Path(f).expanduser().resolve()
            if not key.is_file():
                raise SystemExit(f"dev: --api-key-file {f}: no such file")
            extra += bind(key, KEY_IN_CONTAINER, readonly=True)
            out += ["--api-key-file", KEY_IN_CONTAINER]
        else:
            out.append(a)
    if not out:
        say("evals: name a command, e.g. `evals test`, `evals run pipelines/pr.toml`, `evals graders`")
        return 2
    if out[0] == "test":
        cmd = [*PYTEST, "tests/evals", *out[1:]]
    elif out[0] in ("export", "review"):
        cmd = ["python", "-m", f"evals.{out[0]}", *out[1:]]
    else:
        cmd = ["python", "-m", "evals", *out]
    return in_container(cmd, extra=extra)


def postgres_tests(args: list[str]) -> int:
    full = "--full" in args
    args = [a for a in args if a != "--full"]
    try:
        if args:
            return in_container([*PYTEST, *args], service="dev-pg")
        # As CI's postgres job: the Postgres-only tests, none may be skipped, then (--full) the whole suite on Postgres.
        # (-rs lists every skip; a skipped Postgres test means the server was not reached.)
        line = " ".join(PYTEST) + " -m postgres -rs | tee /tmp/pg.txt; status=$?; " \
            'if grep -q SKIPPED /tmp/pg.txt; then echo "dev: Postgres tests were skipped"; exit 1; fi; exit $status'
        code = in_container(["bash", "-o", "pipefail", "-c", line], service="dev-pg")
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
    inside = sorted(IN_CONTAINER_TOOLS) if "all" in names else [n for n in names if n in IN_CONTAINER_TOOLS]
    outside = [n for n in names if n not in IN_CONTAINER_TOOLS]
    codes = []
    if inside:
        codes.append(in_container(["python", "scripts/quality.py", "--only", ",".join(inside)]))
    if outside:
        host = ["hadolint", "shellcheck", "trivy"] if "all" in outside else outside
        codes.append(run([sys.executable, str(ROOT / "scripts" / "quality.py"), "--only", ",".join(host), *rest]))
    return 0 if all(c == 0 for c in codes) else 1


def main(argv: list[str]) -> int:
    data_arg = None
    while argv and (argv[0] == "--data" or argv[0].startswith("--data=")):
        if "=" in argv[0]:
            data_arg, argv = argv[0].split("=", 1)[1], argv[1:]
        elif len(argv) < 2:
            say("--data needs a folder")
            return 2
        else:
            data_arg, argv = argv[1], argv[2:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd not in COMMANDS:
        say(f"unknown command {cmd!r} (one of: {', '.join(COMMANDS)})")
        return 2
    if data_arg is not None and cmd not in ("experiments", "script", "evals"):
        say(f"--data is for experiments, script and evals, not {cmd}")
        return 2
    start = time.monotonic()
    try:
        if cmd == "down":
            return run(compose("--profile", "postgres", "--profile", "research", "down", "--volumes",
                               "--remove-orphans"))
        data = data_dir(data_arg) if cmd in ("experiments", "script", "evals") else None
        code = build()
        if code != 0 or cmd == "build":
            return code
        if cmd == "test":
            return in_container([*PYTEST, *args])
        if cmd == "postgres-tests":
            return postgres_tests(args)
        if cmd == "quality":
            return quality(args)
        if cmd == "experiments":
            return script(["scripts/experiments.py", *args], data)
        if cmd == "script":
            return script(args, data)
        if cmd == "evals":
            return evals(args, data)
        if cmd == "smoke":  # the host half (Docker, read-only) starts the dev image itself, beside the stack's app
            return subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke.py"), *args],  # noqa: S603
                                  cwd=ROOT).returncode
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
