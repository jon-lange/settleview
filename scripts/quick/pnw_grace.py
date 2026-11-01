"""PNW exit study 3: whipsaw grace period G(g,h) / re-check R(d) on the live `bstop-10` stop (pre-registered in
docs/research/pnw-tuning.md, "Exit study 3"). Read-only on the databases.

  python scripts/dev.py script scripts/quick/pnw_grace.py train     # both views, append to experiments_results.csv,
                                                                # TRAIN tables + whipsaw ledger, freeze <= 2 picks
  python scripts/dev.py script scripts/quick/pnw_grace.py holdout   # frozen picks + bstop-10 + hold, holdout once

View A (minute marks, all windows) approximates grace by floor(g/60) minute marks; view B (435 full tapes) checks
the per-second index and sells at the tape-proxy bid like experiments.PrintSim.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pnw_exits as P  # noqa: E402

X = P.X
ENTRY = P.ENTRY
FROZEN = X.OUT / "pnw_grace_picks.json"
LIVE = "bstop-10"
GS, HS, DS = (30, 60, 90, 120, 180), (20, 30, 50, None), (10, 20, 30)


def gname(g, h):
    return f"G{g}h{'none' if h is None else h}"


G_RULES = [gname(g, h) for g in GS for h in HS]
R_RULES = [f"R{d}" for d in DS]


def parse(e):
    if e.startswith("G"):
        g, h = e[1:].split("h")
        return "G", int(g), (None if h == "none" else float(h))
    if e.startswith("R"):
        return "R", int(e[1:]), None
    return e, None, None


# ------------------------------------------------------------------------------------------- view A: minute
def sim_min(D, k, side, e, slip):
    idx = np.flatnonzero(k >= 0)
    k, side = k[idx], side[idx]
    i = np.arange(len(idx))
    px = np.where(side > 0, D.YA[idx, k], 1 - D.YB[idx, k])
    paid = np.minimum(0.99, np.round(px + slip, 4))
    cost = paid + X.fee(paid)
    Ball = np.where(side[:, None] > 0, D.YB[idx], 1 - D.YA[idx])
    cols = np.arange(17)[None, :]
    valid = (cols > k[:, None]) & (cols <= X.K_CLOSE)
    won = (side > 0) == (D.y[idx] == 1)
    with np.errstate(invalid="ignore"):
        dist = side[:, None] * (D.btc_minutes()[idx] - D.strike[idx][:, None])
        kind, g, h = parse(e)
        if kind == "hold":
            m = np.zeros(Ball.shape, bool)
        elif kind == LIVE:
            m = dist < -10
        elif kind == "G":
            grace = cols <= k[:, None] + g // 60
            m = np.where(grace, (dist < -h) if h is not None else False, dist < -10)
        else:
            raise ValueError(e)
        st_hit = valid & m & ~np.isnan(Ball)
    js = X._first(st_hit)
    use_s = js >= 0
    sale = np.maximum(0.0, np.round(Ball[i, np.clip(js, 0, 16)] - slip, 4))
    pnl = np.where(use_s, sale - X.fee(np.nan_to_num(sale)) - cost, won.astype(float) - cost)
    return {"idx": idx, "pnl": pnl, "won": won, "reason": np.where(use_s, 4, 0),
            "t_fire": np.where(use_s, js, -1).astype(float)}


# ------------------------------------------------------------------------------------------- view B: tapes
def sim_tape(D, PS, k, side, e, slip, latency_ms=1_000):
    kind, g, h = parse(e)
    res = {"idx": [], "pnl": [], "won": [], "reason": [], "t_fire": []}
    for r in sorted(D.tapes):
        if k[r] < 0:
            continue
        p = PS.path(r, int(k[r]), int(side[r]))
        if p is None:
            continue
        px, sts, spx, close, fill_ts = p
        paid = min(0.99, round(px + slip, 4))
        cost = paid + float(X.fee(paid))
        won = (side[r] > 0) == (D.y[r] == 1)
        t_st, m = np.inf, None
        if kind != "hold":
            secs = np.arange((fill_ts // 1000 + 1) * 1000, close, 1000, dtype=np.int64)
            with np.errstate(invalid="ignore"):
                dist = side[r] * (D.btc(secs) - D.strike[r])
                b10 = dist < -10
                if kind == LIVE:
                    fire = b10
                elif kind == "G":
                    grace = (secs - fill_ts) < g * 1000
                    fire = np.where(grace, (dist < -h) if h is not None else False, b10)
                elif kind == "R":
                    fire = np.zeros_like(b10)
                    fire[g:] = b10[g:] & b10[:-g] if len(b10) > g else fire[g:]
                else:
                    raise ValueError(e)
            bad = np.flatnonzero(fire)
            if len(bad):
                t_st = float(secs[bad[0]])
                m = int(np.searchsorted(sts, t_st + latency_ms, "left"))
                m = m if m < len(sts) else None
        if m is not None:
            sale = max(0.0, round(float(spx[m]) - slip, 4))
            reason, pnl = 4, sale - float(X.fee(sale)) - cost
        else:
            reason, pnl = 0, float(won) - cost
        for key, v in (("idx", r), ("pnl", pnl), ("won", won), ("reason", reason),
                       ("t_fire", t_st if m is not None else -1.0)):
            res[key].append(v)
    return {key: np.asarray(v, dtype=(int if key == "idx" else float if key in ("pnl", "t_fire") else None))
            for key, v in res.items()}


def check(D, PS, k, side):
    for e in ("hold", LIVE):
        for slip in X.SLIPS:
            a, b = sim_min(D, k, side, e, slip), X.sim_minute(D, k, side, X.Exit.parse(e), slip)
            assert (a["idx"] == b["idx"]).all() and np.allclose(a["pnl"], b["pnl"], atol=1e-12) \
                and (a["reason"] == b["reason"]).all(), ("minute", e)
            a, b = sim_tape(D, PS, k, side, e, slip), PS.run(k, side, X.Exit.parse(e), slip)
            assert (a["idx"] == b["idx"]).all() and np.allclose(a["pnl"], b["pnl"], atol=1e-12) \
                and (a["reason"] == b["reason"]).all(), ("tape", e)
    # G with g=30 at minute marks has no grace mark -> must equal bstop-10
    for h in HS:
        a, b = sim_min(D, k, side, gname(30, h), 0.01), sim_min(D, k, side, LIVE, 0.01)
        assert np.allclose(a["pnl"], b["pnl"])
    print("check: minute sim == experiments.sim_minute and tape sim == experiments.PrintSim.run for hold/bstop-10 "
          "(0/1/2c); minute G30* == bstop-10")


# ------------------------------------------------------------------------------------------- reporting
def ledger(o, ref, m):
    """Whipsaw ledger of rule o vs bstop-10 (ref) on rows m (same entries, aligned)."""
    rf, of = ref["reason"][m] == 4, o["reason"][m] == 4
    won = ref["won"][m].astype(bool)
    d = 100 * (o["pnl"][m] - ref["pnl"][m])
    saved = rf & ~of & won
    extra = rf & ~of & ~won
    delayed = rf & of & (o["t_fire"][m] > ref["t_fire"][m])
    return {"wsaw_n": int(saved.sum()), "wsaw_c": float(d[saved].sum()), "xloss_n": int(extra.sum()),
            "xloss_c": float(d[extra].sum()), "delay_n": int(delayed.sum()), "delay_c": float(d[delayed].sum())}


def row(o, ref, m):
    p = o["pnl"][m]
    st = o["reason"][m] == 4
    s = X.summarize(p)
    dc, tc = P.paired(p, ref["pnl"][m])
    return {**s, "avg_loss_c": 100 * float(p[p < 0].mean()), "fired": 100 * float(st.mean()),
            "fired_won": 100 * float(o["won"][m][st].mean()) if st.any() else float("nan"),
            "d_live": dc, "t_live": tc, **ledger(o, ref, m)}


COLS = ["n", "win", "be", "net_c", "t", "avg_loss_c", "worst_c", "fired", "fired_won", "d_live", "t_live",
        "wsaw_n", "wsaw_c", "xloss_n", "xloss_c", "delay_n", "delay_c"]


def setup():
    D = X.load(X.OUT / "kalshi_trades.db", X.ROOT / "data" / "btcflow.db", tapes=True)
    k, side = X.parse_entry(ENTRY)(D)
    return D, X.PrintSim(D), k, side


def rows_for(D, out, e, fam, run_ts, k):
    rows = []
    for split, s in X.split_rows(D, out).items():
        rows.append({"run_ts": run_ts, "config": f"{ENTRY}|{e}" + ("@tape" if fam.endswith("tape") else ""),
                     "family": fam, "entry": ENTRY, "exit": e, "res": "minute", "slip_c": out["slip_c"],
                     "split": split, "tried": int((k >= 0).sum()), **s})
    return rows


def train():
    D, PS, k, side = setup()
    check(D, PS, k, side)
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    rows, outs = [], {"A": {}, "B": {}}
    views = {"A": (sim_min, G_RULES, "pnw_grace_min"),
             "B": (lambda D_, k_, s_, e, sl: sim_tape(D_, PS, k_, s_, e, sl), G_RULES + R_RULES + [LIVE, "hold"],
                   "pnw_grace_tape")}
    for v, (fn, rules, fam) in views.items():
        for e in dict.fromkeys(rules + [LIVE, "hold"]):
            for slip in X.SLIPS:
                out = fn(D, k, side, e, slip)
                out["slip_c"] = int(round(slip * 100))
                if e in rules:
                    rows += rows_for(D, out, e, fam, run_ts, k)
                if slip == X.PRIMARY_SLIP:
                    outs[v][e] = out
    X.append(rows)
    print(f"appended {len(rows)} rows")
    tabs = {}
    for v, O in outs.items():
        m = ~D.hold[O[LIVE]["idx"]]
        T = pd.DataFrame({e: row(o, O[LIVE], m) for e, o in O.items()}).T
        tabs[v] = T
        T.to_csv(X.OUT / f"pnw_grace_train_{v}.csv")
        with pd.option_context("display.width", 300, "display.max_rows", 100):
            print(f"\nTRAIN view {v} ({'minute marks' if v == 'A' else 'second-resolution tapes'}), 1c:")
            print(T[COLS].to_string(float_format=lambda x: f"{x:.2f}"))
    B = tabs["B"].drop(index=[LIVE, "hold"]).sort_values(["net_c", "t"], ascending=False)
    picks = []
    p1 = B.index[0]
    if B.loc[p1, "d_live"] > 0:
        picks.append(p1)
    A = tabs["A"].drop(index=[LIVE, "hold"] + [gname(30, h) for h in HS]).sort_values(["net_c", "t"], ascending=False)
    for e, r in A.iterrows():
        if e in picks:
            continue
        if r.d_live > 0:
            picks.append(e)
        break
    print("\nFROZEN picks (train only):", picks)
    if FROZEN.exists():
        print("(already frozen; not overwriting)")
    else:
        FROZEN.write_text(json.dumps({"frozen_utc": datetime.now(UTC).isoformat(), "picks": picks,
                                      "P1_view": "B", "P2_view": "A"}, indent=1))


def holdout():
    fz = json.loads(FROZEN.read_text())
    D, PS, k, side = setup()
    days = sorted(D.mk.day.unique())
    hd = days[20:]
    h_half = np.where(D.mk.day.isin(set(hd[:5])), 1, np.where(D.mk.day.isin(set(hd[5:])), 2, 0))
    for v, fn in (("A", sim_min), ("B", lambda D_, k_, s_, e, sl: sim_tape(D_, PS, k_, s_, e, sl))):
        run = [e for e in fz["picks"] if not (v == "A" and e.startswith("R"))] + [LIVE, "hold"]
        O = {e: P.chron(D, fn(D, k, side, e, X.PRIMARY_SLIP)) for e in run}
        idx = O[LIVE]["idx"]
        h = D.hold[idx]
        half = D.mk.half.to_numpy()[idx]
        print(f"\nHOLDOUT view {v}:")
        for e, o in O.items():
            r = row(o, O[LIVE], h)
            p = o["pnl"]
            hh = [X.summarize(p[h & (h_half[idx] == q)])["net_c"] for q in (1, 2)]
            f15 = [X.summarize(p[half == q]).get("net_c", float("nan")) for q in (1, 2)]
            dh, th = P.paired(p[h], O["hold"]["pnl"][h])
            print(f" [{'PICK' if e in fz['picks'] else 'REF'}] {e}: n={r['n']} win={r['win']:.1f}% be={r['be']:.1f}% "
                  f"net={r['net_c']:+.2f}c t={r['t']:+.2f} avgL={r['avg_loss_c']:.1f} worst={r['worst_c']:.0f} "
                  f"fired={r['fired']:.0f}% fired&won={r['fired_won']:.0f}% | vs live {r['d_live']:+.2f}c "
                  f"(t {r['t_live']:+.2f}) vs hold {dh:+.2f}c (t {th:+.2f}) | saved {r['wsaw_n']} ({r['wsaw_c']:+.0f}c) "
                  f"extra losses {r['xloss_n']} ({r['xloss_c']:+.0f}c) delayed {r['delay_n']} ({r['delay_c']:+.0f}c) "
                  f"| 5d halves {hh[0]:+.2f}/{hh[1]:+.2f} 15d halves {f15[0]:+.2f}/{f15[1]:+.2f}")


if __name__ == "__main__":
    {"train": train, "holdout": holdout}[sys.argv[1]]()
