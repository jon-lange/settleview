"""The market table (pure): every BTC Up/Down market the app knows, one row each.

Adding a market (e.g. Kalshi hourly) starts here: a row with its venue, span, series (Kalshi ticker prefix or
Polymarket slug prefix), window length, strike source, whether it may be traded, and its phone Play strategy (None:
no Play tab). The adapters' series guards, the Polymarket market stream, the Play screens' tabs and the phone
strategies' entry cutoffs all read it. A row with `tradable=False` is recognised (slugs parse, windows are known) but
every gateway refuses its orders.

Every lookup raises UnknownMarket for a key, venue or span the table does not have: never a silent default.
"""

from __future__ import annotations

from dataclasses import dataclass


class UnknownMarket(ValueError):
    """A market key, venue, span or phone strategy that is not in the table."""


@dataclass(frozen=True)
class VenueSpec:
    key: str  # "kalshi" | "polymarket" (the Play screens' venue)
    name: str  # display name ("No live Polymarket price ...")
    fee_rounding: str  # how the venue rounds a fee (the stake box's maths): "up_0.0001" | "cent"


@dataclass(frozen=True)
class MarketSpec:
    key: str  # "kalshi_15m", "polymarket_5m", ...
    venue: str  # a VENUES key
    span: str  # "5m" | "15m" | "1h"
    series: str  # Kalshi series ticker / Polymarket slug prefix; a market id is f"{series}-..."
    window_ms: int
    kind: str  # "updown": Up when the settlement average >= the strike (ties are Up)
    strike: str  # "kalshi_floor_strike" | "pm_price_to_beat" (both: CF Benchmarks BRTI 60 s average at the open)
    phone_strategy: str | None  # the Play tab's TradingService strategy; None = no tab
    play_key: str | None  # the Play tab's venue key in payloads ("kalshi", "polymarket", "polymarket_5m")
    entry_cutoff_ms: int | None  # phone entries need more than this left; None = TradingConfig.min_left_ms (2 min)
    tradable: bool  # False: recognised, never traded (every gateway refuses it)
    label: str


VENUES: dict[str, VenueSpec] = {
    "kalshi": VenueSpec("kalshi", "Kalshi", "up_0.0001"),
    "polymarket": VenueSpec("polymarket", "Polymarket", "cent"),
}

# Display order matters: a venue's Play tabs follow it.
MARKETS: tuple[MarketSpec, ...] = (
    MarketSpec("kalshi_15m", "kalshi", "15m", "KXBTC15M", 900_000, "updown", "kalshi_floor_strike",
               phone_strategy="you", play_key="kalshi", entry_cutoff_ms=None, tradable=True, label="Kalshi 15 min"),
    MarketSpec("polymarket_15m", "polymarket", "15m", "cpc-btc-updown-15m", 900_000, "updown", "pm_price_to_beat",
               phone_strategy="you_poly", play_key="polymarket", entry_cutoff_ms=None, tradable=True,
               label="Polymarket 15 min"),
    # Entries close at 1:00 left (the 15-minute windows' 2:00 would leave 3 minutes).
    MarketSpec("polymarket_5m", "polymarket", "5m", "cpc-btc-updown-5m", 300_000, "updown", "pm_price_to_beat",
               phone_strategy="you_poly5", play_key="polymarket_5m", entry_cutoff_ms=60_000, tradable=True,  # gitleaks:allow - a market key, not a secret
               label="Polymarket 5 min"),
    MarketSpec("polymarket_1h", "polymarket", "1h", "cpc-btc-updown-1h", 3_600_000, "updown", "pm_price_to_beat",
               phone_strategy=None, play_key=None, entry_cutoff_ms=None, tradable=False, label="Polymarket 1 hour"),
)
_BY_KEY = {m.key: m for m in MARKETS}
if len(_BY_KEY) != len(MARKETS):
    raise UnknownMarket("duplicate market key")
if not all(m.venue in VENUES for m in MARKETS):
    raise UnknownMarket("market on an unknown venue")
if len({(m.venue, m.span) for m in MARKETS}) != len(MARKETS):
    raise UnknownMarket("two markets share a venue and span")


def venue(key: str) -> VenueSpec:
    try:
        return VENUES[key]
    except (KeyError, TypeError):
        raise UnknownMarket(f"unknown venue {key!r}") from None


def market(key: str) -> MarketSpec:
    try:
        return _BY_KEY[key]
    except (KeyError, TypeError):
        raise UnknownMarket(f"unknown market {key!r}") from None


def venue_markets(venue_key: str) -> tuple[MarketSpec, ...]:
    """Every market on a venue (tradable or not), in table order."""
    venue(venue_key)
    return tuple(m for m in MARKETS if m.venue == venue_key)


def by_venue_span(venue_key: str, span: str) -> MarketSpec:
    for m in venue_markets(venue_key):
        if m.span == span:
            return m
    raise UnknownMarket(f"{venue_key!r} has no {span!r} market")


def by_phone_strategy(strategy: str) -> MarketSpec:
    for m in MARKETS:
        if m.phone_strategy is not None and m.phone_strategy == strategy:
            return m
    raise UnknownMarket(f"no market has the phone strategy {strategy!r}")


def phone_markets(venue_key: str | None = None) -> tuple[MarketSpec, ...]:
    """The markets with a Play tab (all venues, or one), in table order."""
    pool = MARKETS if venue_key is None else venue_markets(venue_key)
    return tuple(m for m in pool if m.phone_strategy is not None)


def tradable_spans(venue_key: str) -> tuple[str, ...]:
    """The spans a venue's gateway may trade, in table order."""
    return tuple(m.span for m in venue_markets(venue_key) if m.tradable)


def allowed_series(venue_key: str) -> frozenset[str]:
    """The series a venue's gateway may trade (its order guard's allow-list)."""
    return frozenset(m.series for m in venue_markets(venue_key) if m.tradable)


# -- perpetuals (KTD12) -------------------------------------------------------------------------------------------
# Perps live in their own table: a perp row among the window markets would add it to the 15-minute gateway's
# allow-list and give it a span tab. The ticker differs per environment (Kalshi demo lists KXBTCPERP1, production
# KXBTCPERP; read from GET /margin/markets 2026-10-30), so the margin client's allow-list is per env.
PERP_ENVS = ("perp_demo", "perp_prod")


@dataclass(frozen=True)
class PerpSpec:
    key: str  # "kalshi_btc_perp"
    venue: str  # a VENUES key
    tickers: tuple[tuple[str, str], ...]  # (env, ticker) for every PERP_ENVS entry
    contract_btc: float  # underlying per contract (Kalshi contract_size)
    tick: float  # price step, dollars per contract (Kalshi tick_size)
    count_step: float  # smallest count step (1.0: Kalshi reports fractional_trading_enabled false for BTC)
    label: str

    def ticker(self, env: str) -> str:
        for e, t in self.tickers:
            if e == env:
                return t
        raise UnknownMarket(f"{self.key!r} has no ticker for env {env!r}")


PERPS: tuple[PerpSpec, ...] = (
    PerpSpec("kalshi_btc_perp", "kalshi", (("perp_demo", "KXBTCPERP1"), ("perp_prod", "KXBTCPERP")),
             contract_btc=0.0001, tick=0.0001, count_step=1.0, label="Kalshi BTC perpetual"),
)
_PERP_BY_KEY = {p.key: p for p in PERPS}
if len(_PERP_BY_KEY) != len(PERPS) or set(_PERP_BY_KEY) & set(_BY_KEY):
    raise UnknownMarket("duplicate perp key")
if not all(p.venue in VENUES and {e for e, _ in p.tickers} == set(PERP_ENVS) for p in PERPS):
    raise UnknownMarket("perp on an unknown venue or without a ticker per env")


def perp(key: str) -> PerpSpec:
    try:
        return _PERP_BY_KEY[key]
    except (KeyError, TypeError):
        raise UnknownMarket(f"unknown perp {key!r}") from None


def allowed_perp_tickers(env: str) -> frozenset[str]:
    """The perp tickers the margin client may trade in one env (its order guard's allow-list)."""
    if env not in PERP_ENVS:
        raise UnknownMarket(f"unknown perp env {env!r}")
    return frozenset(p.ticker(env) for p in PERPS)
