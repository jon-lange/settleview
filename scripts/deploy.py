"""The one operator entry point for production deploys (docs/operations.md, "Staging and the deploy gate"): production
gets an image only after staging ran that same image and the smoke suite passed on it. A thin wrapper around standard
commands (docker build / cosign verify / docker compose pull, up --wait, run migrate, the pytest smoke suite); its only
logic of its own is the preflight (the checkout, and no live agent session) and Git Bash's path conversion. Standard
library only, any OS, from the deploy checkout:

    python scripts/deploy.py --sha <commit on origin/main>                                # build that commit
    python scripts/deploy.py --sha <commit> --image ghcr.io/o/settleview@sha256:<digest>  # a signed release image
    python scripts/deploy.py --sha <commit> --dry-run                                     # print the commands only

Rolling back is deploying the previous release the same way (its commit, and its image by digest when it came from
the registry): the end of every run prints that command. The schema is Alembic's: the migrate job of the image being
deployed runs (`docker compose run --rm migrate`) before the app is replaced, and refuses a database it does not know.

In order, stopping at the first failure:
  1. Preflight: the checkout (its compose files are deployed too) is at <sha>, unchanged, and <sha> is on origin/main;
     no agent session is live (running or paused) on production, since an app restart ends them. That is asked of
     production's Postgres as settleview_research (read-only by permission), inside its own container.
  2. The image: `docker build --build-arg GIT_SHA=<sha> -t settleview:<sha12> .`, or `cosign verify` of the --image
     reference (tag@digest; signed by this repository's release workflow) and `docker compose pull`.
  3. Staging (deploy/staging/compose.staging.yaml: no real-money path) `up -d --wait` on that image, then the smoke
     suite against it (scripts/dev.py smoke). A failure stops here: production is not touched.
  4. Production: the agent-session check again, a database dump (the backup service, `--once`), the migrate job of the
     new image, `docker compose up -d --wait` with SETTLEVIEW_IMAGE=<image>, the smoke suite (read-only: GETs only).
     The image is tagged settleview:local too, so a plain `docker compose up -d` later keeps it (`.env` is the
     owner's and is never written).
  5. Staging is stopped (its volumes kept; --keep-staging leaves it running).

Exit codes: 0 deployed and smoke-tested; 1 a step failed (it says which, and whether production changed); 2 refused
before anything ran.

Git Bash on Windows rewrites absolute container paths in arguments it passes to native programs
(docs/solutions/2026-11-01-git-bash-msys-path-conversion.md): every command here runs with MSYS_NO_PATHCONV=1, and an
argument to this script that Git Bash already rewrote is refused with the fix.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROD_PROJECT = "settleview"
STAGING_PROJECT = "settleview-staging"
STAGING_FILES = ("compose.yaml", "deploy/staging/compose.staging.yaml")
STAGING_ENV = "deploy/staging/staging.env"
STAGING_SECRETS = "deploy/staging/secrets"
OFFLINE_FILE = "deploy/staging/compose.offline.yaml"
LOCAL_TAG = "settleview:local"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"  # GitHub Actions' keyless signing (cosign)
# Live sessions end at an app restart (application/agent_sessions.py LIVE).
SESSIONS_SQL = "select count(*) from app.agent_sessions where state in ('running', 'paused')"
# As the research role, over TCP with its password read from the secret file inside the postgres container (never on
# a command line here); the role's every transaction is read-only (deploy/postgres/initdb.sh).
SESSIONS_SH = ('PGPASSWORD="$(cat /run/secrets/pg_research_password)" psql -h 127.0.0.1 -U settleview_research '
               '-d settleview -v ON_ERROR_STOP=1 -At -c "$0"')
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST_REF = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
GITHUB_REPO = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")
# Git Bash's rewrite of a POSIX path into its install folder (C:/Program Files/Git/run/... and the like).
MSYS_MANGLED = re.compile(r"^[A-Za-z]:[/\\](Program Files[/\\]Git|msys64|git-sdk-64)[/\\]", re.IGNORECASE)


class Refused(Exception):
    """Stop before anything changed (exit 2)."""


class Failed(Exception):
    """A step failed (exit 1)."""


@dataclass
class Result:
    code: int
    out: str = ""
    err: str = ""


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of every command: no COMPOSE_* setting may redirect compose to another project or file, and Git
    Bash converts no path (MSYS_NO_PATHCONV / MSYS2_ARG_CONV_EXCL; ignored everywhere else)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_") and k != "SETTLEVIEW_IMAGE"}
    env.update({"MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"})
    env.update(extra or {})
    return env


def real_runner(cmd: Sequence[str], *, env: dict[str, str] | None = None, capture: bool = True,
                timeout: float = 1800) -> Result:
    exe = shutil.which(cmd[0]) or cmd[0]
    p = subprocess.run([exe, *cmd[1:]], cwd=ROOT, env=child_env(env), timeout=timeout,  # noqa: S603 - argv built here
                       capture_output=capture, text=True, encoding="utf-8", errors="replace")
    return Result(p.returncode, p.stdout or "", p.stderr or "")


Runner = Callable[..., Result]


@dataclass
class Plan:
    sha: str
    image: str | None = None  # a registry reference by digest (name@sha256:...); None = build the checkout
    prod_project: str = PROD_PROJECT
    staging_project: str = STAGING_PROJECT
    prod_files: list[str] = field(default_factory=list)  # extra -f files for production (tests), after compose.yaml
    offline: bool = False  # staging with no internet (deploy/staging/compose.offline.yaml)
    keep_staging: bool = False
    allow_unpublished: bool = False  # tests: a commit not (yet) on origin/main
    wait_s: int = 300

    @property
    def target_image(self) -> str:
        return self.image or f"settleview:{self.sha[:12]}"

    def compose(self, project: str, *args: str) -> list[str]:
        if project == self.staging_project:
            files = [*STAGING_FILES, *([OFFLINE_FILE] if self.offline else [])]
            head = ["docker", "compose", "-p", project, "--env-file", STAGING_ENV]
        else:
            files = ["compose.yaml", *self.prod_files]
            head = ["docker", "compose", "-p", project]
        return [*head, *[a for f in files for a in ("-f", f)], *args]

    def up(self, project: str) -> list[str]:
        return self.compose(project, "up", "-d", "--wait", "--wait-timeout", str(self.wait_s), "--no-build",
                            "--pull", "never", "--remove-orphans")

    def smoke(self, project: str) -> list[str]:
        return [sys.executable, "scripts/dev.py", "smoke", "--project", project]


class Deployer:
    def __init__(self, plan: Plan, run: Runner = real_runner, say: Callable[[str], None] | None = None) -> None:
        self.plan = plan
        self.run = run
        self.say = say or (lambda m: print(f"deploy: {m}", flush=True))
        self.prod_changed = False
        self.rollback: str | None = None

    def must(self, cmd: Sequence[str], what: str, *, capture: bool = False, timeout: float = 1800) -> Result:
        self.say("+ " + " ".join(shlex.quote(c) for c in cmd))
        r = self.run(list(cmd), env={"SETTLEVIEW_IMAGE": self.plan.target_image}, capture=capture, timeout=timeout)
        if r.code != 0:
            raise Failed(f"{what} failed (exit {r.code})" + (f": {(r.err or r.out).strip()[-600:]}" if capture else ""))
        return r

    def container(self, service: str) -> str | None:
        r = self.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.plan.prod_project}",
                      "--filter", f"label=com.docker.compose.service={service}",
                      "--filter", "label=com.docker.compose.oneoff=False"])
        ids = r.out.split() if r.code == 0 else []
        return ids[0] if ids else None

    # -- preflight (the only logic of its own) ------------------------------------------------------------------------
    def preflight(self) -> None:
        p = self.plan
        if not SHA.fullmatch(p.sha):
            raise Refused(f"--sha must be a full 40-character commit id, got {p.sha!r}")
        if p.image is not None and not DIGEST_REF.fullmatch(p.image):
            raise Refused(f"--image {p.image!r}: deploy a registry image by digest only (name@sha256:<64 hex>)")
        for name in (p.prod_project, p.staging_project):
            if not PROJECT_NAME.fullmatch(name):
                raise Refused(f"{name!r} is not a compose project name")
        if p.staging_project == p.prod_project or p.staging_project == PROD_PROJECT:
            raise Refused("the staging project must differ from production (and never be `settleview`)")
        # The compose files come from this checkout even when the image is pulled: it must be that commit, unchanged.
        head = self.run(["git", "rev-parse", "HEAD"]).out.strip()
        if head != p.sha:
            raise Refused(f"the checkout is at {head[:12] or '?'}, not {p.sha[:12]}: `git checkout {p.sha}` here "
                          "first (docs/operations.md, \"Deploying\")")
        if self.run(["git", "status", "--porcelain", "--untracked-files=no"]).out.strip():
            raise Refused("the checkout has uncommitted changes: the image and compose files would not be the commit")
        if not p.allow_unpublished and self.run(["git", "merge-base", "--is-ancestor", p.sha, "origin/main"]).code:
            raise Refused(f"{p.sha[:12]} is not on origin/main (fetch first; docs/PUBLISHING.md): deploy only "
                          "published commits")

    def check_sessions(self) -> None:
        cid = self.container("postgres")
        if cid is None:
            if self.container("app") is not None:
                raise Refused("production's app runs but its postgres container was not found: cannot check agent "
                              "sessions")
            self.say(f"no production stack ({self.plan.prod_project}) is running: no agent session to end")
            return
        r = self.run(["docker", "exec", cid, "sh", "-c", SESSIONS_SH, SESSIONS_SQL])
        if r.code != 0:
            if "does not exist" in r.err and "agent_sessions" in r.err:
                return  # a database the app has not migrated yet
            raise Refused(f"could not read production's agent sessions: {r.err.strip()[-300:]}")
        try:
            n = int(r.out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise Refused(f"unexpected answer to the agent-session query: {r.out.strip()[:100]!r}") from None
        if n:
            raise Refused(f"{n} agent session(s) are live on production: a deploy restarts the app and ends them. "
                          "Stop them (or wait for their end time), then deploy")
        self.say("agent sessions: none live on production")

    def note_rollback(self) -> None:
        """The command that redeploys what production runs now (read from its app container's labels)."""
        cid = self.container("app")
        if cid is None:
            return
        fmt = '{{index .Config.Labels "org.opencontainers.image.revision"}} {{.Config.Image}}'
        parts = self.run(["docker", "inspect", "--format", fmt, cid]).out.split()
        if len(parts) == 2 and SHA.fullmatch(parts[0]):
            image = f" --image {parts[1]}" if DIGEST_REF.fullmatch(parts[1]) else ""
            self.rollback = f"git checkout {parts[0]} && python scripts/deploy.py --sha {parts[0]}{image}"

    # -- standard commands --------------------------------------------------------------------------------------------
    def cosign_identity(self) -> str:
        url = self.run(["git", "remote", "get-url", "origin"]).out.strip()
        m = GITHUB_REPO.search(url)
        if m is None:
            raise Refused("origin is not a GitHub repository: cannot tell whose signature to require")
        # The release workflow, for a vX.Y.Z tag or a hand-run main build (docs/operations.md, "Releases and deploying
        # by digest").
        return (rf"^https://github\.com/{re.escape(m.group(1))}/\.github/workflows/release\.yml"
                r"@refs/(tags/v[0-9]+\.[0-9]+\.[0-9]+|heads/main)$")

    def image(self) -> None:
        p = self.plan
        if p.image is None:
            self.must(["docker", "build", "--build-arg", f"GIT_SHA={p.sha}", "-t", p.target_image, "."],
                      "docker build", timeout=3600)
            return
        self.must(["cosign", "verify", "--certificate-oidc-issuer", OIDC_ISSUER,
                   "--certificate-identity-regexp", self.cosign_identity(), p.target_image], "cosign verify",
                  capture=True)
        self.must(p.compose(p.staging_project, "pull", "--quiet"), "docker compose pull", timeout=3600)

    def deploy(self) -> int:
        p = self.plan
        try:
            self.preflight()
            self.check_sessions()
            self.note_rollback()
            self.image()
            self.must([sys.executable, "deploy/postgres/make_secrets.py", STAGING_SECRETS], "staging secrets",
                      capture=True)
            self.must(p.up(p.staging_project), "staging up", timeout=p.wait_s + 600)
            if self.run(p.smoke(p.staging_project), capture=False, timeout=1800).code:
                raise Failed("the smoke suite failed on staging (`docker compose -p "
                             f"{p.staging_project} logs`)")
            self.say("staging passed the smoke suite")
            self.check_sessions()  # again: a session may have started while staging ran
            if self.container("backup") is not None:
                self.must(p.compose(p.prod_project, "exec", "-T", "backup", "bash", "/usr/local/bin/settleview-backup", "--once"),
                          "database dump before the migration", timeout=900)
            self.prod_changed = True
            self.must(p.compose(p.prod_project, "run", "--rm", "migrate"), "the migrate job", timeout=900)
            self.must(p.up(p.prod_project), "production up", timeout=p.wait_s + 600)
            if self.run(p.smoke(p.prod_project), capture=False, timeout=1800).code:
                raise Failed("the smoke suite failed on PRODUCTION")
            if p.prod_project == PROD_PROJECT:
                self.must(["docker", "tag", p.target_image, LOCAL_TAG], f"docker tag {LOCAL_TAG}")
            if not p.keep_staging:
                self.must(p.compose(p.staging_project, "stop"), "staging stop", timeout=300)
        except Refused as e:
            self.say(f"REFUSED: {e}")
            return 2
        except Failed as e:
            self.say(f"FAILED: {e}")
            self.say("production was changed" if self.prod_changed else "production was NOT changed")
            self.print_rollback()
            return 1
        self.say(f"DEPLOYED {p.target_image} (commit {p.sha[:12]}) to {p.prod_project}; staging and production "
                 "passed the smoke suite")
        self.print_rollback()
        return 0

    def print_rollback(self) -> None:
        if self.rollback and self.prod_changed:
            self.say(f"to roll back, redeploy the previous release:\n    {self.rollback}")

    def describe(self) -> list[str]:
        """The commands it would run (--dry-run)."""
        p = self.plan
        image = ([f"docker build --build-arg GIT_SHA={p.sha} -t {p.target_image} ."] if p.image is None else
                 [f"cosign verify --certificate-oidc-issuer {OIDC_ISSUER} --certificate-identity-regexp "
                  rf"'^https://github\.com/<origin>/\.github/workflows/release\.yml@refs/...' {p.image}",
                  " ".join(p.compose(p.staging_project, "pull", "--quiet"))])
        env = f"SETTLEVIEW_IMAGE={p.target_image} "
        return [f"check: the checkout is at {p.sha[:12]}, clean" + ("" if p.allow_unpublished else
                                                                    ", and the commit is on origin/main"),
                f"check: no live agent session on {p.prod_project} (settleview_research, read-only)", *image,
                f"python deploy/postgres/make_secrets.py {STAGING_SECRETS}",
                env + " ".join(p.up(p.staging_project)), " ".join(p.smoke(p.staging_project)),
                f"check: no live agent session on {p.prod_project} (again)",
                " ".join(p.compose(p.prod_project, "exec", "-T", "backup", "bash", "/usr/local/bin/settleview-backup", "--once")),
                env + " ".join(p.compose(p.prod_project, "run", "--rm", "migrate")),
                env + " ".join(p.up(p.prod_project)), " ".join(p.smoke(p.prod_project)),
                *([f"docker tag {p.target_image} {LOCAL_TAG}"] if p.prod_project == PROD_PROJECT else []),
                *([] if p.keep_staging else [" ".join(p.compose(p.staging_project, "stop"))])]


def mangled(argv: Sequence[str]) -> list[str]:
    """Arguments that Git Bash already rewrote from a POSIX path into a Windows one."""
    return [a for a in argv if MSYS_MANGLED.match(a.split("=", 1)[-1])]


def main(argv: list[str] | None = None, run: Runner = real_runner) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bad = mangled(argv)
    if bad:
        print(f"deploy: Git Bash rewrote {bad[0]!r} from a container path: run it as `MSYS_NO_PATHCONV=1 python "
              "scripts/deploy.py ...` (docs/operations.md, \"Deploying\")", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description="Deploy production, gated on staging and the smoke suite.")
    ap.add_argument("--sha", required=True, help="the commit to deploy (40 hex; on origin/main)")
    ap.add_argument("--image", help="deploy this signed registry image (name@sha256:<digest>) instead of building")
    ap.add_argument("--staging-project", default=STAGING_PROJECT, help="staging's compose project")
    ap.add_argument("--offline", action="store_true", help="staging without internet (compose.offline.yaml)")
    ap.add_argument("--keep-staging", action="store_true", help="leave staging running afterwards")
    ap.add_argument("--wait", type=int, default=300, help="seconds each stack may take to become healthy")
    ap.add_argument("--dry-run", action="store_true", help="print the commands and run nothing")
    ap.add_argument("--prod-project", default=PROD_PROJECT, help=argparse.SUPPRESS)  # tests: a throwaway "prod"
    ap.add_argument("--prod-file", action="append", default=[], help=argparse.SUPPRESS)
    ap.add_argument("--allow-unpublished", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    plan = Plan(sha=a.sha.strip().lower(), image=a.image, prod_project=a.prod_project,
                staging_project=a.staging_project, prod_files=a.prod_file, offline=a.offline,
                keep_staging=a.keep_staging, allow_unpublished=a.allow_unpublished, wait_s=a.wait)
    d = Deployer(plan, run)
    if a.dry_run:
        print("\n".join(d.describe()))
        return 0
    return d.deploy()


if __name__ == "__main__":
    sys.exit(main())
