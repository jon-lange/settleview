"""Kalshi PRODUCTION perps (KXBTCPERP) checks for the Perps tab plan (U1 facts on real money, chosen by the user on
2026-10-30 because the demo account is not margin-enabled). REAL MONEY: at most $4.50 moved into one numbered perps
subaccount, at most 2 contracts (~$16 notional) open, flattened and swept back at the end.

Phase `reads` (the default) is read-only: signed by the read-only KalshiAuth (GET only).

Phase `orders` runs ONLY with `orders --users-go-ahead` and either `--subaccount N` (an existing numbered subaccount
with no position) or `--create-subaccount` (creates one, permanently). Steps:

  O1  create the subaccount (if asked)                 O9  whole-position bracket trigger, both legs
  O2  transfer $3.00 primary -> N, timed; replay the   O10 edit only the stop (both legs re-sent): updated_time
      same client_transfer_id (idempotent?)            O11 transfer $1.50 more, add 1 contract: trigger covers 2?
  O3  private WebSocket capture (fill, user_orders)    O12 reduce 1 (reduce-only IOC)
  O4  IOC buy 1 at 0.5% through the ask (banding?)     O13 tight bracket around the mark: which mark fires it,
  O5  duplicate client_order_id on a far reduce-only        what order the fired leg sends
      IOC (cannot fill): the second answer's code      O14 flat; fills (order_source, fees, realized P&L)
  O6  /margin/risk liquidation price, positions        O15 sweep the subaccount's equity back to primary
  O7  trigger anchored to the finished IOC (409?)
  O8  stop past the liquidation price (400?)

Guards in this script (before anything is signed): host external-api.kalshi.com / the margin WebSocket only; ticker
KXBTCPERP only; subaccounts 0 and N only; transfers total <= $4.50; position never above 2 contracts; prices within 2%
of the mark; every order immediate-or-cancel; no cancel-all, no isolated paths. Credentials come from the app's .env
through Settings and are never printed. User ids are redacted from saved WebSocket captures.
Run: .venv/Scripts/python.exe scripts/quick/perps_prod_checks.py [reads | orders --users-go-ahead --subaccount N]
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import time
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import websockets  # noqa: E402

from btc_signal_engine.adapters.kalshi.auth import KalshiAuth, load_private_key, sign  # noqa: E402
from btc_signal_engine.bootstrap.config import Settings  # noqa: E402

ENV_FILE = Path(os.environ.get("BTCFLOW_ENV_FILE") or ROOT / ".env")  # the .env holding the keys (never printed)
REST = "https://external-api.kalshi.com/trade-api/v2"
PREFIX = "/trade-api/v2"
WS_URL = "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin"
WS_PATH = "/trade-api/ws/v2/margin"
HOSTS = {"external-api.kalshi.com", "external-api-margin-ws.kalshi.com"}
TICKER = "KXBTCPERP"
TICK = Decimal("0.0001")
MAX_TRANSFER_CENTS = 450
MAX_CONTRACTS = 2
BAND = Decimal("0.005")
FIXTURES = ROOT / "tests" / "fixtures" / "kalshi_margin_ws"
OUT = ROOT / "data" / "research" / "perps_prod_checks"

results: list[tuple[str, bool, str]] = []
facts: dict[str, object] = {}
ws_msgs: list[dict] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    facts[name] = detail
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def prod_only(url: str) -> None:
    host = urlsplit(url).hostname
    if host not in HOSTS:
        raise SystemExit(f"refusing a request to {host!r}: this script talks to Kalshi's perps hosts only")


def px(d: Decimal) -> str:
    return str(d.quantize(TICK))


def floor_tick(x: Decimal) -> Decimal:
    return (x / TICK).to_integral_value(ROUND_FLOOR) * TICK


def ceil_tick(x: Decimal) -> Decimal:
    return (x / TICK).to_integral_value(ROUND_CEILING) * TICK


class Writes:
    """The only signer for writes in this script: an explicit allow-list of (method, path) for one subaccount."""

    ALLOWED = (("POST", re.compile(r"^/margin/orders$")),
               ("PUT", re.compile(rf"^/margin/cross/positions/{TICKER}/exit_trigger$")),
               ("DELETE", re.compile(rf"^/margin/cross/positions/{TICKER}/exit_trigger$")),
               ("POST", re.compile(r"^/portfolio/margin/subaccounts/transfer$")),
               ("POST", re.compile(r"^/portfolio/margin/subaccounts$")))

    def __init__(self, key_id: str, key, sub: int | None) -> None:
        self._key_id, self._key, self.sub = key_id, key, sub
        self.moved_cents = 0
        self.position = 0

    def headers(self, method: str, path: str) -> dict[str, str]:
        if not any(m == method and rx.match(path) for m, rx in self.ALLOWED):
            raise SystemExit(f"refusing to sign {method} {path}: not on this script's allow-list")
        ts = str(int(time.time() * 1000))
        return {"KALSHI-ACCESS-KEY": self._key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self._key, ts + method + PREFIX + path)}


class Prod:
    def __init__(self, auth: KalshiAuth, writes: Writes | None) -> None:
        async def guard(request: httpx.Request) -> None:
            prod_only(str(request.url))
        self.http = httpx.AsyncClient(base_url=REST, timeout=10.0, follow_redirects=False,
                                      event_hooks={"request": [guard]})
        self.auth, self.w = auth, writes

    async def get(self, path: str, **params) -> tuple[int, object]:
        r = await self.http.get(path, params={k: v for k, v in params.items() if v is not None}, auth=self.auth)
        return r.status_code, _body(r)

    async def write(self, method: str, path: str, body: dict | None = None, **params) -> tuple[int, object, float]:
        assert self.w is not None
        headers = self.w.headers(method, path)
        t0 = time.perf_counter()
        r = await self.http.request(method, path, json=body, headers=headers,
                                    params={k: v for k, v in params.items() if v is not None})
        return r.status_code, _body(r), round(1000 * (time.perf_counter() - t0), 1)

    # -- guarded writes ---------------------------------------------------------------------------------------------
    async def transfer(self, frm: int, to: int, cents: int, transfer_id: str | None = None):
        assert self.w is not None and {frm, to} == {0, self.w.sub} and cents > 0
        if frm == 0:
            if self.w.moved_cents + cents > MAX_TRANSFER_CENTS and transfer_id is None:
                raise SystemExit(f"refusing: transfers into the subaccount would exceed {MAX_TRANSFER_CENTS} cents")
            if transfer_id is None:
                self.w.moved_cents += cents
        body = {"client_transfer_id": transfer_id or str(uuid.uuid4()), "from_subaccount": frm, "to_subaccount": to,
                "amount_cents": cents}
        s, b, ms = await self.write("POST", "/portfolio/margin/subaccounts/transfer", body)
        return s, b, ms, body["client_transfer_id"]

    async def order(self, side: str, price: Decimal, count: int, *, reduce_only: bool, coid: str | None = None,
                    mark: Decimal):
        assert self.w is not None
        if abs(price - mark) / mark > Decimal("0.02"):
            raise SystemExit(f"refusing: price {price} is more than 2% from the mark {mark}")
        grows = (side == "bid") and not reduce_only
        if grows and self.w.position + count > MAX_CONTRACTS:
            raise SystemExit(f"refusing: the position would exceed {MAX_CONTRACTS} contracts")
        body = {"ticker": TICKER, "client_order_id": coid or f"svperp-{uuid.uuid4().hex[:16]}", "side": side,
                "count": f"{count}.00", "price": px(price), "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross", "reduce_only": reduce_only, "subaccount": self.w.sub}
        s, b, ms = await self.write("POST", "/margin/orders", body)
        return s, b, ms, body

    async def trigger(self, method: str, **body):
        assert self.w is not None
        return await self.write(method, f"/margin/cross/positions/{TICKER}/exit_trigger",
                                body or None, subaccount=self.w.sub)


def _body(r: httpx.Response) -> object:
    try:
        return r.json()
    except ValueError:
        return r.text[:300]


async def market(p: Prod) -> dict:
    s, b = await p.get(f"/margin/markets/{TICKER}")
    if s != 200:
        raise SystemExit(f"market read failed: {s} {b}")
    return b.get("market", b)


async def book(p: Prod) -> tuple[Decimal, Decimal, Decimal]:
    """(best bid, best ask, liquidation mark) from the order book and the market's mark."""
    s, b = await p.get(f"/margin/markets/{TICKER}/orderbook")
    ob = b["orderbook"]
    bid = max(Decimal(x[0]) for x in ob["bids"])
    ask = min(Decimal(x[0]) for x in ob["asks"])
    m = await market(p)
    return bid, ask, Decimal(m["liquidation_mark_price"]["price"])


async def sub_balance(p: Prod, sub: int) -> dict | None:
    s, b = await p.get("/margin/balance")
    for x in b.get("subaccount_balances", []) if isinstance(b, dict) else []:
        if x.get("subaccount") == sub:
            return x
    return None


async def sub_position(p: Prod, sub: int) -> Decimal:
    s, b = await p.get("/margin/positions", subaccount=sub, ticker=TICKER)
    return sum((Decimal(x["position"]) for x in b.get("positions", [])), Decimal(0))


async def reads(p: Prod) -> None:
    for name, path, params in (("R1 margin enabled", "/margin/enabled", {}),
                               ("R2 exchange status", "/margin/exchange/status", {}),
                               ("R3 balance", "/margin/balance", {}),
                               ("R4 positions (all subaccounts)", "/margin/positions", {}),
                               ("R5 risk", "/margin/risk", {}),
                               ("R6 fee tiers (BTC)", "/margin/fee_tiers", {}),
                               ("R7 notional risk limit", "/margin/notional_risk_limit", {}),
                               ("R8 perps API limits", "/account/limits/perps", {}),
                               ("R9 cross exit triggers (primary)", f"/margin/cross/positions/{TICKER}/exit_trigger", {}),
                               ("R10 orders", "/margin/orders", {"limit": 20}),
                               ("R11 funding estimate", "/margin/funding_rates/estimate", {"ticker": TICKER})):
        s, b = await p.get(path, **params)
        if name.startswith("R6") and isinstance(b, dict):
            b = {k: {TICKER: v.get(TICKER)} for k, v in b.items() if isinstance(v, dict)}
        step(name, s == 200, f"{s} {json.dumps(b, default=str)[:900]}")


async def ws_private(auth_headers: dict[str, str], stop: asyncio.Event) -> None:
    prod_only(WS_URL)
    try:
        ws = await websockets.connect(WS_URL, additional_headers=auth_headers, max_size=None, ping_interval=10,
                                      ping_timeout=10, open_timeout=10, compression=None)
    except Exception as exc:  # never log headers
        ws_msgs.append({"rx_ms": int(time.time() * 1000), "msg": {"type": "connect_error", "err": type(exc).__name__}})
        return
    try:
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["fill", "user_orders"]}}))
        await ws.send(json.dumps({"id": 2, "cmd": "subscribe",
                                  "params": {"channels": ["ticker"], "market_tickers": [TICKER]}}))
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except TimeoutError:
                continue
            ws_msgs.append({"rx_ms": int(time.time() * 1000), "msg": json.loads(raw)})
    finally:
        await ws.close()


def redact(o):
    if isinstance(o, dict):
        return {k: ("redacted" if k in ("user_id", "member_id") else redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(v) for v in o]
    return o


async def orders(p: Prod, ws_headers: dict[str, str], create: bool) -> None:
    w = p.w
    assert w is not None
    start_ms = int(time.time() * 1000)
    bal0 = await sub_balance(p, 0)
    step("O0 primary perps balance before", bal0 is not None, json.dumps(bal0))

    if create:
        s, b, ms = await p.write("POST", "/portfolio/margin/subaccounts", {})  # Kalshi wants a JSON body
        ok = s == 201 and isinstance(b, dict) and isinstance(b.get("subaccount_number"), int)
        step("O1 create subaccount", ok, f"{s} {b} in {ms} ms")
        if not ok:
            return
        w.sub = b["subaccount_number"]
    if not (isinstance(w.sub, int) and 0 <= w.sub <= 63):
        raise SystemExit("no subaccount to use")
    primary = w.sub == 0  # KD4 as changed 2026-10-30: trade from the primary perps account, no transfers
    if await sub_position(p, w.sub) != 0:
        raise SystemExit(f"subaccount {w.sub} already holds a position: stopping")
    subbal = await sub_balance(p, w.sub)
    step("O1b subaccount before", True, json.dumps(subbal))

    stop = asyncio.Event()
    ws_task = asyncio.create_task(ws_private(ws_headers, stop))
    try:
        # O2 transfer, timed, then replay the same id (numbered subaccount only)
        if primary:
            step("O2 transfers", True, "skipped: trading from the primary perps account")
        else:
            s, b, ms, tid = await p.transfer(0, w.sub, 300)
            after = await sub_balance(p, w.sub)
            step("O2a transfer $3.00 primary -> sub", s == 200,
                 f"{s} {b} in {ms} ms; sub right after: {json.dumps(after)}")
            if s != 200:
                return
            s2, b2, ms2, _ = await p.transfer(0, w.sub, 300, transfer_id=tid)
            after2 = await sub_balance(p, w.sub)
            grew = after2 and after and Decimal(after2["account_equity"]) > Decimal(after["account_equity"])
            step("O2b replay same client_transfer_id", not grew,
                 f"{s2} {b2} in {ms2} ms; sub after replay: {json.dumps(after2)}; moved twice={bool(grew)}")
            if grew:  # not idempotent: send the duplicate straight back
                await p.transfer(w.sub, 0, 300)

        # O4 IOC buy 1 at 0.5% through the ask
        bid, ask, mark = await book(p)
        cap = floor_tick(ask * (1 + BAND))
        s, b, ms, body = await p.order("bid", cap, 1, reduce_only=False, mark=mark)
        filled = Decimal(b.get("fill_count", "0")) if isinstance(b, dict) else Decimal(0)
        w.position += int(filled)
        entry_id = b.get("order_id") if isinstance(b, dict) else None
        step("O4 IOC buy 1 at ask+0.5%", s == 201 and filled == 1,
             f"book {bid}/{ask} mark {mark}; sent at {body['price']}; {s} {b} in {ms} ms")
        if filled != 1:
            return

        # O5 duplicate client_order_id on an order that cannot fill (reduce-only sell above the ask)
        bid, ask, mark = await book(p)
        far = ceil_tick(ask * Decimal("1.01"))
        s1, b1, ms1, body = await p.order("ask", far, 1, reduce_only=True, mark=mark)
        s2, b2, ms2, _ = await p.order("ask", far, 1, reduce_only=True, coid=body["client_order_id"], mark=mark)
        step("O5 duplicate client_order_id", True,
             f"first {s1} {b1} ({ms1} ms); second same coid {s2} {b2} ({ms2} ms); coid length {len(body['client_order_id'])}")

        # O6 risk and positions
        s, risk = await p.get("/margin/risk")
        mine = [x for x in risk.get("positions", []) if x.get("subaccount") == w.sub] if isinstance(risk, dict) else []
        s, pos = await p.get("/margin/positions", subaccount=w.sub)
        subbal = await sub_balance(p, w.sub)
        liq = Decimal(mine[0]["estimated_liquidation_price"]) if mine and mine[0].get(
            "estimated_liquidation_price") else None
        step("O6 risk for 1 contract on $3", bool(mine),
             f"risk={json.dumps(mine)}; positions={json.dumps(pos)}; balance={json.dumps(subbal)}")

        # O7 trigger anchored to the finished IOC
        s, b, ms = await p.trigger("PUT", anchor_order_id=entry_id, stop_loss_price=px(floor_tick(mark * Decimal("0.9"))),
                                   client_trigger_id=f"svanchor-{uuid.uuid4().hex[:12]}")
        step("O7 trigger anchored to a finished IOC", s != 200, f"{s} {b} ({ms} ms)")
        if s == 200:
            await p.trigger("DELETE")

        # O8 stop past the liquidation price
        if liq is not None:
            s, b, ms = await p.trigger("PUT", stop_loss_price=px(floor_tick(liq * Decimal("0.99"))))
            step("O8 stop below the liquidation price (long)", s != 200, f"liq {liq}; {s} {b} ({ms} ms)")
            if s == 200:
                await p.trigger("DELETE")
        else:
            step("O8 stop below the liquidation price (long)", False, "no liquidation price from /margin/risk")

        # O9 whole-position bracket, both legs
        bid, ask, mark = await book(p)
        sl, tp = floor_tick(mark * Decimal("0.97")), ceil_tick(mark * Decimal("1.03"))
        s, b, ms = await p.trigger("PUT", stop_loss_price=px(sl), take_profit_price=px(tp))
        s_g, got = await p.get(f"/margin/cross/positions/{TICKER}/exit_trigger", subaccount=w.sub)
        step("O9 whole-position bracket", s == 200, f"set sl {sl} tp {tp}: {s} {b} ({ms} ms); get: {json.dumps(got)}")
        upd0 = b.get("updated_time") if isinstance(b, dict) else None

        # O10 edit only the stop, re-sending the take-profit
        sl2 = floor_tick(mark * Decimal("0.96"))
        s, b, ms = await p.trigger("PUT", stop_loss_price=px(sl2), take_profit_price=px(tp))
        step("O10 edit stop, re-send tp", s == 200 and isinstance(b, dict) and b.get("take_profit_price") is not None,
             f"{s} {b} ({ms} ms); updated_time {upd0} -> {b.get('updated_time') if isinstance(b, dict) else None}")

        # O11 add 1 with $1.50 more
        if not primary:
            s, b, ms, _ = await p.transfer(0, w.sub, 150)
            step("O11a transfer $1.50 more", s == 200, f"{s} {b} ({ms} ms)")
        bid, ask, mark = await book(p)
        s, b, ms, body = await p.order("bid", floor_tick(ask * (1 + BAND)), 1, reduce_only=False, mark=mark)
        filled = Decimal(b.get("fill_count", "0")) if isinstance(b, dict) else Decimal(0)
        w.position += int(filled)
        s_g, got = await p.get(f"/margin/cross/positions/{TICKER}/exit_trigger", subaccount=w.sub)
        step("O11b add 1; whole trigger still covers the position", filled == 1,
             f"{s} {b} ({ms} ms); position now {await sub_position(p, w.sub)}; trigger: {json.dumps(got)}")

        # O12 reduce 1
        bid, ask, mark = await book(p)
        s, b, ms, body = await p.order("ask", ceil_tick(bid * (1 - BAND)), 1, reduce_only=True, mark=mark)
        filled = Decimal(b.get("fill_count", "0")) if isinstance(b, dict) else Decimal(0)
        w.position -= int(filled)
        step("O12 reduce 1 (reduce-only IOC at bid-0.5%)", filled == 1,
             f"book {bid}/{ask}; {s} {b} ({ms} ms); position now {await sub_position(p, w.sub)}")

        # O13 tight bracket around the mark: let a leg fire
        bid, ask, mark = await book(p)
        sl3, tp3 = floor_tick(mark - 2 * TICK), ceil_tick(mark + 2 * TICK)
        s, b, ms = await p.trigger("PUT", stop_loss_price=px(sl3), take_profit_price=px(tp3))
        set_ms = int(time.time() * 1000)
        fired = None
        for _ in range(180):
            await asyncio.sleep(1.0)
            if await sub_position(p, w.sub) == 0:
                fired = int(time.time() * 1000)
                break
        s_g, got = await p.get(f"/margin/cross/positions/{TICKER}/exit_trigger", subaccount=w.sub)
        step("O13 tight bracket fires", fired is not None,
             f"mark {mark} sl {sl3} tp {tp3}; set {s} ({ms} ms); flat after {None if fired is None else fired - set_ms} ms;"
             f" trigger after: {json.dumps(got)}")
        if fired is not None:
            w.position = 0
    finally:
        await asyncio.sleep(1.5)
        stop.set()
        await ws_task
        # flatten and sweep whatever is left
        left = await sub_position(p, w.sub) if w.sub is not None else Decimal(0)
        if left > 0:
            await p.trigger("DELETE")
            bid, ask, mark = await book(p)
            await p.order("ask", ceil_tick(bid * Decimal("0.99")), int(left), reduce_only=True, mark=mark)
            left = await sub_position(p, w.sub)
        s, fills = await p.get("/margin/fills", subaccount=w.sub, ticker=TICKER, min_ts=start_ms // 1000, limit=50)
        step("O14 flat; fills", left == 0, f"position {left}; fills {json.dumps(fills)[:2500]}")
        if left == 0 and w.sub and not primary:
            sb = await sub_balance(p, w.sub)
            cents = math.floor(Decimal(sb["account_equity"]) * 100) if sb else 0
            if cents > 0:
                s, b, ms, _ = await p.transfer(w.sub, 0, cents)
                step("O15 sweep back to primary", s == 200, f"{cents} cents: {s} {b} ({ms} ms); "
                                                            f"sub now {json.dumps(await sub_balance(p, w.sub))}")
        bal1 = await sub_balance(p, 0)
        step("O16 primary perps balance after", True, f"before {json.dumps(bal0)} after {json.dumps(bal1)}")
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / "prod_private_capture.json").write_text(json.dumps(redact(ws_msgs), indent=1), encoding="utf-8")


async def main(args: list[str]) -> None:
    phase = args[0] if args else "reads"
    settings = Settings(_env_file=ENV_FILE)
    key_id, key_path = settings.trading_prod_key_id, settings.trading_prod_key_path
    if not key_id or not key_path:
        raise SystemExit("production trading credentials are not configured: stopping")
    key = load_private_key(key_path)
    auth = KalshiAuth(key_id, key)
    sub = int(args[args.index("--subaccount") + 1]) if "--subaccount" in args else None
    writes = Writes(key_id, key, sub) if phase == "orders" else None
    del key_id, key_path
    p = Prod(auth, writes)
    try:
        if phase == "reads":
            await reads(p)
        elif phase == "orders":
            await orders(p, auth.headers("GET", WS_PATH), create="--create-subaccount" in args)
        else:
            raise SystemExit(f"unknown phase {phase!r}")
    finally:
        await p.http.aclose()
        OUT.mkdir(parents=True, exist_ok=True)
        out = OUT / f"{phase}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
        out.write_text(json.dumps({"results": results, "facts": facts}, indent=1, default=str), encoding="utf-8")
        print(f"\nsaved {out.relative_to(ROOT)}")


def run() -> int:
    args = sys.argv[1:]
    if args and args[0] == "orders":
        if "--users-go-ahead" not in args:
            raise SystemExit("the orders phase uses REAL money: run it only with the user's explicit in-session go-ahead "
                             "(pass --users-go-ahead)")
        if ("--subaccount" in args) == ("--create-subaccount" in args):
            raise SystemExit("pass exactly one of --subaccount N or --create-subaccount")
    asyncio.run(main(args))
    print("summary:", f"{sum(ok for _, ok, _ in results)}/{len(results)} passed")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(run())
