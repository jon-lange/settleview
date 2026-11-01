"""The one operator entry point for production deploys and rollbacks (docs/operations.md, "Staging and the deploy
gate"): production gets an image only after staging ran that same image and the smoke suite passed on it. Standard
library only, any OS (Git Bash included), from the deploy checkout:

    python scripts/deploy.py deploy --sha <commit on origin/main>              # build that commit, gate, deploy
    python scripts/deploy.py deploy --sha <commit> --image ghcr.io/o/settleview@sha256:<digest>   # a released image
    python scripts/deploy.py deploy --sha <commit> --dry-run                    # print the plan, run nothing
    python scripts/deploy.py rollback                                           # back to the image before the last
    python scripts/deploy.py rollback --to <image id from .deploy-history.jsonl>

`deploy`, in order, stopping at the first failure:
  0. The checkout (its compose files are deployed too) must be at <sha>, unchanged, and <sha> on origin/main.
  1. Refuse while any agent session is live (running or paused) on production: a restart of the app ends them. Asked
     of production's Postgres as settleview_research (read-only by permission), inside its own container.
  2. The running production image is kept under a tag (settleview:previous-<image id>), BEFORE anything is built: a
     rebuild of the same tag would otherwise leave it untagged, and Docker's containerd store then drops it.
  3. The image: build the checkout (`docker build --build-arg GIT_SHA=<sha> -t settleview:<sha12> .`), or pull the
     --image reference, which must be by digest and passes the release checks (ReleaseHooks.verify: cosign). Its
     org.opencontainers.image.revision label must be <sha>.
  4. Staging (project settleview-staging, deploy/staging/compose.staging.yaml: no real-money path) up on that image,
     its own secrets created if missing; then the smoke suite against it (scripts/dev.py smoke). A failure stops here:
     production is not touched.
  5. Production: the agent-session check again, the schema check and the pre-migration dump (ReleaseHooks), then
     `docker compose up -d` with SETTLEVIEW_IMAGE=<image> (compose.yaml's image), then the smoke suite against
     production (read-only: GETs only, no order, no setting). The image is tagged settleview:local too, so a plain
     `docker compose up -d` later keeps it (`.env` is the owner's and is never written).
  6. One line appended to .deploy-history.jsonl (git-ignored): the commit, the image reference, its image id and
     digest, the previous image (id, commit, tag) and the result. Staging is stopped (volumes kept; --keep-staging
     leaves it running).

`rollback`: the previous image of the last production change in the history (or --to an image id the history
names), after the agent-session check and the schema check (refused across a migration the old image cannot run on):
tagged settleview:local, `docker compose up -d` on it, the smoke suite, a history line.

Exit codes: 0 done and smoke-tested; 1 a step failed (it says which, and whether production changed); 2 refused
before anything ran (arguments, checkout, live agent sessions, no history).

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
HISTORY = ".deploy-history.jsonl"  # in the deploy checkout, git-ignored
LOCAL_TAG = "settleview:local"
REVISION_LABEL = "org.opencontainers.image.revision"
# Live sessions end at an app restart (application/agent_sessions.py LIVE).
SESSIONS_SQL = "select count(*) from app.agent_sessions where state in ('running', 'paused')"
# As the research role, over TCP with its password read from the secret file inside the postgres container (never on
# a command line here); the role's every transaction is read-only (deploy/postgres/initdb.sh).
SESSIONS_SH = ('PGPASSWORD="$(cat /run/secrets/pg_research_password)" psql -h 127.0.0.1 -U settleview_research '
               '-d settleview -v ON_ERROR_STOP=1 -At -c "$0"')
BACKUP_ONCE = ["bash", "/usr/local/bin/settleview-backup", "--once"]  # compose.yaml's backup service (backup.sh)
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST_REF = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
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


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Plan:
    sha: str = ""
    image: str | None = None  # a registry reference by digest (name@sha256:...); None = build the checkout
    prod_project: str = PROD_PROJECT
    staging_project: str = STAGING_PROJECT
    prod_files: list[str] = field(default_factory=list)  # extra -f files for production (tests), after compose.yaml
    offline: bool = False  # staging with no internet (deploy/staging/compose.offline.yaml)
    keep_staging: bool = False
    allow_unpublished: bool = False  # tests: a commit not (yet) on origin/main
    wait_s: int = 300
    history: Path = ROOT / HISTORY
    rollback_to: str | None = None  # rollback: an image id from the history

    @property
    def build(self) -> bool:
        return self.image is None

    @property
    def target_image(self) -> str:
        return self.image or f"settleview:{self.sha[:12]}"

    @property
    def default_project(self) -> bool:
        return self.prod_project == PROD_PROJECT

    def compose(self, project: str, *args: str) -> list[str]:
        if project == self.staging_project:
            files = [*STAGING_FILES, *([OFFLINE_FILE] if self.offline else [])]
            head = ["docker", "compose", "-p", project, "--env-file", STAGING_ENV]
        else:
            files = ["compose.yaml", *self.prod_files]
            head = ["docker", "compose", "-p", project]
        return [*head, *[a for f in files for a in ("-f", f)], *args]


class ReleaseHooks:
    """The seam for release engineering (P3 #4, deploy/deploy_image.py): cosign verification of a registry image, the
    schema check against the database's Alembic revision, and the pre-migration dump. That module replaces these
    methods; until it is wired in, `verify` refuses every registry image (nothing unverified reaches production),
    `check_schema` reports that it did not run, and `dump` is the backup service's own one-off dump."""

    def __init__(self, d: Deployer) -> None:
        self.d = d

    def verify(self, ref: str) -> None:
        """Raise Refused unless `ref` (name@sha256:...) is signed by the release workflow."""
        raise Refused(f"{ref}: signature verification (cosign, deploy/deploy_image.py) is not available in this "
                      "checkout: deploy a local build (--sha only) instead")

    def check_schema(self, image: str, *, rollback: bool) -> None:
        """Raise Refused when `image` cannot run on production's database (a rollback across an irreversible
        migration)."""
        self.d.say("schema check: not available in this checkout (deploy/deploy_image.py); the migrate job still "
                   "refuses a schema it does not know" + (" — check the migrations between the two commits by hand"
                                                        if rollback else ""))

    def dump(self) -> None:
        """A dump of production's database before the migrate job runs."""
        cid = self.d.container(self.d.plan.prod_project, "backup")
        if cid is None:
            self.d.say("pre-migration dump: no backup service running (a first deploy): skipped")
            return
        self.d.must(["docker", "exec", cid, *BACKUP_ONCE], "pre-migration dump (backup --once)", timeout=900)
        self.d.say("pre-migration dump: done (the backups volume, today's file)")


class Deployer:
    def __init__(self, plan: Plan, run: Runner = real_runner, say: Callable[[str], None] | None = None,
                 hooks: Callable[[Deployer], ReleaseHooks] = ReleaseHooks) -> None:
        self.plan = plan
        self.run = run
        self.say = say or (lambda m: print(f"deploy: {m}", flush=True))
        self.hooks = hooks(self)
        self.prod_changed = False
        self.previous: dict | None = None  # the production image before this change: {image_id, sha, tag}
        self.target: dict = {}  # the image going out: {image, image_id, digest}

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

    def image_info(self, ref: str) -> dict | None:
        r = self.run(["docker", "image", "inspect", ref])
        if r.code != 0:
            return None
        info = json.loads(r.out)[0]
        labels = (info.get("Config") or {}).get("Labels") or {}
        return {"image_id": info.get("Id"), "sha": labels.get(REVISION_LABEL),
                "digests": info.get("RepoDigests") or []}

    # -- checks -------------------------------------------------------------------------------------------------------
    def check_projects(self) -> None:
        p = self.plan
        for name in (p.prod_project, p.staging_project):
            if not PROJECT_NAME.fullmatch(name):
                raise Refused(f"{name!r} is not a compose project name")
        if p.staging_project == p.prod_project or p.staging_project == PROD_PROJECT:
            raise Refused("the staging project must differ from production (and never be `settleview`)")
        if self.run(["docker", "version", "--format", "{{.Server.Version}}"]).code != 0:
            raise Refused("Docker is not running (docker version failed)")

    def preflight(self) -> None:
        p = self.plan
        if not SHA.fullmatch(p.sha):
            raise Refused(f"--sha must be a full 40-character commit id, got {p.sha!r}")
        if p.image is not None and not DIGEST_REF.fullmatch(p.image):
            raise Refused(f"--image {p.image!r}: deploy a registry image by digest only (name@sha256:<64 hex>)")
        self.check_projects()
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

    # -- the steps ----------------------------------------------------------------------------------------------------
    def keep_previous(self) -> None:
        """Tag the image production runs now, so it outlives a rebuild of its tag (the rollback target)."""
        cid = self.container(self.plan.prod_project, "app")
        if cid is None:
            self.say("no running production app: nothing to roll back to")
            return
        image_id = json.loads(self.must(["docker", "inspect", cid], "docker inspect (production app)").out)[0]["Image"]
        tag = f"settleview:previous-{image_id.removeprefix('sha256:')[:12]}"
        if self.run(["docker", "tag", image_id, tag]).code != 0:
            # Its tag was rebuilt and the old image dropped (Docker's containerd store): the container still runs,
            # but there is no image left to return to. Said loudly and recorded; the deploy itself is not blocked.
            self.previous = {"image_id": image_id, "sha": None, "tag": None}
            self.say(f"WARNING: the running production image {image_id[:19]} is no longer on this host: there will "
                     "be no rollback target for this deploy")
            return
        info = self.image_info(tag) or {}
        self.previous = {"image_id": image_id, "sha": info.get("sha"), "tag": tag}
        self.say(f"the running production image ({image_id[:19]}, commit {(info.get('sha') or '?')[:12]}) is kept "
                 f"as {tag}")

    def image(self) -> None:
        p = self.plan
        if p.build:
            self.must(["docker", "build", "--build-arg", f"GIT_SHA={p.sha}", "-t", p.target_image, "."],
                      "docker build", capture=False, timeout=3600)
        else:
            if self.image_info(p.target_image) is None:
                self.must(["docker", "pull", p.target_image], "docker pull", capture=False, timeout=3600)
            self.hooks.verify(p.target_image)
        info = self.image_info(p.target_image)
        if info is None:
            raise Failed(f"the image {p.target_image} is not there after the build / pull")
        digest = None
        if not p.build:
            digest = p.target_image.split("@", 1)[1]
            if not any(d.endswith("@" + digest) for d in info["digests"]):
                raise Failed(f"the local image does not carry the digest {digest}")
        if info["sha"] != p.sha:
            raise Failed(f"the image's {REVISION_LABEL} is {info['sha']!r}, not {p.sha}: it was not built from that "
                         "commit")
        self.target = {"image": p.target_image, "image_id": info["image_id"], "digest": digest}
        self.say(f"image {p.target_image} ({info['image_id'][:19]}) is commit {p.sha[:12]}")

    def up(self, project: str, image: str) -> None:
        env = {"SETTLEVIEW_IMAGE": image, "GIT_SHA": self.plan.sha}
        self.must(self.plan.compose(project, "up", "-d", "--no-build", "--pull", "never", "--remove-orphans"),
                  f"compose up ({project})", env=env, capture=False, timeout=900)

    def smoke(self, project: str) -> bool:
        cmd = [sys.executable, "scripts/dev.py", "smoke", "--project", project, "--wait", str(self.plan.wait_s)]
        self.say("+ " + " ".join(cmd))
        return self.run(cmd, capture=False, timeout=self.plan.wait_s + 900).code == 0

    def staging(self) -> None:
        self.must([sys.executable, "deploy/postgres/make_secrets.py", STAGING_SECRETS], "staging secrets")
        self.up(self.plan.staging_project, self.plan.target_image)
        if not self.smoke(self.plan.staging_project):
            raise Failed(f"the smoke suite failed on staging: production was NOT touched. Inspect with `docker compose "
                         f"-p {self.plan.staging_project} logs`; remove with "
                         f"`{' '.join(self.plan.compose(self.plan.staging_project, 'down', '--volumes'))}`")
        self.say("staging passed the smoke suite")

    def go_live(self, image: str, *, rollback: bool) -> None:
        """Production on `image`: sessions again, schema check, dump, up, smoke; settleview:local follows on success."""
        self.check_sessions()  # again: a session may have started while staging ran
        self.hooks.check_schema(image, rollback=rollback)
        self.hooks.dump()
        self.prod_changed = True
        self.up(self.plan.prod_project, image)
        if not self.smoke(self.plan.prod_project):
            raise Failed("the smoke suite failed on PRODUCTION after the change: `python scripts/deploy.py rollback`"
                         if not rollback else "the smoke suite failed on PRODUCTION after the rollback")
        if self.plan.default_project:
            self.must(["docker", "tag", image, LOCAL_TAG], f"docker tag {LOCAL_TAG}")

    def record(self, action: str, result: str) -> None:
        p = self.plan
        line = {"ts": utc_now(), "action": action, "project": p.prod_project, "sha": p.sha or None,
                "image": self.target.get("image"), "image_id": self.target.get("image_id"),
                "digest": self.target.get("digest"), "previous": self.previous, "result": result}
        with p.history.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.say(f"recorded in {p.history.name}: {action} {result}")

    def finish_staging(self) -> None:
        if not self.plan.keep_staging:
            self.must(self.plan.compose(self.plan.staging_project, "stop"), "compose stop (staging)", timeout=300)

    # -- the commands -------------------------------------------------------------------------------------------------
    def _guarded(self, action: str, body: Callable[[], None]) -> int:
        try:
            body()
        except Refused as e:
            self.say(f"REFUSED: {e}")
            return 2
        except Failed as e:
            self.say(f"FAILED: {e}")
            self.say("production was changed" if self.prod_changed else "production was NOT changed")
            if self.prod_changed:
                self.record(action, "smoke_failed")
            return 1
        return 0

    def deploy(self) -> int:
        def body() -> None:
            self.preflight()
            self.check_sessions()
            self.keep_previous()
            self.image()
            self.staging()
            self.go_live(self.plan.target_image, rollback=False)
            self.record("deploy", "ok")
            self.finish_staging()
            self.say(f"DEPLOYED {self.plan.target_image} (commit {self.plan.sha[:12]}) to {self.plan.prod_project}; "
                     "staging and production passed the smoke suite. To undo: python scripts/deploy.py rollback")
        return self._guarded("deploy", body)

    def history(self) -> list[dict]:
        try:
            lines = self.plan.history.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [r for r in (json.loads(x) for x in lines if x.strip()) if r.get("project") == self.plan.prod_project]

    def rollback_target(self) -> dict:
        rows = self.history()
        if not rows:
            raise Refused(f"no history for {self.plan.prod_project} in {self.plan.history.name}: nothing to roll back to")
        if self.plan.rollback_to:
            known = [r for r in rows if r.get("image_id") == self.plan.rollback_to] + [
                r["previous"] for r in rows if (r.get("previous") or {}).get("image_id") == self.plan.rollback_to]
            if not known:
                raise Refused(f"{self.plan.rollback_to} is not an image this history deployed")
            k = known[-1]
            return {"image_id": k["image_id"], "sha": k.get("sha"), "tag": k.get("tag") or k.get("image")}
        prev = rows[-1].get("previous")
        if not prev:
            raise Refused("the last production change has no previous image (it was the first deploy)")
        return prev

    def rollback(self) -> int:
        def body() -> None:
            self.check_projects()
            t = self.rollback_target()
            ref = t.get("tag") or t["image_id"]
            info = self.image_info(ref) or self.image_info(t["image_id"])
            if info is None or info["image_id"] != t["image_id"]:
                raise Refused(f"the image {ref} ({t['image_id'][:19]}) is no longer on this host")
            self.plan.sha = t.get("sha") or ""
            head = self.run(["git", "rev-parse", "HEAD"]).out.strip()
            if self.plan.sha and head != self.plan.sha:
                self.say(f"note: the checkout is at {head[:12]}, the image is commit {self.plan.sha[:12]}: the "
                         f"compose files are this checkout's (`git checkout {self.plan.sha}` first to use that "
                         "release's)")
            self.keep_previous()
            self.target = {"image": ref, "image_id": t["image_id"], "digest": None}
            self.go_live(ref, rollback=True)
            self.record("rollback", "ok")
            self.say(f"ROLLED BACK {self.plan.prod_project} to {ref} (commit {(self.plan.sha or '?')[:12]})")
        return self._guarded("rollback", body)

    def describe(self) -> list[str]:
        """The plan as the commands it would run (--dry-run)."""
        p = self.plan
        build = (f"docker build --build-arg GIT_SHA={p.sha} -t {p.target_image} ." if p.build
                 else f"docker pull {p.target_image}   # unless present; cosign, digest and revision label checked")
        live = " ".join(p.compose(p.prod_project, "up", "-d", "--no-build", "--pull", "never"))
        return [f"check: the checkout is at {p.sha[:12]} and clean"
                + ("" if p.allow_unpublished else ", and the commit is on origin/main"),
                f"check: no live agent session on {p.prod_project} (settleview_research, read-only)",
                "docker tag <running production image> settleview:previous-<its id>", build,
                f"python deploy/postgres/make_secrets.py {STAGING_SECRETS}",
                f"SETTLEVIEW_IMAGE={p.target_image} " + " ".join(p.compose(p.staging_project, "up", "-d",
                                                                             "--no-build", "--pull", "never")),
                f"python scripts/dev.py smoke --project {p.staging_project} --wait {p.wait_s}",
                f"check: no live agent session on {p.prod_project} (again); schema check; pre-migration dump",
                f"SETTLEVIEW_IMAGE={p.target_image} {live}",
                f"python scripts/dev.py smoke --project {p.prod_project} --wait {p.wait_s}",
                *([f"docker tag {p.target_image} {LOCAL_TAG}"] if p.default_project else []),
                f"append to {HISTORY}",
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
    ap = argparse.ArgumentParser(description="Production deploys (gated on staging and the smoke suite) and rollbacks.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--prod-project", default=PROD_PROJECT, help=argparse.SUPPRESS)
    common.add_argument("--staging-project", default=STAGING_PROJECT, help="staging's compose project")
    common.add_argument("--prod-file", action="append", default=[], help=argparse.SUPPRESS)  # tests: throwaway "prod"
    common.add_argument("--history", default=str(ROOT / HISTORY), help=argparse.SUPPRESS)
    common.add_argument("--wait", type=int, default=300, help="seconds a stack may take to become healthy")
    d = sub.add_parser("deploy", parents=[common], help="gate on staging, then deploy production")
    d.add_argument("--sha", required=True, help="the commit to deploy (40 hex; on origin/main)")
    d.add_argument("--image", help="deploy this registry image (name@sha256:<digest>) instead of building")
    d.add_argument("--offline", action="store_true", help="staging without internet (compose.offline.yaml)")
    d.add_argument("--keep-staging", action="store_true", help="leave staging running afterwards")
    d.add_argument("--allow-unpublished", action="store_true", help=argparse.SUPPRESS)  # tests only
    d.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
    r = sub.add_parser("rollback", parents=[common], help="production back to the previous image in the history")
    r.add_argument("--to", help="an image id (sha256:...) the history names")
    a = ap.parse_args(argv)
    plan = Plan(prod_project=a.prod_project, staging_project=a.staging_project, prod_files=a.prod_file,
                history=Path(a.history), wait_s=a.wait)
    if a.cmd == "rollback":
        if a.to and not IMAGE_ID.fullmatch(a.to):
            ap.error("--to must be an image id, sha256:<64 hex>")
        plan.rollback_to = a.to
        return Deployer(plan, run).rollback()
    plan.sha, plan.image, plan.offline = a.sha.strip().lower(), a.image, a.offline
    plan.keep_staging, plan.allow_unpublished = a.keep_staging, a.allow_unpublished
    dep = Deployer(plan, run)
    if a.dry_run:
        print("\n".join(dep.describe()))
        return 0
    return dep.deploy()


if __name__ == "__main__":
    sys.exit(main())
