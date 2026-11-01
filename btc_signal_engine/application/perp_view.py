"""The Perps screen's read model (perps plan U7; KTD11, R5, R13, R15): the whole contract View in one dict.

`PerpTradingService.view()` owns the trading fields (mode, readiness, position, trigger, banners, `can`, outcome,
limits, balance); this module adds what the screen needs around them: the feature flag (`enabled`), the modes this
server can switch to, Kalshi's trading status, the market's terms, the live quote, funding and the warning settings.
A value nobody has read is `null` with a `*_reason` beside it, never a number (R15).

Sources, all in memory (no network, no database scan here; the service's own view reads the settings and today's
orders): the quote is the margin stream's PerpQuoteCache (book top, and the ticker channel's last price, reference
price, liquidation mark and funding; None when older than 2 s, its age still readable); the status, market row,
risk parameters, fee rates and the REST funding estimate are what the service's reconcile and monitor last read.

* `quote.mark` is Kalshi's liquidation mark from the ticker channel: the price the position card's liquidation
  distance is measured from (the service's `_mark`), so the header and the card never disagree.
* `funding` is the ticker channel's rate and next funding time while the quote is fresh, else the monitor's last REST
  estimate (`GET /margin/funding_rates/estimate`): both are Kalshi's own figures.
* `market.max_leverage_long` / `_short` are Kalshi's side leverage estimates at their smallest notional bucket (the
  most a small entry can open at; `domain/perps.size_position` re-checks the estimate at the ticket's real notional).
* `exchange.maintenance_note` is advisory (KTD10: trading pauses follow Kalshi's status, never a clock): the market
  schedule's next close, or Kalshi's estimated resume time while paused; null when Kalshi gives neither.

`changes(now)` is the push channel's diff: the top-level fields whose value differs from the last call (the stream's
`event: view` frames carry only those and the browser merges them).
"""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from btc_signal_engine.application.perp_trading import PerpTradingService
from btc_signal_engine.domain import perps as pm
from btc_signal_engine.domain.markets import PERP_ENVS, PerpSpec, perp

OFF_TEXT = "Perps are switched off"
WARNINGS_NOTE = "Warnings show only while this screen is open."


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%a %d %b %H:%M UTC")


def _max_leverage(estimates: dict | None) -> float | None:
    if not estimates:
        return None
    smallest = min(float(k) for k in estimates)
    try:
        return float(pm.max_leverage(estimates, smallest))
    except pm.PerpRefused:
        return None


class PerpView:
    """The contract View for one server. `service` None = the feature flag is off (nothing was built)."""

    def __init__(self, service: PerpTradingService | None, *, settings: dict[str, float],
                 modes_unavailable: dict[str, str] | None = None, spec: PerpSpec | None = None) -> None:
        """`settings`: {band, amber, red} (fractions; the protective band and the liquidation warning bands).
        `modes_unavailable`: {env: why that mode cannot be chosen} (e.g. no key configured)."""
        self.service = service
        self.settings = {k: float(settings[k]) for k in ("band", "amber", "red")}
        self.modes_unavailable = dict(modes_unavailable or {})
        self.spec = spec or (service.spec if service is not None else perp("kalshi_btc_perp"))
        self._last: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return self.service is not None

    def modes_available(self) -> list[str]:
        s = self.service
        if s is None:
            return []
        return ["off", *(e for e in PERP_ENVS if s.gateways.get(e) is not None
                         and (e != "perp_prod" or s.config.prod_enabled))]

    def view(self, now: int) -> dict:
        s = self.service
        if s is None:
            return self._off(now)
        out = s.view(now)
        st = s.market_state()
        env = s.env
        src = st["quote_source"]
        q = src.quote() if env is not None and src is not None else None
        quote, quote_reason = self._quote(q, src, env)
        funding, funding_reason = self._funding(st, q, env)
        market, market_reason = self._market(st, env)
        out.update(enabled=True, enabled_reason=None, modes_available=self.modes_available(),
                   modes_unavailable=dict(self.modes_unavailable), exchange=self._exchange(st, env),
                   market=market, market_reason=market_reason, quote=quote, quote_reason=quote_reason,
                   funding=funding, funding_reason=funding_reason, settings=dict(self.settings),
                   warnings_note=WARNINGS_NOTE)
        return out

    def changes(self, now: int) -> dict:
        """The top-level fields that changed since the last call (all of them on the first)."""
        v = self.view(now)
        changed = {k: x for k, x in v.items() if k not in self._last or self._last[k] != x}
        self._last = v
        return changed

    # -- parts -------------------------------------------------------------------------------------------------------
    def _off(self, now: int) -> dict:
        why = OFF_TEXT
        return {"server_ms": now, "enabled": False, "enabled_reason": why, "mode": "off", "modes_available": [],
                "modes_unavailable": {}, "ready": False, "ready_reason": why, "killed": None,
                "exchange": {"trading_active": None, "reason": why, "maintenance_note": None},
                "market": None, "market_reason": why, "quote": None, "quote_reason": why,
                "funding": None, "funding_reason": why, "balance": None, "balance_reason": why,
                "position": None, "trigger": {"state": "none", "stop_loss": None, "take_profit": None,
                                              "updated_time": None, "reason": None},
                "unknown_order": False, "foreign": [], "banners": [],
                "can": {"entry": False, "entry_reason": why, "exit": False, "exit_reason": why,
                        "trigger": False, "trigger_reason": why},
                "outcome": None, "limits": {"per_trade": None, "per_day": None, "committed_today": None, "text": why},
                "settings": dict(self.settings), "warnings_note": WARNINGS_NOTE}

    @staticmethod
    def _quote(q: Any, src: Any, env: str | None) -> tuple[dict | None, str | None]:
        if env is None:
            return None, "Perps trading is off: no Kalshi perps price is streamed."
        if src is None:
            return None, f"No Kalshi perps price stream for {env}."
        if q is None:
            return None, f"No fresh Kalshi perps price ({src.why_none() or 'no reason given'})."
        return {"bid": q.bid, "ask": q.ask, "bid_size": q.bid_size, "ask_size": q.ask_size, "last": q.last_price,
                "mark": q.liquidation_mark, "reference": q.reference_price, "age_ms": q.age_ms}, None

    @staticmethod
    def _funding(st: dict, q: Any, env: str | None) -> tuple[dict | None, str | None]:
        if env is None:
            return None, "Perps trading is off."
        if q is not None and q.funding_rate is not None and q.next_funding_ms is not None:
            return {"rate": q.funding_rate, "next_ms": q.next_funding_ms}, None
        f = st["funding"]
        if f is not None and f.get("rate") is not None and f.get("next_ms") is not None:
            return {"rate": float(f["rate"]), "next_ms": int(f["next_ms"])}, None
        return None, "Kalshi's funding rate has not been read yet."

    def _market(self, st: dict, env: str | None) -> tuple[dict, str | None]:
        spec = self.spec
        m = st["market"]
        row = (m or {}).get("row") or st["market_row"] or {}
        out = {"ticker": spec.ticker(env) if env else None, "tick": spec.tick, "contract_btc": spec.contract_btc,
               "count_step": spec.count_step, "taker_fee": None if m is None else m["taker"],
               "maker_fee": None if m is None else m["maker"],
               "max_leverage_long": _max_leverage(row.get("long_leverage_estimates")),
               "max_leverage_short": _max_leverage(row.get("short_leverage_estimates"))}
        if env is None:
            return out, "Perps trading is off."
        if m is None:
            return out, "Kalshi's market terms (fees, leverage estimates) have not been read yet."
        return out, None

    @staticmethod
    def _exchange(st: dict, env: str | None) -> dict:
        if env is None:
            return {"trading_active": None, "reason": "Perps trading is off.", "maintenance_note": None}
        status = st["status"]
        if status is None:
            return {"trading_active": None, "reason": "Kalshi's perps trading status has not been read yet.",
                    "maintenance_note": None}
        note = None
        if not status["trading_active"] and status.get("resume_ms"):
            note = f"Kalshi estimates trading resumes {_utc(int(status['resume_ms']))}."
        else:
            sched = (st["market_row"] or {}).get("schedule") or {}
            if sched.get("next_close_ts"):
                note = f"Kalshi's next scheduled perps pause: {_utc(int(sched['next_close_ts']))}."
        return {"trading_active": bool(status["trading_active"]), "reason": status.get("reason"),
                "maintenance_note": note}
