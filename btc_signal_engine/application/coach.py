"""Coach evaluation (15m Kalshi window): the numbers behind the Play screens' call. Read only; never places orders.

Each tick (about twice a second, from the decision loop) the service computes, for the current KXBTC15M window:
  - the live Kalshi quote (only when it is for this window and no older than QUOTE_FRESH_MS);
  - our fair odds of UP from the recorded per-second index (domain/coach/fair_value.py) and the model behind them
    (sigma, the settlement average's distribution: the Play screen's "likely close range" cone);
  - the engine's leans: the 15m lean (`engine.lean`) and the 1m / 5m leans as short-term pressure.
PlayService (application/play.py) reads the latest result through `evaluation`.

`series` gives one window's recorded data every 5 s (our odds, Kalshi's recorded quotes, BTC): the Play odds pane.
The live window's points are kept once they are SERIES_STABLE_MS old (their rows and quotes are recorded by then), so
a request computes only the newest ones; the whole series is rebuilt every SERIES_REBUILD_MS (a row that landed late).
A finished window's whole series is cached once its result is known.

`market` needs only `index_range`: the runtime passes application/index_tail.IndexTail (the recent index read
incrementally) so neither the tick nor the series re-reads 31 minutes of rows each time.

Pause (`coach_settings.paused`, persisted): the second half of the Play kill switch. While paused every phone
trading service refuses new entries (TradingService's `paused_fn`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable

from btc_signal_engine.application.ports import MarketStore, Repository
from btc_signal_engine.application.settings_store import put_setting
from btc_signal_engine.domain.coach.fair_value import HISTORY_MS, fair_from_index
from btc_signal_engine.domain.settlement.settlement import window_average

log = logging.getLogger(__name__)

H = 900
H_MS = H * 1000
QUOTE_FRESH_MS = 2_000  # never use an older Kalshi price for this window's evaluation
STALE_TRADE_S = 15
SCORE_LAG_MS = 3_000  # recorded rows may trail the live feed by the writer's flush interval
LABELS = {60: "1m", 300: "5m", 900: "15m"}
SETTINGS = "coach_settings"
SERIES_STEP_MS = 5_000
SERIES_CACHE = 24
SERIES_STABLE_MS = 15_000  # a live point this old is final: its index rows and quotes are recorded
SERIES_REBUILD_MS = 60_000  # the live window's kept points are recomputed this often
QUOTE_STALE_MS = 10_000
PAUSED_SENTENCE = "Paused: new entries are refused on every Play tab until you resume."
RUNNING_SENTENCE = "Running."


class CoachService:
    def __init__(self, repo: Repository, market: MarketStore, horizons: Iterable[int]) -> None:
        self._repo = repo
        self._market = market
        self._horizons = tuple(sorted(horizons))
        self._eval: dict | None = None
        self._open_avgs: dict[int, float] = {}
        self._leans: dict[int, dict] = {}
        self._series: dict[int, dict] = {}
        # the live window's final points: boundary -> (open_avg, ticker, points, built at)
        self._live_points: dict[int, tuple[float | None, str | None, list[dict], int]] = {}
        paused = repo.select(SETTINGS, {"key": "paused"})
        self._paused = bool(paused and (paused[0]["value"] or {}).get("paused"))
        self._paused_since = paused[0]["updated_ts"] if paused else None

    # -- data helpers ---------------------------------------------------------------------
    def _open_avg(self, boundary: int) -> float | None:
        if boundary not in self._open_avgs:
            v = window_average(self._market.index_range(boundary - 60_000, boundary), boundary)
            if v is None:
                return None
            self._open_avgs[boundary] = v
            for b in [b for b in self._open_avgs if b < boundary - 4 * H_MS]:
                del self._open_avgs[b]
        return self._open_avgs[boundary]

    def _rows(self, start: int, end: int) -> list[tuple[int, float]]:
        return [(r.ts, r.price) for r in self._market.index_range(start, end)]

    def _lean(self, h: int) -> dict | None:
        lean = self._leans.get(h)
        if not lean:
            return None
        return {k: lean.get(k) for k in ("call", "side", "strength", "up_met", "down_met", "total", "sentence", "at")}

    # -- live tick ----------------------------------------------------------------------------
    async def tick(self, now: int, state_snapshot_fn: Callable[[int], tuple[dict, dict]], leans: dict[int, dict],
                   kalshi_live: dict | None) -> None:
        """The feature snapshot (the indicator library: CPU work) runs in a worker thread; the evaluation itself stays
        on the loop. `state_snapshot_fn` must not read state the loop keeps writing: the runtime passes the snapshot of
        a copy taken on the loop (FeatureState.copy)."""
        at = now - now % 1000
        try:
            features = (await asyncio.to_thread(state_snapshot_fn, at))[0] if state_snapshot_fn else {}
        except Exception:
            features = {}
        self._leans = dict(leans or {})
        try:
            self._eval = self._evaluate(now, at, features, kalshi_live)
        except Exception:
            log.warning("coach evaluation failed", exc_info=True)

    def _evaluate(self, now: int, at: int, features: dict, kalshi_live: dict | None) -> dict:
        boundary = at - at % H_MS
        end = boundary + H_MS
        ev: dict = {"at": at, "boundary": boundary, "window_end": end, "engine": {"lean": self._lean(H)},
                    "pressure": [{"horizon": h, "label": LABELS.get(h, f"{h // 60}m"),
                                  "lean_call": (self._leans.get(h) or {}).get("call")}
                                 for h in self._horizons if h != H],
                    "market": None, "fair_up": None, "model": None, "code": None, "reason": None}
        q = kalshi_live
        if q and q.get("open_ts") == boundary and q.get("ts") is not None and now - q["ts"] <= QUOTE_FRESH_MS:
            ev["market"] = {k: q.get(k) for k in ("ticker", "ts", "yes_bid", "yes_ask", "no_bid", "no_ask", "last",
                                                  "strike", "open_ts", "close_ts")}
        age = features.get("last_trade_age") if features else None
        open_avg = self._open_avg(boundary)
        f = fair_from_index(at, self._rows(at - HISTORY_MS, at), open_avg, end) if open_avg is not None else None
        if age is not None and age > STALE_TRADE_S:
            ev["code"], ev["reason"] = "stale", f"Price data is stale: no Coinbase trade for {age:.0f} seconds."
        elif open_avg is None:
            ev["code"], ev["reason"] = "warming", ("Waiting for this window's starting price "
                                                   "(the average price over the minute before it opened).")
        elif f is None:
            ev["code"], ev["reason"] = "warming", "Waiting for enough recent per-second prices to measure volatility."
        if f is not None:
            ev["fair_up"] = f["fair_up"]
            # at / window_end / mean_close / sd_close: the settlement average's distribution from the same computation
            # as fair_up (fair_value.close_avg_dist), for the Play screen's "where it's headed" cone.
            ev["model"] = {"price": f["price"], "open_avg": open_avg, "sigma_per_s": round(f["sigma"], 4),
                           "kalshi_strike": ev["market"]["strike"] if ev["market"] else None,
                           "at": f["at"], "window_end": f["window_end"], "mean_close": round(f["mean_close"], 2),
                           "sd_close": round(f["sd_close"], 4)}
        return ev

    @property
    def evaluation(self) -> dict | None:
        """The latest evaluation tick() computed (None before the first); read it, never change it."""
        return self._eval

    # -- pause (half of the Play kill switch) -----------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._paused

    def pause_state(self) -> dict:
        return {"paused": self._paused, "since": self._paused_since,
                "sentence": PAUSED_SENTENCE if self._paused else RUNNING_SENTENCE}

    def set_paused(self, paused: bool, now: int) -> dict:
        """Persisted, so it survives restarts."""
        self._paused = bool(paused)
        self._paused_since = now
        put_setting(self._repo, {"key": "paused"}, {"paused": self._paused}, now, table=SETTINGS)
        return self.pause_state()

    # -- per-window chart series ------------------------------------------------------------------------
    def series(self, boundary: int, now: int) -> dict:
        """Recorded data for one 15m window every 5 s: our odds, Kalshi Up/Down bid+ask, BTC price; the strike. Only
        rows recorded before `now` are used; nothing is filled in or invented."""
        end = boundary + H_MS
        cached = self._series.get(boundary)
        if cached is not None:
            return {**cached, "now": now}
        upto = min(end, now)
        rows = self._market.index_range(boundary - max(HISTORY_MS, 60_000), max(upto, boundary))
        pairs = [(r.ts, r.price) for r in rows]
        open_avg = window_average([r for r in rows if boundary - 60_000 <= r.ts < boundary], boundary)
        quotes = self._repo.select("kalshi_quotes", filters=[("ts", ">=", boundary), ("ts", "<", upto)],
                                   order_by=["ts"]) if upto > boundary else []
        counts: dict[str, int] = {}
        for q in quotes:
            counts[q["ticker"]] = counts.get(q["ticker"], 0) + 1
        mrows = self._repo.select("kalshi_markets", {"open_ts": boundary})
        ticker = mrows[0]["ticker"] if mrows else (max(counts, key=counts.get) if counts else None)
        quotes = [q for q in quotes if q["ticker"] == ticker]
        kept = self._live_points.get(boundary)
        if kept is not None and kept[0] == open_avg and kept[1] == ticker and now - kept[3] < SERIES_REBUILD_MS:
            points = [p for p in kept[2] if p["ts"] <= now - SERIES_STABLE_MS]
            built = kept[3]
        else:
            points, built = [], now
        t_from = points[-1]["ts"] + SERIES_STEP_MS if points else boundary
        qi, ri, last_q = 0, 0, None
        for t in range(t_from, upto, SERIES_STEP_MS):
            while qi < len(quotes) and quotes[qi]["ts"] <= t:
                last_q = quotes[qi]
                qi += 1
            while ri < len(pairs) and pairs[ri][0] <= t:
                ri += 1
            price = pairs[ri - 1][1] if ri and pairs[ri - 1][0] >= t - 5_000 else None
            f = fair_from_index(t, pairs, open_avg, end) if open_avg is not None else None
            q = last_q if last_q is not None and t - last_q["ts"] <= QUOTE_STALE_MS else None
            points.append({"ts": t, "fair_up": round(f["fair_up"], 4) if f else None, "price": price,
                           "yes_bid": q["yes_bid"] if q else None, "yes_ask": q["yes_ask"] if q else None,
                           "no_bid": q["no_bid"] if q else None, "no_ask": q["no_ask"] if q else None})
        result = None
        if mrows and mrows[0].get("result") in ("yes", "no"):
            result = "UP" if mrows[0]["result"] == "yes" else "DOWN"
        live = self._eval["market"] if self._eval and self._eval.get("boundary") == boundary else None
        kalshi_strike = mrows[0].get("floor_strike") if mrows else None
        if kalshi_strike is None and live:
            kalshi_strike = live.get("strike")
        out = {"horizon": H, "boundary": boundary, "window_end": end, "now": now, "ticker": ticker,
               "strike": open_avg, "kalshi_strike": kalshi_strike, "result": result, "step_ms": SERIES_STEP_MS,
               "points": points,
               "coverage": {"points": len(points), "with_odds": sum(1 for p in points if p["fair_up"] is not None),
                            "with_quotes": sum(1 for p in points if p["yes_ask"] is not None),
                            "quotes": len(quotes)},
               "note": "Recorded data only: our odds from the per-second Coinbase index, Kalshi prices as this app "
                       "recorded them (gaps stay gaps). Strike = our open average (Kalshi's own strike shown "
                       "separately when known)."}
        if end + SCORE_LAG_MS < now and result is not None:
            if len(self._series) >= SERIES_CACHE:
                self._series.pop(next(iter(self._series)))
            self._series[boundary] = out
            self._live_points.pop(boundary, None)
        elif upto > boundary:
            self._live_points = {b: v for b, v in list(self._live_points.items()) if b >= boundary - 2 * H_MS}
            self._live_points[boundary] = (open_avg, ticker, [p for p in points if p["ts"] <= now - SERIES_STABLE_MS],
                                           built)
        return out
