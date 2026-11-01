"""Pre-registered daily trend / carry backtest for Kalshi BTC perps (KXBTCPERP). Offline research only.

Written 2026-10-30 BEFORE any result was computed. Source note: scratchpad perps-strategies.md, sections 4.1 and
4.3. Nothing here talks to Kalshi, the live app or any authenticated endpoint. Public market data only.

Data (written as new files under btc-flow-engine/data/research/)
------------------------------------------------------------------
- btc_daily_coinbase.csv: Coinbase Exchange public candles, BTC-USD, granularity 86400 (UTC days), paginated 300 bars.
- perp_funding_history.csv: funding proxy, Deribit BTC-PERPETUAL public `get_funding_rate_history` (hourly
  `interest_1h`; longs pay when positive). Fetch note (after download, before any run): Deribit history starts
  2019-04-30, so the backtest starts 2019-05-01. Kalshi's own funding history is NOT used: it sits on
  the margin host and this task must not contact Kalshi. BitMEX (the only public 8 h history back to 2016) is
  geo-blocked (HTTP 403), so the backtest starts on the first full UTC day with Deribit funding (warm-up from earlier
  candles is allowed for the signal only).
- perp_funding_hyperliquid.csv: Hyperliquid public `info` fundingHistory (May 2023 ->), cross-check only.

The 5 configs (counted; Bonferroni k = 5)
------------------------------------------
Signals use the Coinbase daily close of UTC day t (known at 00:00 UTC of day t+1), the position is held through day
t+1 and earns r_{t+1} = close_{t+1}/close_t - 1. Size: 1 contract-equivalent = constant notional 1 (normalized
returns, % of notional); $ figures on 1 contract (0.0001 BTC) are also shown.
1. TSMOM-7: sign(close_t/close_{t-7} - 1): +1 long, -1 short (0 keeps the prior position). Change only on a flip.
2. TSMOM-28: same with N = 28.
3. DONCH-LF (20/10, long/flat): enter long when close_t > max(high over days t-20..t-1); exit when
   close_t < min(low over days t-10..t-1). Otherwise hold the current state.
4. DONCH-LS (20/10, long/short): DONCH-LF plus the mirror: enter short when close_t < min(low t-20..t-1), cover when
   close_t > max(high t-10..t-1). Exits are evaluated before entries, so a long can exit and go short on one day.
5. CARRY-SHORT-HI: short 1 when the trailing 9-interval (72 h) mean proxy funding, expressed per 8 h, is
   > 0.03 %/8 h at 00:00 UTC; cover when it falls < 0.01 %/8 h; flat otherwise. Signal on the raw proxy rate.
Not run: MR-8H (needs hourly bars; this task is daily-data only). It is not counted.

Costs (fixed, not tuned)
------------------------
- Kalshi taker 0.12 % of notional per fill + 0.05 % slippage per fill = 0.17 % per unit of |position change|
  (a long->short flip is 2 fills).
- Funding: position x sum of the day's hourly proxy funding (longs pay positive, shorts receive), on day t+1.
  PRIMARY = Deribit proxy x 1. Sensitivities (reported, not tuned): S2 = proxy x 2 (the note's "Kalshi ~2x other
  venues"); S3 = constant 18.6 %/yr (Kalshi's 30-day average, 2026-10-30), i.e. 0.186/365 per day, longs pay,
  shorts receive.
- Benchmark B&H: long 1 every day, same funding, one entry fill at the start of each segment.

Split
-----
train: backtest start -> 2023-12-31; holdout: 2024-01-01 -> latest complete day. Halves of the holdout split at the
calendar midpoint. Run once; the holdout is not a tuning set; nothing is changed after seeing results.

Pass bar (holdout, PRIMARY costs)
---------------------------------
P1 net annualized Sharpe (mean/sd x sqrt(365)) > 0.5, AND P2 net return > 0 in both holdout halves, AND
P3 net Sharpe > B&H-minus-funding net Sharpe on the holdout.
Verdicts:
- CANDIDATE: passes P1-P3 AND holdout t > 2.33 (one-sided p < 0.05/5, Bonferroni) AND still passes P1-P3 under S3.
- OPEN: passes P1-P3 but misses the Bonferroni t or fails under S3.
- inconclusive: fewer than 10 holdout entries (too few position changes to judge), whatever the numbers.
- KILLED: fails any of P1-P3 otherwise.
Train numbers are reported for context only (no selection is done on them).

Usage
-----
python scripts/perp_trend_backtest.py fetch     # downloads public data (Coinbase, Deribit, Hyperliquid)
python scripts/perp_trend_backtest.py run       # runs the 5 configs once, prints the report
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, UTC
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data" / "research"
CANDLES = DATA / "btc_daily_coinbase.csv"
FUNDING = DATA / "perp_funding_history.csv"
FUNDING_HL = DATA / "perp_funding_hyperliquid.csv"

FILL_COST = 0.0012 + 0.0005
KALSHI_DRAG_YR = 0.186
TRAIN_END = pd.Timestamp("2023-12-31")
HOLD_START = pd.Timestamp("2024-01-01")
K_CONFIGS = 5
BONF_T = 2.326  # one-sided z for p = 0.01
CONTRACT_BTC = 0.0001


# ----------------------------------------------------------------------------------------------- fetch
def _fetch_candles(client) -> pd.DataFrame:
    rows = []
    start = datetime(2015, 7, 1, tzinfo=UTC)
    now = datetime.now(UTC)
    while start < now:
        end = min(start + timedelta(days=299), now)
        r = client.get(
            "https://api.exchange.coinbase.com/products/BTC-USD/candles",
            params={"granularity": 86400, "start": start.isoformat(), "end": end.isoformat()},
        )
        r.raise_for_status()
        rows.extend(r.json())
        start = end + timedelta(days=1)
        time.sleep(0.35)
    df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
    df = df.drop_duplicates("time").sort_values("time")
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    df = df[df["date"] < today]  # complete days only
    return df[["date", "open", "high", "low", "close", "volume"]]


def _fetch_deribit(client) -> pd.DataFrame:
    rows = []
    end = int(time.time() * 1000)
    floor = int(datetime(2018, 8, 1, tzinfo=UTC).timestamp() * 1000)
    while end > floor:
        start = end - 30 * 24 * 3600 * 1000
        r = client.get(
            "https://www.deribit.com/api/v2/public/get_funding_rate_history",
            params={"instrument_name": "BTC-PERPETUAL", "start_timestamp": start, "end_timestamp": end},
        )
        r.raise_for_status()
        res = r.json().get("result", [])
        if not res:
            break
        rows.extend(res)
        first = min(x["timestamp"] for x in res)
        end = first - 1
        time.sleep(0.25)
    df = pd.DataFrame(rows).drop_duplicates("timestamp").sort_values("timestamp")
    df["time_utc"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df["source"] = "deribit_BTC-PERPETUAL"
    return df[["time_utc", "timestamp", "interest_1h", "interest_8h", "index_price", "source"]]


def _fetch_hyperliquid(client) -> pd.DataFrame:
    rows = []
    start = 0
    while True:
        r = client.post("https://api.hyperliquid.xyz/info", json={"type": "fundingHistory", "coin": "BTC", "startTime": start})
        r.raise_for_status()
        res = r.json()
        if not res:
            break
        rows.extend(res)
        last = max(x["time"] for x in res)
        if last <= start or len(res) < 2:
            break
        start = last + 1
        time.sleep(0.25)
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time")
    df["time_utc"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return df[["time_utc", "time", "fundingRate", "premium"]]


def fetch() -> None:
    import httpx

    DATA.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30, headers={"User-Agent": "settleview-research"}) as c:
        cd = _fetch_candles(c)
        cd.to_csv(CANDLES, index=False)
        print(f"candles {len(cd)} {cd['date'].iloc[0]} -> {cd['date'].iloc[-1]}")
        fd = _fetch_deribit(c)
        fd.to_csv(FUNDING, index=False)
        print(f"deribit funding {len(fd)} {fd['time_utc'].iloc[0]} -> {fd['time_utc'].iloc[-1]}")
        hl = _fetch_hyperliquid(c)
        hl.to_csv(FUNDING_HL, index=False)
        print(f"hyperliquid funding {len(hl)} {hl['time_utc'].iloc[0]} -> {hl['time_utc'].iloc[-1]}")


# ----------------------------------------------------------------------------------------------- signals
def tsmom(close: pd.Series, n: int) -> pd.Series:
    s = np.sign(close / close.shift(n) - 1.0)
    s = s.replace(0, np.nan).ffill().fillna(0)
    return s


def donchian(df: pd.DataFrame, long_short: bool) -> pd.Series:
    c = df["close"].to_numpy()
    hi20 = df["high"].shift(1).rolling(20).max().to_numpy()
    lo10 = df["low"].shift(1).rolling(10).min().to_numpy()
    lo20 = df["low"].shift(1).rolling(20).min().to_numpy()
    hi10 = df["high"].shift(1).rolling(10).max().to_numpy()
    pos = np.zeros(len(c))
    p = 0
    for i in range(len(c)):
        if np.isnan(hi20[i]):
            pos[i] = 0
            continue
        if p == 1 and c[i] < lo10[i] or p == -1 and c[i] > hi10[i]:
            p = 0
        if p == 0:
            if c[i] > hi20[i]:
                p = 1
            elif long_short and c[i] < lo20[i]:
                p = -1
        pos[i] = p
    return pd.Series(pos, index=df.index)


def carry_short(f8_trail: pd.Series) -> pd.Series:
    v = f8_trail.to_numpy()
    pos = np.zeros(len(v))
    p = 0
    for i, x in enumerate(v):
        if np.isnan(x):
            p = 0
        elif p == 0 and x > 0.0003:
            p = -1
        elif p == -1 and x < 0.0001:
            p = 0
        pos[i] = p
    return pd.Series(pos, index=f8_trail.index)


# ----------------------------------------------------------------------------------------------- engine
def load() -> pd.DataFrame:
    df = pd.read_csv(CANDLES, parse_dates=["date"]).set_index("date").sort_index()
    full = pd.date_range(df.index[0], df.index[-1], freq="D")
    missing = full.difference(df.index)
    if len(missing):
        print(f"WARNING: {len(missing)} missing candle days (forward-filled close, zero return): {list(missing[:5])}")
        df = df.reindex(full).ffill()
    fd = pd.read_csv(FUNDING)
    fd["t"] = pd.to_datetime(fd["timestamp"], unit="ms", utc=True).dt.tz_localize(None)
    fd = fd.set_index("t").sort_index()
    # hourly interest_1h is the realized hourly funding; stamp at hour t covers (t-1h, t]
    hourly = fd["interest_1h"].astype(float)
    day_key = (hourly.index - pd.Timedelta(seconds=1)).floor("D")
    daily_f = hourly.groupby(day_key).sum()
    hours = hourly.groupby(day_key).count()
    daily_f = daily_f[hours >= 20]
    df["fund_day"] = daily_f.reindex(df.index)
    # trailing 72 h mean per 8 h, known at 00:00 UTC of day t+1 (i.e. end of day t)
    trail = hourly.rolling(72, min_periods=60).sum() / 9.0
    at_midnight = trail[trail.index.hour == 0]
    at_midnight.index = at_midnight.index.floor("D") - pd.Timedelta(days=1)  # value at end of day t
    df["fund8_trail"] = at_midnight.reindex(df.index)
    df["r_next"] = df["close"].shift(-1) / df["close"] - 1.0
    df["fund_next"] = df["fund_day"].shift(-1)
    df["dclose_next"] = df["close"].shift(-1) - df["close"]
    return df


def evaluate(df: pd.DataFrame, pos: pd.Series, mode: str) -> pd.DataFrame:
    """Daily P&L rows for the decision made at the end of day t (held during t+1)."""
    out = pd.DataFrame(index=df.index)
    out["pos"] = pos
    prev = pos.shift(1).fillna(0)
    out["fills"] = (pos - prev).abs()
    out["gross"] = pos * df["r_next"]
    out["trade_cost"] = out["fills"] * FILL_COST
    if mode == "primary":
        f = df["fund_next"]
    elif mode == "x2":
        f = 2.0 * df["fund_next"]
    elif mode == "kalshi186":
        f = pd.Series(KALSHI_DRAG_YR / 365.0, index=df.index)
    else:
        raise ValueError(f"unknown funding mode {mode!r}")
    out["funding"] = pos * f
    out["net"] = out["gross"] - out["trade_cost"] - out["funding"]
    out["usd_net"] = CONTRACT_BTC * (pos * df["dclose_next"] - (out["fills"] * FILL_COST + pos * f) * df["close"])
    out["entries"] = ((pos != 0) & (pos != prev)).astype(int)
    return out


def segment(ev: pd.DataFrame, a, b, first_fill_from_flat: bool = True) -> pd.DataFrame:
    s = ev.loc[a:b].copy()
    if first_fill_from_flat and len(s):
        # a segment opens from flat: charge the opening fill even if the position was carried in
        i0 = s.index[0]
        s.loc[i0, "fills"] = abs(s.loc[i0, "pos"])
        s.loc[i0, "trade_cost"] = s.loc[i0, "fills"] * FILL_COST
        s.loc[i0, "net"] = s.loc[i0, "gross"] - s.loc[i0, "trade_cost"] - s.loc[i0, "funding"]
        s.loc[i0, "entries"] = int(s.loc[i0, "pos"] != 0)
    return s


def stats(s: pd.DataFrame) -> dict:
    n = len(s)
    net = s["net"]
    sd = net.std(ddof=1)
    sharpe = net.mean() / sd * np.sqrt(365) if sd > 0 else np.nan
    t = net.mean() / (sd / np.sqrt(n)) if sd > 0 else np.nan
    cum = net.cumsum()
    mdd = (cum - cum.cummax()).min()
    tc = s["trade_cost"].sum()
    fc = s["funding"].sum()
    return dict(
        days=n,
        entries=int(s["entries"].sum()),
        fills=float(s["fills"].sum()),
        exposure=float((s["pos"] != 0).mean()),
        gross=float(s["gross"].sum()),
        trade_cost=float(tc),
        funding=float(fc),
        net=float(net.sum()),
        net_yr=float(net.mean() * 365),
        sharpe=float(sharpe),
        t=float(t),
        mdd=float(mdd),
        turnover_share=float(tc / (tc + max(fc, 0.0))) if (tc + max(fc, 0.0)) > 0 else np.nan,
        usd=float(s["usd_net"].sum()),
    )


def run() -> None:
    df = load()
    fstart = df["fund_day"].first_valid_index()
    start = fstart  # first full funding day; signal decided at end of this day uses its own funding next day
    df = df.loc[: df.index[-2]]  # last row has no next-day return
    last = df.index[-1]
    hold_mid = HOLD_START + (last - HOLD_START) / 2
    print(f"Candles {df.index[0].date()} -> {last.date()} (decision days); funding proxy from {fstart.date()}")
    print(f"train {start.date()} -> {TRAIN_END.date()}; holdout {HOLD_START.date()} -> {last.date()}, halves split {hold_mid.date()}")
    nan_f = df.loc[start:, "fund_next"].isna().sum()
    print(f"days in backtest without a full funding day (charged 0): {nan_f}")
    df["fund_next"] = df["fund_next"].fillna(0.0)

    # funding descriptive
    fd = df.loc[start:, "fund_day"].dropna()
    print(f"Deribit proxy mean funding {fd.mean()*365*100:.2f}%/yr (train {fd.loc[:TRAIN_END].mean()*365*100:.2f}, "
          f"holdout {fd.loc[HOLD_START:].mean()*365*100:.2f}); share of days > 0: {(fd > 0).mean():.2f}")
    if FUNDING_HL.exists():
        hl = pd.read_csv(FUNDING_HL)
        hl["t"] = pd.to_datetime(hl["time"], unit="ms")
        hl = hl.set_index("t")["fundingRate"].astype(float)
        yrs = (hl.index[-1] - hl.index[0]).total_seconds() / (365 * 86400)
        print(f"Hyperliquid funding (sum of paid stamps / years) {hl.sum()/yrs*100:.2f}%/yr over {hl.index[0].date()} -> "
              f"{hl.index[-1].date()} (Deribit same window {fd.loc[hl.index[0].floor('D'):].mean()*365*100:.2f}%/yr)")

    configs = {
        "TSMOM-7": tsmom(df["close"], 7),
        "TSMOM-28": tsmom(df["close"], 28),
        "DONCH-LF": donchian(df, long_short=False),
        "DONCH-LS": donchian(df, long_short=True),
        "CARRY-SHORT-HI": carry_short(df["fund8_trail"]),
    }
    bh = pd.Series(1.0, index=df.index)

    def seg_stats(pos, mode):
        ev = evaluate(df, pos, mode)
        tr = stats(segment(ev, start, TRAIN_END))
        ho = stats(segment(ev, HOLD_START, last))
        h1 = stats(segment(ev, HOLD_START, hold_mid))
        h2 = stats(segment(ev, hold_mid + pd.Timedelta(days=1), last))
        return tr, ho, h1, h2

    bh_res = {m: seg_stats(bh, m) for m in ("primary", "x2", "kalshi186")}
    rows = []

    def fmt(name, d):
        return (f"  {name:8s} days {d['days']:5d} entries {d['entries']:4d} exp {d['exposure']*100:5.1f}% "
                f"gross {d['gross']*100:8.1f}% tc {d['trade_cost']*100:6.1f}% fund {d['funding']*100:6.1f}% "
                f"net {d['net']*100:8.1f}% ({d['net_yr']*100:6.1f}%/yr) Sh {d['sharpe']:5.2f} t {d['t']:5.2f} "
                f"MDD {d['mdd']*100:6.1f}% tcShare {d['turnover_share']*100:5.1f}% $1ct {d['usd']:+.2f}")

    print("\nBenchmark B&H long 1 (minus funding):")
    for m, (tr, ho, _h1, _h2) in bh_res.items():
        print(f" [{m}]")
        print(fmt("train", tr)); print(fmt("holdout", ho))

    for name, pos in configs.items():
        res = {m: seg_stats(pos, m) for m in ("primary", "x2", "kalshi186")}
        print(f"\n=== {name} ===")
        for m, (tr, ho, h1, h2) in res.items():
            print(f" [{m}]")
            print(fmt("train", tr)); print(fmt("holdout", ho)); print(fmt("H1", h1)); print(fmt("H2", h2))

        def passes(m, res=res):
            tr, ho, h1, h2 = res[m]
            p1 = ho["sharpe"] > 0.5
            p2 = h1["net"] > 0 and h2["net"] > 0
            p3 = ho["sharpe"] > bh_res[m][1]["sharpe"]
            return p1, p2, p3

        p1, p2, p3 = passes("primary")
        ho = res["primary"][1]
        s3 = all(passes("kalshi186"))
        if ho["entries"] < 10:
            verdict = "inconclusive"
        elif p1 and p2 and p3:
            verdict = "CANDIDATE" if (ho["t"] > BONF_T and s3) else "OPEN"
        else:
            verdict = "KILLED"
        print(f" pass P1 Sharpe>0.5 {p1}  P2 both halves>0 {p2}  P3 beats B&H {p3} (B&H Sh {bh_res['primary'][1]['sharpe']:.2f}); "
              f"Bonferroni t>{BONF_T} {ho['t'] > BONF_T}; passes under 18.6%/yr {s3} -> {verdict}")
        rows.append((name, verdict))
    print("\nVERDICTS:", rows)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        fetch()
    elif cmd == "run":
        run()
    else:
        raise SystemExit("usage: perp_trend_backtest.py fetch|run")
