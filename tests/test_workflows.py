"""The GitHub Actions workflows (P3 #4): every action pinned by commit SHA, least privilege per job, and the release
workflow's shape (what triggers it, which job may push, sign or publish, no `latest`).

The YAML is parsed by the docker compose CLI (no YAML library in the project): each workflow is nested under an `x-`
extension key of an otherwise empty compose file, `$` escaped as `$$`, and read back from `docker compose config
--format json`. Skipped where the docker CLI is missing (as tests/deploy/test_compose.py); the text checks run
everywhere."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
PINNED = re.compile(r"^\s*(?:-\s+)?uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s+v\d+\.\d+\.\d+\s*$")


def _parse(path: Path, tmp_path: Path) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI")
    body = path.read_text(encoding="utf-8").replace("$", "$$")
    nested = "".join(f"  {line}" if line.strip() else line for line in body.splitlines(keepends=True))
    (tmp_path / "compose.yaml").write_text(f"x-workflow:\n{nested}\nservices:\n  noop:\n    image: scratch\n",
                                           encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "SETTLEVIEW_"))}
    p = subprocess.run(["docker", "compose", "-p", "svwfparse", "config", "--format", "json"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60, env=clean)
    if p.returncode != 0:
        pytest.fail(f"{path.name} does not parse as YAML: {p.stderr[-2000:]}")
    return json.loads(p.stdout.replace("$$", "$"))["x-workflow"]  # compose prints `$` escaped again


def test_every_action_is_pinned_by_commit_sha():
    for wf in WORKFLOWS:
        for n, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*(-\s+)?uses:", line):
                assert PINNED.match(line), f"{wf.name}:{n}: not pinned by a full commit SHA with its version: {line}"
            assert "\t" not in line, f"{wf.name}:{n}: a tab"


def test_release_text_never_names_latest_and_pins_cosign():
    text = RELEASE.read_text(encoding="utf-8")
    assert not re.search(r":latest\b", text)
    assert re.search(r"cosign-release: v\d+\.\d+\.\d+$", text, re.M)


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_workflows_parse(wf, tmp_path):
    doc = _parse(wf, tmp_path)
    assert doc["jobs"], wf.name
    assert "${{ github." in json.dumps(doc)  # expressions survive the round trip unescaped


def test_release_workflow_least_privilege(tmp_path):
    doc = _parse(RELEASE, tmp_path)
    assert doc["on"]["push"] == {"tags": ["v*.*.*"]}
    assert "workflow_dispatch" in doc["on"]
    assert set(doc["on"]) == {"push", "workflow_dispatch"}
    assert doc["permissions"] == {}  # nothing by default
    jobs = doc["jobs"]
    assert set(jobs) == {"plan", "build", "scan", "sign", "release"}
    perms = {name: job.get("permissions") for name, job in jobs.items()}
    assert perms == {
        "plan": {"contents": "read"},
        "build": {"contents": "read", "packages": "write"},
        "scan": {"contents": "read", "packages": "read"},
        "sign": {"contents": "read", "packages": "write", "id-token": "write"},
        "release": {"contents": "write"},
    }
    # The order: nothing is signed before the scan passed, nothing released before it was signed.
    assert set(jobs["sign"]["needs"]) >= {"build", "scan"}
    assert set(jobs["release"]["needs"]) >= {"build", "sign"}
    assert jobs["release"]["if"] == "needs.plan.outputs.kind == 'release'"
    assert doc["env"]["IMAGE"] == "ghcr.io/jon-lange/settleview"
    for name, job in jobs.items():
        assert job.get("timeout-minutes"), name
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, name
    steps = {st.get("id"): st for st in jobs["build"]["steps"]}
    meta, push = steps["meta"], steps["push"]
    assert meta["uses"].startswith("docker/metadata-action@") and meta["with"]["flavor"] == "latest=false"
    assert "latest" not in meta["with"]["tags"]
    assert push["uses"].startswith("docker/build-push-action@")
    w = push["with"]
    assert w["platforms"] == "linux/amd64,linux/arm64" and w["build-args"] == "GIT_SHA=${{ github.sha }}"
    assert w["no-cache"] is True and w["pull"] is True and w["push"] is True
    assert jobs["build"]["outputs"]["digest"] == "${{ steps.push.outputs.digest }}"
    scan = "\n".join(s.get("run", "") for s in jobs["scan"]["steps"])
    assert "scripts/quality.py --only trivy-image" in scan
