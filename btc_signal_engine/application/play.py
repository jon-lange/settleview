"""PlayService: the read model behind the phone "Play" screens (the user's own calls), per venue.

Venues: "kalshi" (strategy "you") and "polymarket" (strategy "you_poly"), each with window spans (the screen's tabs):
Kalshi 15m; Polymarket 15m and 5m (strategy "you_poly5"). A span other than 15m is its own PlayVenue under the key
"<venue>_<span>" with its own window length, strike (Polymarket's priceToBeat) and fair odds. The tabs come from
the market table (domain/markets.py). Read only. It assembles the Coach's latest
evaluation (window, Kalshi strike, our model's fair_up, 1m/5m/15m pressure), the venue's live quote, the live BTC index,
the last-minute crowd summary and the venue service's live brackets into one payload, using the pure helpers in
domain/strategy/play.py. Orders never go through here: the phone uses the existing /api/trading/prepare, /approve and
/prepare_exit routes with the venue's strategy.

Quotes: Kalshi's is the Coach's live quote (stale after QUOTE_MAX_AGE_MS -> no call, no BUY). Polymarket's comes from
the Polymarket US WebSocket cache only (None when not servable; never the REST BBO). That cache marks each quote
`servable` by its own rule (a quiet book up to 30 s old on a live, trade-covered connection; adapters/polymarket_us/
market_data.py), and a quote carrying that mark is fresh by the cache's verdict, not by QUOTE_MAX_AGE_MS: Play, the
agents' rules (which read Play's `fresh`) and the order path (which re-reads the cache) agree on one notion.
Polymarket quotes are in YES terms; DOWN is the short side
(no_bid = 1 - yes_ask, no_ask = 1 - yes_bid). Both venues settle on CF Benchmarks' BRTI against the same strike, so
the stop math uses Kalshi's strike for the window on both.

Stake limits (2026-10-29, user-set, default none): the venue service's live limits (TradingService.limits(): the
phone's PlayLimits setting, read now) and, in production, its cash backstop (TradingService.cash_cap: the venue's
fresh cash). `stake.max` = the per-trade limit or None (no limit); `stake.cap` = the lower of that and the cash (None
= neither applies; the box is then bounded only by STAKE_CEILING, the API's own highest stake); `stake.sentence`
says it in words ("No per-trade limit · up to your $90.09 cash").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from btc_signal_engine.application.trading import TradingService
from btc_signal_engine.domain.markets import phone_markets, venue as venue_spec
from btc_signal_engine.domain.strategy import play as P

WINDOW_MS = 900_000
QUOTE_MAX_AGE_MS = 5_000
INDEX_MAX_AGE_MS = 10_000
BALANCE_MAX_AGE_MS = 300_000  # an older account reading is shown as "balance unavailable", never as a number
# The tabs come from the market table's phone markets (domain/markets.py), in its order.
VENUES = tuple(dict.fromkeys(m.venue for m in phone_markets()))  # ("kalshi", "polymarket")
SPANS = {v: tuple(m.span for m in phone_markets(v)) for v in VENUES}  # the tabs each venue offers, in display order
FEE_ROUNDING = {v: venue_spec(v).fee_rounding for v in VENUES}  # how each venue rounds a fee (the stake box's maths)
VENUE_NAMES = {v: venue_spec(v).name for v in VENUES}
_TAB_KEYS = {(m.venue, m.span): m.play_key for m in phone_markets()}  # (venue, span) -> "kalshi" | "polymarket_5m" ...


def venue_key(venue: str, span: str) -> str | None:
    """The Play tab's venue key for (venue, span), None when the table has no such tab (the caller says so)."""
    return _TAB_KEYS.get((venue, span))
STAKE_CEILING = 10_000.0  # the trading routes' highest stake (ProposalBody.stake, /api/play/now ?stake)
SERIES_POINTS = 300  # btc_series: per-second index thinned to at most this many points (every 3rd second at most)


@dataclass
class PlayVenue:
    svc: TradingService | None  # None: this venue is not available on this server
    quote_fn: Callable[[int], dict | None] = lambda now: None  # {ticker, yes_bid, yes_ask, [no_*], close_ts, age_ms}
    crowd_fn: Callable[[], dict | None] = lambda: None
    yes_only: bool = False  # derive the DOWN side from the YES book (Polymarket)
    missing: str = "This venue is not available on this server."
    # The account's last reading from the app's existing balance loops (no extra venue calls):
    # {"cash": $ | None, "positions": $ | None (None = not known), "at": read at ms | None}
    balance_fn: Callable[[], dict | None] = lambda: None
    window_ms: int = WINDOW_MS
    # A span with its own strike / odds (not the Coach's 15-minute window): strike_fn(start) -> the window's price to
    # beat; fair_fn(now, start, end, strike) -> fair_value.fair_from_index's result (fair_up, sigma, at, window_end,
    # mean_close, sd_close) or None. None = the Coach's evaluation (Kalshi strike, our fair_up, its model).
    strike_fn: Callable[[int], float | None] | None = None
    fair_fn: Callable[[int, int, int, float], dict | None] | None = None


class PlayService:
    def __init__(self, venues: dict[str, PlayVenue], *, evaluation_fn: Callable[[], dict | None],
                 index_fn: Callable[[], tuple[int, float] | None] = lambda: None,
                 index_series_fn: Callable[[int, int], list[tuple[int, float]]] = lambda start, end: []) -> None:
        self.venues = venues
        self.index_series_fn = index_series_fn
        self.evaluation_fn = evaluation_fn
        self.index_fn = index_fn
        # Per-second reuse (every phone and tab polling within one whole second shares one read):
        # window start -> (second, index series); (tab key, window start, strike) -> (second, fair_fn result).
        self._series_cache: dict[int, tuple[int, list[tuple[int, float]]]] = {}
        self._fair_cache: dict[tuple, tuple[int, dict | None]] = {}

    # -- helpers ------------------------------------------------------------------------------------------------
    def _btc(self, now: int, ev: dict) -> tuple[float | None, str | None]:
        try:
            got = self.index_fn()
        except Exception:
            got = None
        if got and got[1] is not None and now - int(got[0]) <= INDEX_MAX_AGE_MS:
            return float(got[1]), "live"
        model = ev.get("model") or {}
        return (float(model["price"]), "model") if model.get("price") is not None else (None, None)

    def _series(self, boundary: int, now: int) -> tuple[list[list], dict]:
        """The live "price to beat" chart: per-second BTC index since the window opened (at most SERIES_POINTS) and
        its reference levels (running high / low, open) from the full series."""
        sec = now // 1000
        hit = self._series_cache.get(boundary)
        if hit is not None and hit[0] == sec:
            pts = hit[1]
        else:
            try:
                pts = self.index_series_fn(boundary, now + 1)
                self._series_cache = {k: x for k, x in self._series_cache.items() if x[0] == sec}
                self._series_cache[boundary] = (sec, pts)
            except Exception:
                pts = []
        return P.thin_series(pts, SERIES_POINTS), P.window_levels(pts)

    def _fair(self, key: str, v: PlayVenue, now: int, boundary: int, end: int, strike: float | None) -> dict | None:
        """The span's own fair odds (v.fair_fn), read at most once per whole second per window and strike."""
        if not v.fair_fn or strike is None:
            return None
        sec, ck = now // 1000, (key, boundary, strike)
        hit = self._fair_cache.get(ck)
        if hit is not None and hit[0] == sec:
            return hit[1]
        try:
            model = v.fair_fn(now, boundary, end, strike)
        except Exception:
            return None
        self._fair_cache = {k: x for k, x in self._fair_cache.items() if x[0] == sec}
        self._fair_cache[ck] = (sec, model)
        return model

    @staticmethod
    def _mode(svc: TradingService, now: int) -> dict:
        r = svc.mode_ready(svc.mode, now)
        return {"mode": svc.mode, "env": svc.modes[svc.mode], "label": svc.mode_labels[svc.mode],
                "real_money": svc.mode == "prod", "ready": r.ok, "reason": r.reason or None, "killed": svc.killed(),
                "present": svc.present(now), "venue": svc.venue}

    @staticmethod
    def _fee_fn(svc: TradingService) -> Callable[[float, float], float]:
        return lambda c, p: svc._fee(c, p, svc.config.fee_rate)

    def _quote(self, v: PlayVenue, now: int, end: int) -> tuple[dict | None, bool]:
        try:
            q = v.quote_fn(now)
        except Exception:
            q = None
        if not q:
            return None, False
        q = dict(q)
        if v.yes_only:
            q["no_bid"], q["no_ask"] = P.short_side(q.get("yes_bid"), q.get("yes_ask"))
        age = q.get("age_ms")
        if "servable" in q:  # the venue cache judged it (Polymarket: a quiet book can be current; module docstring)
            fresh = q["servable"] is True and age is not None and int(age) >= 0 and q.get("close_ts") == end
        else:
            fresh = age is not None and 0 <= int(age) <= QUOTE_MAX_AGE_MS and q.get("close_ts") == end
        return q, fresh

    def _position(self, v: PlayVenue, b: dict, btc: float | None, quote: dict | None,
                  fair_up: float | None = None) -> dict:
        svc = v.svc
        view = svc.bracket_view(b)
        side = view["side"]
        bid = view.get("bid")
        if bid is None and quote and quote.get("ticker") == view["ticker"] and view["env"] != "demo":
            bid = quote.get("yes_bid" if side == "UP" else "no_bid")  # same real book (shadow / production)
        stop = view.get("stop_index_below") if side == "UP" else view.get("stop_index_above")
        held, filled = float(view.get("held") or 0.0), float(view.get("filled") or 0.0)
        cost = (round(float(view["entry_cost"]) * held / filled, 4)
                if view.get("entry_cost") is not None and filled > 0 and held > 0 else None)
        advice = P.trade_advice(side, bid=bid, btc=btc, stop_level=stop, held=held, cost=cost,
                                fee_fn=self._fee_fn(svc))
        # The held side's own book from the screen's live quote (same real book; never for a demo bracket).
        same = bool(quote) and quote.get("ticker") == view["ticker"] and view["env"] != "demo"
        bk, ak = ("yes_bid", "yes_ask") if side == "UP" else ("no_bid", "no_ask")
        outlook = P.outlook(side, bid=quote.get(bk) if same else None, ask=quote.get(ak) if same else None,
                            fresh=same and bool(quote.get("fresh")), fair_up=fair_up, held=held,
                            sell_now=advice["sell_now"])
        return {**view, "stop_level": stop, "advice": advice, "outlook": outlook}

    def _balance(self, v: PlayVenue | None, now: int) -> dict:
        try:
            b = v.balance_fn() if v is not None else None
        except Exception:
            b = None
        b = b or {}
        cash, pos, at = b.get("cash"), b.get("positions"), b.get("at")
        age = None if at is None else max(0, now - int(at))
        fresh = cash is not None and age is not None and age <= BALANCE_MAX_AGE_MS
        return {"cash": cash if fresh else None, "positions": pos if fresh else None, "age_ms": age, "fresh": fresh,
                "total": round(cash + (pos or 0.0), 2) if fresh else None}

    def balances(self, now: int) -> dict:
        """Each venue's cash (and positions value when the venue reports it) and the age of the reading; a reading
        older than BALANCE_MAX_AGE_MS or missing is not fresh and carries no numbers. `both` = the two totals added,
        only when both are fresh."""
        out: dict = {k: self._balance(self.venues.get(k), now) for k in VENUES}
        out["both"] = (round(sum(out[k]["total"] for k in VENUES), 2)
                       if all(out[k]["fresh"] for k in VENUES) else None)
        return out

    @staticmethod
    def stake_caps(svc: TradingService, now: int) -> dict:
        """What bounds one trade's stake on this service now: its per-trade limit (None = no limit), the production
        cash (None = not applicable or unknown), the lower of the two (`cap`) and a sentence for the screen."""
        lim, lr = svc.limits()
        env = svc.modes[svc.mode]
        applies = svc.cash_backstop and env == svc.modes.get("prod")
        cash, cr = svc.cash_cap(env, now)
        per = (lim or {}).get("per_trade")
        caps = [x for x in (per, cash) if x is not None]
        cap = round(min(caps), 2) if caps else None
        if not lr.ok:
            sentence = lr.reason
        else:
            first = "No per-trade limit" if per is None else f"Up to ${per:,.2f} a trade (your limit)"
            if not applies:
                second = f"{svc.mode}: no real money moves"
            elif not cr.ok:
                second = "balance unknown: can't check the order fits your cash"
            else:
                second = f"up to your ${cash:,.2f} cash"
            sentence = f"{first} · {second}"
        return {"per_trade": per, "per_day": (lim or {}).get("per_day"), "cash": cash, "cash_applies": applies,
                "cap": cap, "limits_error": None if lr.ok else lr.reason, "cash_error": None if cr.ok else cr.reason,
                "sentence": sentence}

    def balance_caps(self, now: int) -> dict:
        """Each available Play tab's cash backstop ({tab key: {mode, applies, cash, error, sentence}}): what
        GET /api/play/limits shows next to the limits."""
        out = {}
        for k, x in self.venues.items():
            if x.svc is None:
                continue
            c = self.stake_caps(x.svc, now)
            out[k] = {"strategy": x.svc.strategy, "mode": x.svc.mode, "venue": x.svc.venue,
                      "applies": c["cash_applies"], "cash": c["cash"], "error": c["cash_error"],
                      "sentence": c["sentence"]}
        return out

    # -- /api/play/now ------------------------------------------------------------------------------------------
    def _spans(self) -> dict:
        return {k: [s for s in spans if (x := self.venues.get(venue_key(k, s))) is not None and x.svc is not None]
                for k, spans in SPANS.items()}

    def _open_elsewhere(self, key: str | None) -> dict:
        """Live positions on the venue/span tabs not being viewed ({key: count}), so none is ever out of sight."""
        out = {}
        for k, x in self.venues.items():
            if k == key or x.svc is None:
                continue
            try:
                n = len(x.svc.live_brackets())
            except Exception:
                n = 0
            if n:
                out[k] = n
        return out

    def now_view(self, now: int, venue: str, *, span: str, stake: float | None = None) -> dict:
        key = venue_key(venue, span)
        v = self.venues.get(key)
        ev = self.evaluation_fn() or {}
        if v is not None and v.strike_fn is not None:  # its own window (e.g. Polymarket 5 min)
            boundary = now - now % v.window_ms
            end = boundary + v.window_ms
            try:
                strike = v.strike_fn(boundary)
            except Exception:
                strike = None
        else:
            boundary = ev.get("boundary") or now - now % WINDOW_MS
            end = ev.get("window_end") or boundary + WINDOW_MS
            km = ev.get("market") or {}
            strike = km.get("strike") if km.get("close_ts") == end else None
        btc, btc_source = self._btc(now, ev)
        cutoff = v.svc.min_left_ms() if v is not None and v.svc is not None else P.WARN_LEFT_MS
        series, levels = self._series(boundary, now)
        base = {"now": now, "server_now_ms": now, "venue": venue, "span": span, "spans": self._spans(),
                "open_elsewhere": self._open_elsewhere(key),
                "venues": {k: bool(x.svc) for k, x in self.venues.items() if k in VENUES},
                "window": {"start": boundary, "end": end, "seconds_left": max(0, (end - now) // 1000), "ticker": None,
                           "entry_cutoff_ms": cutoff, "entry_open": P.entry_open(end - now, cutoff),
                           "phase": P.clock_phase(end - now, cutoff), "settle_op": P.SETTLE_UP_OP},
                "btc": {**P.btc_gap(btc, strike), "source": btc_source}, "balances": self.balances(now),
                "btc_series": series, "btc_levels": levels}
        if v is None or v.svc is None:
            return {**base, "strategy": None, "available": False, "ready": False,
                    "reason": v.missing if v else f"{venue!r} has no {span} windows here.", "market": None, "call": None,
                    "signals": [], "entry": None, "exit": None, "stake": None, "limits": None, "mode": None,
                    "positions": [], "cone": None}
        svc = v.svc
        q, fresh = self._quote(v, now, end)
        market = None
        if q:
            market = {k: q.get(k) for k in ("ticker", "yes_bid", "yes_ask", "no_bid", "no_ask", "close_ts")}
            market.update(mid_up=P.mid(q.get("yes_bid"), q.get("yes_ask")), age_ms=q.get("age_ms"), fresh=fresh,
                          strike=strike)
        mid_up = market["mid_up"] if market and fresh else None
        fair_up, model = ev.get("fair_up"), ev.get("model")
        if v.strike_fn is not None:
            model = self._fair(key, v, now, boundary, end, strike)
            fair_up = model.get("fair_up") if model else None
        cone_model = model if (model and model.get("window_end") == end and model.get("at") is not None
                               and 0 <= now - int(model["at"]) <= INDEX_MAX_AGE_MS) else None
        cone = P.cone(now=now, end=end, btc=btc, strike=strike, model=cone_model)
        call = P.verdict(mid_up)
        call.update(fair_up=fair_up, model_lean=P.lean(fair_up))
        call["window_end"] = end
        signals = [{"key": "market", "name": "Market mid", "lean": call["lean"],
                    "value": None if mid_up is None else f"UP {mid_up * 100:.0f}%"},
                   {"key": "model", "name": "Our model", "lean": call["model_lean"],
                    "value": None if fair_up is None else f"UP {fair_up * 100:.0f}%"}]
        for p in ev.get("pressure") or []:
            signals.append({"key": f"pressure_{p.get('horizon')}", "name": f"{p.get('label')} pressure",
                            "lean": P.call_lean(p.get("lean_call")), "value": p.get("lean_call")})
        # 15m pressure: the engine's own 15-minute lean (same rule tally as the 1m / 5m ones), so the strip reads
        # 1m · 5m · 15m.
        lean15 = ((ev.get("engine") or {}).get("lean") or {}).get("call")
        signals.append({"key": "pressure_900", "name": "15m pressure", "lean": P.call_lean(lean15), "value": lean15})
        try:
            crowd = v.crowd_fn()
        except Exception:
            crowd = None
        if crowd and market and crowd.get("ticker") != market.get("ticker"):
            crowd = None
        signals.append({"key": "crowd", "name": "Crowd, last minute", "lean": P.crowd_lean((crowd or {}).get("lean")),
                        "value": (crowd or {}).get("label"), "note": (crowd or {}).get("note"),
                        "up_usd": (crowd or {}).get("up_usd"), "down_usd": (crowd or {}).get("down_usd")})
        caps = self.stake_caps(svc, now)
        cap = caps["cap"] if caps["cap"] is not None else STAKE_CEILING
        stake = P.stake_default(cap) if stake is None else max(0.0, min(float(stake), cap))
        fee = self._fee_fn(svc)
        sides = {}
        for side, (bk, ak) in {"UP": ("yes_bid", "yes_ask"), "DOWN": ("no_bid", "no_ask")}.items():
            bid, ask = (market.get(bk), market.get(ak)) if market and fresh else (None, None)
            plan = P.ride_plan(side, strike, ask, btc=btc)
            # No automatic stop on the user's own trades (2026-10-29: default stops closed two trades within 20 s on
            # ordinary noise). The level stays as a suggestion for the optional stop the user can set after entry.
            plan = {**plan, "suggested_stop": plan["stop_level"], "stop_index_below": None, "stop_index_above": None}
            n = P.contracts_for(stake, plan["max_price"], fee, whole=svc.config.whole_contracts,
                                fee_rate=svc.config.fee_rate)
            payoff = P.plan_payoff(n, plan["max_price"], fee)
            sides[side] = {"bid": bid, "ask": ask, "ideal_entry": P.ideal_entry(bid, ask), **plan,
                           "stop_distance": P.stop_distance(side, btc, plan["stop_level"]),
                           "contracts": n, "max_loss": None if payoff["if_loses"] is None else -payoff["if_loses"],
                           **payoff,
                           # a MARKET buy of this stake (price_mode "market"): sized at the ask, up to 99c; its
                           # worst case and whether it fits the limits. None without a fresh ask.
                           "market": svc.market_terms(stake, ask, now)}
        positions = [self._position(v, b, btc, market, fair_up) for b in svc.live_brackets()]
        positions.sort(key=lambda p: p.get("created_ts") or 0, reverse=True)
        for p in positions:  # the entry ring on the chart: BTC at the fill time
            at = [x for x in series if x[0] <= int(p.get("created_ts") or 0)]
            p["entry_btc"] = at[-1][1] if at else None
        if not base["window"]["entry_open"]:
            reason = "Too late to enter this window."
        elif caps["limits_error"] or caps["cash_error"]:
            reason = caps["limits_error"] or caps["cash_error"]
        elif market and fresh and strike is not None:
            reason = None
        elif not market or not fresh:
            reason = (f"No live {VENUE_NAMES[venue]} price right now: BUY is off "
                      "until a fresh quote arrives.")
        else:
            reason = "Waiting for this window's price to beat."
        base["window"]["ticker"] = market.get("ticker") if market else None
        call["agree"] = P.agreement([s["lean"] for s in signals], call["lean"])
        return {**base, "strategy": svc.strategy, "available": True, "market": market, "call": call,
                "signals": signals, "entry": {**sides, "note": P.ENTRY_NOTE},
                "exit": {"rule": "ride to settlement", "take_profit": 0.99, "stop_gap": P.RIDE.stop_gap,
                         "near_stop_usd": P.NEAR_STOP_USD,  # CONSIDER EXIT's distance: the chart's danger band
                         "sentence": "Ride to settlement (resting sell at 99¢); stop sells at the bid once BTC is "
                                     f"${-P.RIDE.stop_gap:,.0f} past the strike on the wrong side."},
                "stake": {"default": P.stake_default(cap), "max": caps["per_trade"], "cap": caps["cap"],
                          "cash_cap": caps["cash"], "cash_applies": caps["cash_applies"],
                          "sentence": caps["sentence"], "value": stake,
                          "fee_rate": svc.config.fee_rate, "whole_contracts": svc.config.whole_contracts,
                          "fee_rounding": FEE_ROUNDING[venue]},
                "limits": {"per_trade": caps["per_trade"], "per_day": caps["per_day"],
                           "day_used": svc.exposure(svc.modes[svc.mode], now)["day"],
                           "error": caps["limits_error"]},
                "mode": self._mode(svc, now), "positions": positions, "cone": cone,
                # settling > 60 min after the close (result not booked): "check the venue" (code review #7)
                "settle_warnings": svc.settle_warnings(now),
                "ready": reason is None, "reason": reason}

    # -- /api/play/scoreboard -----------------------------------------------------------------------------------
    def scoreboard(self, now: int, venue: str, *, span: str, mode: str | None = None, last: int | None = None,
                   both: bool = False) -> dict:
        """`mode` = prod | shadow | sandbox | all (default: each venue's current mode); `both` = both venues (every
        tab); `last` = the last N finished attempts for the attempts block and chart (None = all). Otherwise one
        tab's record (`span`). Each trade carries its tab's venue key (kalshi / polymarket / polymarket_5m)."""
        key = venue_key(venue, span)
        v = self.venues.get(key)
        if v is None or v.svc is None:
            return {"venue": venue, "available": False, "reason": v.missing if v else f"Unknown venue {venue!r}."}
        pick = [(k, x) for k, x in self.venues.items() if x.svc is not None] if both else [(key, v)]
        mine: list[dict] = []
        for name, x in pick:
            want = x.svc.modes.get(mode or x.svc.mode)  # None: this venue has no such mode (Polymarket sandbox)
            mine += [{**t, "venue": name} for t in x.svc.trades(0, now, 500) if mode == "all" or t["env"] == want]
        return {"venue": venue, "span": span, "both": both, "available": True, "mode": mode or v.svc.mode,
                "strategy": v.svc.strategy, **P.scoreboard(mine, now, last=last)}
