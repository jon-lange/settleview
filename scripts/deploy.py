"""The deploy gate: production gets an image only after staging ran that same image and the smoke suite passed on it
(docs/operations.md, "Staging and the deploy gate"). Standard library only, any OS (Git Bash included), from the deploy
checkout:

    python scripts/deploy.py --sha <commit on origin/main>                    # build that commit, gate, deploy
    python scripts/deploy.py --sha <commit> --image ghcr.io/o/settleview@sha256:<digest>   # a released image by digest
    python scripts/deploy.py --sha <commit> --dry-run                          # print the plan, run nothing

In order, stopping at the first failure:
  0. The checkout (its compose files are deployed too) must be at <sha>, unchanged, and <sha> on origin/main.
  1. Refuse while any agent session is live (running or paused) on production: a restart of the app ends them. Asked
     of production's Postgres as settleview_research (read-only by permission), inside its own container.
  2. The image: build the checkout (`docker build --build-arg GIT_SHA=<sha> -t settleview:<sha12> .`), or pull the
     --image reference (by digest: the digest is checked). Its org.opencontainers.image.revision label must be <sha>.
  3. Staging (project settleview-staging, deploy/staging/compose.staging.yaml: no real-money path) up on that image,
     its own secrets created if missing; then the smoke suite against it (scripts/dev.py smoke). A failure stops here:
     production is not touched.
  4. Production: the agent-session check again, the running image tagged settleview:rollback-<UTC time>, then
     `docker compose up -d` with SETTLEVIEW_IMAGE=<image> (compose.yaml's image), then the smoke suite against
     production (read-only: GETs only, no order, no setting). The new image is tagged settleview:local too, so a plain
     `docker compose up -d` later keeps it.
  5. Staging is stopped (its volumes kept; --keep-staging leaves it running) and the rollback command is printed.

Exit codes: 0 deployed and smoke-tested; 1 a step failed (it says which, and whether production changed); 2 refused
before anything ran (arguments, checkout, live agent sessions).

Git Bash on Windows rewrites absolute container paths in arguments it passes to native programs
(docs/solutions/2026-11-01-git-bash-msys-path-conversion.md): every command here runs with MSYS_NO_PATHCONV=1, and an
argument to this script that Git Bash already rewrote is refused with the fix.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
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
REVISION_LABEL = "org.opencontainers.image.revision"
# Live sessions end at an app restart (application/agent_sessions.py LIVE).
SESSIONS_SQL = "select count(*) from app.agent_sessions where state in ('running', 'paused')"
# As the research role, over TCP with its password read from the secret file inside the postgres container (never on
# a command line here); the role's every transaction is read-only (deploy/postgres/initdb.sh).
SESSIONS_SH = ('PGPASSWORD="$(cat /run/secrets/pg_research_password)" psql -h 127.0.0.1 -U settleview_research '
               '-d settleview -v ON_ERROR_STOP=1 -At -c "$0"')
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST_REF = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
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
    image: str | None = None  # a pulled reference (name@sha256:... or a tag); None = build the checkout
    prod_project: str = PROD_PROJECT
    staging_project: str = STAGING_PROJECT
    prod_files: list[str] = field(default_factory=list)  # extra -f files for production (tests), after compose.yaml
    offline: bool = False  # staging with no internet (deploy/staging/compose.offline.yaml)
    keep_staging: bool = False
    allow_unpublished: bool = False  # tests: a commit not (yet) on origin/main
    wait_s: int = 300
    stamp: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S"))

    @property
    def build(self) -> bool:
        return self.image is None

    @property
    def target_image(self) -> str:
        return self.image or f"settleview:{self.sha[:12]}"

    @property
    def rollback_tag(self) -> str:
        return f"settleview:rollback-{self.stamp}"

    def compose(self, project: str, *args: str) -> list[str]:
        if project == self.staging_project:
            files = [*STAGING_FILES, *([OFFLINE_FILE] if self.offline else [])]
            head = ["docker", "compose", "-p", project, "--env-file", STAGING_ENV]
        else:
            files = ["compose.yaml", *self.prod_files]
            head = ["docker", "compose", "-p", project]
        return [*head, *[a for f in files for a in ("-f", f)], *args]


class Deployer:
    def __init__(self, plan: Plan, run: Runner = real_runner, say: Callable[[str], None] | None = None) -> None:
        self.plan = plan
        self.run = run
        self.say = say or (lambda m: print(f"deploy: {m}", flush=True))
        self.prod_changed = False
        self.rollback: list[str] = []

    # -- helpers ------------------------------------------------------------------------------------------------------
    def must(self, cmd: Sequence[str], what: str, *, env: dict[str, str] | None = None, capture: bool = True,
             timeout: float = 1800) -> Result:
        self.say("+ " + " ".join(shlex.quote(c) for c in cmd))
        r = self.run(list(cmd), env=env, capture=capture, timeout=timeout)
        if r.code != 0:
            raise Failed(f"{what} failed (exit {r.code}): {(r.err or r.out).strip()[-600:]}")
        return r

    def container(self, project: str, service: str) -> str | None:
        r = self.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
                      "--filter", f"label=com.docker.compose.service={service}",
                      "--filter", "label=com.docker.compose.oneoff=False"])
        ids = r.out.split() if r.code == 0 else []
        return ids[0] if ids else None

    # -- the steps ----------------------------------------------------------------------------------------------------
    def preflight(self) -> None:
        p = self.plan
        if not SHA.fullmatch(p.sha):
            raise Refused(f"--sha must be a full 40-character commit id, got {p.sha!r}")
        for name in (p.prod_project, p.staging_project):
            if not PROJECT_NAME.fullmatch(name):
                raise Refused(f"{name!r} is not a compose project name")
        if p.staging_project == p.prod_project or p.staging_project == PROD_PROJECT:
            raise Refused("the staging project must differ from production (and never be `settleview`)")
        if p.image is not None and ("@" in p.image) and not DIGEST_REF.fullmatch(p.image):
            raise Refused(f"--image {p.image!r}: a digest reference is name@sha256:<64 hex>")
        if self.run(["docker", "version", "--format", "{{.Server.Version}}"]).code != 0:
            raise Refused("Docker is not running (docker version failed)")
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

    def live_sessions(self) -> int:
        """Live agent sessions on production (0 when it has no database yet: a first deploy)."""
        cid = self.container(self.plan.prod_project, "postgres")
        if cid is None:
            if self.container(self.plan.prod_project, "app") is not None:
                raise Refused("production's app runs but its postgres container was not found: cannot check agent "
                              "sessions")
            self.say(f"no production stack ({self.plan.prod_project}) is running: no agent sessions to end")
            return 0
        r = self.run(["docker", "exec", cid, "sh", "-c", SESSIONS_SH, SESSIONS_SQL])
        if r.code != 0:
            if "does not exist" in r.err and "agent_sessions" in r.err:
                return 0  # a database the app has not migrated yet
            raise Refused(f"could not read production's agent sessions: {r.err.strip()[-300:]}")
        try:
            return int(r.out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise Refused(f"unexpected answer to the agent-session query: {r.out.strip()[:100]!r}") from None

    def check_sessions(self) -> None:
        n = self.live_sessions()
        if n:
            raise Refused(f"{n} agent session(s) are live on production: a deploy restarts the app and ends them. "
                          "Stop them (or wait for their end time), then deploy")
        self.say("agent sessions: none live on production")

    def image(self) -> None:
        p = self.plan
        if p.build:
            self.must(["docker", "build", "--build-arg", f"GIT_SHA={p.sha}", "-t", p.target_image, "."],
                      "docker build", capture=False, timeout=3600)
        elif self.run(["docker", "image", "inspect", p.target_image]).code != 0:
            self.must(["docker", "pull", p.target_image], "docker pull", capture=False, timeout=3600)
        info = json.loads(self.must(["docker", "image", "inspect", p.target_image], "docker image inspect").out)[0]
        if DIGEST_REF.fullmatch(p.target_image):
            digest = p.target_image.split("@", 1)[1]
            if not any(d.endswith("@" + digest) for d in info.get("RepoDigests") or []):
                raise Failed(f"the local image does not carry the digest {digest}")
        rev = ((info.get("Config") or {}).get("Labels") or {}).get(REVISION_LABEL)
        if rev != p.sha:
            raise Failed(f"the image's {REVISION_LABEL} is {rev!r}, not {p.sha}: it was not built from that commit")
        self.say(f"image {p.target_image} ({info['Id'][:19]}) is commit {p.sha[:12]}")

    def up(self, project: str) -> None:
        env = {"SETTLEVIEW_IMAGE": self.plan.target_image, "GIT_SHA": self.plan.sha}
        self.must(self.plan.compose(project, "up", "-d", "--no-build", "--pull", "never", "--remove-orphans"),
                  f"compose up ({project})", env=env, capture=False, timeout=900)

    def smoke(self, project: str) -> bool:
        cmd = [sys.executable, "scripts/dev.py", "smoke", "--project", project, "--wait", str(self.plan.wait_s)]
        self.say("+ " + " ".join(cmd))
        return self.run(cmd, capture=False, timeout=self.plan.wait_s + 900).code == 0

    def staging(self) -> None:
        self.must([sys.executable, "deploy/postgres/make_secrets.py", STAGING_SECRETS], "staging secrets")
        self.up(self.plan.staging_project)
        if not self.smoke(self.plan.staging_project):
            raise Failed(f"the smoke suite failed on staging: production was NOT touched. Inspect with `docker compose "
                         f"-p {self.plan.staging_project} logs`; remove with "
                         f"`{' '.join(self.plan.compose(self.plan.staging_project, 'down', '--volumes'))}`")
        self.say("staging passed the smoke suite")

    def remember_rollback(self) -> None:
        p = self.plan
        cid = self.container(p.prod_project, "app")
        if cid is None:
            self.say("no running production app: nothing to roll back to")
            return
        info = json.loads(self.must(["docker", "inspect", cid], "docker inspect (production app)").out)[0]
        self.must(["docker", "tag", info["Image"], p.rollback_tag], "docker tag (rollback)")
        img = json.loads(self.run(["docker", "image", "inspect", info["Image"]]).out or "[{}]")[0]
        old_sha = ((img.get("Config") or {}).get("Labels") or {}).get(REVISION_LABEL) or "<the previous commit>"
        up = " ".join(p.compose(p.prod_project, "up", "-d", "--no-build", "--pull", "never"))
        self.rollback = [f"git checkout {old_sha}   # the compose files of that release",
                         f"SETTLEVIEW_IMAGE={p.rollback_tag} {up}          # bash / Git Bash / zsh",
                         f"$env:SETTLEVIEW_IMAGE='{p.rollback_tag}'; {up}   # PowerShell"]
        if p.prod_project == PROD_PROJECT:
            self.rollback.append(f"docker tag {p.rollback_tag} settleview:local   # so a plain `docker compose up -d` "
                                 "keeps the old image")
        self.say(f"the running production image is kept as {p.rollback_tag}")

    def production(self) -> None:
        p = self.plan
        self.check_sessions()  # again: a session may have started while staging ran
        self.remember_rollback()
        self.prod_changed = True
        self.up(p.prod_project)
        if not self.smoke(p.prod_project):
            raise Failed("the smoke suite failed on PRODUCTION after the deploy: roll back with the commands below")
        if p.prod_project == PROD_PROJECT:
            self.must(["docker", "tag", p.target_image, "settleview:local"], "docker tag settleview:local")

    def finish_staging(self) -> None:
        if self.plan.keep_staging:
            return
        self.must(self.plan.compose(self.plan.staging_project, "stop"), "compose stop (staging)", timeout=300)

    def deploy(self) -> int:
        try:
            self.preflight()
            self.check_sessions()
            self.image()
            self.staging()
            self.production()
            self.finish_staging()
        except Refused as e:
            self.say(f"REFUSED: {e}")
            return 2
        except Failed as e:
            self.say(f"FAILED: {e}")
            self.say("production was changed" if self.prod_changed else "production was NOT changed")
            self.print_rollback()
            return 1
        self.say(f"DEPLOYED {self.plan.target_image} (commit {self.plan.sha[:12]}) to {self.plan.prod_project}; "
                 "staging and production passed the smoke suite")
        self.print_rollback()
        return 0

    def print_rollback(self) -> None:
        if self.rollback and self.prod_changed:
            self.say("to roll back:\n    " + "\n    ".join(self.rollback))

    def describe(self) -> list[str]:
        """The plan as the commands it would run (--dry-run)."""
        p = self.plan
        build = (f"docker build --build-arg GIT_SHA={p.sha} -t {p.target_image} ." if p.build
                 else f"docker pull {p.target_image}   # unless present; digest and revision label checked")
        return [f"check: the checkout is at {p.sha[:12]} and clean"
                + ("" if p.allow_unpublished else ", and the commit is on origin/main"),
                f"check: no live agent session on {p.prod_project} (settleview_research, read-only)", build,
                f"python deploy/postgres/make_secrets.py {STAGING_SECRETS}",
                f"SETTLEVIEW_IMAGE={p.target_image} " + " ".join(p.compose(p.staging_project, "up", "-d",
                                                                             "--no-build", "--pull", "never")),
                f"python scripts/dev.py smoke --project {p.staging_project} --wait {p.wait_s}",
                f"check: no live agent session on {p.prod_project} (again)",
                f"docker tag <running production image> {p.rollback_tag}",
                f"SETTLEVIEW_IMAGE={p.target_image} " + " ".join(p.compose(p.prod_project, "up", "-d", "--no-build",
                                                                             "--pull", "never")),
                f"python scripts/dev.py smoke --project {p.prod_project} --wait {p.wait_s}",
                *([f"docker tag {p.target_image} settleview:local"] if p.prod_project == PROD_PROJECT else []),
                *([] if p.keep_staging else [" ".join(p.compose(p.staging_project, "stop"))])]


def mangled(argv: Sequence[str]) -> list[str]:
    """Arguments that Git Bash already rewrote from a POSIX path into a Windows one."""
    return [a for a in argv if MSYS_MANGLED.match(a.split("=", 1)[-1])]


def main(argv: list[str] | None = None, run: Runner = real_runner) -> int:
    argv = sys.argv[1:] if argv is None else argv
    bad = mangled(argv)
    if bad:
        print(f"deploy: Git Bash rewrote {bad[0]!r} from a container path: run it as `MSYS_NO_PATHCONV=1 python "
              "scripts/deploy.py ...` (docs/operations.md, \"Deploying\")", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description="Gate a deploy on staging and the smoke suite, then deploy production.")
    ap.add_argument("--sha", required=True, help="the commit to deploy (40 hex; on origin/main)")
    ap.add_argument("--image", help="deploy this image (name@sha256:<digest> or a tag) instead of building the checkout")
    ap.add_argument("--prod-project", default=PROD_PROJECT, help=argparse.SUPPRESS)
    ap.add_argument("--staging-project", default=STAGING_PROJECT, help="staging's compose project")
    ap.add_argument("--prod-file", action="append", default=[], help=argparse.SUPPRESS)  # tests: throwaway "prod"
    ap.add_argument("--offline", action="store_true", help="staging without internet (compose.offline.yaml)")
    ap.add_argument("--keep-staging", action="store_true", help="leave staging running afterwards")
    ap.add_argument("--allow-unpublished", action="store_true", help=argparse.SUPPRESS)  # tests only
    ap.add_argument("--wait", type=int, default=300, help="seconds each stack may take to become healthy")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
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
