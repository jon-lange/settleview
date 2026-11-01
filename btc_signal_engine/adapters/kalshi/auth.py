"""Kalshi API-key request signing (U19), per https://docs.kalshi.com/getting_started/api_keys.

Each request carries KALSHI-ACCESS-KEY (key id), KALSHI-ACCESS-TIMESTAMP (ms) and
KALSHI-ACCESS-SIGNATURE: base64 of the signature over timestamp + METHOD + path, where the
path includes the /trade-api/v2 prefix and excludes the query string. RSA keys sign with
RSA-PSS (SHA-256, MGF1-SHA-256, salt = digest length); Ed25519 keys sign the message directly.

`KalshiSigner` is the one signer every Kalshi credential goes through (review fix plan 2026-10-31, P2 "DRY" and
B3#13): a subclass names its allow-list of (method, path) and the hosts it may sign for, and the base refuses
everything else before a header exists. Host pinning: a request (httpx `auth_flow`, or the SDK's
`create_auth_headers` through `url_headers`) is signed only over https to one of the signer's hosts on the default
port, so a mistaken base URL never receives KALSHI-ACCESS-* headers. `headers(method, path)` (no URL) is for the
WebSocket handshakes, whose URLs are fixed in their stream modules.

* `KalshiAuth` (the account link): GET only, so no order can be placed or cancelled through it.
* `TradingAuth` (adapters/kalshi/trading.py), `MarginAuth` (margin.py), `WsOnlyAuth` (stream.py): their own
  allow-lists; MarginAuth keeps its stricter rules (one env's host; never cancel-all, subaccount create or transfer,
  or any /margin/isolated/* path).

Key material stays in memory and never appears in repr.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

PrivateKey = rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey

# Kalshi's REST hosts (https://docs.kalshi.com/getting_started/api_environments.md): the elections API the account
# link reads, and the external API (production, demo) the trading and margin clients use.
ELECTIONS_HOST = "api.elections.kalshi.com"
PROD_HOST = "external-api.kalshi.com"
DEMO_HOST = "external-api.demo.kalshi.co"
REST_HOSTS = frozenset({ELECTIONS_HOST, PROD_HOST, DEMO_HOST})


class ReadOnlyViolation(RuntimeError):
    """A non-GET request was attempted through the read-only Kalshi account link."""


def load_private_key(path: str | Path) -> PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise ValueError("Kalshi API keys must be RSA or Ed25519")
    return key


def sign(key: PrivateKey, message: str) -> str:
    data = message.encode("utf-8")
    if isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(data)
    else:
        sig = key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
    return base64.b64encode(sig).decode("ascii")


def now_ms() -> int:
    return int(time.time() * 1000)


class KalshiSigner(httpx.Auth):
    """Signs KALSHI-ACCESS-* headers for an allowed (method, path), sent only to one of `hosts` (module docstring).
    Subclasses set `violation` (the exception a refusal raises), `refuses` (its message's start) and `allows`."""

    violation: type[Exception] = RuntimeError
    refuses = "Kalshi signer refuses"

    def __init__(self, key_id: str, private_key: PrivateKey, *, hosts: Iterable[str],
                 clock_ms: Callable[[], int] = now_ms) -> None:
        self._key_id = key_id
        self._key = private_key
        self._clock_ms = clock_ms
        self._hosts = frozenset(hosts)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    @property
    def hosts(self) -> frozenset[str]:
        return self._hosts

    def allows(self, method: str, path: str) -> bool:
        raise NotImplementedError

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Signed headers for one allowed (method, path) (path with /trade-api/..., no query); anything else raises."""
        if not self.allows(method, path):
            raise self.violation(f"{self.refuses} {method} {path}")
        ts = str(self._clock_ms())
        return {"KALSHI-ACCESS-KEY": self._key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self._key, ts + method + path)}

    def _pinned(self, scheme: str, host: str | None, port: int | None) -> None:
        if scheme != "https" or host not in self._hosts or port not in (None, 443):
            only = ", ".join(sorted(self._hosts)) or "none: it signs a WebSocket handshake only"
            raise self.violation(f"{self.refuses} host {host!r} (only {only})")

    def url_headers(self, method: str, url: str) -> dict[str, str]:
        """Signed headers for a full URL (the SDK's auth hook): the host is checked first, then the method and the
        URL's path as written (without the query)."""
        parts = urlsplit(url)
        self._pinned(parts.scheme, parts.hostname, parts.port)
        return self.headers(method, parts.path)

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        url = request.url
        self._pinned(url.scheme, url.host, url.port)
        request.headers.update(self.headers(request.method, url.path))
        yield request


class KalshiAuth(KalshiSigner):
    """The read-only account link: GET only, to Kalshi's REST hosts."""

    violation = ReadOnlyViolation
    refuses = "read-only Kalshi link refuses"

    def __init__(self, key_id: str, private_key: PrivateKey, clock_ms: Callable[[], int] = now_ms, *,
                 hosts: Iterable[str] = REST_HOSTS) -> None:
        super().__init__(key_id, private_key, hosts=hosts, clock_ms=clock_ms)

    def allows(self, method: str, path: str) -> bool:
        return method == "GET"
