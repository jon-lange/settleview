"""Kalshi API-key request signing (U19), per https://docs.kalshi.com/getting_started/api_keys.

Each request carries KALSHI-ACCESS-KEY (key id), KALSHI-ACCESS-TIMESTAMP (ms) and
KALSHI-ACCESS-SIGNATURE: base64 of the signature over timestamp + METHOD + path, where the
path includes the /trade-api/v2 prefix and excludes the query string. RSA keys sign with
RSA-PSS (SHA-256, MGF1-SHA-256, salt = digest length); Ed25519 keys sign the message directly.

The account link is read-only: this auth refuses to sign anything but GET, so no order can
be placed or cancelled through it. Key material stays in memory and never appears in repr.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Generator
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

PrivateKey = rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey


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


class KalshiAuth(httpx.Auth):
    def __init__(self, key_id: str, private_key: PrivateKey,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self._key_id = key_id
        self._key = private_key
        self._clock_ms = clock_ms

    def __repr__(self) -> str:
        return "KalshiAuth(<redacted>)"

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Signed KALSHI-ACCESS-* headers for a GET of `path` (the SDK backend calls this too); anything else raises."""
        if method != "GET":
            raise ReadOnlyViolation(f"read-only Kalshi link refuses {method} {path}")
        ts = str(self._clock_ms())
        return {"KALSHI-ACCESS-KEY": self._key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self._key, ts + method + path)}

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers.update(self.headers(request.method, request.url.path))
        yield request
