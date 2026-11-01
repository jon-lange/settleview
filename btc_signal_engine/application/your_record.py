"""Your record (Ask Settleview, 2026-10-30): the user's own closed Play trades, in the buckets the warnings speak
about. Read only.

Trades: trading_brackets rows of the phone strategies (domain/markets.py phone_strategy) with no session id (an agent
session's or a micro session's brackets are not the user's own), closed, with a fill, in the mode asked for (each
strategy's own env for that mode: Kalshi "prod" and Polymarket "pm_prod" are both production). Net per trade =
proceeds - entry_cost - tp_fees - exit_fees (as TradingService.trades); a win is net > 0.

Buckets (pure, `stats`):
- against_btc: bought the side BTC was against at entry (UP with BTC below the price to beat, DOWN with BTC above it;
  BTC exactly at it counts for neither). BTC and the strike at entry come from the Ask Settleview record of that
  order when there is one (trading_audit "copilot_shown": what the screen showed), else BTC from the recorded index
  (index_1s, the last second at or before the entry, at most INDEX_MAX_AGE_MS old) and the strike from Kalshi's
  market for that close (kalshi_markets.floor_strike; the 15-minute tabs both use it). Polymarket 5-minute windows
  have no recorded strike: a trade with no known BTC or strike is left out of this bucket (counted in `unknown`).
- late: entered with under MIN_LEFT_MS (3 minutes) left (window_end - created_ts).
- band_50_75: paid 50-75c a contract (entry_avg, both ends included).
- early_exits: sold by the user before the close (exit_reason "manual") with a known official result (Kalshi's market
  for that close, 15-minute tabs only): `vs_hold_usd` = what holding each to settlement would have made minus what the
  sale made (positive = the early exits cost money).
Each bucket: {n, wins, net_usd, contracts, net_c (net per contract in cents, None without contracts)}.
"""

from __future__ import annotations

from collections.abc import Iterable

from btc_signal_engine.application.agent_rules import MIN_LEFT_MS
from btc_signal_engine.application.ports import Repository
from btc_signal_engine.domain.markets import by_phone_strategy, phone_markets

BRACKETS, AUDIT, INDEX, KMARKETS = "trading_brackets", "trading_audit", "index_1s", "kalshi_markets"
SHOWN = "copilot_shown"  # the audit event of what Ask Settleview showed with an order
BAND = (0.50, 0.75)
INDEX_MAX_AGE_MS = 10_000
BUCKETS = ("against_btc", "late", "band_50_75")
EPS = 1e-9


def _bucket(trades: list[dict]) -> dict:
    contracts = round(sum(t["filled"] for t in trades), 4)
    net = round(sum(t["net"] for t in trades), 4)
    return {"n": len(trades), "wins": sum(1 for t in trades if t["net"] > 0), "net_usd": net,
            "contracts": contracts, "net_c": round(net / contracts * 100, 2) if contracts > 0 else None}


def against(side: str, gap: float | None) -> bool | None:
    """Was `side` the side BTC was against (gap = BTC - the price to beat)? None when the gap is not known."""
    if gap is None:
        return None
    return (side == "UP" and gap < 0) or (side == "DOWN" and gap > 0)


def stats(trades: Iterable[dict]) -> dict:
    """The buckets of these trades: each {side, filled, entry_avg, left_ms, gap (None = unknown), net, exit,
    won_side (the official winner, None = unknown)}."""
    ts = [t for t in trades if t["filled"] > 0]
    out = {
        "all": _bucket(ts),
        "against_btc": _bucket([t for t in ts if against(t["side"], t["gap"])]),
        "late": _bucket([t for t in ts if t["left_ms"] < MIN_LEFT_MS]),
        "band_50_75": _bucket([t for t in ts if t["entry_avg"] is not None
                               and BAND[0] - EPS <= t["entry_avg"] <= BAND[1] + EPS]),
        "unknown_gap": sum(1 for t in ts if t["gap"] is None),
    }
    early = [t for t in ts if t["exit"] == "manual" and t["won_side"] in ("UP", "DOWN")]
    held = sum((t["filled"] if t["won_side"] == t["side"] else 0.0) - t["cost"] for t in early)
    out["early_exits"] = {**_bucket(early), "vs_hold_usd": round(held - sum(t["net"] for t in early), 4)}
    return out


class YourRecord:
    """Reads the user's closed trades (see the module doc). Closed brackets do not change: each one's features are
    read once and kept; only new closed brackets are read on later calls."""

    def __init__(self, repo: Repository, strategy_modes: dict[str, dict[str, str]]) -> None:
        """`strategy_modes`: phone strategy -> its TradingService.modes ({mode: env})."""
        for s in strategy_modes:
            by_phone_strategy(s)  # an unknown strategy is an error
        self.repo = repo
        self.modes = dict(strategy_modes)
        self._seen: dict[str, dict] = {}  # bracket id -> its trade features

    def envs(self, mode: str) -> list[tuple[str, str]]:
        """(strategy, env) pairs of `mode`; a mode no strategy has is an error."""
        pairs = [(s, m[mode]) for s, m in self.modes.items() if mode in m]
        if not pairs:
            raise ValueError(f"no phone strategy has the mode {mode!r}")
        return pairs

    def trades(self, mode: str) -> list[dict]:
        pairs = set(self.envs(mode))
        rows = self.repo.select(BRACKETS, {"state": "closed"},
                                filters=[("session_id", "is_null", True),
                                         ("strategy", "in", sorted({s for s, _ in pairs}))])
        out = []
        for b in rows:
            if (b.get("strategy"), b.get("env")) not in pairs or not float(b.get("filled") or 0) > 0:
                continue
            t = self._seen.get(b["id"])
            if t is None:
                t = self._seen[b["id"]] = self._features(b)
            out.append(t)
        return out

    def stats(self, mode: str) -> dict:
        return stats(self.trades(mode))

    # -- one closed bracket -------------------------------------------------------------------------------------------
    def _features(self, b: dict) -> dict:
        filled, cost = float(b.get("filled") or 0.0), float(b.get("entry_cost") or 0.0)
        net = round(float(b.get("proceeds") or 0.0) - cost - float(b.get("tp_fees") or 0.0)
                    - float(b.get("exit_fees") or 0.0), 4)
        gap = self._shown_gap(b["id"])
        fifteen = by_phone_strategy(b["strategy"]).span == "15m"
        k = self._kalshi(int(b["window_end"])) if fifteen else None
        if gap is None and k is not None and k.get("floor_strike") is not None:
            btc = self._btc_at(int(b["created_ts"]))
            gap = None if btc is None else round(btc - float(k["floor_strike"]), 2)
        result = (k or {}).get("result")
        return {"id": b["id"], "strategy": b["strategy"], "side": b["side"], "filled": filled, "cost": cost,
                "entry_avg": b.get("entry_avg"), "left_ms": int(b["window_end"]) - int(b["created_ts"]),
                "gap": gap, "net": net, "exit": b.get("exit_reason"),
                "won_side": {"yes": "UP", "no": "DOWN"}.get(result)}

    def _shown_gap(self, bracket_id: str) -> float | None:
        rows = self.repo.select(AUDIT, {"event": SHOWN, "bracket_id": bracket_id}, limit=1)
        g = ((rows[0].get("detail") or {}).get("btc_gap")) if rows else None
        return float(g) if isinstance(g, (int, float)) and not isinstance(g, bool) else None

    def _kalshi(self, close_ts: int) -> dict | None:
        series = next(m.series for m in phone_markets("kalshi"))
        for r in self.repo.select(KMARKETS, {"close_ts": close_ts}):
            if str(r.get("ticker", "")).startswith(series + "-"):
                return r
        return None

    def _btc_at(self, ts: int) -> float | None:
        rows = self.repo.select(INDEX, filters=[("ts", "<=", ts)], order_by=["-ts"], limit=1)
        if not rows or ts - int(rows[0]["ts"]) > INDEX_MAX_AGE_MS:
            return None
        return float(rows[0]["price"])
