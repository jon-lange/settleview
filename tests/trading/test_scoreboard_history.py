"""The Play scoreboard over a long history: TradingService.trades() limits and orders in SQL and reads every listed
manual exit's and settlement's audit in one query, with the same P&L and pnl_source as reading bracket by bracket."""

from __future__ import annotations

from btc_signal_engine.application.play import PlayService, PlayVenue
from tests.trading.helpers import NOW, TICKER, Exchange, Market
from tests.trading.test_strategy_scope import svc_for

N = 600


def _bracket(i: int) -> dict:
    created = NOW - (N - i) * 60_000
    manual = i % 7 == 0
    return {"id": f"b{i:04d}", "env": "prod", "kind": "entry", "ticker": TICKER, "side": "UP" if i % 2 else "DOWN",
            "window_end": created + 600_000, "stake": 1.0, "contracts": 2.0, "limit_price": 0.5, "take_profit": 0.99,
            "stop": 0.0, "max_cost": 1.1, "ticket_hash": "h", "state": "closed", "filled": 2.0, "held": 0.0,
            "tp_filled": 0.0, "entry_avg": 0.5, "entry_cost": 1.0, "proceeds": 2.0 if i % 3 else 0.0,
            "exit_fees": 0.01, "exit_reason": "manual" if manual else "settled", "created_ts": created,
            "updated_ts": created, "closed_ts": created + 600_000, "strategy": "you"}


def _pnl(i: int) -> float:
    return round((2.0 if i % 3 else 0.0) - 0.01 - 1.0, 4)


def _source(i: int) -> str | None:
    if i % 7:
        return "unrecorded"  # settled with no recorded result (the rows before settlement results were read)
    return "estimated" if i % 14 == 0 else "fills"


def test_scoreboard_over_600_brackets_does_one_brackets_and_one_audit_query(store):
    store.insert("trading_brackets", [_bracket(i) for i in range(N)], ignore_conflicts=False)
    audits = [{"ts": NOW, "event": "manual_exit", "env": "prod", "bracket_id": f"b{i:04d}", "strategy": "you",
               "detail": {"pnl_source": "estimated" if i % 14 == 0 else "fills"}} for i in range(0, N, 7)]
    # another strategy's audit for one of these ids must not change its label
    audits.append({"ts": NOW, "event": "manual_exit", "env": "prod", "bracket_id": "b0007", "strategy": "taker",
                   "detail": {"pnl_source": "estimated"}})
    store.insert("trading_audit", audits, ignore_conflicts=False)
    svc = svc_for(store, Exchange(Market()), "you")
    reads: list[str] = []
    select = store.select

    def counting(table, *a, **kw):
        reads.append(table)
        return select(table, *a, **kw)
    store.select = counting
    play = PlayService({"kalshi": PlayVenue(svc)}, evaluation_fn=lambda: {})
    sb = play.scoreboard(NOW, "kalshi", span="15m", last=20)
    assert reads.count("trading_brackets") == 1 and reads.count("trading_audit") == 1
    pts = sb["attempts"]["points"]
    assert [p["id"] for p in pts] == [f"b{i:04d}" for i in range(N - 20, N)]
    assert [p["pnl"] for p in pts] == [round(_pnl(i), 2) for i in range(N - 20, N)]
    assert sb["record"]["trades"] == 500  # the newest 500, as before
    rows = svc.trades(0, NOW, 500)
    assert [t["id"] for t in rows] == [f"b{i:04d}" for i in range(N - 1, N - 501, -1)]
    assert {t["id"]: t["pnl_source"] for t in rows} == {f"b{i:04d}": _source(i) for i in range(N - 500, N)}
    assert {t["id"]: t["pnl"] for t in rows} == {f"b{i:04d}": _pnl(i) for i in range(N - 500, N)}
