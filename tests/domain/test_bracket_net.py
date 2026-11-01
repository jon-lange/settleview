"""One bracket-net rule (domain/brackets.py), characterized against every place that used to compute it.

The reference formulas below are the copies as they stood on main (a17bab7) before the P2 DRY refactor. On stored
data (4-dp dollar figures) every call site's figure is unchanged; the two named fixes are pinned at the end:
float noise is no longer a win or a loss, and a whole-cent display rounds the 4-dp net (as the Record tab shows it).
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from btc_signal_engine.application import agent_fleet_view, notifier, record_summary, rule_scores, trading
from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.agent_sessions import position_view
from btc_signal_engine.domain import brackets


def old_raw(b: dict) -> float:  # the unrounded difference every copy started from
    return (float(b.get("proceeds") or 0.0) - float(b.get("entry_cost") or 0.0) - float(b.get("tp_fees") or 0.0)
            - float(b.get("exit_fees") or 0.0))


def old_net4(b: dict) -> float:
    return round(old_raw(b), 4)


def old_trading_raw(b: dict) -> float:  # trading.py subtracted the entry cost last
    return ((b.get("proceeds") or 0.0) - (b.get("tp_fees") or 0.0) - (b.get("exit_fees") or 0.0)
            - (b.get("entry_cost") or 0.0))


def four_dp(rng: random.Random) -> float:
    return rng.randrange(0, 50_000) / 10_000


def stored_brackets(n: int = 2000) -> list[dict]:
    """Brackets as the store holds them: 4-dp dollars, some figures missing."""
    rng = random.Random(20261101)
    out = []
    for i in range(n):
        b = {"id": f"b{i}", "state": "closed", "filled": rng.choice([0.0, 1.0, 2.5]), "side": rng.choice(["UP", "DOWN"]),
             "proceeds": four_dp(rng), "entry_cost": four_dp(rng), "tp_fees": rng.choice([None, 0.0, four_dp(rng) / 50]),
             "exit_fees": rng.choice([None, 0.0, four_dp(rng) / 50])}
        b["tp_fees"] = None if b["tp_fees"] is None else round(b["tp_fees"], 4)
        b["exit_fees"] = None if b["exit_fees"] is None else round(b["exit_fees"], 4)
        out.append(b)
    return out


STORED = stored_brackets()


def test_every_four_dp_call_site_is_unchanged_on_stored_data():
    for b in STORED:
        want = old_net4(b)
        assert brackets.net(b) == want
        assert record_summary.net4(b) == want
        assert agent_fleet_view._net(b) == want
        assert round(old_trading_raw(b), 4) == want  # trading.trades() / the settled audit's P&L
        if b["filled"] > 0:
            assert position_view(b)["realized"] == want
            link = {"rule": "r", "market": "kalshi_15m"}
            assert rule_scores.trade_of(link, b)["net"] == want


def test_session_totals_are_unchanged_on_stored_data():
    filled = [b for b in STORED if b["filled"] > 0]
    old = round(sum(old_raw(b) for b in filled), 4)
    assert brackets.total(filled) == old
    runner = SimpleNamespace(config=SimpleNamespace(trades_kept=3), TRADE_KEYS=AgentRunner.TRADE_KEYS)
    _, summary = AgentRunner._trades(runner, STORED)
    assert summary["net"] == old
    assert summary["won"] == sum(1 for b in filled if brackets.net(b) > 0)
    # the old count differed only where float noise made a $0 net look like a win (test_float_noise_is_not_a_win)
    assert all(brackets.net(b) == 0.0 for b in filled if (old_raw(b) > 0) != (brackets.net(b) > 0))


def test_whole_cent_displays_change_only_on_an_exact_half_cent():
    """notifier and the closed sentence rounded the raw difference to the cent; they now round the 4-dp net. The
    two differ only when the 4-dp net is exactly on a half cent and float noise pushed the raw difference across."""
    changed = 0
    for b in STORED:
        new = brackets.net_cents_display(b)
        assert notifier._net(b) == new
        old = round(old_trading_raw(b), 2)
        if old != new:
            changed += 1
            assert round(abs(brackets.net(b)) * 10_000) % 100 == 50
            assert abs(old - new) == pytest.approx(0.01)
    assert changed < len(STORED) // 50  # rare: about one in 200 stored nets is an exact half cent


def test_closed_sentence_uses_the_cent_rule():
    b = {"exit_reason": "settled", "proceeds": 1.0, "entry_cost": 0.43, "tp_fees": 0.0, "exit_fees": 0.01}
    assert trading.closed_sentence(b) == ("This position is already closed: its window ended and it settled "
                                          "(+$0.56).")


def test_float_noise_is_not_a_win():
    """Named fix: the agent's memory summary counted 1.1 - 1.0 - 0.1 (8.3e-17) as a win."""
    b = {"state": "closed", "filled": 1.0, "proceeds": 1.1, "entry_cost": 1.0, "tp_fees": 0.1, "exit_fees": None}
    assert old_raw(b) > 0  # what the old memory summary saw
    assert brackets.net(b) == 0.0
    runner = SimpleNamespace(config=SimpleNamespace(trades_kept=3), TRADE_KEYS=AgentRunner.TRADE_KEYS)
    _, summary = AgentRunner._trades(runner, [b])
    assert (summary["won"], summary["lost"], summary["net"]) == (0, 1, 0.0)


def test_closed_filled():
    assert brackets.closed_filled({"state": "closed", "filled": 0.5})
    assert not brackets.closed_filled({"state": "closed", "filled": 0})
    assert not brackets.closed_filled({"state": "open", "filled": 1})


@pytest.mark.parametrize(("side", "below", "above"), [("UP", 77000.0, None), ("DOWN", None, 77000.0),
                                                       ("FLAT", None, None)])
def test_stop_fields(side, below, above):
    assert brackets.stop_fields(side, 77000.0) == {"stop_index_below": below, "stop_index_above": above}


def test_stop_of_reads_the_side_s_field():
    row = {"side": "UP", "stop_index_below": 1.0, "stop_index_above": 2.0}
    assert brackets.stop_of(row) == 1.0
    assert brackets.stop_of(row, "DOWN") == 2.0
    assert brackets.stop_of({"side": "DOWN", "stop_index_above": 3.0}) == 3.0
    assert brackets.stop_of({"side": "UP"}) is None
