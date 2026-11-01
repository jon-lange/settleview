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


# -- one signer base, host pinning (review fix plan P2 "DRY", B3#13) ----------------------------------------------------
def _signers():
    from btc_signal_engine.adapters.kalshi.margin import MarginAuth
    from btc_signal_engine.adapters.kalshi.stream import WsOnlyAuth
    from btc_signal_engine.adapters.kalshi.trading import TradingAuth

    key = ed25519.Ed25519PrivateKey.generate()
    return {"link": KalshiAuth("k", key), "trading": TradingAuth("k", key), "margin": MarginAuth("k", key, env="perp_demo"),
            "ws": WsOnlyAuth("k", key)}


def test_every_kalshi_credential_signs_through_the_one_base():
    from btc_signal_engine.adapters.kalshi.auth import KalshiSigner

    for s in _signers().values():
        assert isinstance(s, KalshiSigner) and "<redacted>" in repr(s)


@pytest.mark.parametrize(("name", "url"), [
    ("link", "https://evil.example/trade-api/v2/portfolio/balance"),
    ("link", "http://api.elections.kalshi.com/trade-api/v2/portfolio/balance"),  # not https
    ("link", "https://api.elections.kalshi.com:8443/trade-api/v2/portfolio/balance"),  # not the default port
    ("trading", "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance"),  # not a trading host
    ("trading", "https://external-api.kalshi.com.evil.example/trade-api/v2/portfolio/balance"),
    ("margin", "https://external-api.kalshi.com/trade-api/v2/margin/positions"),  # perp_demo signs demo only
    ("ws", "https://external-api.kalshi.com/trade-api/ws/v2"),  # the handshake signer pins no REST host
])
def test_a_signer_refuses_a_host_it_is_not_pinned_to_before_signing(name, url):
    s = _signers()[name]
    with pytest.raises(s.violation, match="host"):
        s.url_headers("GET", url)
    seen: list[httpx.Request] = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)), auth=s) as c:
        with pytest.raises(s.violation, match="host"):
            c.get(url)
    assert seen == []


@pytest.mark.parametrize(("name", "url"), [
    ("link", "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance"),
    ("link", "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance"),
    ("trading", "https://external-api.kalshi.com/trade-api/v2/portfolio/balance"),
    ("trading", "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance?limit=5"),
    ("margin", "https://external-api.demo.kalshi.co/trade-api/v2/margin/positions"),
])
def test_a_pinned_host_signs_the_path_without_the_query(name, url):
    h = _signers()[name].url_headers("GET", url)
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}


def test_margin_auth_keeps_its_allow_list_exclusions():
    """AGENTS.md: MarginAuth never signs cancel-all, subaccount create or transfer, or any /margin/isolated/* path,
    whatever the host."""
    m = _signers()["margin"]
    base = "https://external-api.demo.kalshi.co/trade-api/v2"
    for method, path in [("DELETE", "/margin/orders"), ("POST", "/portfolio/subaccounts"),
                         ("POST", "/portfolio/subaccounts/transfer"), ("GET", "/margin/isolated/positions"),
                         ("POST", "/margin/isolated/orders"), ("GET", "/margin/isolated")]:
        with pytest.raises(m.violation, match="refuses"):
            m.url_headers(method, base + path)
    assert m.url_headers("DELETE", base + "/margin/orders/abc-123")  # cancel ONE order stays allowed
