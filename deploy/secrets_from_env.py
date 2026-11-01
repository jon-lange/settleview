"""Move the venue / API secrets out of `.env` into the secret files compose.yaml mounts, once (the cut-over to secrets
as files; docs/operations.md, "Secrets as files"). Any OS, standard library only:

    python deploy/secrets_from_env.py [--env PATH] [--secrets DIR] [--keep-env] [--dry-run]

For each of these settings found with a value in the `.env` (default: next to compose.yaml):

  BTCFLOW_POLYMARKET_KEY_ID      -> secrets/polymarket_key_id
  BTCFLOW_POLYMARKET_SECRET_KEY  -> secrets/polymarket_secret_key
  BTCFLOW_ANTHROPIC_API_KEY      -> secrets/anthropic_api_key
  BTCFLOW_TELEGRAM_BOT_TOKEN     -> secrets/telegram_bot_token
  BTCFLOW_KALSHI_KEY_ID          -> secrets/kalshi_key_id
  BTCFLOW_TRADING_DEMO_KEY_ID    -> secrets/trading_demo_key_id
  BTCFLOW_TRADING_PROD_KEY_ID    -> secrets/trading_prod_key_id

the value is written to its file (one line), read back and compared, and only then is the line removed from `.env`
(replaced by a comment naming the file, never the value; the rest of `.env` is kept byte for byte, rewritten
atomically). A file that already holds the same value is kept; one that holds a different value is a conflict: that
setting is left as it is in both places and the exit code is 1. Every other secret file is created empty (= not
configured), so compose finds them all. It prints setting and file names only, never a value, and makes no other copy
of a secret (no backup of `.env`). Running it again changes nothing.

--keep-env leaves `.env` as it is (for a checkout that also runs without Docker): then the app in Docker refuses to
start with "both BTCFLOW_X and BTCFLOW_X_FILE are set" until the lines are gone; that is deliberate (no silent choice
between two values). --dry-run says what it would do and writes nothing.

Key FILES (the Kalshi .pem paths, *_KEY_PATH) are not values and are not moved: keep mounting them read-only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The same names as deploy/postgres/make_secrets.py APP_SECRETS and compose.yaml's secrets.
APP_SECRETS = ("polymarket_key_id", "polymarket_secret_key", "anthropic_api_key", "telegram_bot_token", "kalshi_key_id",
               "trading_demo_key_id", "trading_prod_key_id")


class EnvLine:
    """One `.env` line: its raw text and, for an assignment, the key and the value as compose reads it."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.key: str | None = None
        self.value: str | None = None
        self.bad = False
        body = raw.rstrip("\r\n").lstrip("﻿").strip()
        if not body or body.startswith("#"):
            return
        if body.startswith("export "):
            body = body[len("export "):].lstrip()
        key, sep, rest = body.partition("=")
        if not sep:
            return
        self.key = key.strip()
        rest = rest.strip()
        if rest[:1] in ("'", '"'):
            q = rest[0]
            out, i, closed = [], 1, False
            while i < len(rest):
                c = rest[i]
                if q == '"' and c == "\\" and i + 1 < len(rest):
                    out.append({"n": "\n", "t": "\t", "r": "\r"}.get(rest[i + 1], rest[i + 1]))
                    i += 2
                    continue
                if c == q:
                    closed = True
                    break
                out.append(c)
                i += 1
            self.value = "".join(out)
            self.bad = not closed  # a value spanning lines: not handled here (move it by hand)
        else:
            cut = rest.find(" #")
            self.value = (rest[:cut] if cut >= 0 else rest).strip()


def _write(f: Path, text: str) -> None:
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)  # readable by the containers' users (as make_secrets)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
        out.write(text)


def _held(f: Path) -> str | None:
    """The value a secret file holds (one trailing newline dropped, as the settings read it), None when empty or
    missing."""
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return None
    text = text.removesuffix("\n").removesuffix("\r")
    return text if text.strip() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python deploy/secrets_from_env.py", description=__doc__.splitlines()[0])
    ap.add_argument("--env", type=Path, default=ROOT / ".env", help="the .env file (default: next to compose.yaml)")
    ap.add_argument("--secrets", type=Path, default=ROOT / "secrets", help="the secrets folder (default ./secrets)")
    ap.add_argument("--keep-env", action="store_true", help="write the files but leave .env as it is")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen; write nothing")
    a = ap.parse_args(argv)
    say = print
    lines = [EnvLine(raw) for raw in a.env.read_text(encoding="utf-8").splitlines(keepends=True)] \
        if a.env.exists() else []
    if not a.env.exists():
        say(f"no {a.env}: nothing to move (secret files are only created where missing)")
    if not a.dry_run:
        a.secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    drop: set[int] = set()
    conflicts = 0
    for name in APP_SECRETS:
        var, f = f"BTCFLOW_{name.upper()}", a.secrets / name
        found = [(i, ln) for i, ln in enumerate(lines) if ln.key == var]
        for ln in lines:
            if ln.key == var + "_FILE" and ln.value:
                say(f"note    {var}_FILE is set in {a.env.name}: compose now sets it to /run/secrets/{name}; put that "
                    f"file's content in {f} and delete the line (not moved by this script)")
        values = [ln for _i, ln in found if ln.value]
        if any(ln.bad for _i, ln in found):
            say(f"skip    {var}: its value in {a.env.name} spans lines; move it to {f} by hand")
            conflicts += 1
            continue
        if not values:
            drop.update(i for i, _ln in found)  # an empty assignment: nothing to keep
            if f.exists():
                say(f"kept    {f}")
            else:
                if not a.dry_run:
                    _write(f, "")
                say(f"created {f} (empty: not configured)")
            continue
        value = values[-1].value  # as compose: the last assignment wins
        held = _held(f)
        if held is not None and held != value:
            say(f"CONFLICT {var}: {f} already holds a different value; left as it is in both places")
            conflicts += 1
            continue
        if held is None:
            if not a.dry_run:
                _write(f, value + "\n")
                if _held(f) != value:
                    say(f"ERROR   {var}: {f} did not read back the same; {a.env.name} left as it is")
                    conflicts += 1
                    continue
            say(f"moved   {var} -> {f}")
        else:
            say(f"same    {var}: {f} already holds it")
        drop.update(i for i, _ln in found)
    if drop and not a.keep_env and lines:
        out = []
        for i, ln in enumerate(lines):
            if i in drop:
                nl = "\r\n" if ln.raw.endswith("\r\n") else "\n"
                out.append(f"# {ln.key}: moved to secrets/{ln.key[len('BTCFLOW_'):].lower()} "
                           f"(deploy/secrets_from_env.py, {stamp}){nl}")
            else:
                out.append(ln.raw)
        if a.dry_run:
            say(f"would remove {len(drop)} line(s) from {a.env}")
        else:
            tmp = a.env.with_name(a.env.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as w:
                w.write("".join(out))
            os.replace(tmp, a.env)
            say(f"removed {len(drop)} line(s) from {a.env} (each replaced by a comment naming its file)")
    elif drop and a.keep_env:
        say(f"kept {a.env} as it is (--keep-env): remove those lines before starting the stack in Docker")
    say("done" + (" (dry run: nothing written)" if a.dry_run else "") + (f"; {conflicts} to resolve by hand"
                                                                          if conflicts else ""))
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
