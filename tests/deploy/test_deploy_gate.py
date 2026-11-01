"""scripts/deploy.py, the deploy gate, with a recording fake in place of Docker and git: the order of the steps, what
stops it, and that production is never touched before staging passed. (It ran for real against throwaway projects:
docs/operations.md, "Staging and the deploy gate".)"""

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
DIGEST = "ghcr.io/owner/settleview@sha256:" + "b" * 64


class Fake:
    """Answers the commands deploy.py runs; records every one. `sessions` = live agent sessions on production;
    `smoke` = {project: passes}."""

    def __init__(self, *, sessions: list[int] | None = None, smoke: dict[str, bool] | None = None, head: str = SHA,
                 dirty: str = "", on_main: bool = True, revision: str = SHA, digests: list[str] | None = None,
                 prod_running: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.sessions = list(sessions or [0, 0])
        self.smoke_ok = smoke or {}
        self.head, self.dirty, self.on_main, self.revision = head, dirty, on_main, revision
        self.digests = digests or []
        self.prod_running = prod_running

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
        if cmd[:2] == ["docker", "exec"]:
            return R(0, f"{self.sessions.pop(0)}\n")
        if cmd[:3] == ["docker", "image", "inspect"]:
            return R(0, json.dumps([{"Id": "sha256:" + "c" * 64, "RepoDigests": self.digests,
                                     "Config": {"Labels": {dg.REVISION_LABEL: self.revision}}}]))
        if cmd[:2] == ["docker", "inspect"]:
            return R(0, json.dumps([{"Image": "sha256:" + "d" * 64}]))
        if "smoke" in cmd:
            return R(0 if self.smoke_ok.get(cmd[cmd.index("--project") + 1], True) else 1)
        return R(0)

    def ran(self, *words: str) -> list[list[str]]:
        return [c for c in self.calls if all(w in c for w in words)]

    def index(self, *words: str) -> int:
        return next(i for i, c in enumerate(self.calls) if all(w in c for w in words))


def deploy(fake: Fake, **plan) -> tuple[int, list[str]]:
    said: list[str] = []
    code = dg.Deployer(dg.Plan(sha=SHA, stamp="20261101-120000", **plan), fake, said.append).deploy()
    return code, said


def test_the_whole_gate_in_order_staging_before_production():
    f = Fake()
    code, said = deploy(f)
    assert code == 0, said
    build = f.index("docker", "build")
    stg_up = f.index("-p", "settleview-staging", "up")
    stg_smoke = f.index("smoke", "settleview-staging")
    tag_rb = f.index("docker", "tag", "settleview:rollback-20261101-120000")
    prod_up = f.index("-p", "settleview", "up")
    prod_smoke = f.index("smoke", "settleview")
    assert build < stg_up < stg_smoke < tag_rb < prod_up < prod_smoke
    assert f.calls[build][-4:] == ["GIT_SHA=" + SHA, "-t", "settleview:aaaaaaaaaaaa", "."]
    # both stacks run exactly the built image; staging with its own override and env file
    for i in (stg_up, prod_up):
        assert f.envs[i]["SETTLEVIEW_IMAGE"] == "settleview:aaaaaaaaaaaa"
        assert {"--no-build", "--pull", "never"} <= set(f.calls[i])
    assert "deploy/staging/compose.staging.yaml" in f.calls[stg_up] and "deploy/staging/staging.env" in f.calls[stg_up]
    assert "deploy/staging/compose.staging.yaml" not in f.calls[prod_up]
    assert f.ran("docker", "tag", "settleview:aaaaaaaaaaaa", "settleview:local")  # the default project only
    assert f.ran("-p", "settleview-staging", "stop")
    assert any("SETTLEVIEW_IMAGE=settleview:rollback-20261101-120000" in s for s in said)  # the rollback command
    assert len(f.ran("docker", "exec")) == 2  # agent sessions checked before the build and again before production


def test_live_agent_sessions_refuse_before_anything_runs():
    f = Fake(sessions=[2])
    code, said = deploy(f)
    assert code == 2 and "2 agent session(s) are live" in said[-1]
    assert not f.ran("docker", "build") and not f.ran("up")
    q = f.ran("docker", "exec")[0]
    assert "settleview_research" in q[-2] and "agent_sessions" in q[-1] and "PGPASSWORD" in q[-2]


def test_a_session_started_during_staging_still_stops_production():
    f = Fake(sessions=[0, 1])
    code, _ = deploy(f)
    assert code == 2
    assert f.ran("-p", "settleview-staging", "up") and not f.ran("-p", "settleview", "up")


def test_a_failed_staging_smoke_never_touches_production():
    f = Fake(smoke={"settleview-staging": False})
    code, said = deploy(f)
    assert code == 1 and "production was NOT changed" in said
    assert not f.ran("-p", "settleview", "up") and not f.ran("docker", "tag")


def test_a_failed_production_smoke_prints_the_rollback():
    f = Fake(smoke={"settleview": False})
    code, said = deploy(f)
    assert code == 1 and "production was changed" in said
    assert any("to roll back" in s and "git checkout" in s for s in said)
    assert not f.ran("docker", "tag", "settleview:aaaaaaaaaaaa", "settleview:local")


@pytest.mark.parametrize(("kw", "why"), [
    ({"head": "b" * 40}, "the checkout is at"),
    ({"dirty": " M compose.yaml"}, "uncommitted changes"),
    ({"on_main": False}, "not on origin/main"),
])
def test_the_checkout_must_be_the_published_commit(kw, why):
    f = Fake(**kw)
    code, said = deploy(f)
    assert code == 2 and why in said[-1]
    assert not f.ran("docker", "exec") and not f.ran("up")


def test_a_wrong_revision_label_stops_before_staging():
    f = Fake(revision="e" * 40)
    code, said = deploy(f)
    assert code == 1 and any("was not built from that commit" in s for s in said)
    assert not f.ran("up")


def test_deploy_by_digest_pulls_and_checks_the_digest():
    f = Fake(digests=["ghcr.io/owner/settleview@sha256:" + "b" * 64])
    code, _ = deploy(f, image=DIGEST)
    assert code == 0 and not f.ran("docker", "build")
    assert f.envs[f.index("-p", "settleview", "up")]["SETTLEVIEW_IMAGE"] == DIGEST
    f = Fake(digests=["ghcr.io/owner/settleview@sha256:" + "f" * 64])
    code, said = deploy(f, image=DIGEST)
    assert code == 1 and any("does not carry the digest" in s for s in said)


@pytest.mark.parametrize("plan", [
    {"staging_project": "settleview"}, {"prod_project": "x", "staging_project": "x"}, {"prod_project": "Bad Name"},
])
def test_project_names_are_guarded(plan):
    code, said = deploy(Fake(), **plan)
    assert code == 2


def test_a_throwaway_production_project_never_retags_settleview_local():
    f = Fake()
    f.prod_running = False
    code, said = deploy(f, prod_project="settleview-p3t-prod", staging_project="settleview-staging-p3t",
                        prod_files=["x.yaml"], offline=True)
    assert code == 0
    assert not f.ran("settleview:local")
    up = f.calls[f.index("-p", "settleview-staging-p3t", "up")]
    assert "deploy/staging/compose.offline.yaml" in up
    assert "x.yaml" in f.calls[f.index("-p", "settleview-p3t-prod", "up")]


def test_every_command_runs_without_git_bash_path_conversion_and_without_compose_overrides(monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "settleview")
    monkeypatch.setenv("SETTLEVIEW_IMAGE", "evil:latest")
    env = dg.child_env({"SETTLEVIEW_IMAGE": "settleview:x"})
    assert env["MSYS_NO_PATHCONV"] == "1" and env["MSYS2_ARG_CONV_EXCL"] == "*"
    assert "COMPOSE_PROJECT_NAME" not in env and env["SETTLEVIEW_IMAGE"] == "settleview:x"
    assert "SETTLEVIEW_IMAGE" not in dg.child_env()


def test_an_argument_git_bash_already_rewrote_is_refused(capsys):
    assert dg.main(["--sha", SHA, "--prod-file", "C:/Program Files/Git/app/x.yaml"], run=Fake()) == 2
    assert "MSYS_NO_PATHCONV=1" in capsys.readouterr().err
    assert dg.mangled(["--x=C:/msys64/run/secrets/a", "C:/Users/me/x"]) == ["--x=C:/msys64/run/secrets/a"]  # prepush-scan: allow (placeholder path)


def test_dry_run_prints_the_plan_and_runs_nothing(capsys):
    f = Fake()
    assert dg.main(["--sha", SHA, "--dry-run"], run=f) == 0
    out = capsys.readouterr().out
    assert not f.calls
    assert out.index("settleview-staging") < out.index("smoke --project settleview-staging") < out.index(
        "-p settleview -f compose.yaml up")
