"""scripts/release.py (P3 #4, release engineering): the changelog / version edits, the refusals, the CI plan, and a
real prepare in a throwaway git repository (a bare `origin` next to it; nothing leaves tmp_path)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_script", ROOT / "scripts" / "release.py")
rel = importlib.util.module_from_spec(_spec)
sys.modules["release_script"] = rel
_spec.loader.exec_module(rel)

CHANGELOG = """# Changelog

Intro.

## [Unreleased]

### Added
- A thing.

### Fixed
- Another thing.

## [0.0.9] - 2026-01-01

### Added
- Old.
"""
PYPROJECT = '[project]\nname = "settleview"\nversion = "0.1.0"\n\n[tool.x]\nversion = "9"\n'
LOCK = ('version = 1\n\n[[package]]\nname = "other"\nversion = "1.0.0"\n\n'
        '[[package]]\nname = "settleview"\nversion = "0.1.0"\nsource = { editable = "." }\n')
NOREPLY = "1+release-test" + "@users.noreply.github.com"


def test_close_unreleased_moves_the_entries_under_the_version():
    out = rel.close_unreleased(CHANGELOG, "0.1.0", "2026-11-01")
    assert "## [Unreleased]\n\n## [0.1.0] - 2026-11-01\n\n### Added\n- A thing." in out
    assert rel.unreleased_body(out).strip() == ""
    assert rel.section(out, "0.1.0").startswith("### Added\n- A thing.")
    assert rel.section(out, "0.1.0").endswith("- Another thing.")
    assert rel.section(out, "0.0.9") == "### Added\n- Old."


def test_an_empty_unreleased_or_a_repeated_version_is_refused():
    empty = rel.close_unreleased(CHANGELOG, "0.1.0", "2026-11-01")
    with pytest.raises(rel.ReleaseError, match="no change"):
        rel.close_unreleased(empty, "0.1.1", "2026-11-02")
    with pytest.raises(rel.ReleaseError, match="already has"):
        rel.close_unreleased(CHANGELOG.replace("Old.", "Old.\n\n## [0.2.0] - x\n- y"), "0.2.0", "2026-11-01")
    with pytest.raises(rel.ReleaseError, match="no '## \\[1.0.0\\]'"):
        rel.section(CHANGELOG, "1.0.0")


def test_versions_and_bumps():
    assert rel.bump((0, 1, 0), "patch") == "0.1.1"
    assert rel.bump((0, 1, 3), "minor") == "0.2.0"
    assert rel.bump((0, 9, 3), "major") == "1.0.0"
    for bad in ("v0.1.0", "0.1", "0.1.0-rc1", "01.1.0"):
        with pytest.raises(rel.ReleaseError):
            rel.parse(bad)
    with pytest.raises(rel.ReleaseError):
        rel.bump((0, 1, 0), "huge")


def test_only_the_project_version_and_the_lock_entry_change():
    assert rel.set_pyproject_version(PYPROJECT, "0.2.0") == PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"')
    out = rel.set_lock_version(LOCK, "0.2.0")
    assert 'name = "other"\nversion = "1.0.0"' in out
    assert 'name = "settleview"\nversion = "0.2.0"' in out


def test_the_real_lock_and_pyproject_have_the_lines_the_script_edits():
    assert rel.pyproject_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert rel.LOCK_VERSION.search((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert "## [Unreleased]\n" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("no git")
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", NOREPLY), ("user.name", "Release Test"), ("commit.gpgsign", "false"),
                 ("tag.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
        _git(work, "config", k, v)
    (work / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8", newline="\n")
    (work / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8", newline="\n")
    (work / "uv.lock").write_text(LOCK, encoding="utf-8", newline="\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "start")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work


def test_prepare_commits_and_tags_the_first_release(repo, capsys):
    assert rel.main(["--root", str(repo), "prepare", "0.1.0", "--date", "2026-11-01"]) == 0
    assert _git(repo, "tag", "--list") == "v0.1.0"
    assert _git(repo, "log", "-1", "--format=%s") == "Release v0.1.0"
    assert _git(repo, "cat-file", "-t", "v0.1.0") == "tag"  # annotated
    assert _git(repo, "status", "--porcelain") == ""
    assert "## [0.1.0] - 2026-11-01" in (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "SETTLEVIEW_ALLOW_TAGS=1 git push origin v0.1.0" in capsys.readouterr().out
    assert _git(repo, "ls-remote", "--tags", "origin") == ""  # nothing pushed

    # The next one: a bump from the newest tag, after new entries.
    log = (repo / "CHANGELOG.md").read_text(encoding="utf-8").replace("## [Unreleased]\n", "## [Unreleased]\n\n- New.\n", 1)
    (repo / "CHANGELOG.md").write_text(log, encoding="utf-8", newline="\n")
    _git(repo, "commit", "-q", "-am", "more")
    assert rel.main(["--root", str(repo), "prepare", "minor", "--date", "2026-11-02"]) == 0
    assert 'version = "0.2.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "settleview"\nversion = "0.2.0"' in (repo / "uv.lock").read_text(encoding="utf-8")
    assert rel.section((repo / "CHANGELOG.md").read_text(encoding="utf-8"), "0.2.0") == "- New."


def test_prepare_refuses_and_changes_nothing(repo, capsys):
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "stray.txt").write_text("x", encoding="utf-8")
    assert rel.main(["--root", str(repo), "prepare", "0.1.0"]) == 1
    assert "not clean" in capsys.readouterr().err
    (repo / "stray.txt").unlink()
    assert rel.main(["--root", str(repo), "prepare", "patch"]) == 1  # no tag yet: name the first version
    assert "name the first version" in capsys.readouterr().err
    _git(repo, "tag", "v0.3.0")
    assert rel.main(["--root", str(repo), "prepare", "0.2.0"]) == 1
    assert "not newer" in capsys.readouterr().err
    _git(repo, "checkout", "-q", "-b", "feature")
    assert rel.main(["--root", str(repo), "prepare", "0.4.0"]) == 1
    assert "made on main" in capsys.readouterr().err
    _git(repo, "checkout", "-q", "main")
    assert rel.main(["--root", str(repo), "prepare", "0.4.0", "--dry-run"]) == 0
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""


def test_plan_for_a_tag_and_for_main(repo, capsys):
    rel.main(["--root", str(repo), "prepare", "0.1.0", "--date", "2026-11-01"])
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    sha = _git(repo, "rev-parse", "HEAD")
    capsys.readouterr()
    args = ["--root", str(repo), "plan", "--sha", sha, "--repository", "jon-lange/settleview"]
    assert rel.main([*args, "--ref", "refs/tags/v0.1.0"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out == {"kind": "release", "version": "0.1.0", "tag": "v0.1.0"}
    assert rel.main([*args, "--ref", "refs/heads/main"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out == {"kind": "main", "version": f"sha-{sha[:7]}", "tag": ""}
    for ref, why in (("refs/heads/feature", "tag or for main"), ("refs/tags/v0.2.0", "pyproject.toml says 0.1.0"),
                     ("refs/tags/release-1", "tag or for main")):
        assert rel.main([*args, "--ref", ref]) == 1
        assert why in capsys.readouterr().err
    assert rel.main(["--root", str(repo), "plan", "--sha", sha, "--repository", "someone/fork",
                     "--ref", "refs/tags/v0.1.0"]) == 1


def test_plan_refuses_a_tag_that_is_not_on_origin_main(repo, capsys):
    rel.main(["--root", str(repo), "prepare", "0.1.0", "--date", "2026-11-01"])  # committed and tagged, not pushed
    _git(repo, "fetch", "-q", "origin")
    sha = _git(repo, "rev-parse", "HEAD")
    assert rel.main(["--root", str(repo), "plan", "--sha", sha, "--repository", "jon-lange/settleview",
                     "--ref", "refs/tags/v0.1.0"]) == 1
    assert "not on origin/main" in capsys.readouterr().err


def test_notes_name_the_digest_and_the_exact_verify_identity(repo, capsys):
    rel.main(["--root", str(repo), "prepare", "0.1.0", "--date", "2026-11-01"])
    capsys.readouterr()
    ref = "ghcr.io/jon-lange/settleview:0.1.0@sha256:" + "a" * 64
    assert rel.main(["--root", str(repo), "notes", "0.1.0", "--image", ref]) == 0
    out = capsys.readouterr().out
    assert out.startswith("### Added\n- A thing.")
    assert f"cosign verify {ref}" in out
    assert "--certificate-identity https://github.com/jon-lange/settleview/.github/workflows/release.yml@refs/tags/v0.1.0" in out
