"""Pre-push scan: refuse a push that would publish private history, personal details or secrets.

Called by `.githooks/pre-push` (install once per clone: `git config core.hooksPath .githooks`). Git passes the remote's
name and URL as arguments and one line per ref on stdin: `<local ref> <local sha> <remote ref> <remote sha>`. For every
commit that the remote does not have yet, it checks:

  refs      a tag matching `archive/*` (pushed or deleted) is always refused; any other tag needs
            SETTLEVIEW_ALLOW_TAGS=1; a commit that belongs to a local `archive/*` tag's history is refused
  identity  author and committer email must be a GitHub no-reply address (`<id>+<user>@users.noreply.github.com`,
            bots like `dependabot[bot]` included) or GitHub's own `noreply@github.com` (merges made on github.com)
  content   the added lines of each commit's diff (binary files skipped) and the commit message: any @gmail.com
            address, a Windows user-profile path (a drive letter's Users folder; Git Bash's /c/Users and WSL's
            /mnt/c/Users too), a private key block, common secret shapes (GitHub, AWS, Anthropic, OpenAI, Slack, Google, Stripe, Telegram tokens), and
            the local patterns below

Local patterns are never committed; they come from this machine:
  - `.git/info/prepush-patterns` (in the main clone's git dir, shared by worktrees): one Python regex per line,
    `#` comments; matched case-insensitively. Put your personal email(s), real name variants, LAN names here.
  - the global/system `git config user.email`, when it is not a no-reply address (your personal email, as typed);
  - the deploy checkout's folder name from the untracked `CLAUDE.local.md` ("The live stack's deploy checkout is
    `<name>`"), at the top of this worktree or the main clone.

A line with `prepush-scan: allow` passes the generic path, gmail and token-shape checks (for placeholders in docs and
synthetic tests). It never passes a private key block or a local pattern.

SETTLEVIEW_PREPUSH_TRIVY=1 also runs the repository secret scan (`python scripts/quality.py --only trivy-fs`:
Trivy in its pinned container, from the host's Docker; it scans the working tree, not the pushed
commits). Off by default: it takes a while.

Manual use: `python scripts/prepush_scan.py --range origin/main..HEAD` scans a range without pushing.
A finding names the file and line and a masked excerpt; a matched value is never printed in full.
Standard library only; runs on any Python 3.8+ (`uv run --no-project python scripts/prepush_scan.py`).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ZERO = "0" * 40
ALLOW_MARK = "prepush-scan: allow"

NOREPLY = re.compile(r"^(?:\d+\+)?[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?@users\.noreply\.github\.com$",
                     re.IGNORECASE)
GITHUB_WEB = "noreply@github.com"  # the committer of merges and edits made on github.com

# (kind, pattern). The generic ones; a line marked ALLOW_MARK skips them.
GENERIC = [
    ("gmail address", re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail)\.com\b", re.IGNORECASE)),
    ("Windows user-profile path", re.compile(r"\b[A-Za-z]:(?:\\\\|\\|/)+Users(?:\\\\|\\|/)+[^\\/\s\"'`<>|]+",
                                             re.IGNORECASE)),
    ("Windows user-profile path", re.compile(r"(?<![\w.])/(?:mnt/)?[A-Za-z]/Users/[^/\s\"'`<>|]+", re.IGNORECASE)),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe secret key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b")),
]
# Never allowed, marked or not.
PRIVATE_KEY = ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"))

DEPLOY_NAME = re.compile(r"deploy checkout(?:'s folder)?(?: name)?(?: is|:)\s*`([^`\s]+)`", re.IGNORECASE)


@dataclass(frozen=True)
class PushRef:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @property
    def deleting(self) -> bool:
        return self.local_sha == ZERO

    @property
    def new(self) -> bool:
        return self.remote_sha == ZERO


@dataclass(frozen=True)
class Finding:
    where: str   # "commit abc1234", "path/to/file:12", "refs/tags/x"
    kind: str
    excerpt: str = ""

    def __str__(self) -> str:
        return f"{self.where}: {self.kind}" + (f" ({self.excerpt})" if self.excerpt else "")


# ---- pure functions (tests/test_prepush_scan.py) -------------------------------------------------------------

def parse_push_lines(text: str) -> list[PushRef]:
    """Git's pre-push stdin: `<local ref> <local sha> <remote ref> <remote sha>` per line."""
    refs = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 4:
            raise ValueError(f"unexpected pre-push line: {line!r}")
        refs.append(PushRef(*parts))
    return refs


def check_ref(ref: PushRef, allow_tags: bool) -> list[Finding]:
    """Tags: archive/* never (pushed or deleted, under either name); others only with SETTLEVIEW_ALLOW_TAGS=1."""
    out = []
    for name in (ref.remote_ref, ref.local_ref):
        if name.startswith("refs/tags/archive/") or name.startswith("archive/"):
            out.append(Finding(ref.remote_ref, "archive/* tags are local-only and are never pushed or deleted"))
            return out
    if ref.remote_ref.startswith("refs/tags/") and not allow_tags:
        out.append(Finding(ref.remote_ref, "tag push refused (set SETTLEVIEW_ALLOW_TAGS=1 to push a release tag)"))
    return out


def email_ok(email: str) -> bool:
    email = email.strip()
    return bool(NOREPLY.match(email)) or email.lower() == GITHUB_WEB


def mask(value: str) -> str:
    """Enough to find it, never the whole value: the first 4 characters and the length."""
    value = value.strip()
    if len(value) <= 6:
        return f"{value[:2]}... ({len(value)} chars)"
    return f"{value[:4]}... ({len(value)} chars)"


def deploy_name_from(text: str) -> str | None:
    m = DEPLOY_NAME.search(text)
    return m.group(1) if m else None


def local_patterns(patterns_text: str = "", claude_local: str = "", owner_emails: Iterable[str] = ()) -> list:
    """This machine's patterns: the prepush-patterns file, the deploy folder name, non-noreply configured emails."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for line in patterns_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                out.append(("local pattern (.git/info/prepush-patterns)", re.compile(line, re.IGNORECASE)))
            except re.error as exc:
                raise ValueError(f".git/info/prepush-patterns: bad regex {line!r}: {exc}") from exc
    name = deploy_name_from(claude_local)
    if name:
        out.append(("deploy checkout folder name (CLAUDE.local.md)",
                    re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.IGNORECASE)))
    for email in owner_emails:
        email = email.strip()
        if email and not email_ok(email):
            out.append(("owner's configured email (git config user.email)", re.compile(re.escape(email), re.IGNORECASE)))
    return out


def scan_line(line: str, extra: list[tuple[str, re.Pattern[str]]]) -> list[tuple[str, str]]:
    """(kind, masked excerpt) for each problem in one added line."""
    hits = []
    allowed = ALLOW_MARK in line
    checks = [PRIVATE_KEY, *extra] + ([] if allowed else GENERIC)
    seen = set()
    for kind, rx in checks:
        for m in rx.finditer(line):
            key = (kind, m.start())
            if key not in seen:
                seen.add(key)
                hits.append((kind, mask(m.group(0))))
    return hits


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    """(path, new line number, text) for every added line of a unified diff; binary files yield nothing."""
    path = None
    lineno = 0
    hunk = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path = None
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target.startswith('"') and target.endswith('"'):
                target = target[1:-1]
            path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            continue
        if raw.startswith("--- ") or raw.startswith("Binary files "):
            continue
        m = hunk.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            yield path, lineno, raw[1:]
            lineno += 1
        elif raw.startswith(" "):
            lineno += 1


def scan_diff(diff: str, extra: list[tuple[str, re.Pattern[str]]]) -> list[Finding]:
    out = []
    for path, lineno, text in added_lines(diff):
        for kind, excerpt in scan_line(text, extra):
            out.append(Finding(f"{path}:{lineno}", kind, excerpt))
    return out


def scan_message(sha: str, message: str, extra: list[tuple[str, re.Pattern[str]]]) -> list[Finding]:
    out = []
    for i, line in enumerate(message.splitlines(), 1):
        for kind, excerpt in scan_line(line, extra):
            out.append(Finding(f"commit {sha[:10]} message line {i}", kind, excerpt))
    return out


def check_identity(sha: str, author: str, committer: str) -> list[Finding]:
    out = []
    for role, email in (("author", author), ("committer", committer)):
        if not email_ok(email):
            out.append(Finding(f"commit {sha[:10]}", f"{role} email is not a GitHub no-reply address", mask(email)))
    return out


# ---- git -----------------------------------------------------------------------------------------------------

def git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], capture_output=True)  # noqa: S603,S607 - git from PATH, as git itself runs hooks
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else ""


def object_exists(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],  # noqa: S603,S607
                          capture_output=True).returncode == 0


def commits_to_push(ref: PushRef, remote: str) -> list[str]:
    """Commits the remote lacks: <remote sha>..<local sha>, or for a new branch (or an unknown remote tip) everything
    not on that remote's tracking refs (a URL with no named remote: the whole history)."""
    if ref.deleting:
        return []
    if not ref.new and object_exists(ref.remote_sha):
        spec = [ref.local_sha, f"^{ref.remote_sha}"]
    else:
        named = remote in git("remote").split()
        spec = [ref.local_sha, "--not", f"--remotes={remote}"] if named else [ref.local_sha]
    return git("rev-list", *spec).split()


def archive_commits() -> set[str]:
    tags = git("tag", "--list", "archive/*").split()
    return set(git("rev-list", *tags).split()) if tags else set()


def commit_diff(sha: str) -> str:
    return git("show", "--format=", "--patch", "--unified=0", "--no-color", "--no-ext-diff", "--no-textconv",
               "--diff-merges=first-parent", "--src-prefix=a/", "--dst-prefix=b/", sha)


def config_emails() -> list[str]:
    return [e for scope in ("--global", "--system")
            for e in git("config", scope, "--get-all", "user.email", check=False).splitlines()]


def read_local_files() -> tuple[str, str]:
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    patterns = common / "info" / "prepush-patterns"
    top = Path(git("rev-parse", "--show-toplevel").strip())
    claude = next((p for p in (top / "CLAUDE.local.md", common.parent / "CLAUDE.local.md") if p.is_file()), None)
    return (patterns.read_text(encoding="utf-8") if patterns.is_file() else "",
            claude.read_text(encoding="utf-8") if claude else "")


def scan_commits(shas: list[str], extra, archived: set[str]) -> list[Finding]:
    out = []
    for sha in shas:
        if sha in archived:
            out.append(Finding(f"commit {sha[:10]}", "belongs to the local-only archive/* history"))
            continue
        author, committer = git("show", "-s", "--format=%ae%x00%ce", sha).strip().split("\x00")
        out += check_identity(sha, author, committer)
        out += scan_message(sha, git("show", "-s", "--format=%B", sha), extra)
        out += [Finding(f"commit {sha[:10]} {f.where}", f.kind, f.excerpt) for f in scan_diff(commit_diff(sha), extra)]
    return out


def run_trivy() -> int:
    top = git("rev-parse", "--show-toplevel").strip()
    print("pre-push: SETTLEVIEW_PREPUSH_TRIVY=1: running the Trivy secret scan (trivy-fs)", file=sys.stderr)
    return subprocess.run([sys.executable, "scripts/quality.py", "--only", "trivy-fs"],  # noqa: S603
                          cwd=top).returncode


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("remote", nargs="?", default="", help="the remote's name (git passes it)")
    p.add_argument("url", nargs="?", default="", help="the remote's URL (git passes it)")
    p.add_argument("--range", dest="rev_range", help="scan this revision range instead of reading git's stdin")
    a = p.parse_args(argv)

    patterns_text, claude_local = read_local_files()
    extra = local_patterns(patterns_text, claude_local, config_emails())
    archived = archive_commits()
    findings: list[Finding] = []
    if a.rev_range:
        findings += scan_commits(git("rev-list", a.rev_range).split(), extra, archived)
    else:
        allow_tags = os.environ.get("SETTLEVIEW_ALLOW_TAGS") == "1"
        refs = parse_push_lines(sys.stdin.read() if stdin_text is None else stdin_text)
        seen: set[str] = set()
        for ref in refs:
            findings += check_ref(ref, allow_tags)
            shas = [s for s in commits_to_push(ref, a.remote) if s not in seen]
            seen.update(shas)
            findings += scan_commits(shas, extra, archived)

    if findings:
        print(f"pre-push: BLOCKED, {len(findings)} finding(s):", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print("Nothing was pushed. Fix the commits (amend, rebase) and push again; a placeholder path or address in "
              "docs or tests may carry `prepush-scan: allow` on its line. See docs/PUBLISHING.md, "
              "\"The pre-push scan\".", file=sys.stderr)
        return 1
    if os.environ.get("SETTLEVIEW_PREPUSH_TRIVY") == "1" and run_trivy() != 0:
        print("pre-push: BLOCKED by the Trivy secret scan (trivy-fs)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
