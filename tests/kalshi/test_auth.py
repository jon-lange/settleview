"""Kalshi API-key request signing (U19). Keys are throwaway, generated per test; no network."""

from __future__ import annotations

import base64

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from btc_signal_engine.adapters.kalshi.auth import KalshiAuth, ReadOnlyViolation, load_private_key

BASE = "https://api.elections.kalshi.com/trade-api/v2"
NOW_MS = 1793207700123


def _write_pem(tmp_path, key) -> str:
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    path = tmp_path / "kalshi.key"
    path.write_bytes(pem)
    return str(path)


def _send(auth: KalshiAuth, method: str, url: str) -> tuple[list[httpx.Request], httpx.Response | None]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE, auth=auth) as c:
        resp = c.request(method, url)
    return seen, resp


def test_rsa_signed_request_carries_documented_headers_and_verifiable_pss_signature(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = KalshiAuth("key-123", load_private_key(_write_pem(tmp_path, key)), clock_ms=lambda: NOW_MS)

    seen, _ = _send(auth, "GET", "/portfolio/fills?cursor=abc&limit=100")

    req = seen[0]
    assert req.headers["KALSHI-ACCESS-KEY"] == "key-123"
    assert req.headers["KALSHI-ACCESS-TIMESTAMP"] == str(NOW_MS)
    assert req.url.query == b"cursor=abc&limit=100"  # the query is still sent ...
    message = f"{NOW_MS}GET/trade-api/v2/portfolio/fills".encode()  # ... but not signed
    sig = base64.b64decode(req.headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(sig, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                            hashes.SHA256())


def test_signature_over_query_string_would_not_verify(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = KalshiAuth("k", load_private_key(_write_pem(tmp_path, key)), clock_ms=lambda: NOW_MS)
    seen, _ = _send(auth, "GET", "/portfolio/fills?cursor=abc")
    sig = base64.b64decode(seen[0].headers["KALSHI-ACCESS-SIGNATURE"])
    with pytest.raises(InvalidSignature):
        key.public_key().verify(sig, f"{NOW_MS}GET/trade-api/v2/portfolio/fills?cursor=abc".encode(),
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())


def test_ed25519_keys_are_supported(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    auth = KalshiAuth("k", load_private_key(_write_pem(tmp_path, key)), clock_ms=lambda: NOW_MS)
    seen, _ = _send(auth, "GET", "/portfolio/balance")
    sig = base64.b64decode(seen[0].headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(sig, f"{NOW_MS}GET/trade-api/v2/portfolio/balance".encode())


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_non_get_requests_are_refused_before_anything_is_sent(tmp_path, method):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = KalshiAuth("k", load_private_key(_write_pem(tmp_path, key)), clock_ms=lambda: NOW_MS)
    seen: list[httpx.Request] = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
                      base_url=BASE, auth=auth) as c:
        with pytest.raises(ReadOnlyViolation):
            c.request(method, "/portfolio/orders")
    assert seen == []


def test_auth_repr_never_exposes_key_material(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = KalshiAuth("secret-key-id", load_private_key(_write_pem(tmp_path, key)))
    assert "secret-key-id" not in repr(auth) and "PRIVATE" not in repr(auth)
