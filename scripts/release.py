"""Releases: bump the version, close the changelog's "Unreleased" section, commit and tag (docs/operations.md,
"Releases"). Any OS, standard library only; run it on the host in the dev clone (it only edits three files and runs git):

    python scripts/release.py prepare 0.1.0          # or patch | minor | major (from the newest v*.*.* tag)
    python scripts/release.py prepare minor --dry-run
    python scripts/release.py notes 0.1.0            # that version's changelog section (the GitHub Release's notes)
    python scripts/release.py plan --ref refs/tags/v0.1.0 --sha <40 hex>   # CI (.github/workflows/release.yml)

`prepare` refuses unless: the branch is `main`, the working tree is clean, `main` contains `origin/main` (fetch
first), the tag does not exist yet, the version is greater than the newest release tag, and "## [Unreleased]" lists at
least one change. It then:
  - CHANGELOG.md: the Unreleased entries become "## [X.Y.Z] - YYYY-MM-DD" under a new, empty "## [Unreleased]"
  - pyproject.toml `version` and uv.lock's own `settleview` entry become X.Y.Z (CI's `uv lock --check` stays green)
  - commits "Release vX.Y.Z" (those three files) and makes the annotated tag vX.Y.Z

It never pushes. The owner pushes `main`, then the tag; the pre-push hook refuses every tag unless
SETTLEVIEW_ALLOW_TAGS=1 is set for that one push (archive/* tags are refused even then):

    git push origin main
    SETTLEVIEW_ALLOW_TAGS=1 git push origin vX.Y.Z      # PowerShell: $env:SETTLEVIEW_ALLOW_TAGS=1; git push ...

`plan` is the release workflow's first job: for a tag it checks the tag against pyproject.toml and the changelog and
that the tagged commit is on origin/main; for `main` only a `sha-<short>` image is built. Anything else is refused.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/jon-lange/settleview"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
UNRELEASED = "## [Unreleased]"
PYPROJECT_VERSION = re.compile(r'^(version = ")([^"]+)(")$', re.M)
LOCK_VERSION = re.compile(r'(\[\[package\]\]\nname = "settleview"\nversion = ")([^"]+)(")')


class ReleaseError(Exception):
    """A refusal: printed as one line, exit code 1, nothing changed."""


def parse(v: str) -> tuple[int, int, int]:
    m = SEMVER.match(v)
    if not m:
        raise ReleaseError(f"{v!r} is not a version X.Y.Z (no 'v', no pre-release suffix)")
    return int(m[1]), int(m[2]), int(m[3])


def bump(current: tuple[int, int, int], part: str) -> str:
    major, minor, patch = current
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"unknown bump {part!r} (patch, minor, major or a version X.Y.Z)")


# --- the three files -------------------------------------------------------------------------------------------------

def _read(p: Path) -> str:
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def _write(p: Path, text: str) -> None:
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _heading(line: str) -> bool:
    return line.startswith("## ")


def unreleased_body(changelog: str) -> str:
    lines = changelog.split("\n")
    try:
        start = lines.index(UNRELEASED)
    except ValueError:
        raise ReleaseError(f"CHANGELOG.md has no '{UNRELEASED}' heading") from None
    end = next((i for i in range(start + 1, len(lines)) if _heading(lines[i])), len(lines))
    return "\n".join(lines[start + 1:end])


def close_unreleased(changelog: str, version: str, date: str) -> str:
    """The Unreleased entries move under '## [version] - date'; a new empty Unreleased heading stays on top."""
    body = unreleased_body(changelog)
    if not re.search(r"^\s*- \S", body, re.M):
        raise ReleaseError("CHANGELOG.md's Unreleased section lists no change: nothing to release")
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.M):
        raise ReleaseError(f"CHANGELOG.md already has a section for {version}")
    return changelog.replace(f"{UNRELEASED}\n", f"{UNRELEASED}\n\n## [{version}] - {date}\n", 1)


def section(changelog: str, version: str) -> str:
    """The body of '## [version] ...' up to the next '## ' heading (the release notes), stripped."""
    lines = changelog.split("\n")
    start = next((i for i, line in enumerate(lines) if re.match(rf"^## \[{re.escape(version)}\]( |$)", line)), None)
    if start is None:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")
    end = next((i for i in range(start + 1, len(lines)) if _heading(lines[i])), len(lines))
    body = "\n".join(lines[start + 1:end]).strip()
    if not body:
        raise ReleaseError(f"CHANGELOG.md's {version} section is empty")
    return body


def pyproject_version(text: str) -> str:
    m = PYPROJECT_VERSION.search(text)
    if not m:
        raise ReleaseError("pyproject.toml has no `version = \"...\"` line")
    return m[2]


def set_pyproject_version(text: str, version: str) -> str:
    pyproject_version(text)
    return PYPROJECT_VERSION.sub(lambda m: f"{m[1]}{version}{m[3]}", text, count=1)


def set_lock_version(text: str, version: str) -> str:
    if not LOCK_VERSION.search(text):
        raise ReleaseError("uv.lock has no `settleview` package entry")
    return LOCK_VERSION.sub(lambda m: f"{m[1]}{version}{m[3]}", text, count=1)


# --- git -------------------------------------------------------------------------------------------------------------

def git(root: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)  # noqa: S603, S607 - fixed git argv
    if check and p.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip() if p.returncode == 0 else ""


def ref_exists(root: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", ref],  # noqa: S603, S607
                          capture_output=True).returncode == 0


def newest_tag(root: Path) -> str | None:
    tags = [t for t in git(root, "tag", "--list", "v*.*.*").split() if TAG.match(t)]
    return max(tags, key=lambda t: parse(t[1:])) if tags else None


def prepare(root: Path, what: str, date: str, dry_run: bool) -> int:
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise ReleaseError(f"releases are made on main (this is {branch!r})")
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise ReleaseError("the working tree is not clean: commit or stash first")
    if not ref_exists(root, "refs/remotes/origin/main"):
        raise ReleaseError("no origin/main here: `git fetch origin` first")
    if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", "origin/main", "HEAD"],  # noqa: S603, S607
                      capture_output=True).returncode != 0:
        raise ReleaseError("main does not contain origin/main: `git fetch origin` and bring main up to date first")

    pyproject, lock, changelog = root / "pyproject.toml", root / "uv.lock", root / "CHANGELOG.md"
    current = pyproject_version(_read(pyproject))
    latest = newest_tag(root)
    if SEMVER.match(what):
        version = what
    elif latest is None:
        raise ReleaseError(f"no release tag yet: name the first version (pyproject.toml says {current})")
    else:
        version = bump(parse(latest[1:]), what)
    tag = f"v{version}"
    if latest is not None and parse(version) <= parse(latest[1:]):
        raise ReleaseError(f"{version} is not newer than the newest release, {latest}")
    if ref_exists(root, f"refs/tags/{tag}"):
        raise ReleaseError(f"the tag {tag} exists already")

    new_changelog = close_unreleased(_read(changelog), version, date)
    new_pyproject = set_pyproject_version(_read(pyproject), version)
    new_lock = set_lock_version(_read(lock), version)
    print(f"release: {tag} ({date}); pyproject.toml {current} -> {version}; CHANGELOG.md Unreleased -> [{version}]")
    if dry_run:
        print("release: --dry-run: nothing written, no commit, no tag")
        return 0
    _write(changelog, new_changelog)
    _write(pyproject, new_pyproject)
    _write(lock, new_lock)
    git(root, "add", "--", "CHANGELOG.md", "pyproject.toml", "uv.lock")
    git(root, "commit", "--quiet", "-m", f"Release {tag}", "--", "CHANGELOG.md", "pyproject.toml", "uv.lock")
    git(root, "tag", "--annotate", tag, "-m", f"Settleview {version}")
    print(f"release: committed 'Release {tag}' ({git(root, 'rev-parse', '--short', 'HEAD')}) and tagged {tag}. "
          "Nothing is pushed. Next (the owner):")
    print("  git push origin main")
    print(f"  SETTLEVIEW_ALLOW_TAGS=1 git push origin {tag}    # the release workflow builds, scans, signs, releases")
    return 0


def plan(root: Path, ref: str, sha: str, repository: str) -> int:
    """key=value lines for $GITHUB_OUTPUT: kind (release | main), version, tag. The image's tags themselves come
    from docker/metadata-action in the workflow."""
    if repository != "jon-lange/settleview":
        raise ReleaseError(f"the release workflow pushes {IMAGE} only; this is {repository!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError(f"{sha!r} is not a commit sha")
    short = sha[:7]
    if ref == "refs/heads/main":
        print("kind=main")
        print(f"version=sha-{short}")
        print("tag=")
        return 0
    if not ref.startswith("refs/tags/") or not TAG.match(ref.removeprefix("refs/tags/")):
        raise ReleaseError(f"{ref!r}: the release workflow runs for a vX.Y.Z tag or for main only")
    tag = ref.removeprefix("refs/tags/")
    version = tag[1:]
    have = pyproject_version(_read(root / "pyproject.toml"))
    if have != version:
        raise ReleaseError(f"the tag is {tag} but pyproject.toml says {have}: tag the release commit "
                           "(`python scripts/release.py prepare`)")
    section(_read(root / "CHANGELOG.md"), version)
    if git(root, "rev-parse", "HEAD") != sha:
        raise ReleaseError(f"the checkout is not {sha}")
    if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", sha, "origin/main"],  # noqa: S603, S607
                      capture_output=True).returncode != 0:
        raise ReleaseError(f"{tag} ({short}) is not on origin/main: release only commits on main")
    print("kind=release")
    print(f"version={version}")
    print(f"tag={tag}")
    return 0


def notes(root: Path, version: str, image: str | None) -> int:
    body = section(_read(root / "CHANGELOG.md"), version)
    print(body)
    if image:
        tag = f"v{version}"
        print(f"""
## Image

`{image}` (linux/amd64, linux/arm64; deploy this tag@digest, never the tag alone). Signed with cosign keyless by this
repository's release workflow; the CycloneDX SBOMs below are attached to it as attestations too. Verify before
deploying (docs/operations.md, "Releases and deploying by digest"):

```bash
cosign verify {image} \\
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \\
  --certificate-identity https://github.com/jon-lange/settleview/.github/workflows/release.yml@refs/tags/{tag}
```""")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("prepare", help="bump, close the changelog section, commit and tag (no push)")
    pr.add_argument("what", help="X.Y.Z, or patch | minor | major from the newest v*.*.* tag")
    pr.add_argument("--date", default=dt.date.today().isoformat(), help="the release date (default today)")
    pr.add_argument("--dry-run", action="store_true", help="check and say what would change; write nothing")
    no = sub.add_parser("notes", help="print a version's changelog section")
    no.add_argument("version")
    no.add_argument("--image", help="also print the image (REPO@sha256:...) and its cosign verify command")
    pl = sub.add_parser("plan", help="CI: what the release workflow builds for this ref")
    pl.add_argument("--ref", required=True)
    pl.add_argument("--sha", required=True)
    pl.add_argument("--repository", required=True)
    a = p.parse_args(argv)
    try:
        if a.cmd == "prepare":
            dt.date.fromisoformat(a.date)
            return prepare(a.root, a.what, a.date, a.dry_run)
        if a.cmd == "notes":
            parse(a.version)
            return notes(a.root, a.version, a.image)
        return plan(a.root, a.ref, a.sha, a.repository)
    except ReleaseError as e:
        print(f"release: refused: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
