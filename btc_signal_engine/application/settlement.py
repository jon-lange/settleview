"""Settle windows when they close (U8, KTD6–KTD8).

Each (horizon, boundary) window is one row in `windows`. The engine opens it at the
boundary; this service resolves it from the per-second index once the close minute has
passed. 15m windows are later overridden by Kalshi's published result (KalshiSync).
"""

from __future__ import annotations

from btc_signal_engine.application.ports import MarketStore, Repository
from btc_signal_engine.domain.models.decisions import Call
from btc_signal_engine.domain.settlement.scoring import WINDOW_PENDING, WINDOW_RESOLVED, WINDOW_UNRESOLVED
from btc_signal_engine.domain.settlement.settlement import AVG_WINDOW_MS, actual_direction, settle_window

# How long after close we keep retrying before declaring a window unresolved
# (index rows for the last seconds arrive within the writer's flush interval).
GIVE_UP_MS = 30_000


class SettlementService:
    def __init__(self, market: MarketStore, repo: Repository, tie_policy: str = "kalshi", lag_ms: int = 2000) -> None:
        self._market = market
        self._repo = repo
        self._tie = tie_policy
        self._lag = lag_ms

    def open_window(self, horizon: int, boundary: int, last_price: float | None, now: int) -> None:
        self._repo.insert("windows", [{
            "horizon": horizon, "boundary": boundary, "status": WINDOW_PENDING,
            "last_price": last_price, "tie": False, "updated_ts": now,
        }])

    def settle_due(self, now: int) -> int:
        """Resolve every pending window whose close minute has fully passed. Returns count settled."""
        due = [w for w in self._repo.select("windows", {"status": WINDOW_PENDING})
               if w["boundary"] + w["horizon"] * 1000 + self._lag <= now]
        settled = 0
        for w in due:
            settled += self.settle_window(w, now)
        return settled

    def settle_window(self, w: dict, now: int) -> int:
        h, b = w["horizon"], w["boundary"]
        rows = self._market.index_range(b - AVG_WINDOW_MS, b + h * 1000)
        s = settle_window(h, b, rows)
        if s.resolvable:
            actual, tie = actual_direction(s.open_avg, s.close_avg, self._tie)
            self._repo.update("windows", {"horizon": h, "boundary": b}, {
                "status": WINDOW_RESOLVED, "open_avg": s.open_avg, "close_avg": s.close_avg,
                "actual": actual.value if actual else None, "proxy_actual": actual.value if actual else None,
                "tie": tie, "source": "proxy", "updated_ts": now,
            })
            return 1
        if now >= b + h * 1000 + GIVE_UP_MS:
            self._repo.update("windows", {"horizon": h, "boundary": b}, {
                "status": WINDOW_UNRESOLVED, "open_avg": s.open_avg, "close_avg": s.close_avg, "updated_ts": now,
            })
            return 1
        return 0

    def apply_kalshi_result(self, boundary: int, result: str, ticker: str, strike: float | None, now: int) -> bool:
        """A finalized KXBTC15M result overrides the proxy for the 15m window opening at `boundary` (KTD8)."""
        rows = self._repo.select("windows", {"horizon": 900, "boundary": boundary})
        if not rows:
            return False
        actual = Call.UP if result == "yes" else Call.DOWN
        self._repo.update("windows", {"horizon": 900, "boundary": boundary}, {
            "status": WINDOW_RESOLVED, "actual": actual.value, "source": "kalshi",
            "kalshi_result": result, "kalshi_ticker": ticker, "kalshi_strike": strike, "updated_ts": now,
        })
        return True
