"""The phone strategies' own trade limits (you, you_poly, you_poly5): user-set, stored in the app DB, default none.

The user's direction (2026-10-29): "add capability, not limits" and "make them settings and default to no limits".
So the phone's per-trade and per-day limits are one setting shared by every phone strategy, stored in
strategy_settings (strategy "play", key "limits", like access / phone_pin), each a positive dollar amount or the
explicit string "none". Never set = none / none. Read at check time by every phone TradingService (`for_trading`),
so a change applies to the next order without a restart. The per-window limit is the per-trade one (or none).

A stored value that is not exactly that shape is UNREADABLE: entries are refused with "Trade limits setting
unreadable: ..." until the user saves the limits again. It is never read as none or as some number.

Changing them (`set`): lowering (or adding a limit where there was none) needs nothing more; RAISING or REMOVING a
limit needs `confirm` == LIMITS_CONFIRM exactly (as does any change while the stored value is unreadable: it cannot be
shown to be a lowering). Every change is audited (trading_audit, event "limits_changed", strategy "play", env "all",
detail {old, new, raised}).

Market protection (`market_band`, 2026-10-30): a Market buy fills at any price up to the ask it was sized at plus
this band (the user chose a "price-protected Market"), not up to 99c, so its worst case stays near the stake and fits
the cash. Stored like the limits (strategy "play", key "market_band"): whole cents from BAND_CHOICES or "none" (= up
to 99c). Never set = DEFAULT_BAND_CENTS. Any other stored value is UNREADABLE: market buys are refused until it is
saved again. Changing it needs no phrase (the limits and the cash backstop still bound the worst case); it is audited
(event "market_band_changed").

What stays even with no limits is not here: in production every phone entry must fit the venue's fresh cash
(TradingService's cash backstop) and every gateway keeps its hard order ceiling (a bug guard, see bootstrap).

One daily total (KD7, perps plan 2026-10-29: "$20 a day" means $20 in total): `CommittedToday` adds up what every
registered trading service has committed today (the user's day, domain/days.py): event-contract stakes (TradingService.committed) and perps
collateral (PerpTradingService). The real-money environments count together (Kalshi prod, Polymarket US prod, Kalshi
perps prod); each practice environment counts only with itself. Every service given `CommittedToday.total` as its
`committed_today_fn` checks the per-day limit against that one figure.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from btc_signal_engine.application.actor import actor
from btc_signal_engine.application.ports import Repository
from btc_signal_engine.application.settings_store import put_setting
from btc_signal_engine.domain.fmt import usd

LIMITS_CONFIRM = "I understand this raises my limit"
SETTING = {"strategy": "play", "key": "limits"}
NONE = "none"  # the stored value of "no limit"
FIELDS = ("per_trade", "per_day")
AUDIT_STRATEGY = "play"  # the audit rows of this setting (shared by every phone strategy)
LABELS = {"per_trade": "per-trade", "per_day": "per-day"}


BAND_SETTING = {"strategy": "play", "key": "market_band"}
BAND_CHOICES = (5, 10, 15, 25)  # cents above the ask; or None = up to 99c
DEFAULT_BAND_CENTS = 15


class LimitsUnreadable(ValueError):
    """The stored limits are not a value this code wrote: entries are refused until they are saved again."""


REAL_MONEY_ENVS = frozenset({"prod", "pm_prod", "perp_prod"})  # one daily pool (KD7)


def day_pool(env: str) -> str:
    """The daily-limit pool of an environment: "real money" for every production env, else the env itself."""
    return "real money" if env in REAL_MONEY_ENVS else str(env)


class CommittedToday:
    """KD7's one daily figure. Each source is `(now) -> {env: dollars committed today in that env}` (the user's day);
    `total(env, now)` sums every source over the envs in `env`'s pool. A source that fails raises: the caller refuses
    (never counted as zero)."""

    def __init__(self) -> None:
        self._sources: list[Callable[[int], dict[str, float]]] = []

    def add_source(self, fn: Callable[[int], dict[str, float]]) -> None:
        self._sources.append(fn)

    def total(self, env: str, now: int) -> float:
        pool = day_pool(env)
        return round(sum(float(v) for fn in self._sources for e, v in fn(now).items() if day_pool(e) == pool), 4)


@dataclass(frozen=True)
class TradeLimits:
    per_trade: float | None  # None = no app limit
    per_day: float | None
    set_at: int | None  # None = never set (the default none / none)

    def as_dict(self) -> dict:
        return {"per_trade": self.per_trade, "per_day": self.per_day}


def _parse_one(name: str, v: Any) -> float | None:
    if v == NONE and isinstance(v, str):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise LimitsUnreadable(f"{name} is {v!r} (expected a dollar amount or \"none\")")
    x = float(v)
    if not math.isfinite(x) or x <= 0:
        raise LimitsUnreadable(f"{name} is {v!r} (expected a positive dollar amount or \"none\")")
    return x


def parse_stored(value: Any) -> tuple[float | None, float | None]:
    """The stored JSON -> (per_trade, per_day); anything else raises LimitsUnreadable (never a guess)."""
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise LimitsUnreadable(f"stored value {value!r} is not {{per_trade, per_day}}")
    return _parse_one("per_trade", value["per_trade"]), _parse_one("per_day", value["per_day"])


def validate_input(name: str, v: Any) -> float | None:
    """A requested limit: None (no limit) or a positive finite dollar amount, rounded to the cent (at least 1c)."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ValueError(f"{name} must be a dollar amount or null (no limit).")
    x = round(float(v), 2)
    if x < 0.01:
        raise ValueError(f"{name} must be at least $0.01, or null (no limit).")
    return x


def parse_band(value: Any) -> int | None:
    """The stored market band -> cents (one of BAND_CHOICES) or None (up to 99c); anything else raises."""
    if value == NONE and isinstance(value, str):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in BAND_CHOICES:
        raise LimitsUnreadable(f"market protection is {value!r} (expected one of {BAND_CHOICES} or \"none\")")
    return value


def _band_text(cents: int | None) -> str:
    return "up to 99c" if cents is None else f"ask + {cents}c"


def _store(v: float | None) -> float | str:
    return NONE if v is None else v


def _money(v: float | None) -> str:
    return "no limit" if v is None else usd(v)


def raises(old: float | None, new: float | None) -> bool:
    """True when `new` allows more than `old` (None = no limit)."""
    if new is None:
        return old is not None
    return old is not None and new > old + 1e-9


class PlayLimits:
    def __init__(self, repo: Repository | None = None) -> None:
        self.repo = repo
        self._mem: dict | None = None  # without a repository (tests): the stored row in memory
        self._band_mem: dict | None = None

    def _row(self) -> dict | None:
        if self.repo is None:
            return self._mem
        rows = self.repo.select("strategy_settings", SETTING)
        return rows[0] if rows else None

    def current(self) -> TradeLimits:
        """The limits now. Raises LimitsUnreadable when the stored value is not readable."""
        row = self._row()
        if row is None:
            return TradeLimits(None, None, None)  # never set: the user's chosen default, no limits
        per_trade, per_day = parse_stored(row.get("value"))
        return TradeLimits(per_trade, per_day, row.get("updated_ts"))

    def for_trading(self) -> dict:
        """TradingService's `limits_fn`: {"per_trade", "per_window", "per_day"} ($ or None = no limit). Raises
        LimitsUnreadable (a ValueError) when the setting cannot be read; the service refuses entries with it."""
        lim = self.current()
        return {"per_trade": lim.per_trade, "per_window": lim.per_trade, "per_day": lim.per_day}

    def _band_row(self) -> dict | None:
        if self.repo is None:
            return self._band_mem
        rows = self.repo.select("strategy_settings", BAND_SETTING)
        return rows[0] if rows else None

    def market_band_cents(self) -> int | None:
        """The market protection now in cents (None = up to 99c). Raises LimitsUnreadable when unreadable."""
        row = self._band_row()
        return DEFAULT_BAND_CENTS if row is None else parse_band(row.get("value"))

    def market_band(self) -> float | None:
        """TradingService's `market_band_fn`: the band in dollars, or None (a market buy may fill up to 99c)."""
        c = self.market_band_cents()
        return None if c is None else c / 100

    def band_view(self) -> dict:
        try:
            return {"cents": self.market_band_cents(), "choices": list(BAND_CHOICES), "readable": True, "error": None}
        except LimitsUnreadable as e:
            return {"cents": None, "choices": list(BAND_CHOICES), "readable": False,
                    "error": f"Market protection setting unreadable: {e}. Market buys are refused until you save it "
                             "again."}

    def set_market_band(self, cents: Any, now: int) -> dict:
        """Save the market protection: cents in BAND_CHOICES or None (up to 99c). Refusal code "invalid"."""
        if cents is not None and (isinstance(cents, bool) or not isinstance(cents, int) or cents not in BAND_CHOICES):
            return {"ok": False, "code": "invalid",
                    "reason": f"Market protection must be one of {', '.join(map(str, BAND_CHOICES))} cents, or null "
                              "(up to 99c)."}
        try:
            old: Any = self.market_band_cents()
        except LimitsUnreadable as e:
            old = {"unreadable": str(e)}
        if old == cents:
            return {"ok": True, "changed": False, "market_band": self.band_view()}
        value = NONE if cents is None else cents
        if self.repo is None:
            self._band_mem = {**BAND_SETTING, "value": value, "updated_ts": now}
        else:
            put_setting(self.repo, BAND_SETTING, value, now)
            was = "unreadable" if isinstance(old, dict) else _band_text(old)
            self.repo.insert("trading_audit", [{
                "ts": now, "event": "market_band_changed", "env": "all", "strategy": AUDIT_STRATEGY,
                "bracket_id": None, "session_id": None, "code": "changed",
                "reason": f"Market protection: {_band_text(cents)} (was {was}).",
                "detail": {"old": old, "new": cents}, "actor": actor()}])
        return {"ok": True, "changed": True, "market_band": self.band_view()}

    def view(self) -> dict:
        try:
            lim = self.current()
        except LimitsUnreadable as e:
            row = self._row() or {}
            return {"per_trade": None, "per_day": None, "set_at": row.get("updated_ts"), "readable": False,
                    "error": f"Trade limits setting unreadable: {e}. Entries are refused until you save the limits "
                             "again.", "confirm_phrase": LIMITS_CONFIRM}
        return {"per_trade": lim.per_trade, "per_day": lim.per_day, "set_at": lim.set_at, "readable": True,
                "error": None, "confirm_phrase": LIMITS_CONFIRM}

    def history(self, limit: int = 10) -> list[dict]:
        if self.repo is None:
            return []
        rows = self.repo.select("trading_audit", {"event": "limits_changed", "strategy": AUDIT_STRATEGY},
                                order_by=["-ts"], limit=limit)
        return [{"ts": r["ts"], "reason": r.get("reason"), "detail": r.get("detail")} for r in rows]

    def set(self, per_trade: Any, per_day: Any, confirm: str | None, now: int) -> dict:
        """Save new limits. Refusals: code "invalid" (a value is not a positive amount or null) or
        "confirm_required" (a raise or removal, or the stored value is unreadable, without the exact phrase)."""
        try:
            new = {"per_trade": validate_input("per_trade", per_trade), "per_day": validate_input("per_day", per_day)}
        except ValueError as e:
            return {"ok": False, "code": "invalid", "reason": str(e)}
        try:
            cur = self.current()
            old: dict | None = cur.as_dict()
        except LimitsUnreadable as e:
            cur, old, unreadable = None, None, str(e)
        if old is not None and old == new:
            return {"ok": True, "changed": False, **self.view()}
        raised = [f for f in FIELDS if old is None or raises(old[f], new[f])]
        confirmed = (confirm or "") == LIMITS_CONFIRM
        if raised and not confirmed:
            if old is None:
                why = "The saved limits are unreadable, so this change cannot be shown to lower them."
            else:
                why = "This raises or removes your " + " and ".join(
                    f"{LABELS[f]} limit ({_money(old[f])} -> {_money(new[f])})" for f in raised) + "."
            return {"ok": False, "code": "confirm_required", "raises": raised, "confirm_phrase": LIMITS_CONFIRM,
                    "reason": f"{why} Type exactly \"{LIMITS_CONFIRM}\" to confirm; nothing was changed."}
        value = {f: _store(new[f]) for f in FIELDS}
        if self.repo is None:
            self._mem = {**SETTING, "value": value, "updated_ts": now}
        else:
            put_setting(self.repo, SETTING, value, now)
        old_detail = old if old is not None else {"unreadable": unreadable}
        reason = (f"Phone trade limits: per trade {_money(new['per_trade'])}, per day {_money(new['per_day'])}"
                  + (f" (was {_money(old['per_trade'])} / {_money(old['per_day'])})." if old is not None
                     else " (the saved value was unreadable)."))
        if self.repo is not None:
            self.repo.insert("trading_audit", [{
                "ts": now, "event": "limits_changed", "env": "all", "strategy": AUDIT_STRATEGY, "bracket_id": None,
                "session_id": None, "code": "raised" if raised else "lowered", "reason": reason,
                "detail": {"old": old_detail, "new": new, "raised": raised, "confirmed": bool(raised)},
                "actor": actor()}])
        return {"ok": True, "changed": True, **self.view()}
