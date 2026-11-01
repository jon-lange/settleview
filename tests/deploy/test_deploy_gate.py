"""scripts/deploy.py, the one entry point for production deploys and rollbacks, with a recording fake in place of
Docker and git: the order of the steps, what stops it, that production is never touched before staging passed, the
history it keeps and the rollback that reads it. (It also ran for real against throwaway projects: docs/operations.md,
"Staging and the deploy gate".)"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("deploy_gate", ROOT / "scripts" / "deploy.py")
dg = importlib.util.module_from_spec(_spec)
sys.modules["deploy_gate"] = dg  # dataclasses look their module up
_spec.loader.exec_module(dg)

SHA = "a" * 40
OLD_SHA = "9" * 40
DIGEST = "ghcr.io/owner/settleview@sha256:" + "b" * 64
NEW_ID = "sha256:" + "c" * 64
OLD_ID = "sha256:" + "d" * 64


class Fake:
    """Answers the commands deploy.py runs; records every one. `sessions` = live agent sessions on production, one per
    check; `smoke` = {project: passes}; `present` = image references already on the host."""

    def __init__(self, *, sessions: list[int] | None = None, smoke: dict[str, bool] | None = None, head: str = SHA,
                 dirty: str = "", on_main: bool = True, revision: str = SHA, digests: list[str] | None = None,
                 prod_running: bool = True, present: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.sessions = list(sessions or [0, 0])
        self.smoke_ok = smoke or {}
        self.head, self.dirty, self.on_main, self.revision = head, dirty, on_main, revision
        self.digests = digests or []
        self.prod_running = prod_running
        self.present = present if present is not None else {"settleview:aaaaaaaaaaaa", DIGEST}

    def __call__(self, cmd, *, env=None, capture=True, timeout=0):
        self.calls.append(list(cmd))
        self.envs.append(env)
        R = dg.Result
        c = " ".join(cmd)
        if cmd[:2] == ["docker", "version"]:
            return R(0, "29.0.0")
        if cmd[:2] == ["git", "rev-parse"]:
            return R(0, self.head + "\n")
        if cmd[:2] == ["git", "status"]:
            return R(0, self.dirty)
        if cmd[:2] == ["git", "merge-base"]:
            return R(0 if self.on_main else 1)
        if cmd[:2] == ["docker", "ps"]:
            return R(0, ("cid-" + c.split("service=")[1].split()[0] + "\n") if self.prod_running
                     and "project=settleview " in c + " " else "")
        if cmd[:2] == ["docker", "exec"] and "psql" in c:
            return R(0, f"{self.sessions.pop(0)}\n")
        if cmd[:3] == ["docker", "image", "inspect"]:
            ref = cmd[3]
            if ref.startswith("settleview:previous-") or ref == OLD_ID:
                return R(0, json.dumps([{"Id": OLD_ID, "Config": {"Labels": {dg.REVISION_LABEL: OLD_SHA}}}]))
            if ref not in self.present:
                return R(1, "", "No such image")
            return R(0, json.dumps([{"Id": NEW_ID, "RepoDigests": self.digests,
                                     "Config": {"Labels": {dg.REVISION_LABEL: self.revision}}}]))
        if cmd[:2] == ["docker", "pull"]:
            self.present.add(cmd[2])
            return R(0)
        if cmd[:2] == ["docker", "inspect"]:
            return R(0, json.dumps([{"Image": OLD_ID}]))
        if "smoke" in cmd:
            return R(0 if self.smoke_ok.get(cmd[cmd.index("--project") + 1], True) else 1)
        return R(0)

    def ran(self, *words: str) -> list[list[str]]:
        return [c for c in self.calls if all(w in c for w in words)]

    def index(self, *words: str) -> int:
        return next(i for i, c in enumerate(self.calls) if all(w in c for w in words))


class Verified(dg.ReleaseHooks):
    """Release engineering's checks as they will be wired in: verification passes, the rest is recorded."""

    seen: list[str] = []

    def verify(self, ref):
        Verified.seen.append(f"verify {ref}")

    def check_schema(self, image, *, rollback):
        Verified.seen.append(f"schema {image} {rollback}")


def deployer(fake: Fake, tmp_path: Path, hooks=dg.ReleaseHooks, **plan) -> tuple[dg.Deployer, list[str]]:
    said: list[str] = []
    plan.setdefault("sha", SHA)
    return dg.Deployer(dg.Plan(history=tmp_path / "h.jsonl", **plan), fake, said.append, hooks), said


def history(tmp_path: Path) -> list[dict]:
    return [json.loads(x) for x in (tmp_path / "h.jsonl").read_text(encoding="utf-8").splitlines()]


def research_queries(f: Fake) -> list[list[str]]:
    return [c for c in f.calls if "settleview_research" in " ".join(c)]


def test_the_whole_gate_in_order_staging_before_production(tmp_path):
    f = Fake()
    d, said = deployer(f, tmp_path)
    assert d.deploy() == 0, said
    keep = f.index("docker", "tag", OLD_ID, "settleview:previous-dddddddddddd")
    build = f.index("docker", "build")
    stg_up = f.index("-p", "settleview-staging", "up")
    stg_smoke = f.index("smoke", "settleview-staging")
    dump = f.index("docker", "exec", "cid-backup")
    prod_up = f.index("-p", "settleview", "up")
    prod_smoke = f.index("smoke", "settleview")
    local = f.index("docker", "tag", "settleview:aaaaaaaaaaaa", "settleview:local")
    assert keep < build < stg_up < stg_smoke < dump < prod_up < prod_smoke < local
    assert f.calls[dump][-3:] == ["bash", "/usr/local/bin/settleview-backup", "--once"]
    assert f.calls[build][-4:] == ["GIT_SHA=" + SHA, "-t", "settleview:aaaaaaaaaaaa", "."]
    for i in (stg_up, prod_up):  # both stacks run exactly the built image
        assert f.envs[i]["SETTLEVIEW_IMAGE"] == "settleview:aaaaaaaaaaaa"
        assert {"--no-build", "--pull", "never"} <= set(f.calls[i])
    assert "deploy/staging/compose.staging.yaml" in f.calls[stg_up] and "deploy/staging/staging.env" in f.calls[stg_up]
    assert "deploy/staging/compose.staging.yaml" not in f.calls[prod_up]
    assert f.ran("-p", "settleview-staging", "stop")
    assert len(research_queries(f)) == 2  # before anything, and again before production
    (h,) = history(tmp_path)
    assert h["action"] == "deploy" and h["result"] == "ok" and h["sha"] == SHA and h["project"] == "settleview"
    assert h["image"] == "settleview:aaaaaaaaaaaa" and h["image_id"] == NEW_ID and h["digest"] is None
    assert h["previous"] == {"image_id": OLD_ID, "sha": OLD_SHA, "tag": "settleview:previous-dddddddddddd"}


def test_live_agent_sessions_refuse_before_anything_runs(tmp_path):
    f = Fake(sessions=[2])
    d, said = deployer(f, tmp_path)
    assert d.deploy() == 2 and "2 agent session(s) are live" in said[-1]
    assert not f.ran("docker", "build") and not f.ran("up") and not f.ran("docker", "tag")
    (q,) = research_queries(f)
    assert "PGPASSWORD" in q[-2] and "agent_sessions" in q[-1]
    assert not (tmp_path / "h.jsonl").exists()


def test_a_session_started_during_staging_still_stops_production(tmp_path):
    f = Fake(sessions=[0, 1])
    d, _ = deployer(f, tmp_path)
    assert d.deploy() == 2
    assert f.ran("-p", "settleview-staging", "up") and not f.ran("-p", "settleview", "up")


def test_a_failed_staging_smoke_never_touches_production(tmp_path):
    f = Fake(smoke={"settleview-staging": False})
    d, said = deployer(f, tmp_path)
    assert d.deploy() == 1 and "production was NOT changed" in said
    assert not f.ran("-p", "settleview", "up") and not f.ran("settleview:local")
    assert not (tmp_path / "h.jsonl").exists()


def test_a_failed_production_smoke_is_recorded_and_points_at_rollback(tmp_path):
    f = Fake(smoke={"settleview": False})
    d, said = deployer(f, tmp_path)
    assert d.deploy() == 1 and "production was changed" in said
    assert any("scripts/deploy.py rollback" in s for s in said)
    assert not f.ran("docker", "tag", "settleview:aaaaaaaaaaaa", "settleview:local")
    assert history(tmp_path)[0]["result"] == "smoke_failed"


@pytest.mark.parametrize(("kw", "why"), [
    ({"head": "b" * 40}, "the checkout is at"),
    ({"dirty": " M compose.yaml"}, "uncommitted changes"),
    ({"on_main": False}, "not on origin/main"),
])
def test_the_checkout_must_be_the_published_commit(kw, why, tmp_path):
    f = Fake(**kw)
    d, said = deployer(f, tmp_path)
    assert d.deploy() == 2 and why in said[-1]
    assert not f.ran("docker", "exec") and not f.ran("up")


def test_a_wrong_revision_label_stops_before_staging(tmp_path):
    f = Fake(revision="e" * 40)
    d, said = deployer(f, tmp_path)
    assert d.deploy() == 1 and any("was not built from that commit" in s for s in said)
    assert not f.ran("up")


def test_a_registry_image_needs_a_digest_and_the_release_verification(tmp_path):
    d, said = deployer(Fake(), tmp_path, image="ghcr.io/owner/settleview:1.2.3")
    assert d.deploy() == 2 and "by digest only" in said[-1]
    # Until deploy/deploy_image.py is wired in, nothing unverified is deployed.
    f = Fake(digests=[DIGEST], present=set())
    d, said = deployer(f, tmp_path, image=DIGEST)
    assert d.deploy() == 2 and "signature verification" in said[-1] and not f.ran("up")


def test_deploy_by_digest_with_the_release_hooks(tmp_path):
    Verified.seen = []
    f = Fake(digests=[DIGEST], present=set())
    d, said = deployer(f, tmp_path, Verified, image=DIGEST)
    assert d.deploy() == 0, said
    assert f.ran("docker", "pull", DIGEST) and not f.ran("docker", "build")
    assert Verified.seen == [f"verify {DIGEST}", f"schema {DIGEST} False"]
    assert f.envs[f.index("-p", "settleview", "up")]["SETTLEVIEW_IMAGE"] == DIGEST
    assert history(tmp_path)[0]["digest"] == "sha256:" + "b" * 64
    f = Fake(digests=["ghcr.io/owner/settleview@sha256:" + "f" * 64])
    d, said = deployer(f, tmp_path, Verified, image=DIGEST)
    assert d.deploy() == 1 and any("does not carry the digest" in s for s in said)


@pytest.mark.parametrize("plan", [
    {"staging_project": "settleview"}, {"prod_project": "x", "staging_project": "x"}, {"prod_project": "Bad Name"},
])
def test_project_names_are_guarded(plan, tmp_path):
    d, _ = deployer(Fake(), tmp_path, **plan)
    assert d.deploy() == 2


def test_a_throwaway_production_project_never_retags_settleview_local(tmp_path):
    f = Fake(prod_running=False)
    d, said = deployer(f, tmp_path, prod_project="settleview-p3t-prod", staging_project="settleview-staging-p3t",
                       prod_files=["x.yaml"], offline=True)
    assert d.deploy() == 0, said
    assert not f.ran("settleview:local")
    assert "deploy/staging/compose.offline.yaml" in f.calls[f.index("-p", "settleview-staging-p3t", "up")]
    assert "x.yaml" in f.calls[f.index("-p", "settleview-p3t-prod", "up")]
    assert history(tmp_path)[0]["previous"] is None  # a first deploy


def test_rollback_goes_to_the_previous_image_of_the_last_change(tmp_path):
    Verified.seen = []
    d, _ = deployer(Fake(), tmp_path)
    assert d.deploy() == 0
    f = Fake()
    d, said = deployer(f, tmp_path, Verified, sha="")
    assert d.rollback() == 0, said
    tag = "settleview:previous-dddddddddddd"
    up = f.index("-p", "settleview", "up")
    assert f.envs[up]["SETTLEVIEW_IMAGE"] == tag
    assert f.index("docker", "exec", "cid-backup") < up < f.index("smoke", "settleview") < f.index(
        "docker", "tag", tag, "settleview:local")
    assert not f.ran("-p", "settleview-staging", "up") and Verified.seen == [f"schema {tag} True"]
    assert any("the checkout is at aaaaaaaaaaaa, the image is commit 999999999999" in s for s in said)
    last = history(tmp_path)[-1]
    assert last["action"] == "rollback" and last["image_id"] == OLD_ID and last["sha"] == OLD_SHA


def test_rollback_refuses_without_history_with_live_sessions_or_an_unknown_image(tmp_path):
    d, said = deployer(Fake(), tmp_path, sha="")
    assert d.rollback() == 2 and "no history" in said[-1]
    assert deployer(Fake(), tmp_path)[0].deploy() == 0
    f = Fake(sessions=[3])
    d, said = deployer(f, tmp_path, sha="")
    assert d.rollback() == 2 and "3 agent session(s)" in said[-1] and not f.ran("up")
    d, said = deployer(Fake(), tmp_path, sha="", rollback_to="sha256:" + "0" * 64)
    assert d.rollback() == 2 and "not an image this history deployed" in said[-1]


def test_every_command_runs_without_git_bash_path_conversion_and_without_compose_overrides(monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "settleview")
    monkeypatch.setenv("SETTLEVIEW_IMAGE", "evil:latest")
    env = dg.child_env({"SETTLEVIEW_IMAGE": "settleview:x"})
    assert env["MSYS_NO_PATHCONV"] == "1" and env["MSYS2_ARG_CONV_EXCL"] == "*"
    assert "COMPOSE_PROJECT_NAME" not in env and env["SETTLEVIEW_IMAGE"] == "settleview:x"
    assert "SETTLEVIEW_IMAGE" not in dg.child_env()


def test_an_argument_git_bash_already_rewrote_is_refused(capsys):
    assert dg.main(["deploy", "--sha", SHA, "--prod-file", "C:/Program Files/Git/app/x.yaml"], run=Fake()) == 2
    assert "MSYS_NO_PATHCONV=1" in capsys.readouterr().err
    assert dg.mangled(["--x=C:/msys64/run/secrets/a", "C:/Users/me/x"]) == ["--x=C:/msys64/run/secrets/a"]  # prepush-scan: allow (placeholder path)


def test_dry_run_prints_the_plan_and_runs_nothing(capsys):
    f = Fake()
    assert dg.main(["deploy", "--sha", SHA, "--dry-run"], run=f) == 0
    out = capsys.readouterr().out
    assert not f.calls
    assert out.index("settleview-staging") < out.index("smoke --project settleview-staging") < out.index(
        "-p settleview -f compose.yaml up")
