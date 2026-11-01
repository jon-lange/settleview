"""The day limit's "today" is the user's day (domain/days.py), the same day Record and Play's P&L use (review B5#2):
a stake committed after local midnight counts today, one before it does not, whatever the UTC date says."""

from __future__ import annotations

from btc_signal_engine.application.play import PlayService, PlayVenue
from btc_signal_engine.domain import days
from tests.trading.helpers import NOW, TICKER, Exchange, Market
from tests.trading.test_strategy_scope import svc_for

CHI = days.zone("America/Chicago")


def _bracket(i: int, created: int, cost: float) -> dict:
    return {"id": f"d{i}", "env": "prod", "kind": "entry", "ticker": TICKER, "side": "UP", "window_end": created + 600_000,
            "stake": cost, "contracts": 2.0, "limit_price": 0.5, "take_profit": 0.99, "stop": 0.0, "max_cost": cost,
            "ticket_hash": "h", "state": "closed", "filled": 2.0, "held": 0.0, "tp_filled": 0.0, "entry_avg": 0.5,
            "entry_cost": cost, "proceeds": 2.0, "exit_fees": 0.0, "exit_reason": "settled", "created_ts": created,
            "updated_ts": created, "closed_ts": created + 600_000, "strategy": "you"}


def test_committed_today_and_play_pnl_follow_the_users_zone(store):
    local0 = days.day_start(NOW, CHI)
    store.insert("trading_brackets", [_bracket(1, local0 + 60_000, 1.5), _bracket(2, local0 - 60_000, 4.0)],
                 ignore_conflicts=False)
    svc = svc_for(store, Exchange(Market()), "you")
    svc.day_tz = CHI
    assert svc.exposure("prod", NOW)["day"] == 1.5  # only the stake after Chicago's midnight
    play = PlayService({"kalshi": PlayVenue(svc)}, evaluation_fn=lambda: {})
    play.day_tz = CHI
    sb = play.scoreboard(NOW, "kalshi", span="15m")
    assert sb["day_tz"] == "America/Chicago"
    # d1 closed after local midnight (P&L 2.0 - 1.5); d2 closed at local0 + 9 min, also today (2.0 - 4.0)
    assert sb["pnl"]["today"] == round((2.0 - 1.5) + (2.0 - 4.0), 2)
    utc = svc_for(store, Exchange(Market()), "you", set_mode=False)  # the default zone: UTC, said by its name
    assert days.day_start(NOW, utc.day_tz) < local0 - 60_000  # NOW is an afternoon: both stakes are on its UTC day
    assert utc.exposure("prod", NOW)["day"] == 5.5
