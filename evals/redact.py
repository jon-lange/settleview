"""Redaction for eval exports, and the adversarial scan that checks it (house rule 7: distrust the sanitization label).

`Redactor` (docs/research/2026-10-31-eval-data-inventory.md, "Sensitive data"):
- drops `observe.balances` (and any `balances` field), every `message_ids` list, and any key in `DROP_KEYS`;
- replaces the value of a `cash` / `balance` / `equity` field with `<balance>`, and in free text the number after
  one of those words (`cash $12.34`, `Polymarket cash 86.52`, `balance: 5`) with `<balance>`;
- bracket ids, request ids with hex in them, and any other run of 16+ hex characters: a salted hash `h_<12 hex>`,
  stable within one export (the same id -> the same pseudonym, so links between rows survive);
- session ids and agent ids: `s01`, `a01`, ... in order of first sight;
- `phone:<id>` (a note's author device) -> `user`;
- secret shapes (API keys, bearer tokens, PEM blocks, long high-entropy tokens), e-mail addresses, IP addresses and
  file paths -> `<secret>`, `<email>`, `<ip>`, `<path>`.
Each rule can be switched off by name (`disabled`), which only the mutation tests do.

`scan(obj)` is independent of the redactor: it walks the finished items and returns every hit of the deny-list (the
same shapes, matched on the output, plus anything that looks like an unhashed id). The exporter fails on any hit, so
an export is never trusted because it says "redacted".
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

DROP_KEYS = frozenset({"balances", "message_ids", "token", "token_hash", "ticket_hash", "tp_order_id",
                       "client_order_id", "order_id", "detail"})
BALANCE_KEYS = frozenset({"cash", "balance", "equity", "available_cash", "buying_power"})
SESSION_KEYS = frozenset({"session_id", "session"})
AGENT_KEYS = frozenset({"agent_id", "agent"})
ID_KEYS = frozenset({"id", "bracket_id", "request_id", "wake_bracket", "proposal_id"})
KEEP_HEX_KEYS = ("sha256",)  # fingerprints: hashes of prompt text and tool specs, not identifiers of anything private

_WORD = r"(?:cash|balances?|equity)"
BALANCE_TEXT = re.compile(rf"(?i)(?<!<)\b({_WORD})\b([^\d\n$.;!?]{{0,24}}?)(?<![\w.])(\$?\s?-?\d[\d,]*(?:\.\d+)?)")
# a number that follows a redacted one closely ("cash $12.30 and $90.10"): the same account's other balance
BALANCE_NEXT = re.compile(r"(<balance>)(\s*(?:,|/|&|\+|and|or)\s*)(\$?-?\d[\d,]*(?:\.\d+)?)(?![\w])")
# amounts anywhere in the same sentence fragment as a balance word ("(Kalshi $41.27, Polymarket $88.40 cash)"):
# a fragment (a sentence) ends at . ; ! ? or a line break (a decimal point is not an end)
FRAGMENT_END = re.compile(r"((?<!\d)\.|\.(?!\d)|[;!?\n])")
BALANCE_WORD = re.compile(rf"(?i)(?<!<)\b{_WORD}\b|<balance>")  # a redacted amount marks its fragment too
AMOUNT = re.compile(r"\$\s?-?\d[\d,]*(?:\.\d+)?|(?<![\w.$])-?\d[\d,]*\.\d{2}(?![\d])")
HEX =re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{16,}(?![0-9a-f])")
UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
PHONE = re.compile(r"\bphone:[A-Za-z0-9_-]+")
SECRETS = (
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("pem", re.compile(r"-----BEGIN [A-Z ]+-----")),
    ("key_file", re.compile(r"(?i)\b[\w.-]*key\.pem\b|\.pem\b")),
    ("password", re.compile(r"(?i)\bpassword\s*[=:]\s*\S+")),
    # a long token with a digit, a letter and one unbroken 20+ character stretch (a key, a bearer or write token);
    # market slugs (cpc-btc-updown-15m-2026-10-29-1500z) and tool names have only short dash/underscore segments
    # (a run of hex only is an id: the hex_ids rule's, hashed, so each rule has one job and its own mutation check)
    ("high_entropy", re.compile(r"(?<![A-Za-z0-9_-])(?![0-9a-fA-F]+(?![A-Za-z0-9_-]))"
                                r"(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])"
                                r"(?=[A-Za-z0-9_-]*[A-Za-z0-9]{20})[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")),
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PATH = re.compile(r"(?i)(?:\b[A-Z]:\\[^\s\"']+|(?<![\w.])/(?:home|Users|etc|var|root|run/secrets)/[^\s\"']*)")
ANTHROPIC_IDS = re.compile(r"\b(?:msg|toolu|req)_[A-Za-z0-9]{10,}\b")
PSEUDONYM = re.compile(r"^h_[0-9a-f]{12}$")

RULES = ("balances", "hex_ids", "uuids", "pseudonyms", "phone", "secrets", "emails", "ips", "paths", "anthropic_ids",
         "drop_keys")


@dataclass
class Redactor:
    salt: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    disabled: frozenset[str] = frozenset()
    _ids: dict[str, str] = field(default_factory=dict)
    _sessions: dict[str, str] = field(default_factory=dict)
    _agents: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = set(self.disabled) - set(RULES)
        if unknown:
            raise ValueError(f"unknown redaction rule(s) {sorted(unknown)}; rules: {', '.join(RULES)}")

    def on(self, rule: str) -> bool:
        return rule not in self.disabled

    @property
    def salt_id(self) -> str:
        return hashlib.sha256(b"salt-id:" + self.salt).hexdigest()[:8]

    def hid(self, value: str) -> str:
        """A salted, stable pseudonym for an identifier."""
        v = str(value)
        if PSEUDONYM.match(v):
            return v
        if v not in self._ids:
            self._ids[v] = "h_" + hashlib.sha256(self.salt + v.encode()).hexdigest()[:12]
        return self._ids[v]

    def session(self, value: Any) -> Any:
        if value is None or not self.on("pseudonyms"):
            return value
        return self._sessions.setdefault(str(value), f"s{len(self._sessions) + 1:02d}")

    def agent(self, value: Any) -> Any:
        if value is None or not self.on("pseudonyms"):
            return value
        return self._agents.setdefault(str(value), f"a{len(self._agents) + 1:02d}")

    # -- free text ----------------------------------------------------------------------------------------------
    def text(self, s: str) -> str:
        # ids first (hashed, so links survive), then anything else secret-shaped is masked
        if self.on("anthropic_ids"):
            s = ANTHROPIC_IDS.sub("<id>", s)
        if self.on("uuids"):
            s = UUID.sub(lambda m: self.hid(m.group(0).lower()), s)
        if self.on("hex_ids"):
            s = HEX.sub(lambda m: self.hid(m.group(0).lower()), s)
        if self.on("secrets"):
            for _, rx in SECRETS:
                s = rx.sub("<secret>", s)
        if self.on("paths"):
            s = PATH.sub("<path>", s)
        if self.on("emails"):
            s = EMAIL.sub("<email>", s)
        if self.on("ips"):
            s = IP.sub("<ip>", s)
        if self.on("phone"):
            s = PHONE.sub("user", s)
        if self.on("balances"):
            while True:  # to a fixed point: a match can hide the next number of the same list
                t = BALANCE_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}<balance>", s)
                t = BALANCE_NEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}<balance>", t)
                if t == s:
                    break
                s = t
            s = balance_fragments(s)
        for k, v in self._sessions_raw().items():  # a raw session id quoted in text (short prefixes too)
            s = s.replace(k, v)
        return s

    def _sessions_raw(self) -> dict[str, str]:
        if not self.on("pseudonyms"):
            return {}
        out = {}
        for raw, p in self._sessions.items():
            out[raw] = p
            if len(raw) >= 16:
                out[raw[:8]] = p  # the UI and the agent quote the first 8 characters ("session 72bc4abe")
        return out

    # -- structures ---------------------------------------------------------------------------------------------
    def value(self, v: Any, key: str | None = None) -> Any:
        k = (key or "").lower()
        if isinstance(v, dict):
            out = {}
            for kk, vv in v.items():
                lk = str(kk).lower()
                if self.on("drop_keys") and lk in DROP_KEYS:
                    continue
                if self.on("balances") and lk in BALANCE_KEYS:
                    out[kk] = None if vv is None else "<balance>"
                    continue
                out[kk] = self.value(vv, str(kk))
            return out
        if isinstance(v, list):
            return [self.value(x, key) for x in v]
        if isinstance(v, str):
            if k.endswith(KEEP_HEX_KEYS):
                return v
            if k in SESSION_KEYS:
                return self.session(v)
            if k in AGENT_KEYS:
                return self.agent(v)
            if k in ID_KEYS and self.on("hex_ids") and (HEX.search(v) or UUID.search(v)):
                return self.text(v) if ":" in v else self.hid(v.lower())
            if k == "by" and self.on("phone") and v.startswith("phone:"):
                return "user"
            return self.text(v)
        return v


def _pieces(s: str) -> list[str]:
    return FRAGMENT_END.split(s)


def balance_fragments(s: str) -> str:
    """Every amount in a sentence fragment that names a balance word -> <balance> (over-redacts on purpose)."""
    if not BALANCE_WORD.search(s):
        return s
    return "".join(AMOUNT.sub("<balance>", p) if BALANCE_WORD.search(p) else p for p in _pieces(s))


def fragment_hits(s: str) -> list[str]:
    return [m.group(0) for p in _pieces(s) if BALANCE_WORD.search(p) for m in AMOUNT.finditer(p)]


# -- the scan -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Hit:
    where: str
    rule: str
    sample: str  # masked: the first and last 2 characters only

    def __str__(self) -> str:
        return f"{self.where}: {self.rule} ({self.sample})"


def _mask(s: str) -> str:
    return s if len(s) <= 6 else f"{s[:2]}…{s[-2:]} ({len(s)} chars)"


DENY = (
    *SECRETS,
    ("email", EMAIL),
    ("ip", IP),
    ("path", PATH),
    ("anthropic_id", ANTHROPIC_IDS),
    ("uuid", UUID),
    ("hex_id", HEX),
    ("phone_id", PHONE),
    ("balance", BALANCE_TEXT),
    ("balance_list", BALANCE_NEXT),
)


def scan(obj: Any, where: str = "$", extra: tuple[str, ...] = ()) -> list[Hit]:
    """Every deny-list hit in `obj` (strings anywhere, and balance / id / secret-shaped keys). `extra`: exact values
    that must not appear (a test's planted values; the exporter's raw session ids)."""
    hits: list[Hit] = []

    def walk(v: Any, path: str, key: str) -> None:
        lk = key.lower()
        if isinstance(v, dict):
            for kk, vv in v.items():
                lkk = str(kk).lower()
                if lkk in DROP_KEYS:
                    hits.append(Hit(f"{path}.{kk}", "dropped_key_present", _mask(str(kk))))
                if lkk in BALANCE_KEYS and vv not in (None, "<balance>"):
                    hits.append(Hit(f"{path}.{kk}", "balance_field", _mask(str(vv))))
                    continue
                walk(vv, f"{path}.{kk}", str(kk))
            return
        if isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]", key)
            return
        if not isinstance(v, str):
            return
        if lk.endswith(KEEP_HEX_KEYS) and re.fullmatch(r"[0-9a-f]{64}|unknown", v):
            return
        for rule, rx in DENY:
            for m in rx.finditer(v):
                hits.append(Hit(path, rule, _mask(m.group(0))))
        for amount in fragment_hits(v):
            hits.append(Hit(path, "balance_fragment", _mask(amount)))
        for x in extra:
            if x and x in v:
                hits.append(Hit(path, "planted_value", _mask(x)))

    walk(obj, where, "")
    return hits
