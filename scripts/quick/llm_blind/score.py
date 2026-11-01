"""Score LLM paper-trading decisions against Kalshi's official results. Entry at seen ask + 1c, exact fee, hold."""
import json
import math
import sys
D = sys.argv[1]
key = {k["id"]: k for k in json.load(open(D + "/llm_key/answer_key.json"))}
dec = []
for f in ("decisions_A.jsonl", "decisions_B.jsonl"):
    try:
        dec += [json.loads(l) for l in open(D + "/llm_trader/" + f) if l.strip()]
    except FileNotFoundError:
        print("missing", f)
fee = lambda p: math.ceil(0.07 * p * (1 - p) * 1e4 - 1e-9) / 1e4
rows, briers_llm, briers_mkt = [], [], []
for d in dec:
    k = key.get(d["id"])
    if not k: continue
    up = 1 if k["result"] == "yes" else 0
    p = float(d.get("prob_up", 0.5)); mkt = (k["up_ask"] + (1 - k["down_ask"])) / 2
    briers_llm.append((p - up) ** 2); briers_mkt.append((mkt - up) ** 2)
    if d["decision"] in ("UP", "DOWN"):
        ask = k["up_ask"] if d["decision"] == "UP" else k["down_ask"]
        paid = min(0.99, round(ask + 0.01, 4)); won = (d["decision"] == "UP") == (up == 1)
        rows.append(((1.0 if won else 0.0) - paid - fee(paid), won, paid))
n = len(rows); print(f"decisions: {len(dec)} | trades: {n} | skips: {len(dec)-n}")
if n:
    pnl = [r[0] for r in rows]; mu = sum(pnl) / n; sd = (sum((x - mu) ** 2 for x in pnl) / (n - 1)) ** .5 if n > 1 else float('nan')
    be = sum(r[2] + fee(r[2]) for r in rows) / n
    print(f"win {100*sum(r[1] for r in rows)/n:.1f}% vs break-even {100*be:.1f}% | net {100*mu:+.2f}c/contract | total ${sum(pnl):+.2f} per 1 contract each | t {mu/(sd/math.sqrt(n)):+.2f}")
print(f"Brier (all {len(briers_llm)} windows): LLM {sum(briers_llm)/len(briers_llm):.4f} vs market mid {sum(briers_mkt)/len(briers_mkt):.4f} (lower is better)")
