"""Quality gates: the same checks CI runs (.github/workflows/ci.yml, jobs `lint` and `trivy`), on any OS.

    python scripts/quality.py                       # everything: ruff, imports, hadolint, shellcheck, trivy (builds the image)
    python scripts/quality.py --only ruff           # one tool, or several: --only ruff,shellcheck
    python scripts/quality.py --only trivy --image settleview:local   # scan an image you already built (no build)

Tools (exit code non-zero when any of them fails; each one's own output is shown as is):
  ruff          `ruff check .` (pyproject.toml [tool.ruff]); from this Python's environment (the dev container's, or
                CI's), else PATH. Locally: `python scripts/dev.py quality` runs it in the dev container
  imports       `lint-imports` (.importlinter: the layer contracts); from this Python's environment (the project
                installed: the dev container), else PATH
  hadolint      the Dockerfiles (the app image's and deploy/dev/'s), in the official hadolint container
  shellcheck    deploy/**/*.sh at severity warning and the git hooks in .githooks/ at every severity, in the
                official ShellCheck container
  trivy-image   the app image: vulnerabilities with a fix (HIGH, CRITICAL) and secrets
  trivy-config  misconfigurations (HIGH, CRITICAL) in the Dockerfile, compose.yaml and deploy/
  trivy-fs      secrets in the repository (never .env*, secrets/, *.pem, compose.override.yaml or data/)
  trivy         all three Trivy scans

Docker runs the containers (Docker Desktop on Windows / macOS, Docker Engine on Linux): no tool is installed on the
host. Every image is pinned by version and digest below; CI uses this same file, so the flags cannot drift apart.
Without --image the app image is built from this checkout as settleview:quality (`docker build`; the layer cache makes
a rebuild quick). Trivy's vulnerability database is cached in the Docker volume --trivy-cache. A finding that must be
accepted goes in .trivyignore with a reason and a date (docs/operations.md, "Quality gates"). Standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Pinned tools: an exact version released at least two weeks before it was adopted, and its multi-arch digest.
TRIVY = "aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222"
HADOLINT = "hadolint/hadolint:v2.12.0@sha256:30a8fd2e785ab6176eed53f74769e04f125afb2f74a6c52aef7d463583b6d45e"
SHELLCHECK = "koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb"

SEVERITY = "HIGH,CRITICAL"
# Never read by any scan: local secrets and keys, local data, environments, git internals, generated test artifacts.
SKIP_DIRS = ["secrets", "keys", "data", ".venv", ".venv*", "venv", ".git", "node_modules", "tests/e2e/.artifacts",
             "**/__pycache__", ".hypothesis", ".pytest_cache", ".ruff_cache", ".import_linter_cache"]  # globs: .venv-py312 too
SKIP_FILES = [".env", ".env.*", "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "compose.override.yaml"]
CONFIG_TARGETS = ["Dockerfile", "compose.yaml", "deploy"]

TOOLS = ["ruff", "imports", "hadolint", "shellcheck", "trivy-image", "trivy-config", "trivy-fs"]
GROUPS = {"trivy": ["trivy-image", "trivy-config", "trivy-fs"], "all": TOOLS}


def say(msg: str) -> None:
    print(f"quality: {msg}", flush=True)


def run(cmd: list[str], *, stdin=None) -> int:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, stdin=stdin).returncode  # noqa: S603 - argv built here from pinned images and repo paths


def docker() -> str:
    exe = shutil.which("docker")
    if exe is None:
        raise SystemExit("quality: docker is not on PATH (Docker Desktop or Docker Engine runs the pinned tool images)")
    return exe


def src_mount(target: str = "/src") -> list[str]:
    return ["--mount", f"type=bind,source={ROOT},target={target},readonly"]


def ruff_cmd() -> list[str] | None:
    try:
        import ruff  # noqa: F401 - only the question "is it installed here"
        return [sys.executable, "-m", "ruff"]
    except ImportError:
        pass
    exe = shutil.which("ruff")
    return [exe] if exe else None


def check_ruff(_a) -> int:
    cmd = ruff_cmd()
    if cmd is None:
        say("ruff is not installed here: run `python scripts/dev.py quality` (the dev container has the pinned one)")
        return 1
    os.chdir(ROOT)
    return run([*cmd, "check", "."])


def check_imports(_a) -> int:
    # The console script next to this interpreter (the environment the project is installed in), else PATH.
    here = Path(sys.executable).parent
    found = [p for p in (here / "lint-imports", here / "lint-imports.exe") if p.is_file()]
    exe = str(found[0]) if found else shutil.which("lint-imports")
    if exe is None:
        say("lint-imports is not installed here: run `python scripts/dev.py quality --only imports` (the dev container)")
        return 1
    os.chdir(ROOT)
    return run([exe, "--no-cache"])


DOCKERFILES = ["Dockerfile", "deploy/dev/Dockerfile"]  # the app image; the dev container (scripts/dev.py)


def check_hadolint(_a) -> int:
    code = 0
    for name in DOCKERFILES:
        say(f"hadolint {name}")
        with open(ROOT / name, "rb") as f:
            code = run([docker(), "run", "--rm", "-i", HADOLINT, "hadolint", "-"], stdin=f) or code
    return code


def check_shellcheck(_a) -> int:
    """deploy/**/*.sh at severity warning; the git hooks in .githooks/ (no extension, POSIX sh) at every severity."""
    scripts = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "deploy").rglob("*.sh"))
    hooks = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / ".githooks").glob("*") if p.is_file())
    base = [docker(), "run", "--rm", *src_mount("/mnt"), "-w", "/mnt", SHELLCHECK]
    code = run([*base, "-S", "warning", *scripts]) if scripts else 0
    if hooks:
        say(f"shellcheck {', '.join(hooks)} (every severity, POSIX sh)")
        code = run([*base, "-S", "style", "-s", "sh", *hooks]) or code
    return code


def trivy(a, args: list[str], name: str) -> int:
    """One Trivy scan. JSON first (the exit code applies to it), then the table for the log and, with --sarif-dir,
    SARIF for code scanning: one scan, three views."""
    out = Path(tempfile.mkdtemp(prefix="quality-trivy-"))
    try:
        base = [docker(), "run", "--rm", "-v", f"{a.trivy_cache}:/root/.cache", *src_mount(), "-w", "/src",
                "--mount", f"type=bind,source={out},target=/out"]
        if name == "trivy-image":
            base += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
        ignore = ["--ignorefile", "/src/.trivyignore"] if (ROOT / ".trivyignore").is_file() else []
        code = run([*base, TRIVY, *args, *ignore, "--exit-code", "1", "--format", "json", "--output",
                    f"/out/{name}.json"])
        if not (out / f"{name}.json").is_file():
            say(f"{name}: Trivy wrote no report (exit {code})")
            return code or 1
        conv = [docker(), "run", "--rm", "--mount", f"type=bind,source={out},target=/out", TRIVY, "convert", "--quiet"]
        run([*conv, "--format", "table", f"/out/{name}.json"])
        if a.sarif_dir:
            dest = Path(a.sarif_dir).resolve()
            dest.mkdir(parents=True, exist_ok=True)
            run([*conv, "--format", "sarif", "--output", f"/out/{name}.sarif", f"/out/{name}.json"])
            shutil.copyfile(out / f"{name}.sarif", dest / f"{name}.sarif")
        return code
    finally:
        shutil.rmtree(out, ignore_errors=True)


def check_trivy_image(a) -> int:
    image = a.image
    if image is None:
        image = a.tag
        say(f"building {image} from this checkout (--image NAME scans an existing image instead)")
        if run([docker(), "build", "--build-arg", "GIT_SHA=quality", "-t", image, str(ROOT)]) != 0:
            return 1
    return trivy(a, ["image", "--quiet", "--scanners", "vuln,secret", "--severity", SEVERITY, "--ignore-unfixed",
                     image], "trivy-image")


def check_trivy_config(a) -> int:
    # One scan of the repository root, limited to the deploy files: everything else is skipped.
    keep = {t.split("/")[0] for t in CONFIG_TARGETS}
    skip_dirs = sorted({*SKIP_DIRS, *(p.name for p in ROOT.iterdir() if p.is_dir() and p.name not in keep)})
    skip_files = sorted({*SKIP_FILES, *(p.name for p in ROOT.iterdir() if p.is_file() and p.name not in keep)})
    return trivy(a, ["config", "--quiet", "--severity", SEVERITY, *_skips(skip_dirs, skip_files), "."],
                 "trivy-config")


def check_trivy_fs(a) -> int:
    return trivy(a, ["fs", "--quiet", "--scanners", "secret", *_skips(SKIP_DIRS, SKIP_FILES), "."], "trivy-fs")


def _skips(dirs: list[str], files: list[str]) -> list[str]:
    return [x for d in dirs for x in ("--skip-dirs", d)] + [x for f in files for x in ("--skip-files", f)]


CHECKS = {"ruff": check_ruff, "imports": check_imports, "hadolint": check_hadolint, "shellcheck": check_shellcheck,
          "trivy-image": check_trivy_image, "trivy-config": check_trivy_config, "trivy-fs": check_trivy_fs}


def selected(only: str | None) -> list[str]:
    if not only:
        return list(TOOLS)
    out: list[str] = []
    for name in (n.strip() for n in only.split(",")):
        names = GROUPS.get(name, [name])
        for n in names:
            if n not in CHECKS:
                raise SystemExit(f"quality: unknown tool {name!r} (one of: {', '.join([*TOOLS, *GROUPS])})")
            if n not in out:
                out.append(n)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--only", help=f"comma-separated: {', '.join([*TOOLS, *GROUPS])}")
    p.add_argument("--image", help="trivy-image: scan this existing image instead of building one")
    p.add_argument("--tag", default="settleview:quality", help="trivy-image: the tag to build (default %(default)s)")
    p.add_argument("--trivy-cache", default="settleview-quality-trivy-cache",
                   help="Docker volume for Trivy's database (default %(default)s)")
    p.add_argument("--sarif-dir", help="also write each Trivy scan as SARIF into this folder (CI's code scanning)")
    a = p.parse_args(argv)
    failed = []
    for name in selected(a.only):
        say(f"--- {name}")
        code = CHECKS[name](a)
        say(f"{name}: {'ok' if code == 0 else f'FAILED (exit {code})'}")
        if code != 0:
            failed.append(name)
    if failed:
        say(f"failed: {', '.join(failed)}")
        return 1
    say("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
