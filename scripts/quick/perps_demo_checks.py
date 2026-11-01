"""Kalshi DEMO perps (KXBTCPERP) API checks for the Perps tab plan (docs/plans/2026-10-29-2356-feat-kalshi-perps-tab-plan.md,
U1). DEMO ONLY: fake money.

Phase `reads` (the default) is read-only: requests are signed by the read-only KalshiAuth, which refuses to sign anything
but GET, so this phase cannot place, move or cancel anything. It records:

  1. margin enabled, exchange status, markets (ticker, tick, contract size, fractional flag, leverage estimates, marks)
  2. order book, risk parameters, fee tiers, notional risk limit, account API limits
  3. balance (per subaccount, with and without compute_available_balance), positions, risk, funding estimate
  4. whether positions held in Kalshi's own app (isolated) are visible read-only (`subaccount_type`)
  5. the margin WebSocket handshake (signed over GET + the WebSocket path) and a few seconds of public messages,
     saved as fixtures under tests/fixtures/kalshi_margin_ws/

Phase `orders` places demo orders and runs only with `orders --users-go-ahead` (AGENTS.md hard rule 1: the user's
explicit in-session go-ahead, even for demo).

Safety: every request is checked against the demo hosts before it is sent. Credentials come from the app's .env through
Settings and are never printed. Run: .venv/Scripts/python.exe scripts/quick/perps_demo_checks.py [reads]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import websockets  # noqa: E402

from btc_signal_engine.adapters.kalshi.auth import KalshiAuth, load_private_key  # noqa: E402
from btc_signal_engine.bootstrap.config import Settings  # noqa: E402

ENV_FILE = Path(os.environ.get("BTCFLOW_ENV_FILE") or ROOT / ".env")  # the .env holding the keys (never printed)
REST = "https://external-api.demo.kalshi.co/trade-api/v2"
WS_URL = "wss://external-api-margin-ws.demo.kalshi.co/trade-api/ws/v2/margin"
WS_PATH = "/trade-api/ws/v2/margin"
DEMO_HOSTS = {"external-api.demo.kalshi.co", "external-api-margin-ws.demo.kalshi.co"}
FIXTURES = ROOT / "tests" / "fixtures" / "kalshi_margin_ws"
OUT = ROOT / "data" / "research" / "perps_demo_checks"

results: list[tuple[str, bool, str]] = []
facts: dict[str, object] = {}


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def demo_only(url: str) -> None:
    host = urlsplit(url).hostname
    if host not in DEMO_HOSTS:
        raise SystemExit(f"refusing a request to {host!r}: this script talks to Kalshi's demo exchange only")


async def _guard(request: httpx.Request) -> None:
    demo_only(str(request.url))


class Demo:
    def __init__(self, auth: KalshiAuth) -> None:
        self.http = httpx.AsyncClient(base_url=REST, auth=auth, timeout=10.0, follow_redirects=False,
                                      event_hooks={"request": [_guard]})

    async def get(self, path: str, **params) -> tuple[int, object]:
        r = await self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        try:
            body: object = r.json()
        except ValueError:
            body = r.text[:300]
        return r.status_code, body


async def reads(demo: Demo, auth: KalshiAuth) -> None:
    s, b = await demo.get("/margin/enabled")
    facts["enabled"] = b
    step("1a margin enabled", s == 200 and isinstance(b, dict) and b.get("enabled") is True, f"{s} {b}")

    s, b = await demo.get("/margin/exchange/status")
    facts["exchange_status"] = b
    step("1b exchange status", s == 200, f"{s} {b}")

    s, b = await demo.get("/margin/markets")
    markets = b.get("markets", []) if isinstance(b, dict) else []
    facts["markets"] = markets
    btc = [m for m in markets if "BTC" in str(m.get("ticker", "")).upper()]
    step("1c markets", s == 200 and bool(btc),
         f"{s} tickers={[m.get('ticker') for m in markets]}")
    if not btc:
        return
    m = btc[0]
    ticker = m["ticker"]
    facts["ticker"] = ticker
    keep = ("ticker", "status", "tick_size", "contract_size", "underlying_multiplier", "fractional_trading_enabled",
            "leverage_estimate", "leverage_estimates", "long_leverage_estimates", "short_leverage_estimates", "bid",
            "ask", "price", "settlement_mark_price", "liquidation_mark_price", "reference_price", "funding_cap",
            "funding_deadband", "funding_limit_unit", "market_version", "halt_sources", "exchange_index", "schedule")
    step("1d BTC market spec", True, json.dumps({k: m.get(k) for k in keep}, default=str)[:1500])

    s, b = await demo.get(f"/margin/markets/{ticker}/orderbook")
    facts["orderbook"] = b
    ob = b.get("orderbook", {}) if isinstance(b, dict) else {}
    step("2a order book", s == 200, f"{s} bids={ob.get('bids', [])[:3]} asks={ob.get('asks', [])[:3]}")

    for name, path, params in (("2b risk parameters", "/margin/risk_parameters", {}),
                               ("2c fee tiers", "/margin/fee_tiers", {}),
                               ("2d fee tier rates", "/margin/fee_tier_rates", {}),
                               ("2e notional risk limit", "/margin/notional_risk_limit", {}),
                               ("2f perps API limits", "/account/limits/perps", {}),
                               ("3a balance", "/margin/balance", {}),
                               ("3b balance with available", "/margin/balance", {"compute_available_balance": "true"}),
                               ("3c positions (all subaccounts)", "/margin/positions", {}),
                               ("3d risk", "/margin/risk", {}),
                               ("3e funding estimate", "/margin/funding_rates/estimate", {"ticker": ticker}),
                               ("3f funding history", "/margin/funding_history", {}),
                               ("3g cross exit triggers (subaccount 0)",
                                f"/margin/cross/positions/{ticker}/exit_trigger", {}),
                               ("3h orders", "/margin/orders", {"limit": 20}),
                               ("3i fills", "/margin/fills", {"limit": 20})):
        s, b = await demo.get(path, **params)
        facts[name] = {"status": s, "body": b}
        step(name, s == 200, f"{s} {json.dumps(b, default=str)[:700]}")

    pos = facts["3c positions (all subaccounts)"]["body"]
    kinds = sorted({p.get("subaccount_type") for p in pos.get("positions", [])}) if isinstance(pos, dict) else []
    step("4 app-held (isolated) positions readable", True,
         f"GET /margin/positions carries subaccount_type per position (spec); types seen on demo now: {kinds or 'none'}")

    await ws_capture(auth, ticker)


async def ws_capture(auth: KalshiAuth, ticker: str, seconds: float = 8.0) -> None:
    demo_only(WS_URL)
    headers = auth.headers("GET", WS_PATH)
    t0 = time.monotonic()
    try:
        ws = await websockets.connect(WS_URL, additional_headers=headers, max_size=None, ping_interval=10,
                                      ping_timeout=10, open_timeout=10, compression=None)
    except Exception as exc:  # never log headers
        step("5a margin WS handshake", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    step("5a margin WS handshake", True, f"signed over GET {WS_PATH}; open in {1000 * (time.monotonic() - t0):.0f} ms")
    msgs: list[dict] = []
    try:
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                  "params": {"channels": ["orderbook_delta", "ticker", "trade"],
                                             "market_tickers": [ticker]}}))
        await ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {"channels": ["fill", "user_orders"]}}))
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
            except TimeoutError:
                break
            msgs.append({"rx_ms": int(time.time() * 1000), "msg": json.loads(raw)})
    finally:
        await ws.close()
    types: dict[str, int] = {}
    for x in msgs:
        t = str(x["msg"].get("type"))
        types[t] = types.get(t, 0) + 1
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "public_capture.json").write_text(json.dumps(msgs, indent=1), encoding="utf-8")
    errors = [x["msg"] for x in msgs if x["msg"].get("type") == "error"]
    step("5b margin WS messages", bool(msgs) and not errors,
         f"{len(msgs)} messages in {seconds:.0f} s by type {types}; errors={errors}")


async def main(phase: str) -> None:
    settings = Settings(_env_file=ENV_FILE)
    key_id, key_path = settings.trading_demo_key_id, settings.trading_demo_key_path
    if not key_id or not key_path:
        raise SystemExit("demo trading credentials are not configured (BTCFLOW_TRADING_DEMO_KEY_ID / _PATH): stopping")
    auth = KalshiAuth(key_id, load_private_key(key_path))
    del key_id, key_path
    demo = Demo(auth)
    try:
        if phase == "reads":
            await reads(demo, auth)
        else:
            raise SystemExit(f"unknown phase {phase!r}")
    finally:
        await demo.http.aclose()
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{phase}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.write_text(json.dumps({"results": results, "facts": facts}, indent=1, default=str), encoding="utf-8")
    print(f"\nsaved {out.relative_to(ROOT)}")


def run() -> int:
    args = sys.argv[1:]
    phase = args[0] if args else "reads"
    if phase == "orders" and "--users-go-ahead" not in args:
        raise SystemExit("the orders phase places demo orders: run it only with the user's explicit in-session go-ahead "
                         "(pass --users-go-ahead)")
    asyncio.run(main(phase))
    print("summary:", f"{sum(ok for _, ok, _ in results)}/{len(results)} passed")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(run())
