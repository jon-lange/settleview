"""The quality gates' configuration (.pre-commit-config.yaml, .gitleaks.toml, release-please): every hook repository
pinned to a commit, each tool at the version its other pin names, and the project's gitleaks rules matching what they
are meant to catch (gitleaks itself runs them; this only guards an edit that breaks a pattern)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml  # pre-commit's own dependency (PyYAML), locked in uv.lock with it

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".pre-commit-config.yaml"


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _frozen(repo_url: str) -> str:
    """The `# frozen: vX.Y.Z` comment on a repository's rev line."""
    text = CONFIG.read_text(encoding="utf-8")
    m = re.search(rf"- repo: {re.escape(repo_url)}\n\s+rev: [0-9a-f]{{40}}  # frozen: v(\S+)\n", text)
    assert m, f"{repo_url}: rev is not a commit SHA with its `# frozen: vX.Y.Z` comment"
    return m.group(1)


def _lock_version(name: str) -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (pkg,) = [p for p in lock["package"] if p["name"] == name]
    return pkg["version"]


def test_every_hook_repository_is_pinned_by_commit():
    for repo in _config()["repos"]:
        if repo["repo"] == "local":
            continue
        assert re.fullmatch(r"[0-9a-f]{40}", repo["rev"]), repo["repo"]
        _frozen(repo["repo"])


def test_hook_versions_match_the_other_pins():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"ruff=={_frozen("https://github.com/astral-sh/ruff-pre-commit")}"' in pyproject
    (hadolint,) = [r for r in _config()["repos"] if r["repo"] == "https://github.com/hadolint/hadolint"]
    (docker_hook,) = hadolint["hooks"]
    assert docker_hook["id"] == "hadolint-docker"
    # the official hook's entry names the image untagged (`latest`): the entry pins the rev's tag and a digest
    assert re.fullmatch(rf"ghcr\.io/hadolint/hadolint:v{re.escape(_frozen(hadolint['repo']))}@sha256:[0-9a-f]{{64}} "
                        r"hadolint", docker_hook["entry"])
    (local,) = [r for r in _config()["repos"] if r["repo"] == "local"]
    (imports,) = [h for h in local["hooks"] if h["id"] == "lint-imports"]
    assert imports["additional_dependencies"] == [f"import-linter=={_lock_version('import-linter')}"]
    assert _lock_version("pre-commit") in pyproject


def test_hooks_cover_the_gates_and_both_git_hooks():
    cfg = _config()
    assert cfg["default_install_hook_types"] == ["pre-commit", "pre-push"]
    ids = {h.get("alias", h["id"]) for r in cfg["repos"] for h in r["hooks"]}
    assert ids == {"gitleaks", "gitleaks-history", "ruff-check", "shellcheck", "hadolint-docker", "actionlint",
                   "lint-imports"}
    hooks = {h.get("alias", h["id"]): h for r in cfg["repos"] for h in r["hooks"]}
    history = hooks["gitleaks-history"]
    assert {"pre-commit", "pre-push"} <= set(history["stages"]) and history["always_run"] is True
    assert history["entry"].startswith("gitleaks git --redact")
    # A push runs gitleaks' history scan only (it needs no host tool but pre-commit); every hook runs on commit.
    assert cfg["default_stages"] == ["pre-commit"]
    on_push = {name for name, h in hooks.items() if "pre-push" in h.get("stages", cfg["default_stages"])}
    assert on_push == {"gitleaks-history"}


def test_the_dev_container_skips_only_the_docker_hook():
    """It has no Docker by design (deploy/dev/Dockerfile); the host's hook and CI's pre-commit/action run hadolint."""
    dev = yaml.safe_load((ROOT / "deploy" / "dev" / "compose.yaml").read_text(encoding="utf-8"))
    for name in ("dev", "dev-pg", "evals", "research"):
        env = dev["services"][name].get("environment") or dev["x-env"]
        assert env["SKIP"] == "hadolint-docker", name


def _gitleaks_rules() -> dict[str, dict]:
    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert cfg["extend"] == {"useDefault": True}  # gitleaks' own rules stay on
    return {r["id"]: r for r in cfg["rules"]}


def _caught(rule: dict, line: str) -> bool:
    m = re.search(rule["regex"], line)
    if m is None:
        return False
    allow = [a for lst in rule.get("allowlists", []) for a in lst.get("regexes", [])]
    return not any(re.search(a, m.group(0)) for a in allow)


def test_personal_data_rules():
    rules = _gitleaks_rules()
    gmail, drive, posix = (rules["gmail-address"], rules["windows-user-profile-path"],
                           rules["posix-windows-user-profile-path"])
    at = "@"
    assert _caught(gmail, f"mail me: jane.doe+x{at}gmail.com")
    assert _caught(gmail, f"old{at}googlemail.com")
    assert not _caught(gmail, f"someone{at}example.com")
    for path in ("C:\\Users\\JaneDoe\\x", "c:/users/jane/x", "D:\\\\Users\\\\jane", "path=C:/Users/Jane Doe/x"):  # gitleaks:allow
        assert _caught(drive, path), path
    for path in ("/c/Users/jane/x", "cd /mnt/c/Users/jane/", "x=/d/users/jane"):  # gitleaks:allow - made up
        assert _caught(posix, path), path
    # The usual placeholders pass; other paths are not profiles.
    for path in ("C:/Users/you/.kalshi/k.pem", "C:\\Users\\Someone\\k", "C:/Users/me/x", "C:/Users/<name>/x"):
        assert not _caught(drive, path), path
    assert not _caught(posix, "/c/Users/me/x")
    assert not _caught(drive, "C:/msys64/run/secrets/a") and not _caught(posix, "/usr/users/x")


def test_release_please_config():
    cfg = json.loads((ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    (pkg,) = cfg["packages"].values()
    assert pkg["release-type"] == "python" and pkg["changelog-path"] == "CHANGELOG.md"
    assert pkg["include-component-in-tag"] is False  # tags are vX.Y.Z, as the release workflow and deploy expect
    # uv.lock's own entry moves with pyproject.toml's version, so CI's `uv lock --check` stays green on the release PR.
    assert {"type": "toml", "path": "uv.lock",
            "jsonpath": "$.package[?(@.name.value=='settleview')].version"} in pkg["extra-files"]
    assert isinstance(json.loads((ROOT / ".release-please-manifest.json").read_text(encoding="utf-8")), dict)
