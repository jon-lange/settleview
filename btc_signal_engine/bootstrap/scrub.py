"""Telemetry scrubbing: what leaves the process in a span, log record or metric point never carries a secret.

Every exporter is wrapped (bootstrap/telemetry.py) so each span name, attribute, event, link and status, each log body
and attribute and each metric point's attributes pass through `Scrubber` on their way out. It removes:
- the exact values of the app's own secrets (the Telegram bot token, the Anthropic key, the Polymarket secret, the
  database passwords: whatever the caller hands in, read from the settings);
- a Telegram bot token in a URL path (`/bot<id>:<token>/`) or anywhere else;
- user:password in a URL; every query value in a URL attribute (`url.full`, `http.url`, `url.query`, `http.target`),
  and in free text the values of query keys that look secret (token, key, sig, signature, password, auth, ...);
- `Authorization` / `Bearer` / `Basic` credentials, cookies, API-key and signature headers or `key=value` pairs
  (also `pin=...` / `"pin": "..."`: the phone PIN is never logged on purpose, this catches it if a body is);
- Anthropic-style keys (`sk-ant-...`), PEM private keys, and long base64 runs that mix upper case, lower case and
  digits (signatures, secrets);
- any captured HTTP header attribute but a few harmless ones, and the value of any attribute whose key names a
  credential (authorization, cookie, password, secret, api key, signature, credential, private key).
Pure standard library: the rules are tested without the OpenTelemetry SDK.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "<redacted>"
MIN_SECRET_LEN = 6  # shorter "secrets" (a PIN, an empty value) would blank ordinary text
URL_KEYS = frozenset({"url.full", "http.url", "url.query", "http.target", "url.original"})
SAFE_HEADERS = frozenset({"content-type", "content-length", "user-agent", "accept", "content-encoding",
                          "x-request-id", "retry-after"})

_TELEGRAM_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_TELEGRAM_TOKEN = re.compile(r"(?<![0-9])\d{5,}:[A-Za-z0-9_-]{30,}")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_QUERY_ANY = re.compile(r"([?&][^=&#\s]+)=([^&#\s]*)")
_SECRET_WORD = (r"(?:[a-z0-9_.-]*(?:token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|apikey|access[_-]?key|"  # noqa: S105 - a pattern of secret words, not a secret
                r"private[_-]?key|sig|signature|auth|authorization|credential|cookie|session|jwt|pin|code)"
                r"[a-z0-9_.-]*)")
_QUERY_SECRET = re.compile(r"(?i)([?&]" + _SECRET_WORD + r")=([^&#\s]*)")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|digest|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_KV_SECRET = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[_-]?key|apikey|"
    r"kalshi-access-(?:key|signature|timestamp)|x-pm-[a-z-]+|password|passwd|secret|secret[_-]?key|token|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|pin|access[_-]?pin)([\"']?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&]+)")
_ANTHROPIC_KEY = re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_B64 = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}|[A-Za-z0-9_-]{40,}")
_SECRET_KEY = re.compile(r"(?i)(^|[._-])(authorization|cookie|set[-_]cookie|password|passwd|secret|api[-_]?key|"
                         r"signature|credential|credentials|private[-_]?key|access[-_]?token|refresh[-_]?token|pin)"
                         r"([._-]|$)")
_HEADER_KEY = re.compile(r"^(?:http|rpc)\.(?:request|response)\.header\.(.+)$")


def _mixed(s: str) -> bool:
    return any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s)


class Scrubber:
    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        found = {s.strip() for s in secrets if isinstance(s, str) and len(s.strip()) >= MIN_SECRET_LEN}
        self._secrets = sorted(found, key=len, reverse=True)  # the longest first: no partial left behind

    def text(self, s: str, *, url: bool = False) -> str:
        """`s` with every secret it carries replaced; `url` True for a URL attribute (every query value goes)."""
        if not s:
            return s
        for v in self._secrets:
            if v in s:
                s = s.replace(v, REDACTED)
        s = _PEM.sub(REDACTED, s)
        s = _TELEGRAM_PATH.sub("/bot" + REDACTED, s)
        s = _TELEGRAM_TOKEN.sub(REDACTED, s)
        s = _USERINFO.sub(lambda m: m.group(1) + REDACTED + "@", s)
        s = (_QUERY_ANY if url else _QUERY_SECRET).sub(lambda m: m.group(1) + "=" + REDACTED, s)
        s = _AUTH_SCHEME.sub(lambda m: m.group(1) + " " + REDACTED, s)
        s = _KV_SECRET.sub(lambda m: m.group(1) + m.group(2) + REDACTED, s)
        s = _ANTHROPIC_KEY.sub(REDACTED, s)
        s = _B64.sub(lambda m: REDACTED if _mixed(m.group(0)) else m.group(0), s)
        return s

    def value(self, key: str, v: Any) -> Any:
        """One attribute's value, scrubbed by its key and its content (strings, bytes and sequences of them)."""
        h = _HEADER_KEY.match(key)
        if h is not None and h.group(1).lower().replace("_", "-") not in SAFE_HEADERS:
            return REDACTED
        if _SECRET_KEY.search(key):
            return REDACTED
        url = key in URL_KEYS
        if isinstance(v, str) and key == "url.query" and not v.startswith("?"):
            return self.text("?" + v, url=True)[1:]  # a bare query string: its first value too
        if isinstance(v, str):
            return self.text(v, url=url)
        if isinstance(v, bytes):
            return self.text(v.decode("utf-8", "replace"), url=url)
        if isinstance(v, (list, tuple)):
            return tuple(self.value(key, x) if isinstance(x, (str, bytes)) else x for x in v)
        if isinstance(v, Mapping):
            return {k: self.value(f"{key}.{k}", x) for k, x in v.items()}
        return v

    def attributes(self, attrs: Mapping[str, Any] | None) -> dict[str, Any]:
        return {k: self.value(k, v) for k, v in (attrs or {}).items()}

    def body(self, v: Any) -> Any:
        """A log record's body (usually the formatted message)."""
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, Mapping):
            return self.attributes(v)
        if isinstance(v, (list, tuple)):
            return [self.body(x) for x in v]
        if isinstance(v, bytes):
            return self.text(v.decode("utf-8", "replace"))
        return v
