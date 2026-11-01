"""The PC browser's credential (P0 #1, docs/plans/2026-10-31-1800-review-fix-plan.md): who is "the PC" is a paired
browser, never a network position.

- A one-time pairing code: `python -m btc_signal_engine.ops pair-pc` (on the host, or `docker compose exec app ...`)
  writes `<data dir>/pc-pairing.json` = {sha256 of the code, created_ms, expires_ms} and prints the code and a link.
  The plain code is never stored. It is good for PAIRING_TTL_MS (5 minutes), for one browser: the app deletes the file
  when a browser pairs with it, and after MAX_PAIR_FAILURES wrong codes (a new `pair-pc` replaces any pending code).
- The browser's cookie (PC_COOKIE): "<issued_ms>.<nonce>.<hmac>", HMAC-SHA256 under the PC secret, valid for
  PC_COOKIE_DAYS. The secret is `<data dir>/pc-secret` (32 random bytes, hex; created on first use, mode 0600). It is a
  file in the app's own data folder on purpose, not a database row: the recorders share the app's database role (and
  the research role reads the app schema), but only the app's container mounts its data folder.
- "Sign out PC browsers" (the Phone access card, or `ops signout-pc`) replaces the secret: every PC cookie stops
  working. The app re-reads the secret file at most every RELOAD_MS, so a sign-out from `ops` applies within seconds.

Standard library only: `ops` imports this module and must never load the app (.importlinter "supervisor-is-light").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

PC_COOKIE = "settleview_pc"
SECRET_FILE = "pc-secret"  # noqa: S105 - a file name, not a secret
PAIRING_FILE = "pc-pairing.json"
PAIRING_TTL_MS = 5 * 60_000
PC_COOKIE_DAYS = 400  # browsers cap a cookie's lifetime at 400 days
PC_COOKIE_MS = PC_COOKIE_DAYS * 86_400_000
MAX_PAIR_FAILURES = 5
RELOAD_MS = 5_000
# No 0/O, 1/I/L: a code is read off a terminal and typed. 12 characters of 31 is about 59 bits.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 12


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def normalize_code(code: str) -> str:
    """The code as typed or pasted: case, spaces and dashes do not matter."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())[:64]


def _digest(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode("ascii", "ignore")).hexdigest()


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (a temp file then a rename), readable by this user only where the OS has
    modes (POSIX; on Windows the folder's ACL applies)."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _make_code(now: int) -> tuple[str, int]:
    """(a random code as XXXX-XXXX-XXXX, its expiry)."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LEN, 4)), now + PAIRING_TTL_MS


def create_pairing_code(folder: Path, now_ms: int | None = None) -> tuple[str, int]:
    """A new one-time pairing code (replacing any pending one): (code as XXXX-XXXX-XXXX, expires_ms)."""
    now = _now_ms() if now_ms is None else now_ms
    code, expires = _make_code(now)
    folder.mkdir(parents=True, exist_ok=True)
    _write_private(folder / PAIRING_FILE, json.dumps({"hash": _digest(code), "created_ms": now,
                                                      "expires_ms": expires}))
    return code, expires


def _signature(secret: bytes, payload: str) -> str:
    return hmac.new(secret, ("pc." + payload).encode("ascii"), hashlib.sha256).hexdigest()


def cookie_from_secret_file(folder: Path, now_ms: int | None = None) -> str:
    """A PC cookie signed under the secret file the running app already uses, read only: nothing is created, replaced
    or written (FileNotFoundError / ValueError when the app has made no valid secret yet). For the deploy gate's
    read-only smoke suite (scripts/smoke.py), which mounts the app's data volume read-only and sends GETs only; anyone
    who can read that volume can be the PC anyway."""
    secret = bytes.fromhex((Path(folder) / SECRET_FILE).read_text(encoding="ascii").strip())
    if len(secret) < 16:
        raise ValueError("the PC secret file is too short")
    payload = f"{_now_ms() if now_ms is None else now_ms}.{secrets.token_hex(12)}"
    return f"{payload}.{_signature(secret, payload)}"


def rotate_secret_file(folder: Path) -> None:
    """A new PC secret: every PC browser's cookie stops working (they pair again)."""
    folder.mkdir(parents=True, exist_ok=True)
    _write_private(folder / SECRET_FILE, secrets.token_hex(32) + "\n")


class PcCredentials:
    """The app's side: verifies and issues PC cookies, and redeems pairing codes. `folder` None keeps everything in
    memory (tests). `clock_ms` is injectable for tests."""

    def __init__(self, folder: Path | None = None, *, clock_ms: Callable[[], int] = _now_ms) -> None:
        self.folder = Path(folder) if folder is not None else None
        self.clock_ms = clock_ms
        self._lock = threading.Lock()
        self._mem_code: dict | None = None  # the pending code without a folder
        self._failures = 0  # wrong codes against the pending code whose hash is _failed_hash
        self._failed_hash: str | None = None
        self._secret = self._load_or_create()
        self._loaded_ms = clock_ms()

    # -- the secret -------------------------------------------------------------------------------------------------
    def _load_or_create(self) -> bytes:
        if self.folder is None:
            return secrets.token_bytes(32)
        path = self.folder / SECRET_FILE
        try:
            return bytes.fromhex(path.read_text(encoding="ascii").strip())
        except FileNotFoundError:
            pass
        except ValueError:
            # A damaged secret file: replace it (every PC browser pairs again) rather than run without one.
            pass
        rotate_secret_file(self.folder)
        return bytes.fromhex((self.folder / SECRET_FILE).read_text(encoding="ascii").strip())

    def reload_if_stale(self) -> None:
        """Re-read the secret file (an `ops signout-pc` in another process) at most every RELOAD_MS. Blocking: the
        middleware calls it in a worker thread."""
        if self.folder is None or self.clock_ms() - self._loaded_ms < RELOAD_MS:
            return
        secret = self._load_or_create()
        with self._lock:
            self._secret, self._loaded_ms = secret, self.clock_ms()

    @property
    def stale(self) -> bool:
        return self.folder is not None and self.clock_ms() - self._loaded_ms >= RELOAD_MS

    def sign_out_all(self) -> None:
        """Every PC browser is signed out (a new secret); each must pair again."""
        with self._lock:
            if self.folder is None:
                self._secret = secrets.token_bytes(32)
            else:
                rotate_secret_file(self.folder)
                self._secret = bytes.fromhex((self.folder / SECRET_FILE).read_text(encoding="ascii").strip())
            self._loaded_ms = self.clock_ms()

    # -- cookies ----------------------------------------------------------------------------------------------------
    def _sign(self, payload: str) -> str:
        return _signature(self._secret, payload)

    def issue(self) -> str:
        payload = f"{self.clock_ms()}.{secrets.token_hex(12)}"
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: str | None) -> bool:
        if not token or len(token) > 200:
            return False
        try:
            issued, nonce, sig = token.split(".")
            payload = f"{int(issued)}.{nonce}"
        except ValueError:
            return False
        if not hmac.compare_digest(self._sign(payload), sig):
            return False
        return self.clock_ms() < int(issued) + PC_COOKIE_MS

    # -- pairing ----------------------------------------------------------------------------------------------------
    def new_code(self) -> tuple[str, int]:
        """A pairing code from inside the app (tests; the real one comes from `ops pair-pc`)."""
        if self.folder is not None:
            with self._lock:
                self._failures = 0
            return create_pairing_code(self.folder, self.clock_ms())
        code, expires = _make_code(self.clock_ms())
        with self._lock:
            self._mem_code = {"hash": _digest(code), "expires_ms": expires}
            self._failures = 0
        return code, expires

    def _pending(self) -> dict | None:
        if self.folder is None:
            return self._mem_code
        try:
            rec = json.loads((self.folder / PAIRING_FILE).read_text(encoding="ascii"))
        except (OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) and isinstance(rec.get("hash"), str) else None

    def _drop_pending(self) -> None:
        self._failures = 0
        if self.folder is None:
            self._mem_code = None
        else:
            (self.folder / PAIRING_FILE).unlink(missing_ok=True)

    def redeem(self, code: str) -> tuple[bool, str]:
        """(ok, message). A right, unexpired code is used up; wrong codes are counted and the pending code is dropped
        after MAX_PAIR_FAILURES. Blocking (file I/O): call it in a worker thread."""
        with self._lock:
            rec = self._pending()
            now = self.clock_ms()
            if rec is None:
                return False, "No pairing code is waiting. Run `python -m btc_signal_engine.ops pair-pc` again."
            if now >= int(rec.get("expires_ms") or 0):
                self._drop_pending()
                return False, "That pairing code has expired. Run `python -m btc_signal_engine.ops pair-pc` again."
            if not normalize_code(code) or not hmac.compare_digest(_digest(code), rec["hash"]):
                if self._failed_hash != rec["hash"]:  # a new code from `ops pair-pc`: its own count
                    self._failed_hash, self._failures = rec["hash"], 0
                self._failures += 1
                if self._failures >= MAX_PAIR_FAILURES:
                    self._drop_pending()
                    return False, ("Too many wrong codes: the pairing code is cancelled. Run `python -m "
                                   "btc_signal_engine.ops pair-pc` again.")
                return False, "Wrong pairing code."
            self._drop_pending()
            return True, "This browser is paired: it is the PC."
