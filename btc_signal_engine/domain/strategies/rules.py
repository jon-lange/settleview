"""Strategies A, B, C, E, F and filter D (KTD19; F = weighted evidence score). All are hypotheses with tunable defaults.

Thresholds called "buy share" mean the fraction of BTC volume that was aggressive buying
over the window (0.60 = 60% buying pressure). DOWN is always the mirror of UP.
"""

from __future__ import annotations

from typing import Any

from btc_signal_engine.domain.models.decisions import Vote
from btc_signal_engine.domain.strategies.base import Context, Features, Param, RuleVote, abstain, buy_share, pct

H1, H5, H15 = 60, 300, 900


class Momentum:
    """A - momentum continuation: pressure, price and the latest flow agree, trend not opposed.

    `min_conditions` = k: a side needs at least k of its 4 conditions and more of them than the other side.
    k = 4 is the original all-must-hold rule. Specs stored before RULE_VERSION 2 lack the param and read it as 4.
    """

    name = "momentum"
    RULE_VERSION = "2"
    params = {
        # listed first so the challenger search (which varies the first params of a rule) can tune k
        "min_conditions": Param({H1: 4, H5: 4, H15: 4}, kind=int, choices=(2, 3, 4),
                                help="How many of the 4 conditions must point the same way"),
        "flow_window": Param({H1: 15, H5: 60, H15: 180}, kind=int, choices=(5, 15, 30, 60, 180, 300, 900),
                             help="Seconds of trade flow and price movement to look at"),
        "min_buy_share": Param({H1: 0.60, H5: 0.58, H15: 0.55}, 0.5, 0.95,
                               help="Share of volume that must be aggressive buying (selling for DOWN)"),
        "trend_tf": Param({H1: "5m", H5: "15m", H15: "15m"}, kind=str, choices=("1m", "5m", "15m"),
                          help="Candle timeframe whose trend must not point the other way"),
    }

    def required(self, p: dict[str, Any], horizon: int) -> set[str]:
        w = p["flow_window"]
        return {f"buy_vol_{w}", f"sell_vol_{w}", f"momentum_{w}", "imbalance_15", f"ema_align_{p['trend_tf']}"}

    def evaluate(self, f: Features, p: dict[str, Any], ctx: Context) -> RuleVote:
        w, thr, tf = p["flow_window"], p["min_buy_share"], p["trend_tf"]
        k = p.get("min_conditions", 4)
        share, mom, recent, trend = buy_share(f, w), f[f"momentum_{w}"], f["imbalance_15"], f[f"ema_align_{tf}"]
        if share is None:
            return abstain(f"No trades in the last {w}s.")
        up = (share >= thr, mom > 0, recent > 0, trend != -1)
        down = (1 - share >= thr, mom < 0, recent < 0, trend != 1)
        up_text = (f"buy share {pct(share)} >= {pct(thr)}", f"{w}s momentum up", "last 15s net buying",
                   f"{tf} trend not down")
        down_text = (f"sell share {pct(1 - share)} >= {pct(thr)}", f"{w}s momentum down", "last 15s net selling",
                     f"{tf} trend not up")
        n_up, n_down = sum(up), sum(down)
        if n_up == 4:
            return RuleVote(Vote.UP, f"Buyers drove {pct(share)} of volume over {w}s, price rose, the last 15s "
                            f"kept buying and the {tf} trend is not down.", up_text)
        if n_down == 4:
            return RuleVote(Vote.DOWN, f"Sellers drove {pct(1 - share)} of volume over {w}s, price fell, the last 15s "
                            f"kept selling and the {tf} trend is not up.", down_text)
        if n_up >= k and n_up > n_down:
            met = tuple(t for t, ok in zip(up_text, up, strict=True) if ok)
            return RuleVote(Vote.UP, f"{n_up} of 4 momentum conditions point up (need {k}): {', '.join(met)}.", met)
        if n_down >= k and n_down > n_up:
            met = tuple(t for t, ok in zip(down_text, down, strict=True) if ok)
            return RuleVote(Vote.DOWN, f"{n_down} of 4 momentum conditions point down (need {k}): {', '.join(met)}.",
                            met)
        return abstain(f"Pressure ({pct(share)} buying over {w}s) and price did not line up strongly enough.")

    def checklist(self, f: Features, p: dict[str, Any]) -> list[tuple[str, bool, bool]]:
        """(condition, met for UP, met for DOWN) - powers the live lean (R27)."""
        w, thr, tf = p["flow_window"], p["min_buy_share"], p["trend_tf"]
        share = buy_share(f, w) or 0.5
        mom, recent, trend = f.get(f"momentum_{w}") or 0, f.get("imbalance_15") or 0, f.get(f"ema_align_{tf}") or 0
        return [
            (f"{pct(thr)}+ of {w}s volume on one side (now {pct(share)} buying)", share >= thr, 1 - share >= thr),
            (f"price moving that way over {w}s", mom > 0, mom < 0),
            ("last 15s still pushing that way", recent > 0, recent < 0),
            (f"{tf} trend not against it", trend != -1, trend != 1),
        ]


class TrendAlignment:
    """B — multi-timeframe trend alignment with short momentum agreeing."""

    name = "trend_alignment"
    RULE_VERSION = "1"
    params = {
        "min_votes": Param({H1: 2, H5: 2, H15: 3}, 1, 3, kind=int,
                           help="How many of the 1m/5m/15m trends must agree"),
        "require_15m": Param({H1: False, H5: True, H15: True}, kind=bool,
                             help="The 15m trend must be one of the agreeing votes"),
    }

    def required(self, p: dict[str, Any], horizon: int) -> set[str]:
        return {"ema_votes_up", "ema_votes_down", "momentum_60", "ema_align_15m"}

    def evaluate(self, f: Features, p: dict[str, Any], ctx: Context) -> RuleVote:
        k, need15 = p["min_votes"], p["require_15m"]
        up, down, mom, t15 = f["ema_votes_up"], f["ema_votes_down"], f["momentum_60"], f["ema_align_15m"]
        if up >= k and mom > 0 and (not need15 or t15 == 1):
            return RuleVote(Vote.UP, f"{up} of 3 candle trends point up and the last minute rose.",
                            (f"{up}/3 trends up (need {k})", "60s momentum up") + (("15m trend up",) if need15 else ()))
        if down >= k and mom < 0 and (not need15 or t15 == -1):
            return RuleVote(Vote.DOWN, f"{down} of 3 candle trends point down and the last minute fell.",
                            (f"{down}/3 trends down (need {k})", "60s momentum down")
                            + (("15m trend down",) if need15 else ()))
        return abstain(f"Trends disagree ({up} up, {down} down) or the last minute does not confirm them.")

    def checklist(self, f: Features, p: dict[str, Any]) -> list[tuple[str, bool, bool]]:
        k, need15 = p["min_votes"], p["require_15m"]
        up, down = f.get("ema_votes_up") or 0, f.get("ema_votes_down") or 0
        mom, t15 = f.get("momentum_60") or 0, f.get("ema_align_15m") or 0
        items = [(f"{k}+ of 3 candle trends agree (now {up} up, {down} down)", up >= k, down >= k),
                 ("last minute moving that way", mom > 0, mom < 0)]
        if need15:
            items.append(("15m trend agrees", t15 == 1, t15 == -1))
        return items


class Divergence:
    """C — flow disagrees with price. Recorded for research; always abstains until a reversal rule is validated."""

    name = "divergence"
    RULE_VERSION = "1"
    params = {"min_buy_share": Param({H1: 0.60, H5: 0.60, H15: 0.60}, 0.5, 0.95,
                                     help="Pressure needed before a disagreement with price counts")}

    def required(self, p: dict[str, Any], horizon: int) -> set[str]:
        return {"buy_vol_60", "sell_vol_60", "momentum_60"}

    def evaluate(self, f: Features, p: dict[str, Any], ctx: Context) -> RuleVote:
        share, mom, thr = buy_share(f, 60), f["momentum_60"], p["min_buy_share"]
        if share is not None and ((share >= thr and mom < 0) or (1 - share >= thr and mom > 0)):
            return abstain("Order flow and price disagree; no reversal rule is validated yet, so sitting out.",
                           "divergence detected")
        return abstain("No flow/price divergence.")

    def checklist(self, f: Features, p: dict[str, Any]) -> list[tuple[str, bool, bool]]:
        return []  # research note only; never leans


class Breakout:
    """E — a tight range of completed 1m candles, then a close beyond it on expanded volume."""

    name = "breakout"
    RULE_VERSION = "1"
    params = {
        "range_candles": Param({H1: 10, H5: 20, H15: 45}, kind=int, choices=(10, 20, 45),
                               help="How many 1m candles form the consolidation range"),
        "max_width_pct": Param({H1: 0.05, H5: 0.10, H15: 0.20}, 0.01, 2.0,
                               help="Maximum range width (percent of price) to count as consolidation"),
        "volume_mult": Param({H1: 1.5, H5: 1.5, H15: 1.5}, 1.0, 5.0,
                             help="Breakout candle volume vs the range's average"),
    }

    def required(self, p: dict[str, Any], horizon: int) -> set[str]:
        n = p["range_candles"]
        return {f"range_width_pct_{n}", f"range_high_{n}", f"range_low_{n}", f"range_avg_vol_{n}",
                "last_close_1m", "last_volume_1m"}

    def evaluate(self, f: Features, p: dict[str, Any], ctx: Context) -> RuleVote:
        n, wmax, m = p["range_candles"], p["max_width_pct"], p["volume_mult"]
        width, hi, lo = f[f"range_width_pct_{n}"], f[f"range_high_{n}"], f[f"range_low_{n}"]
        avg_vol, close, vol = f[f"range_avg_vol_{n}"], f["last_close_1m"], f["last_volume_1m"]
        if width > wmax:
            return abstain(f"No consolidation: the last {n} minutes ranged {width:.2f}% (max {wmax:.2f}%).")
        if vol < m * avg_vol:
            return abstain(f"Price is consolidating, but the last minute's volume did not expand {m:g}x.")
        if close > hi:
            return RuleVote(Vote.UP, f"Price broke above a tight {n}-minute range on {vol / avg_vol:.1f}x volume.",
                            (f"{n}m range {width:.2f}% <= {wmax:.2f}%", "closed above range high", f"volume >= {m:g}x"))
        if close < lo:
            return RuleVote(Vote.DOWN, f"Price broke below a tight {n}-minute range on {vol / avg_vol:.1f}x volume.",
                            (f"{n}m range {width:.2f}% <= {wmax:.2f}%", "closed below range low", f"volume >= {m:g}x"))
        return abstain("Volume expanded but price stayed inside the range.")

    def checklist(self, f: Features, p: dict[str, Any]) -> list[tuple[str, bool, bool]]:
        n, wmax, m = p["range_candles"], p["max_width_pct"], p["volume_mult"]
        width = f.get(f"range_width_pct_{n}")
        tight = width is not None and width <= wmax
        avg_vol, vol = f.get(f"range_avg_vol_{n}") or 0, f.get("last_volume_1m") or 0
        close, hi, lo = f.get("last_close_1m"), f.get(f"range_high_{n}"), f.get(f"range_low_{n}")
        expanded = avg_vol > 0 and vol >= m * avg_vol
        return [(f"last {n} minutes in a tight range (<= {wmax:g}%)", tight, tight),
                (f"breakout candle volume >= {m:g}x average", expanded, expanded),
                ("closed beyond the range", close is not None and hi is not None and close > hi,
                 close is not None and lo is not None and close < lo)]


def _clip(x: float) -> float:
    return -1.0 if x < -1.0 else 1.0 if x > 1.0 else x


class Evidence:
    """F - weigh the evidence into one score instead of requiring every condition.

    s = sum_i w_i * clip(x_i, -1, 1). Each x_i is an input scaled so that about the 90th percentile of |x| on real
    BTC-USD data maps to 1 (scales measured 2026-10-28; book inputs from a live capture). UP if s >= threshold,
    DOWN if s <= -threshold, else abstain. Default weights are hypotheses, like every seed rule (KTD19):

      input (feature)                          x_i                                   weight
      trade-flow pressure (imbalance_60)       imbalance_60 / 0.8                    1.0
      price momentum (momentum_60, rv_60)      (momentum_60 / rv_60) / 1.75          1.0
      order-book flow (ofi_30)                 ofi_30 / 20                           1.0 * book_weight
      order-book tilt (microprice_bps)         microprice_bps / half-spread in bps   0.5 * book_weight
                                               (= 2*bid_size/(bid_size+ask_size) - 1)
      buy/sell trade pace (intensity_ratio)    (intensity_ratio - 0.5) / 0.25        0.5
      candle trends (ema_votes_up/down)        (up - down) / 3                       0.5
      price vs starting average (kc_z)         kc_z / 1.0                            1.0 * kc_weight

    momentum_60 / rv_60 is the minute's return in units of its own realized volatility (about N(0,1) under a
    random walk). Book inputs count 0 when the book is unavailable (backfilled history), so the score then rests
    on trades alone; the decision carries the filters' book_unavailable flag.
    """

    name = "evidence"
    RULE_VERSION = "1"
    OFI_SCALE = 20.0  # live capture 2026-10-28: |ofi_30| p90 ~21 (best-level queues are small vs flow)
    params = {
        "threshold": Param({H1: 1.5, H5: 1.5, H15: 1.5}, 0.25, 4.0,
                           help="How strong the combined evidence score must be to call (max about 5.5)"),
        "book_weight": Param({H1: 1.0, H5: 1.0, H15: 1.0}, 0.0, 3.0,
                             help="Multiplier on the order-book inputs (order-book flow and tilt); 0 ignores them"),
        "kc_weight": Param({H1: 1.0, H5: 1.0, H15: 1.0}, 0.0, 3.0,
                           help="Multiplier on how far price already is from the starting average"),
    }

    def required(self, p: dict[str, Any], horizon: int) -> set[str]:
        # book inputs are optional (not backfillable), so they are not required
        return {"imbalance_60", "momentum_60", "rv_60", "intensity_ratio", "ema_votes_up", "ema_votes_down", "kc_z"}

    def contributions(self, f: Features, p: dict[str, Any]) -> list[tuple[str, float | None, float]]:
        """(label, scaled input x in [-1, 1] or None when unavailable, weighted contribution)."""
        def g(name: str) -> float | None:
            return f.get(name)

        items: list[tuple[str, float | None, float]] = []

        def add(label: str, x: float | None, weight: float) -> None:
            x = None if x is None else _clip(x)
            items.append((label, x, 0.0 if x is None else weight * x))

        imb, mom, rv = g("imbalance_60"), g("momentum_60"), g("rv_60")
        add("trade-flow pressure", None if imb is None else imb / 0.8, 1.0)
        add("price momentum", None if mom is None or rv is None else (mom / rv / 1.75 if rv > 0 else 0.0), 1.0)
        ofi = g("ofi_30")
        add("order-book flow", None if ofi is None else ofi / self.OFI_SCALE, p["book_weight"])
        micro, spread, last = g("microprice_bps"), g("spread"), g("last_price")
        half_spread_bps = None if spread is None or not last else spread / 2 / last * 1e4
        add("order-book tilt", None if micro is None or not half_spread_bps else micro / half_spread_bps,
            0.5 * p["book_weight"])
        ir = g("intensity_ratio")
        add("buy/sell trade pace", None if ir is None else (ir - 0.5) / 0.25, 0.5)
        up, down = g("ema_votes_up"), g("ema_votes_down")
        add("candle trends", None if up is None or down is None else (up - down) / 3, 0.5)
        kz = g("kc_z")
        add("price vs the starting average", kz, p["kc_weight"])
        return items

    def score(self, f: Features, p: dict[str, Any]) -> float:
        return sum(c for _, _, c in self.contributions(f, p))

    def evaluate(self, f: Features, p: dict[str, Any], ctx: Context) -> RuleVote:
        items = self.contributions(f, p)
        s, thr = sum(c for _, _, c in items), p["threshold"]
        if -thr < s < thr:
            return abstain(f"The evidence is mixed or weak (score {s:+.2f}; a call needs {thr:g} either way).")
        sign = 1 if s > 0 else -1
        word = "up" if sign > 0 else "down"
        drivers = sorted((it for it in items if it[2] * sign > 0), key=lambda it: -abs(it[2]))
        top = [label for label, _, _ in drivers[:2]]
        verb = "points" if len(top) == 1 else "point"
        sentence = f"Mostly {' and '.join(top)} {verb} {word}."
        conditions = (f"evidence score {s:+.2f} vs threshold {thr:g}",) + tuple(
            f"{label} {c:+.2f}" for label, _, c in items if c != 0)
        return RuleVote(Vote.UP if sign > 0 else Vote.DOWN, sentence, conditions)

    def checklist(self, f: Features, p: dict[str, Any]) -> list[tuple[str, bool, bool]]:
        """Every input with its signed contribution; an input points UP when its contribution is positive."""
        out = []
        for label, x, c in self.contributions(f, p):
            if x is None:
                out.append((f"{label}: no book data" if label.startswith("order-book") else f"{label}: not ready",
                            False, False))
            else:
                out.append((f"{label} ({c:+.2f})", c > 0, c < 0))
        return out


class Filters:
    """D — veto to SKIP when data or conditions make any call unreliable."""

    name = "filters"
    RULE_VERSION = "1"
    params = {
        "max_trade_age_s": Param({H1: 10, H5: 10, H15: 10}, 1, 120, kind=int, help="Stale if no trade for this long"),
        "max_spread": Param({H1: 5.0, H5: 5.0, H15: 5.0}, 0.01, 100.0, help="Skip when the bid/ask spread is wider (USD)"),
        "min_trades_60": Param({H1: 20, H5: 30, H15: 30}, 0, 1000, kind=int, help="Skip when the last minute was this quiet"),
        "skip_high_vol": Param({H1: True, H5: True, H15: True}, kind=bool,
                               help="Skip when 1-minute volatility is above its 7-day 95th percentile"),
    }

    def check(self, f: Features, avail: dict[str, str], required: set[str], p: dict[str, Any],
              ctx: Context) -> tuple[str | None, str, tuple[str, ...]]:
        """(veto reason or None, sentence, flags)."""
        flags: list[str] = []
        warm = [k for k in sorted(required) if avail.get(k) == "warmup"]
        if warm:
            return "warmup", f"Still collecting history ({warm[0]} not ready).", ()
        age = f.get("last_trade_age")
        if age is None or age > p["max_trade_age_s"]:
            return "stale", "No recent trades; data may be stale.", ()
        if "clock_skew" in ctx.flags:
            return "clock_skew", "This computer's clock disagrees with the exchange by over a second.", ()
        if "data_gap" in ctx.flags:
            return "data_gap", "There is a gap in the data this call would rely on.", ()
        trades = f.get("trades_60")
        if trades is not None and trades < p["min_trades_60"]:
            return "thin_trading", f"Only {trades} trades in the last minute; too quiet to call.", ()
        if p["skip_high_vol"] and ctx.rv_p95 is not None and f.get("rv_60") is not None and f["rv_60"] > ctx.rv_p95:
            return "high_volatility", "The last minute was unusually volatile (above its 7-day 95th percentile).", ()
        spread = f.get("spread")
        if spread is None:
            flags.append("book_unavailable")  # spread check not applicable (KTD10)
        elif spread > p["max_spread"]:
            return "wide_spread", f"The bid/ask spread is ${spread:.2f}, wider than ${p['max_spread']:.2f}.", ()
        return None, "", tuple(flags)


SIGNAL_RULES = {r.name: r for r in (Momentum(), TrendAlignment(), Divergence(), Breakout(), Evidence())}
FILTERS = Filters()
