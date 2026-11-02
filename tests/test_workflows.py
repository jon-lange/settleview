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
    # release-please on every push to main (the release PR; on its merge the tag and the Release), and the image in
    # the same run: a tag made with GITHUB_TOKEN would start no other workflow.
    assert doc["on"]["push"] == {"branches": ["main"]}
    assert "workflow_dispatch" in doc["on"]
    assert set(doc["on"]) == {"push", "workflow_dispatch"}
    assert doc["permissions"] == {}  # nothing by default
    jobs = doc["jobs"]
    assert set(jobs) == {"release-please", "plan", "build", "scan", "sign", "release"}
    perms = {name: job.get("permissions") for name, job in jobs.items()}
    assert perms == {
        "release-please": {"contents": "write", "pull-requests": "write", "issues": "write"},
        "plan": {"contents": "read"},
        "build": {"contents": "read", "packages": "write"},
        "scan": {"contents": "read", "packages": "read"},
        "sign": {"contents": "read", "packages": "write", "id-token": "write"},
        "release": {"contents": "write"},
    }
    rp = jobs["release-please"]
    assert rp["if"] == "github.event_name == 'push'"
    (step,) = rp["steps"]
    assert step["uses"].startswith("googleapis/release-please-action@")
    assert step["with"] == {"config-file": "release-please-config.json",
                            "manifest-file": ".release-please-manifest.json"}
    # The order: nothing is built unless a release was made (or asked for by hand), nothing signed before the scan
    # passed, nothing added to the Release before it was signed.
    assert "needs.plan.outputs.kind == 'release'" in jobs["build"]["if"]
    assert "needs.plan.outputs.kind == 'main'" in jobs["build"]["if"]
    assert "needs.build.result == 'success'" in jobs["scan"]["if"]
    assert set(jobs["sign"]["needs"]) >= {"build", "scan"} and "needs.scan.result == 'success'" in jobs["sign"]["if"]
    assert set(jobs["release"]["needs"]) >= {"build", "sign"}
    assert "needs.plan.outputs.kind == 'release'" in jobs["release"]["if"]
    assert "needs.sign.result == 'success'" in jobs["release"]["if"]
    assert doc["env"]["IMAGE"] == "ghcr.io/jon-lange/settleview"
    for name, job in jobs.items():
        assert job.get("timeout-minutes"), name
        for st in job["steps"]:
            if st.get("uses", "").startswith("actions/checkout@"):
                assert st["with"]["persist-credentials"] is False, name
    steps = {st.get("id"): st for st in jobs["build"]["steps"]}
    meta, push = steps["meta"], steps["push"]
    assert meta["uses"].startswith("docker/metadata-action@") and meta["with"]["flavor"] == "latest=false"
    assert "latest" not in meta["with"]["tags"]
    assert "type=semver,pattern={{version}},value=${{ needs.plan.outputs.tag }}" in meta["with"]["tags"]
    assert push["uses"].startswith("docker/build-push-action@")
    w = push["with"]
    assert w["platforms"] == "linux/amd64,linux/arm64" and w["build-args"] == "GIT_SHA=${{ github.sha }}"
    assert w["no-cache"] is True and w["pull"] is True and w["push"] is True
    assert jobs["build"]["outputs"]["digest"] == "${{ steps.push.outputs.digest }}"
    # The scan: Aqua's action on the pushed digest, each platform, CI's policy (the gate), then each SBOM.
    trivy = [st for st in jobs["scan"]["steps"] if st.get("uses", "").startswith("aquasecurity/trivy-action@")]
    gates = [st for st in trivy if st["with"].get("exit-code") == "1"]
    assert sorted(st["env"]["TRIVY_PLATFORM"] for st in gates) == ["linux/amd64", "linux/arm64"]
    for st in gates:
        assert st["with"]["image-ref"] == "${{ env.IMAGE }}@${{ needs.build.outputs.digest }}"
        assert st["with"]["severity"] == "HIGH,CRITICAL" and st["with"]["scanners"] == "vuln,secret"
        assert st["with"]["ignore-unfixed"] is True and st["env"]["TRIVY_IMAGE_SRC"] == "remote"
    sboms = [st for st in trivy if st["with"].get("format") == "cyclonedx"]
    assert sorted(st["env"]["TRIVY_PLATFORM"] for st in sboms) == ["linux/amd64", "linux/arm64"]


def test_ci_quality_gates_are_pre_commit_and_trivy(tmp_path):
    doc = _parse(ROOT / ".github" / "workflows" / "ci.yml", tmp_path)
    lint = doc["jobs"]["lint"]
    # pre-commit's own action on the runner (it has Docker for hadolint-docker), with its default `--all-files`
    (gate,) = [st for st in lint["steps"] if st.get("uses", "").startswith("pre-commit/action@")]
    assert "extra_args" not in gate.get("with", {})
    checkout = next(st for st in lint["steps"] if st.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0  # gitleaks reads every commit of the branch
    # The dev container's build through Compose's Bake reads the CLI's own Dockerfile outside the context.
    (suite,) = [st for st in doc["jobs"]["test"]["steps"] if st.get("uses", "").startswith("devcontainers/ci@")]
    assert suite["env"]["BUILDX_BAKE_ENTITLEMENTS_FS"] == "0"
    trivy = [st for st in doc["jobs"]["trivy"]["steps"] if st.get("uses", "").startswith("aquasecurity/trivy-action@")]
    gates = {st["with"]["scan-type"]: st["with"] for st in trivy if st["with"].get("exit-code") == "1"}
    assert set(gates) == {"image", "config", "fs"}
    assert gates["image"]["severity"] == gates["config"]["severity"] == "HIGH,CRITICAL"
    assert gates["image"]["scanners"] == "vuln,secret" and gates["image"]["ignore-unfixed"] is True
    assert gates["fs"]["scanners"] == "secret"
    assert "scripts/quality.py" not in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
