"""scripts/prepush_scan.py, the pre-push hook's scanner: pure functions on synthetic diffs, refs and identities.

Every personal-looking value here is built by concatenation, so this file itself passes the scan."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("prepush_scan", ROOT / "scripts" / "prepush_scan.py")
scan = importlib.util.module_from_spec(_spec)
sys.modules["prepush_scan"] = scan  # dataclasses look the module up
_spec.loader.exec_module(scan)

Z = scan.ZERO
A = "a" * 40
B = "b" * 40
GMAIL = "alex.example" + "@" + "gmail.com"
WIN_PATH = "C:" + "\\Users\\x\\code"
WIN_PATH_FWD = "C:" + "/Users/alex/repo"
BASH_PATH = "/c/" + "Users/alex/repo"
WSL_PATH = "/mnt/c/" + "Users/alex"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
GH_TOKEN = "ghp_" + "A1b2C3d4" * 5
TG_TOKEN = "123456789:" + "AA" + "h" * 33
NOREPLY = "31299528+alex-example" + "@users.noreply.github.com"


def diff(path: str, *added: str, context: str = "unchanged") -> str:
    body = "\n".join(f"+{line}" for line in added)
    return (f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -3,0 +4,{len(added)} @@\n{body}\n@@ -10 +11 @@\n-old\n+{context}\n")


# ---- refs -----------------------------------------------------------------------------------------------------

def test_parse_push_lines_reads_gits_protocol():
    refs = scan.parse_push_lines(f"refs/heads/main {A} refs/heads/main {B}\n\n(delete) {Z} refs/heads/old {B}\n")
    assert refs[0] == scan.PushRef("refs/heads/main", A, "refs/heads/main", B)
    assert not refs[0].new and not refs[0].deleting
    assert refs[1].deleting
    assert scan.parse_push_lines(f"refs/heads/x {A} refs/heads/x {Z}")[0].new


def test_parse_push_lines_refuses_garbage():
    with pytest.raises(ValueError):
        scan.parse_push_lines("refs/heads/main abc")


@pytest.mark.parametrize("local, remote", [
    ("refs/tags/archive/private-history", "refs/tags/archive/private-history"),
    ("refs/heads/main", "refs/tags/archive/x"),             # pushed under an archive name
    ("refs/tags/archive/x", "refs/heads/backup"),           # an archive tag pushed as a branch
])
def test_archive_tags_are_always_refused(local, remote):
    ref = scan.PushRef(local, A, remote, Z)
    assert scan.check_ref(ref, allow_tags=True)
    assert "local-only" in str(scan.check_ref(ref, allow_tags=True)[0])


def test_deleting_an_archive_tag_is_refused():
    assert scan.check_ref(scan.PushRef("(delete)", Z, "refs/tags/archive/x", B), allow_tags=True)


def test_other_tags_need_the_opt_in():
    ref = scan.PushRef("refs/tags/v1.0.0", A, "refs/tags/v1.0.0", Z)
    assert "SETTLEVIEW_ALLOW_TAGS" in str(scan.check_ref(ref, allow_tags=False)[0])
    assert scan.check_ref(ref, allow_tags=True) == []


def test_branches_pass_the_ref_check():
    assert scan.check_ref(scan.PushRef("refs/heads/feat", A, "refs/heads/feat", Z), allow_tags=False) == []


# ---- identity -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("email", [
    NOREPLY, "alex-example@users.noreply.github.com", "49699333+dependabot[bot]@users.noreply.github.com",
    "41898282+github-actions[bot]@users.noreply.github.com", "noreply@github.com",
])
def test_noreply_addresses_pass(email):
    assert scan.email_ok(email)


@pytest.mark.parametrize("email", [
    GMAIL, "alex@example.com", "noreply@anthropic.com", "x@users.noreply.github.com.evil.example", "",
    "alex@noreply.github.com",
])
def test_other_addresses_fail(email):
    assert not scan.email_ok(email)


def test_identity_names_the_role_and_masks_the_address():
    found = scan.check_identity(A, GMAIL, NOREPLY)
    assert len(found) == 1 and "author" in found[0].kind
    assert GMAIL not in str(found[0]) and found[0].excerpt.startswith("alex")


# ---- content --------------------------------------------------------------------------------------------------

def test_added_lines_tracks_paths_and_new_line_numbers():
    lines = list(scan.added_lines(diff("docs/a.md", "one", "two", context="three")))
    assert lines == [("docs/a.md", 4, "one"), ("docs/a.md", 5, "two"), ("docs/a.md", 11, "three")]


def test_removed_and_context_lines_are_not_scanned():
    d = (f"diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n-{GMAIL}\n {WIN_PATH}\n+clean\n")
    assert scan.scan_diff(d, []) == []


def test_binary_and_deleted_files_yield_nothing():
    d = ("diff --git a/img.png b/img.png\nnew file mode 100644\nindex 0000000..1111111\n"
         "Binary files /dev/null and b/img.png differ\n"
         "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n"
         f"@@ -1 +0,0 @@\n-{GMAIL}\n")
    assert list(scan.added_lines(d)) == []


@pytest.mark.parametrize("line, kind", [
    (f"contact {GMAIL}", "gmail"),
    (f"path = '{WIN_PATH}'", "Windows user-profile path"),
    (f"see {WIN_PATH_FWD}/x", "Windows user-profile path"),
    (f"cd {BASH_PATH}", "Windows user-profile path"),
    (f"cd {WSL_PATH}", "Windows user-profile path"),
    (PEM, "private key"),
    (f"token = '{GH_TOKEN}'", "GitHub token"),
    (f"BOT={TG_TOKEN}", "Telegram"),
    ("key = 'AKIA" + "ABCDEFGHIJKLMNOP'", "AWS"),
    ("k='sk-ant-" + "api03-" + "x" * 30 + "'", "Anthropic"),
])
def test_scan_finds_each_shape_with_file_and_line(line, kind):
    found = scan.scan_diff(diff("src/x.py", "ok", line), [])
    assert len(found) == 1, found
    assert found[0].where == "src/x.py:5" and kind in found[0].kind


def test_an_anthropic_key_is_reported_once():
    found = scan.scan_line("sk-ant-" + "api03-" + "y" * 40, [])
    assert [k for k, _ in found] == ["Anthropic API key"]


def test_a_finding_never_prints_the_whole_value():
    found = scan.scan_diff(diff("a.env", f"T={GH_TOKEN}"), [])
    text = str(found[0])
    assert GH_TOKEN not in text and "ghp_" in text and f"({len(GH_TOKEN)} chars)" in text


@pytest.mark.parametrize("line", [
    "C:/Program Files/Docker", "/usr/share/Users.txt", "see docs/operations.md", "a@example.com",
    "https://github.com/jon-lange/settleview", "%USERPROFILE%\\.config", "sk-short",
])
def test_ordinary_lines_pass(line):
    assert scan.scan_line(line, []) == []


def test_allow_mark_passes_generic_checks_only():
    mark = "  # " + "prepush-scan: allow"
    assert scan.scan_line(f"example: {WIN_PATH}{mark}", []) == []
    assert scan.scan_line(f"{GMAIL}{mark}", []) == []
    assert scan.scan_line(f"{PEM}{mark}", [])                      # a private key is never allowed
    extra = scan.local_patterns("owner-name", "", ())
    assert scan.scan_line(f"owner-name{mark}", extra)                # nor a local pattern


# ---- local patterns -------------------------------------------------------------------------------------------

def test_local_patterns_from_file_claude_local_and_config():
    claude = "# notes\n- The live stack's deploy checkout is `my-run-dir` (compose project `x`).\n"
    extra = scan.local_patterns("# comment\n\nowner\\.name@example\\.org\n", claude, ["me@example.net", NOREPLY])
    kinds = [k for k, _ in extra]
    assert kinds == ["local pattern (.git/info/prepush-patterns)", "deploy checkout folder name (CLAUDE.local.md)",
                     "owner's configured email (git config user.email)"]      # the no-reply address is not a pattern
    hits = lambda s: [k for k, _ in scan.scan_line(s, extra)]  # noqa: E731 - a one-line helper
    assert hits("mail Owner.Name@Example.org") == [kinds[0]]
    assert hits("cd ../my-run-dir && git pull") == [kinds[1]]
    assert hits("the my-run-dirs folder") == []                    # whole names only
    assert hits("from me@example.net") == [kinds[2]]


def test_deploy_name_needs_the_documented_sentence():
    assert scan.deploy_name_from("The live stack's deploy checkout is `run-here`.") == "run-here"
    assert scan.deploy_name_from("deploy checkout: `other`") == "other"
    assert scan.deploy_name_from("nothing here") is None


def test_a_bad_local_regex_is_an_error_not_ignored():
    with pytest.raises(ValueError, match="prepush-patterns"):
        scan.local_patterns("(unclosed", "", ())


def test_commit_message_is_scanned():
    found = scan.scan_message(A, f"Fix it\n\nfrom {WIN_PATH}\n", [])
    assert found and found[0].where.endswith("message line 3")


# ---- the hook end to end, when git is installed ---------------------------------------------------------------

def _git(cwd: Path, *args: str, env: dict | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, input=stdin, env=env)  # noqa: S603,S607


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_hook_end_to_end(tmp_path, monkeypatch):
    import os
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", "XDG_CONFIG_HOME": str(home)}
    env.pop("SETTLEVIEW_ALLOW_TAGS", None)
    env.pop("SETTLEVIEW_PREPUSH_TRIVY", None)
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(bare), env=env)
    _git(tmp_path, "init", "-q", "-b", "main", str(work), env=env)
    (work / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "prepush_scan.py", work / "scripts" / "prepush_scan.py")
    shutil.copytree(ROOT / ".githooks", work / ".githooks")
    for k, v in (("user.email", NOREPLY), ("user.name", "Alex"), ("core.hooksPath", ".githooks"),
                 ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
        _git(work, "config", k, v, env=env)
    _git(work, "remote", "add", "origin", str(bare), env=env)

    def commit(name: str, text: str, email: str = NOREPLY) -> None:
        (work / name).write_text(text + "\n", encoding="utf-8")
        _git(work, "add", "-A", env=env)
        e = {**env, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": NOREPLY}
        assert _git(work, "commit", "-q", "-m", f"add {name}", env=e).returncode == 0

    def push(*args: str) -> subprocess.CompletedProcess:
        return _git(work, "push", "-q", "origin", *args, env=env)

    commit("README.md", "hello")
    ok = push("main")
    assert ok.returncode == 0, ok.stderr

    commit("a.txt", "author check", email=GMAIL)
    bad = push("main")
    assert bad.returncode != 0 and "author email" in bad.stderr and GMAIL not in bad.stderr
    _git(work, "reset", "-q", "--hard", "HEAD~1", env=env)

    commit("b.txt", f"see {WIN_PATH}")
    bad = push("main")
    assert bad.returncode != 0 and "b.txt:1: Windows user-profile path" in bad.stderr
    _git(work, "reset", "-q", "--hard", "HEAD~1", env=env)

    _git(work, "tag", "archive/old", env=env)
    bad = push("refs/tags/archive/old")
    assert bad.returncode != 0 and "local-only" in bad.stderr
    assert _git(bare, "tag", "--list", env=env).stdout.strip() == ""
