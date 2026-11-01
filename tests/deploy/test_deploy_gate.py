"""scripts/deploy.py, the one entry point for production deploys, with a recording fake in place of Docker, git and
cosign: the order of the standard commands, what stops it, and that production is never touched before staging passed.
(It also ran for real against throwaway projects: docs/operations.md, "Staging and the deploy gate".)"""

from __future__ import annotations

import importlib.util
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


class Fake:
    """Answers the commands deploy.py runs; records every one. `sessions` = live agent sessions on production, one per
    check; `fail` = a word of the commands that fail; `smoke_fail` = projects whose smoke run fails."""

    def __init__(self, *, sessions: list[int] | None = None, fail: tuple[str, ...] = (),
                 smoke_fail: tuple[str, ...] = (), head: str = SHA,
                 dirty: str = "", on_main: bool = True, prod_running: bool = True,
                 running_image: str = "settleview:local") -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.sessions = list(sessions or [0, 0])
        self.fail, self.smoke_fail = fail, smoke_fail
        self.head, self.dirty, self.on_main = head, dirty, on_main
        self.prod_running = prod_running
        self.running_image = running_image

    def __call__(self, cmd, *, env=None, capture=True, timeout=0):
        self.calls.append(list(cmd))
        self.envs.append(env)
        R = dg.Result
        c = " ".join(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            return R(0, self.head + "\n")
        if cmd[:2] == ["git", "status"]:
            return R(0, self.dirty)
        if cmd[:2] == ["git", "merge-base"]:
            return R(0 if self.on_main else 1)
        if cmd[:3] == ["git", "remote", "get-url"]:
            return R(0, "git@github.com:owner/settleview.git\n")
        if cmd[:2] == ["docker", "ps"]:
            return R(0, ("cid-" + c.split("service=")[1].split()[0] + "\n") if self.prod_running
                     and "project=settleview " in c + " " else "")
        if cmd[:2] == ["docker", "exec"] and "psql" in c:
            return R(0, f"{self.sessions.pop(0)}\n")
        if cmd[:2] == ["docker", "inspect"]:
            return R(0, f"{OLD_SHA} {self.running_image}\n")
        if "scripts/smoke.py" in cmd:
            return R(1 if cmd[-1] in self.smoke_fail else 0)
        if any(w in cmd for w in self.fail):
            return R(1, "", "failed")
        return R(0)

    def ran(self, *words: str) -> list[list[str]]:
        return [c for c in self.calls if all(w in c for w in words)]

    def index(self, *words: str) -> int:
        return next(i for i, c in enumerate(self.calls) if all(w in c for w in words))


def deployer(fake: Fake, **plan) -> tuple[dg.Deployer, list[str]]:
    said: list[str] = []
    plan.setdefault("sha", SHA)
    return dg.Deployer(dg.Plan(**plan), fake, said.append), said


def research_queries(f: Fake) -> list[list[str]]:
    return [c for c in f.calls if "settleview_research" in " ".join(c)]


def test_the_whole_gate_in_order_staging_before_production():
    f = Fake()
    d, said = deployer(f)
    assert d.deploy() == 0, said
    build = f.index("docker", "build")
    stg_up = f.index("-p", "settleview-staging", "up")
    stg_smoke = f.index("scripts/smoke.py", "settleview-staging")
    dump = f.index("-p", "settleview", "exec", "backup")
    migrate = f.index("-p", "settleview", "run", "--rm", "migrate")
    prod_up = f.index("-p", "settleview", "up")
    prod_smoke = f.index("scripts/smoke.py", "settleview")
    local = f.index("docker", "tag", "settleview:aaaaaaaaaaaa", "settleview:local")
    pull = f.index("-p", "settleview-staging", "pull", "--quiet", "--ignore-buildable")  # postgres etc., not the build
    assert build < pull < stg_up < stg_smoke < dump < migrate < prod_up < prod_smoke < local
    dev_image = f.index("-f", "deploy/dev/compose.yaml", "build", "dev")  # the image the smoke suite runs in
    assert build < dev_image < stg_smoke
    assert f.calls[build][-4:] == ["GIT_SHA=" + SHA, "-t", "settleview:aaaaaaaaaaaa", "."]
    for i in (stg_up, migrate, prod_up):  # every stack step runs exactly the built image
        assert f.envs[i]["SETTLEVIEW_IMAGE"] == "settleview:aaaaaaaaaaaa"
    for i in (stg_up, prod_up):  # compose's own healthcheck wait, never a build or a pull here
        assert {"--wait", "--no-build", "--pull", "never"} <= set(f.calls[i])
    assert "deploy/staging/compose.staging.yaml" in f.calls[stg_up] and "deploy/staging/staging.env" in f.calls[stg_up]
    assert "deploy/staging/compose.staging.yaml" not in f.calls[prod_up]
    assert f.ran("-p", "settleview-staging", "stop")
    assert len(research_queries(f)) == 2  # before anything, and again before production
    assert any(f"git checkout {OLD_SHA} && python scripts/deploy.py --sha {OLD_SHA}" in s for s in said)


def test_live_agent_sessions_refuse_before_anything_runs():
    f = Fake(sessions=[2])
    d, said = deployer(f)
    assert d.deploy() == 2 and "2 agent session(s) are live" in said[-1]
    assert not f.ran("docker", "build") and not f.ran("up")
    (q,) = research_queries(f)
    assert "PGPASSWORD" in q[-2] and "agent_sessions" in q[-1]


def test_a_session_started_during_staging_still_stops_production():
    f = Fake(sessions=[0, 1])
    d, _ = deployer(f)
    assert d.deploy() == 2
    assert f.ran("-p", "settleview-staging", "up") and not f.ran("-p", "settleview", "up")
    assert not f.ran("-p", "settleview", "run")


def test_a_failed_staging_smoke_never_touches_production():
    f = Fake(smoke_fail=("settleview-staging",))
    d, said = deployer(f)
    assert d.deploy() == 1 and "production was NOT changed" in said
    assert not f.ran("-p", "settleview", "up") and not f.ran("migrate") and not f.ran("settleview:local")


def test_a_failed_migration_stops_before_the_app_is_replaced():
    f = Fake(fail=("migrate",))
    d, said = deployer(f)
    assert d.deploy() == 1 and "production was changed" in said  # the migrate job may have run
    assert not f.ran("-p", "settleview", "up")
    assert any("redeploy the previous release" in s for s in said)


def test_a_failed_production_smoke_prints_the_rollback():
    f = Fake(smoke_fail=("settleview",))
    d, said = deployer(f)
    assert d.deploy() == 1 and "production was changed" in said
    assert not f.ran("docker", "tag")
    assert any(f"python scripts/deploy.py --sha {OLD_SHA}" in s for s in said)


@pytest.mark.parametrize(("kw", "why"), [
    ({"head": "b" * 40}, "the checkout is at"),
    ({"dirty": " M compose.yaml"}, "uncommitted changes"),
    ({"on_main": False}, "not on origin/main"),
])
def test_the_checkout_must_be_the_published_commit(kw, why):
    f = Fake(**kw)
    d, said = deployer(f)
    assert d.deploy() == 2 and why in said[-1]
    assert not f.ran("docker", "exec") and not f.ran("up")


def test_a_registry_image_by_digest_is_cosign_verified_then_pulled():
    f = Fake(running_image=DIGEST.replace("b" * 64, "e" * 64))
    d, said = deployer(f, image=DIGEST)
    assert d.deploy() == 0, said
    verify = f.calls[f.index("cosign", "verify")]
    assert verify[-1] == DIGEST and dg.OIDC_ISSUER in verify
    assert verify[verify.index("--certificate-identity-regexp") + 1] == \
        r"^https://github\.com/owner/settleview/\.github/workflows/release\.yml@refs/(tags/v[0-9]+\.[0-9]+\.[0-9]+|heads/main)$"
    assert f.index("cosign", "verify") < f.index("pull") < f.index("-p", "settleview-staging", "up")
    assert not [c for c in f.calls if c[:2] == ["docker", "build"]] and f.envs[f.index("-p", "settleview", "up")]["SETTLEVIEW_IMAGE"] == DIGEST
    assert any("--image ghcr.io/owner/settleview@sha256:" + "e" * 64 in s for s in said)  # the rollback, by digest


def test_an_unverified_or_undigested_image_is_never_deployed():
    d, said = deployer(Fake(), image="ghcr.io/owner/settleview:1.2.3")
    assert d.deploy() == 2 and "by digest only" in said[-1]
    f = Fake(fail=("cosign",))
    d, said = deployer(f, image=DIGEST)
    assert d.deploy() == 1 and "production was NOT changed" in said
    assert not f.ran("pull") and not f.ran("up")


@pytest.mark.parametrize("plan", [
    {"staging_project": "settleview"}, {"prod_project": "x", "staging_project": "x"}, {"prod_project": "Bad Name"},
])
def test_project_names_are_guarded(plan):
    d, _ = deployer(Fake(), **plan)
    assert d.deploy() == 2


def test_a_throwaway_production_project_never_retags_settleview_local():
    f = Fake(prod_running=False)
    d, said = deployer(f, prod_project="settleview-p3t-prod", staging_project="settleview-staging-p3t",
                       prod_files=["x.yaml"], offline=True)
    assert d.deploy() == 0, said
    assert not f.ran("settleview:local") and not f.ran("exec", "backup")  # no running production to dump
    assert "deploy/staging/compose.offline.yaml" in f.calls[f.index("-p", "settleview-staging-p3t", "up")]
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


def test_dry_run_prints_the_commands_and_runs_nothing(capsys):
    f = Fake()
    assert dg.main(["--sha", SHA, "--dry-run"], run=f) == 0
    out = capsys.readouterr().out
    assert not f.calls
    assert out.index("-p settleview-staging") < out.index("scripts/smoke.py --project settleview-staging") < out.index(
        "run --rm migrate") < out.index("-p settleview -f compose.yaml up -d --wait")
