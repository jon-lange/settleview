"""score_tag.py <scratch> <tag> <decisions.jsonl> [<decisions2.jsonl> ...] -> paired-comparable scores on one key."""
import json
import math
import sys
S, tag, files = sys.argv[1], sys.argv[2], sys.argv[3:]
key = {k["id"]: k for k in json.load(open(f"{S}/llm_key/key_{tag}.json"))}
fee = lambda p: math.ceil(0.07 * p * (1 - p) * 1e4 - 1e-9) / 1e4
for f in files:
    dec = {}
    try:
        for l in open(f):
            if l.strip():
                d = json.loads(l); dec[d["id"]] = d
    except FileNotFoundError:
        print(f, "MISSING"); continue
    pnl, bl, bm, cov = [], [], [], 0
    for wid, k in key.items():
        d = dec.get(wid)
        up = 1 if k["result"] == "yes" else 0
        mkt = (k["up_ask"] + 1 - k["down_ask"]) / 2
        bm.append((mkt - up) ** 2)
        p = float(d.get("prob_up", mkt)) if d else mkt
        bl.append((p - up) ** 2)
        if d: cov += 1
        if d and d.get("decision") in ("UP", "DOWN"):
            ask = k["up_ask"] if d["decision"] == "UP" else k["down_ask"]
            paid = min(0.99, round(ask + 0.01, 4)); won = (d["decision"] == "UP") == (up == 1)
            pnl.append((1.0 if won else 0.0) - paid - fee(paid))
    n = len(pnl); mu = sum(pnl) / n if n else 0.0
    sd = (sum((x - mu) ** 2 for x in pnl) / (n - 1)) ** .5 if n > 1 else float("nan")
    t = mu / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan")
    print(json.dumps({"file": f.split("/")[-1], "tag": tag, "covered": cov, "trades": n, "net_c_per_contract": round(100 * mu, 2),
                      "total_dollars_1ct": round(sum(pnl), 2), "t": round(t, 2), "brier": round(sum(bl) / len(bl), 4),
                      "brier_market": round(sum(bm) / len(bm), 4)}))
