"""Build anonymous decision snapshots (no outcomes) for an LLM paper-trading test. Read-only."""
import sqlite3
import json
import random
import numpy as np
import pandas as pd
import sys
OUT, POOL, N, SEED, TAG = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
import glob
USED = set()
for f in glob.glob(OUT + "/llm_key/*.json"):
    try:
        USED |= {k["mkt"] for k in __import__("json").load(open(f))}
    except (OSError, ValueError, KeyError, TypeError): pass  # an unreadable or partial key file: not used
kt = sqlite3.connect("file:data/research/kalshi_trades.db?mode=ro", uri=True)
db = sqlite3.connect("file:data/btcflow.db?mode=ro", uri=True)
m = pd.read_sql("select mkt, open_ts, close_ts, result, floor_strike from markets where result in ('yes','no') order by open_ts", kt)
cd = pd.read_sql("select mkt, end_ts, yes_bid, yes_ask from candles", kt)
ix = pd.read_sql("select ts, price from index_1s order by ts", db); ts, px = ix.ts.values, ix.price.values
def btc(t):
    i = np.searchsorted(ts, t - 1000) - 1
    return float(px[i]) if i >= 0 and t - 1000 - ts[i] <= 10_000 else None
days = sorted(pd.to_datetime(m.open_ts, unit="ms").dt.date.unique())
sel = {"tune": days[:15], "val": days[15:20], "test": days[20:]}[POOL]
m["hold"] = pd.to_datetime(m.open_ts, unit="ms").dt.date.isin(set(sel)) & ~m.mkt.isin(USED)
cdi = {k: g.set_index("end_ts") for k, g in cd.groupby("mkt")}
prev = dict(zip(m.mkt, m.result.shift(1).fillna("").tolist(), strict=True))
outcomes = m.result.tolist(); idx_of = {k: i for i, k in enumerate(m.mkt)}
cands = []
for r in m[m.hold].itertuples():
    t = r.close_ts - 8 * 60_000           # decision at 8 minutes left
    c = cdi.get(r.mkt)
    if c is None or t not in c.index or r.floor_strike is None: continue
    q = c.loc[t]
    if pd.isna(q.yes_bid) or pd.isna(q.yes_ask): continue
    S = btc(t)
    path = [btc(t - k * 60_000) for k in range(15, -1, -1)]
    if S is None or any(p is None for p in path): continue
    rets = np.diff(np.log([btc(t - k * 60_000) or S for k in range(30, -1, -1)]))
    vol = float(np.sqrt(np.mean(rets ** 2)) * S)  # $ per 1-min, last 30 min
    i = idx_of[r.mkt]; last4 = [("UP" if outcomes[j] == "yes" else "DOWN") for j in range(max(0, i - 4), i)]
    cands.append(dict(mkt=r.mkt, close_ts=int(r.close_ts), t=int(t), strike=float(r.floor_strike), S=S, path=path,
                      vol=vol, yes_bid=float(q.yes_bid), yes_ask=float(q.yes_ask), last4=last4, result=r.result))
random.seed(SEED); sample = random.sample(cands, min(N, len(cands)))
key, snaps = [], []
for n, s in enumerate(sample, 1):
    wid = f"{TAG}-{n:03d}"
    rel = [round(p - s["strike"], 1) for p in s["path"]]
    up_ask, down_ask = round(s["yes_ask"], 2), round(1 - s["yes_bid"], 2)
    snaps.append(
        f"{wid} | 8:00 left | strike K=${s['strike']:,.2f} | BTC now ${s['S']:,.2f} ({s['S']-s['strike']:+.1f} vs K) | "
        f"BTC minus K, last 15 one-minute closes (oldest->now): {rel} | typical 1-min move (30m RMS): ${s['vol']:.1f} | "
        f"Kalshi: UP ask {up_ask:.2f}, DOWN ask {down_ask:.2f} | previous 4 windows (oldest->newest): {', '.join(s['last4']) or 'n/a'}")
    key.append(dict(id=wid, mkt=s["mkt"], result=s["result"], up_ask=up_ask, down_ask=down_ask))
open(OUT + f"/llm_trader/snapshots_{TAG}.txt", "w").write(chr(10).join(snaps) + chr(10))
json.dump(key, open(OUT + f"/llm_key/key_{TAG}.json", "w"))
print("candidates", len(cands), "sampled", len(sample)); print(snaps[0]); print(snaps[1])
