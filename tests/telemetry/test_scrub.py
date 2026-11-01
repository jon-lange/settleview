"""The telemetry scrubber's rules (bootstrap/scrub.py), standard library only: every secret shape the app handles is
removed from text and attributes, and ordinary values (ids, tickers, routes, token counts) are left alone."""

from __future__ import annotations

import pytest

from btc_signal_engine.bootstrap.scrub import REDACTED, Scrubber

TG = "7123456789:AAHfakeTelegramTokenValue_abcdefghijk12"
ANTHROPIC = "sk-ant-api03-FAKEfakeFAKEfake0123456789abcdefABCDEF"
PM_SECRET = "qZ8vY2m4Xr7Lw1Tn5Ks9Hb3Jc6Gd0Fe+Ua/Pi8Ox2Mz="  # gitleaks:allow - planted fake
DB_PW = "Wc3n_x9QpL2vR7tY8uZa1bKd"
SIG = "kH3s9+Qm2Lx7Vb1Nc8Zr5Tw0Py4Jd6Ga/Fe3Ui9Oq2Ws7Ex1Rc5Tv8Yb2Nm4Kl6Hj0Gf3Dd=="

s = Scrubber([DB_PW, "short", None, ""])


@pytest.mark.parametrize("text, gone", [
    (f"https://api.telegram.org/bot{TG}/getUpdates?offset=5", TG),
    (f"telegram said no for {TG}", TG),
    ("postgresql://settleview_app:" + DB_PW + "@postgres:5432/settleview", DB_PW),
    (f"connect failed: password={DB_PW}", DB_PW),
    (f"Authorization: Bearer {ANTHROPIC}", ANTHROPIC),
    ("Authorization: Basic YWRtaW5Ac2V0dGxldmlldy5sb2NhbDpUaHJvd2F3YXktMQ==", "YWRtaW5Ac2V0dGxldmlldy5sb2NhbDpUaHJvd2F3YXktMQ"),
    (f"key {ANTHROPIC} rejected", ANTHROPIC),
    (f"secret_key={PM_SECRET}", PM_SECRET),
    (f"KALSHI-ACCESS-SIGNATURE: {SIG}", SIG),
    (f"signed {SIG} at 12", SIG),
    ("GET /x?token=abc123secret&ticker=KXBTC", "abc123secret"),
    ("GET /x?api_key=k-1-2-3&limit=5", "k-1-2-3"),
    ("Cookie: settleview_session=s3ss10nV4lu3", "s3ss10nV4lu3"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----", "MIIEvQIBADANBg"),
    ("https://user:hunter22@example.com/path", "hunter22"),
])
def test_secrets_are_removed_from_text(text, gone):
    out = s.text(text)
    assert gone not in out and REDACTED in out


@pytest.mark.parametrize("text", [
    "GET /api/play/now", "KXBTC15M-25OCT311900-00", "btc-updown-15m-1730400000", "input_tokens=1200 output_tokens=40",
    "trace 4bf92f3577b34da6a3ce929d0e0e4736", "order 7f1c2a9e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", "short password",
    "SELECT app.trades.ts FROM app.trades WHERE app.trades.ts >= ?", "/usr/local/lib/python3.13/site-packages/x.py",
    "tokens: 5", "http.route /api/agent/sessions/{sid}/stream",
])
def test_ordinary_values_are_kept(text):
    assert s.text(text) == text


def test_url_attributes_lose_every_query_value_free_text_only_secret_ones():
    url = "https://api.example.com/v1/m?ticker=KXBTC&cursor=abc"
    assert s.value("url.full", url) == f"https://api.example.com/v1/m?ticker={REDACTED}&cursor={REDACTED}"
    assert s.value("message", url) == url
    assert s.value("url.query", "pin=1234&token=abc") == f"pin={REDACTED}&token={REDACTED}"


def test_header_attributes_are_redacted_except_harmless_ones():
    attrs = s.attributes({"http.request.header.authorization": ("Bearer x",),
                          "http.request.header.cookie": ("a=b",),
                          "http.request.header.kalshi_access_signature": ("sig",),
                          "http.response.header.set_cookie": ("c=d",),
                          "http.request.header.content_type": ("application/json",),
                          "db.password": "pw", "api_key": "x", "gen_ai.usage.input_tokens": 1200,
                          "gen_ai.tool.name": "place_entry"})
    assert attrs["http.request.header.authorization"] == REDACTED
    assert attrs["http.request.header.cookie"] == REDACTED
    assert attrs["http.request.header.kalshi_access_signature"] == REDACTED
    assert attrs["http.response.header.set_cookie"] == REDACTED
    assert attrs["http.request.header.content_type"] == ("application/json",)
    assert attrs["db.password"] == REDACTED and attrs["api_key"] == REDACTED
    assert attrs["gen_ai.usage.input_tokens"] == 1200 and attrs["gen_ai.tool.name"] == "place_entry"


def test_sequences_bytes_and_bodies():
    out = s.value("x", (f"bot{TG}", 3))
    assert TG not in str(out) and out[1] == 3
    assert TG not in s.value("x", TG.encode())
    assert s.body({"msg": f"Bearer {ANTHROPIC}"}) == {"msg": f"Bearer {REDACTED}"}
    assert ANTHROPIC not in str(s.body([ANTHROPIC]))
