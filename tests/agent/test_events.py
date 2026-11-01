"""Agent event engine (U5): the 1 s diff over compact market state, coalescing into few wakes, the gaps and hourly
cap, drop rules, agent alarms, snooze and urgent events during a running wake. Pure and clock-injected: every time
below is an explicit ms integer; nothing touches a venue."""

from __future__ import annotations

import json

import pytest

from btc_signal_engine.application.agent_events import AgentEvents, EventsConfig, Snapshot

W0 = 1_800_000_000_000 - 1_800_000_000_000 % 900_000  # a 15 min window boundary
W1 = W0 + 900_000
CUTOFF = 60_000  # entry closes 60 s before the window ends


def view(now: int, *, venue: str = "kalshi", span: str = "15m", start: int = W0, mid: float | None = 0.50,
         strike: float | None = 100_000.0, btc: float | None = 100_100.0, action: str = "WAIT",
         agree: int | None = None, leans: dict | None = None, positions: list | None = None, fresh: bool = True,
         cutoff: int = CUTOFF) -> dict:
    """The fields of PlayService.now_view the engine reads."""
    end = start + 900_000
    leans = leans or {"market": None, "model": None, "crowd": None}
    return {"now": now, "venue": venue, "span": span,
            "window": {"start": start, "end": end, "entry_cutoff_ms": cutoff, "entry_open": end - now > cutoff,
                       "seconds_left": max(0, (end - now) // 1000)},
            "market": None if mid is None else {"yes_bid": mid - 0.01, "yes_ask": mid + 0.01, "no_bid": 1 - mid - 0.01,
                                                "no_ask": 1 - mid + 0.01, "mid_up": mid, "strike": strike,
                                                "fresh": fresh},
            "call": {"action": action, "lean": None, "agree": {"n": agree, "of": len(leans)}},
            "signals": [{"key": k, "lean": v} for k, v in leans.items()],
            "btc": {"btc": btc, "strike": strike, "gap": None if btc is None or strike is None else btc - strike},
            "exit": {"rule": "ride to settlement", "take_profit": 0.99, "stop_gap": -10.0, "near_stop_usd": 20.0},
            "positions": positions or []}


def pos(advice: str = "HOLD", held: float = 5.0, pid: int = 7, *, side: str = "UP", stop_level: float | None = None,
        take_profit: float | None = 0.99, bid: float | None = 0.60) -> dict:
    return {"id": pid, "side": side, "held": held, "window_end": W1, "stop_level": stop_level,
            "take_profit": take_profit, "advice": {"action": advice, "bid": bid}}


def snap(*views: dict) -> Snapshot:
    return Snapshot.from_views(list(views))


def types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def run_wake(ev: AgentEvents, now: int, end_after_ms: int = 1_000) -> dict:
    w = ev.begin_wake(now)
    ev.end_wake(now + end_after_ms)
    return w


def started(now: int = W0 + 60_000, **kw) -> AgentEvents:
    ev = AgentEvents(EventsConfig(**kw))
    ev.observe(now, snap(view(now)))  # the first observation sets the baselines and emits nothing
    return ev


# -- coalescing and gaps (AE5) ---------------------------------------------------------------------------------------

def test_200_price_ticks_in_3s_without_a_position_wake_at_most_once():
    t0 = W0 + 60_000
    ev = started(t0)
    wakes = 0
    for i in range(200):  # mid wanders 0.50 -> 0.70 and back, 15 ms apart
        now = t0 + 1 + i * 15
        mid = 0.50 + 0.20 * (i if i < 100 else 200 - i) / 100
        ev.observe(now, snap(view(now, mid=round(mid, 2))))
        if ev.should_wake(now):
            run_wake(ev, now)
            wakes += 1
    for now in range(t0 + 3_000, t0 + 20_000, 1_000):  # then quiet
        ev.observe(now, snap(view(now, mid=0.70)))
        if ev.should_wake(now):
            run_wake(ev, now)
            wakes += 1
    assert wakes == 1


def test_urgent_fill_2s_after_a_wake_wakes_at_the_5s_urgent_gap_not_the_30s_minimum():
    t0 = W0 + 60_000
    ev = started(t0, min_gap_ms=30_000, urgent_gap_ms=5_000)
    ev.push(t0, "price_move", venue="kalshi", span="15m", value={"mid_up": 0.6})
    ev.begin_wake(t0)
    ev.end_wake(t0 + 1_000)
    ev.push(t0 + 3_000, "fill", venue="kalshi", span="15m", value={"id": 7})  # 2 s after the wake ended
    assert not ev.should_wake(t0 + 5_999)
    assert ev.should_wake(t0 + 6_000)
    w = ev.begin_wake(t0 + 6_000)
    assert types(w["events"]) == ["fill"] and w["events"][0]["class"] == "urgent"


def test_normal_events_wait_for_3s_quiet_and_the_minimum_gap():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.push(t0, "call_changed", venue="kalshi", span="15m", value={"action": "ENTER UP"})
    assert not ev.should_wake(t0 + 2_999)
    assert ev.should_wake(t0 + 3_000)
    run_wake(ev, t0 + 3_000)  # ends at t0 + 4_000
    ev.push(t0 + 5_000, "call_changed", venue="kalshi", span="15m", value={"action": "WAIT"})
    assert not ev.should_wake(t0 + 33_999)
    assert ev.should_wake(t0 + 34_000)


def test_normal_events_that_never_go_quiet_wake_10s_after_the_first():
    t0 = W0 + 60_000
    ev = started(t0)
    for i in range(0, 10_000, 1_000):
        ev.push(t0 + i, "price_move", venue="kalshi", span="15m", value={"mid_up": 0.5 + i / 100_000})
        assert not ev.should_wake(t0 + i)
    ev.push(t0 + 10_000, "price_move", venue="kalshi", span="15m", value={"mid_up": 0.61})
    assert ev.should_wake(t0 + 10_000)


def test_never_two_wakes_at_once():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.push(t0, "human_note", value={"text": "stop after this one"})
    ev.begin_wake(t0)
    ev.push(t0 + 10_000, "order_refused", value={"reason": "over the cap"})
    ev.push(t0 + 10_000, "window_ready", venue="kalshi", span="15m")
    assert not ev.should_wake(t0 + 60_000)


def test_wake_events_are_coalesced_by_type_and_market_keeping_first_and_latest_and_serialise_to_json():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, snap(view(t0), view(t0, venue="polymarket", span="5m")))
    ev.push(t0, "price_move", venue="kalshi", span="15m", value={"mid_up": 0.56})
    ev.push(t0 + 500, "price_move", venue="kalshi", span="15m", value={"mid_up": 0.61})
    ev.push(t0 + 600, "price_move", venue="polymarket", span="5m", value={"mid_up": 0.40})
    w = ev.begin_wake(t0 + 4_000)
    km = next(e for e in w["events"] if e["venue"] == "kalshi")
    assert len(w["events"]) == 2
    assert km == {"type": "price_move", "class": "normal", "venue": "kalshi", "span": "15m",
                  "first": {"mid_up": 0.56}, "latest": {"mid_up": 0.61}, "first_ms": t0, "latest_ms": t0 + 500,
                  "count": 2}
    assert json.loads(json.dumps(w)) == w


# -- hourly cap ------------------------------------------------------------------------------------------------------

def test_at_the_wake_cap_normal_events_are_logged_skipped_and_urgent_events_still_wake():
    t0 = W0 + 60_000
    ev = started(t0, max_wakes_per_hour=2, min_gap_ms=0, urgent_gap_ms=0)
    for i in range(2):
        ev.push(t0 + i * 10_000, "human_note", value={"n": i})
        assert ev.should_wake(t0 + i * 10_000)
        run_wake(ev, t0 + i * 10_000)
    ev.push(t0 + 20_000, "call_changed", venue="kalshi", span="15m", value={"action": "ENTER UP"})
    assert not ev.should_wake(t0 + 30_000)
    assert ev.skipped[-1]["reason"] == "hourly wake cap" and types(ev.skipped[-1]["events"]) == ["call_changed"]
    ev.push(t0 + 31_000, "fill", venue="kalshi", span="15m", value={"id": 7})
    assert ev.should_wake(t0 + 31_000)
    assert types(ev.begin_wake(t0 + 31_000)["events"]) == ["fill"]


# -- drop rules ------------------------------------------------------------------------------------------------------

def test_no_position_and_entry_closed_everywhere_with_only_price_events_never_wakes():
    late = W1 - 50_000  # past the 60 s entry cutoff
    ev = started(late)
    wakes = 0
    for i in range(40):
        now = late + 1_000 * (i + 1)
        ev.observe(now, snap(view(now, mid=0.50 + 0.01 * i, btc=100_100.0 + 50 * i)))
        if ev.should_wake(now):
            run_wake(ev, now)
            wakes += 1
    assert wakes == 0
    assert ev.skipped and ev.skipped[-1]["reason"] == "no position and entry closed"


def test_with_the_budget_used_up_and_no_position_only_a_new_window_wakes():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.push(t0, "call_changed", venue="kalshi", span="15m", value={"action": "ENTER UP"})
    assert not ev.should_wake(t0 + 5_000, budget_exhausted=True)
    ev.observe(W1 + 1_000, snap(view(W1 + 1_000, start=W1)))
    assert ev.should_wake(W1 + 4_000, budget_exhausted=True)
    assert types(ev.begin_wake(W1 + 4_000)["events"]) == ["window_ready"]


# -- diff ------------------------------------------------------------------------------------------------------------

def test_a_new_window_at_second_0_with_no_quote_does_not_wake():
    ev = started(W0 + 60_000)
    for now in range(W1, W1 + 8_000, 1_000):  # the new window: no Kalshi quote and no strike yet
        ev.observe(now, snap(view(now, start=W1, mid=None, strike=None)))
        assert not ev.should_wake(now)
    assert ev.pending() == []


def test_a_stale_quote_is_not_ready():
    ev = started(W0 + 60_000)
    for now in range(W1, W1 + 8_000, 1_000):
        ev.observe(now, snap(view(now, start=W1, fresh=False)))
        assert not ev.should_wake(now)
    assert ev.pending() == []


def test_quote_and_strike_arriving_wake_exactly_once_with_window_ready():
    ev = started(W0 + 60_000)
    ev.observe(W1, snap(view(W1, start=W1, mid=None, strike=None)))
    ev.observe(W1 + 3_000, snap(view(W1 + 3_000, start=W1, mid=0.50, strike=None)))  # a quote, no strike yet
    assert ev.pending() == []
    wakes = []
    for now in range(W1 + 6_000, W1 + 120_000, 1_000):
        ev.observe(now, snap(view(now, start=W1)))
        if ev.should_wake(now):
            wakes.append(run_wake(ev, now))
    assert [types(w["events"]) for w in wakes] == [["window_ready"]]
    e = wakes[0]["events"][0]
    assert e["venue"] == "kalshi" and e["latest"]["start"] == W1 and e["latest"]["strike"] == 100_000.0
    assert e["latest"]["seconds_to_close"] == 894 and e["latest"]["seconds_to_entry_cutoff"] == 834


def test_entry_check_fires_once_at_the_lead_before_the_markets_entry_cutoff_when_flat():
    kalshi_cutoff = 120_000  # Kalshi 15m: entries close 2 min before the end -> the check is 5 min before the end
    ev = AgentEvents(EventsConfig())
    ev.observe(W1 - 400_000, snap(view(W1 - 400_000, cutoff=kalshi_cutoff)))
    seen, wakes = [], []
    for now in range(W1 - 399_000, W1 - 100_000, 1_000):
        ev.observe(now, snap(view(now, cutoff=kalshi_cutoff)))
        seen += [e["first_ms"] for e in ev.pending() if e["type"] == "entry_check"]
        if ev.should_wake(now):
            wakes.append(run_wake(ev, now))
    assert set(seen) == {W1 - 300_000}
    assert [types(w["events"]) for w in wakes] == [["entry_check"]]


def test_entry_check_lead_is_configurable():
    ev = AgentEvents(EventsConfig(entry_check_lead_ms=90_000))
    ev.observe(W1 - 400_000, snap(view(W1 - 400_000)))
    ev.observe(W1 - 151_000, snap(view(W1 - 151_000)))
    assert ev.pending() == []
    ev.observe(W1 - 150_000, snap(view(W1 - 150_000)))
    assert types(ev.pending()) == ["entry_check"]


def test_no_entry_check_while_holding_a_position_there():
    ev = AgentEvents(EventsConfig())
    ev.observe(W1 - 400_000, snap(view(W1 - 400_000, positions=[pos()])))
    for now in range(W1 - 130_000, W1 - 100_000, 1_000):
        ev.observe(now, snap(view(now, positions=[pos()])))
    assert "entry_check" not in types(ev.pending())


def test_flat_small_price_and_light_changes_never_wake():
    t0 = W0 + 60_000
    ev = started(t0)
    wakes = 0
    for i in range(1, 240):  # 4 minutes: mid +-5c, BTC +-$100, lights flipping, the call steady
        now = t0 + i * 1_000
        mid = 0.50 + (0.05 if i % 20 < 10 else -0.05)
        lean = "UP" if i % 30 < 15 else "DOWN"
        ev.observe(now, snap(view(now, mid=mid, btc=100_100.0 + (100 if i % 40 < 20 else -100),
                                  leans={"market": lean, "model": lean, "crowd": None})))
        if ev.should_wake(now):
            run_wake(ev, now)
            wakes += 1
    assert wakes == 0


def test_flat_btc_crossing_the_strike_does_not_wake():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, btc=99_990.0)))
    assert ev.pending() == []


def test_flat_large_move_wakes():
    t0 = W0 + 60_000
    ev = started(t0, flat_mid_move=0.10)
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, mid=0.60)))
    assert types(ev.pending()) == ["price_move"]
    ev2 = started(t0)
    ev2.observe(t0 + 1_000, snap(view(t0 + 1_000, btc=100_250.0)))
    assert types(ev2.pending()) == ["price_move"]


def test_flat_call_side_flip_wakes_once_it_holds():
    t0 = W0 + 60_000
    ev = started(t0, signal_hold_ms=5_000)
    for i in range(1, 4):  # a blip that reverts: nothing
        ev.observe(t0 + i * 1_000, snap(view(t0 + i * 1_000, action="ENTER UP")))
    ev.observe(t0 + 4_000, snap(view(t0 + 4_000)))
    assert ev.pending() == []
    wakes = []
    for i in range(5, 40):
        now = t0 + i * 1_000
        ev.observe(now, snap(view(now, action="ENTER UP", agree=2)))
        if ev.should_wake(now):
            wakes.append(run_wake(ev, now))
    assert [types(w["events"]) for w in wakes] == [["call_changed"]]
    assert wakes[0]["events"][0]["latest"] == {"action": "ENTER UP", "from": "WAIT", "agree": 2}


def test_defaults_a_flat_call_flip_held_30s_does_not_wake_and_one_held_60s_does():
    cfg = EventsConfig()
    assert cfg.flat_mid_move == 0.20 and cfg.signal_hold_ms == 60_000
    t0 = W0 + 60_000
    ev = started(t0)
    for i in range(1, 31):  # ENTER UP for 30 s, then back to WAIT: noise
        ev.observe(t0 + i * 1_000, snap(view(t0 + i * 1_000, action="ENTER UP", agree=2)))
    ev.observe(t0 + 31_000, snap(view(t0 + 31_000)))
    assert ev.pending() == []
    t1 = t0 + 32_000
    for i in range(0, 60):  # ENTER UP again, held 59 s: still nothing
        ev.observe(t1 + i * 1_000, snap(view(t1 + i * 1_000, action="ENTER UP", agree=2)))
    assert ev.pending() == []
    ev.observe(t1 + 60_000, snap(view(t1 + 60_000, action="ENTER UP", agree=2)))  # held 60 s
    assert types(ev.pending()) == ["call_changed"]


def test_an_agree_count_change_alone_is_not_a_call_change():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, snap(view(t0, action="ENTER UP", agree=1)))
    for i in range(1, 20):
        ev.observe(t0 + i * 1_000, snap(view(t0 + i * 1_000, action="ENTER UP", agree=1 + i % 2)))
    assert ev.pending() == []


def test_holding_btc_near_the_strike_wakes():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, snap(view(t0, positions=[pos()])))
    wakes = []
    for i in range(1, 20):
        now = t0 + i * 1_000
        ev.observe(now, snap(view(now, btc=100_020.0, positions=[pos()])))
        if ev.should_wake(now):
            wakes.append(run_wake(ev, now))
    assert [types(w["events"]) for w in wakes] == [["btc_near_strike"]]
    assert wakes[0]["events"][0]["latest"]["gap"] == 20.0


def test_holding_in_the_last_90s_is_an_urgent_wake():
    ev = AgentEvents(EventsConfig())
    ev.observe(W1 - 120_000, snap(view(W1 - 120_000, positions=[pos()])))
    run_wake(ev, W1 - 120_000)
    ev.observe(W1 - 91_000, snap(view(W1 - 91_000, positions=[pos()])))
    assert not ev.should_wake(W1 - 91_000)
    ev.observe(W1 - 90_000, snap(view(W1 - 90_000, positions=[pos()])))
    assert ev.should_wake(W1 - 90_000)  # urgent: no 30 s minimum gap, no settling
    w = ev.begin_wake(W1 - 90_000)
    assert w["reason"] == "urgent" and types(w["events"]) == ["window_ending"]


def test_flat_enter_to_wait_wakes_too():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig(signal_hold_ms=5_000))
    ev.observe(t0, snap(view(t0, action="ENTER DOWN", agree=3)))
    for i in range(1, 7):
        ev.observe(t0 + i * 1_000, snap(view(t0 + i * 1_000, action="WAIT")))
    (e,) = ev.pending()
    assert e["type"] == "call_changed" and e["latest"] == {"action": "WAIT", "from": "ENTER DOWN", "agree": None}


def test_holding_btc_nearing_plays_own_stop_rule_wakes_urgently_once():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, snap(view(t0, btc=100_100.0, positions=[pos()])))  # no stop set: Play's rule, strike - $10
    run_wake(ev, t0)
    ev.observe(t0 + 10_000, snap(view(t0 + 10_000, btc=100_040.0, positions=[pos()])))  # $50 from 99,990
    assert "near_stop" not in types(ev.pending())
    ev.observe(t0 + 11_000, snap(view(t0 + 11_000, btc=100_008.0, positions=[pos()])))  # $18: inside $20
    got = {e["type"]: e for e in ev.pending()}
    assert got["near_stop"]["class"] == "urgent"
    assert got["near_stop"]["latest"]["stop_level"] == 99_990.0 and got["near_stop"]["latest"]["stop_set"] is False
    assert got["near_stop"]["latest"]["distance"] == 18.0
    assert ev.should_wake(t0 + 11_000)
    run_wake(ev, t0 + 11_000)
    for i in range(12, 40):  # hovering at the edge of the band: no repeat
        now = t0 + i * 1_000
        ev.observe(now, snap(view(now, btc=100_008.0 + (5 if i % 2 else -5), positions=[pos()])))
    assert "near_stop" not in types(ev.pending())


def test_holding_btc_nearing_a_set_stop_wakes():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    p = pos(stop_level=100_050.0)
    ev.observe(t0, snap(view(t0, btc=100_100.0, positions=[p])))
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, btc=100_065.0, positions=[p])))
    (e,) = [e for e in ev.pending() if e["type"] == "near_stop"]
    assert e["latest"]["stop_level"] == 100_050.0 and e["latest"]["stop_set"] is True


def test_flat_btc_nearing_the_stop_rule_does_not_wake():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, btc=100_005.0)))
    assert ev.pending() == []


def test_advice_back_to_hold_is_a_normal_wake():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, snap(view(t0, positions=[pos("CONSIDER EXIT")])))
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, positions=[pos("HOLD")])))
    (e,) = ev.pending()
    assert e["type"] == "advice_cleared" and e["class"] == "normal"


def test_holding_the_take_profit_nearing_wakes_once():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, snap(view(t0, positions=[pos(bid=0.80)])))
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, positions=[pos(bid=0.93)])))
    assert ev.pending() == []
    ev.observe(t0 + 2_000, snap(view(t0 + 2_000, positions=[pos(bid=0.95)])))
    (e,) = ev.pending()
    assert e["type"] == "near_take_profit" and e["latest"]["take_profit"] == 0.99 and e["latest"]["bid"] == 0.95
    run_wake(ev, t0 + 5_000)
    ev.observe(t0 + 9_000, snap(view(t0 + 9_000, positions=[pos(bid=0.90)])))
    ev.observe(t0 + 10_000, snap(view(t0 + 10_000, positions=[pos(bid=0.96)])))
    assert ev.pending() == []


def test_a_single_market_session_never_wakes_for_another_market():
    t0 = W0 + 60_000
    ev = started(t0)  # the session's snapshot holds kalshi 15m only
    ev.push(t0, "price_move", venue="polymarket", span="15m", value={"mid_up": 0.9})
    ev.push(t0, "window_ready", venue="polymarket", span="5m")
    ev.set_alarms([{"kind": "mid_above", "value": 0.1, "venue": "polymarket", "span": "15m"}])
    for now in range(t0 + 1_000, t0 + 60_000, 1_000):
        ev.observe(now, snap(view(now)))
        assert not ev.should_wake(now)
    assert ev.pending() == []


def test_schedule_gives_each_markets_seconds_to_entry_cutoff_and_close():
    kalshi_cutoff = 120_000
    now = W0 + 300_000
    ev = AgentEvents(EventsConfig())
    ev.observe(now, snap(view(now, cutoff=kalshi_cutoff)))
    (s,) = ev.schedule(now).values()
    assert s["seconds_to_close"] == 600 and s["seconds_to_entry_cutoff"] == 480
    assert s["holding"] is False and s["entry_open"] is True and s["quote_ready"] is True
    assert s["next_decision_point"] == {"type": "entry_check", "in_s": 300}
    ev.observe(W1 - 170_000, snap(view(W1 - 170_000, cutoff=kalshi_cutoff)))
    later = ev.schedule(W1 - 170_000)[("kalshi", "15m")]
    assert later["next_decision_point"] == {"type": "window_ready", "in_s": 170}


def test_holding_a_signal_flip_that_reverts_within_5s_emits_nothing_and_one_held_5s_emits_signal_changed():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig(signal_hold_ms=5_000))
    ev.observe(t0, snap(view(t0, positions=[pos()])))
    for i in range(1, 5):  # flips at t0+1 s, back at t0+5 s (held 4 s)
        ev.observe(t0 + i * 1_000, snap(view(t0 + i * 1_000, leans={"market": None, "model": "UP", "crowd": None},
                                             positions=[pos()])))
    ev.observe(t0 + 5_000, snap(view(t0 + 5_000, positions=[pos()])))
    assert ev.pending() == []
    for i in range(6, 12):  # model and crowd both flip and hold 5 s: one merged event
        now = t0 + i * 1_000
        ev.observe(now, snap(view(now, leans={"market": None, "model": "DOWN", "crowd": "DOWN"}, positions=[pos()])))
    (e,) = ev.pending()
    assert e["type"] == "signal_changed" and e["class"] == "normal"
    assert e["latest"] == {"model": "DOWN", "crowd": "DOWN"}


def test_holding_a_call_change_mid_move_and_btc_near_the_strike_are_diffed():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig(signal_hold_ms=5_000))
    ev.observe(t0, snap(view(t0, positions=[pos()])))
    for i in range(1, 7):  # the call's new side holds 5 s
        now = t0 + i * 1_000
        ev.observe(now, snap(view(now, mid=0.57, action="ENTER UP", agree=2, btc=100_010.0, positions=[pos()])))
    got = {e["type"]: e for e in ev.pending()}
    # BTC $10 over the strike is $20 from Play's stop rule (strike - $10): near_stop too
    assert set(got) == {"call_changed", "price_move", "btc_near_strike", "near_stop"}
    assert got["call_changed"]["latest"] == {"action": "ENTER UP", "from": "WAIT", "agree": 2}
    assert got["price_move"]["latest"]["mid_up"] == 0.57


def test_advice_becoming_consider_exit_is_urgent_and_window_ending_fires_once_with_a_position():
    t0 = W1 - 120_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, snap(view(t0, positions=[pos()])))
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, positions=[pos("CONSIDER EXIT")])))
    (e,) = ev.pending()
    assert e["type"] == "advice_changed" and e["class"] == "urgent"
    run_wake(ev, t0 + 1_000)
    for now in range(W1 - 90_000, W1 - 80_000, 1_000):
        ev.observe(now, snap(view(now, positions=[pos("CONSIDER EXIT")])))
    assert [(e["type"], e["count"]) for e in ev.pending()] == [("window_ending", 1)]


def test_only_the_sessions_own_positions_count():
    v = view(W0 + 60_000, positions=[pos(pid=7), pos(pid=8)])
    assert [p.id for p in Snapshot.from_views([v], position_ids={8}).positions] == [8]


def test_limit_hit_is_urgent_once_per_session():
    ev = started()
    ev.push(W0 + 61_000, "limit_hit", value={"which": "per_day"})
    run_wake(ev, W0 + 61_000)
    ev.push(W0 + 70_000, "limit_hit", value={"which": "per_day"})
    assert ev.pending() == []


def test_an_unknown_event_type_is_an_error():
    with pytest.raises(ValueError):
        started().push(W0, "fil")


# -- alarms ----------------------------------------------------------------------------------------------------------

def test_btc_below_alarm_fires_once_and_is_removed():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.set_alarms([{"kind": "btc_below", "value": 99_900.0}])
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, btc=99_950.0)))
    assert "agent_alarm" not in types(ev.pending()) and len(ev.alarms()) == 1
    ev.observe(t0 + 2_000, snap(view(t0 + 2_000, btc=99_899.0)))
    (e,) = [e for e in ev.pending() if e["type"] == "agent_alarm"]
    (fired,) = e["latest"]["fired"]
    assert fired["alarm"] == {"kind": "btc_below", "value": 99_900.0, "venue": None, "span": None}
    assert fired["observed"] == 99_899.0 and ev.alarms() == []
    run_wake(ev, t0 + 2_000)
    ev.observe(t0 + 3_000, snap(view(t0 + 3_000, btc=99_800.0)))
    assert "agent_alarm" not in types(ev.pending())


def test_mid_and_time_alarms_fire_on_their_market():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.set_alarms([{"kind": "mid_above", "value": 0.6, "venue": "kalshi", "span": "15m"},
                   {"kind": "at_time", "value": t0 + 5_000},
                   {"kind": "seconds_left_below", "value": 600, "venue": "kalshi", "span": "15m"}])
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, mid=0.62)))
    ev.observe(t0 + 5_000, snap(view(t0 + 5_000, mid=0.62)))
    assert [a["kind"] for a in ev.alarms()] == ["seconds_left_below"]
    ev.observe(W1 - 599_000, snap(view(W1 - 599_000, mid=0.62)))
    assert ev.alarms() == []


def test_alarms_are_validated_capped_at_10_and_cleared_at_session_end():
    ev = started()
    with pytest.raises(ValueError):
        ev.set_alarms([{"kind": "btc_under", "value": 1.0}])
    with pytest.raises(ValueError):
        ev.set_alarms([{"kind": "mid_above", "value": 0.6}])  # a mid alarm needs its market
    with pytest.raises(ValueError):
        ev.set_alarms([{"kind": "btc_above", "value": 1.0 + i} for i in range(11)])
    ev.set_alarms([{"kind": "btc_above", "value": 1e6}])
    ev.snooze(until_ms=W1)
    ev.end_session()
    assert ev.alarms() == [] and ev.snoozed() is None


# -- snooze ----------------------------------------------------------------------------------------------------------

def test_snooze_until_the_next_window_mutes_normal_events_but_an_urgent_fill_still_wakes():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.snooze(until_event="window_ready")
    ev.observe(t0 + 1_000, snap(view(t0 + 1_000, mid=0.62, action="ENTER UP")))
    assert not ev.should_wake(t0 + 20_000)
    ev.push(t0 + 20_000, "fill", venue="kalshi", span="15m", value={"id": 7})
    assert ev.should_wake(t0 + 20_000)
    assert types(run_wake(ev, t0 + 20_000)["events"]) == ["fill"]
    ev.observe(t0 + 60_000, snap(view(t0 + 60_000, mid=0.40, action="ENTER DOWN")))
    assert not ev.should_wake(t0 + 100_000)
    ev.observe(W1 + 1_000, snap(view(W1 + 1_000, start=W1, mid=0.40, action="ENTER DOWN")))
    assert ev.should_wake(W1 + 4_000)
    assert "window_ready" in types(ev.begin_wake(W1 + 4_000)["events"])
    assert ev.snoozed() is None


def test_snooze_until_a_time_ends_at_that_time():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.snooze(until_ms=t0 + 60_000)
    ev.push(t0 + 1_000, "call_changed", venue="kalshi", span="15m", value={"action": "ENTER UP"})
    assert not ev.should_wake(t0 + 10_000)
    ev.push(t0 + 60_000, "call_changed", venue="kalshi", span="15m", value={"action": "WAIT"})
    assert ev.should_wake(t0 + 63_000)


# -- during a wake ---------------------------------------------------------------------------------------------------

def test_an_urgent_event_during_a_wake_is_taken_mid_wake_and_not_repeated_in_the_next():
    t0 = W0 + 60_000
    ev = started(t0)
    ev.push(t0, "human_note", value={"text": "hi"})
    ev.begin_wake(t0)
    ev.push(t0 + 2_000, "order_refused", venue="kalshi", span="15m", value={"reason": "stale quote"})
    ev.push(t0 + 2_000, "call_changed", venue="kalshi", span="15m", value={"action": "ENTER UP"})
    assert types(ev.take_urgent_during_wake()) == ["order_refused"]
    assert ev.take_urgent_during_wake() == []
    ev.end_wake(t0 + 3_000)
    assert ev.should_wake(t0 + 33_000)
    assert types(ev.begin_wake(t0 + 33_000)["events"]) == ["call_changed"]


# -- idle check ------------------------------------------------------------------------------------------------------

def test_a_position_open_with_no_wake_for_10_minutes_gets_an_idle_check():
    t0 = W0 + 10_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, snap(view(t0, positions=[pos()])))
    run_wake(ev, t0)
    assert not ev.should_wake(t0 + 1_000 + 599_999)
    assert ev.should_wake(t0 + 1_000 + 600_000)
    assert types(ev.begin_wake(t0 + 601_000)["events"]) == ["idle_check"]


# -- rule positions: ride to settlement, no take-profit order (2026-10-30) --------------------------------------------

def rsnap(*views: dict, rule_ids=frozenset({7})) -> Snapshot:
    return Snapshot.from_views(list(views), rule_ids=set(rule_ids))


def test_a_rule_position_never_wakes_near_take_profit():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    # as now_view shows it (no take-profit order: take_profit None) and, defensively, a stray 99c
    for tp in (None, 0.99):
        ev.observe(t0, rsnap(view(t0, positions=[pos(bid=0.80, take_profit=tp)])))
        ev.observe(t0 + 1_000, rsnap(view(t0 + 1_000, positions=[pos(bid=0.97, take_profit=tp)])))
        assert "near_take_profit" not in types(ev.pending())


def test_a_rule_position_keeps_near_stop_and_window_ending():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig())
    ev.observe(t0, rsnap(view(t0, btc=100_100.0, positions=[pos(stop_level=100_000.0)])))
    ev.observe(t0 + 1_000, rsnap(view(t0 + 1_000, btc=100_015.0, positions=[pos(stop_level=100_000.0)])))
    assert "near_stop" in types(ev.pending())
    end = W1 - 60_000
    ev.observe(end, rsnap(view(end, btc=100_015.0, positions=[pos(stop_level=100_000.0)])))
    assert "window_ending" in types(ev.pending())


def test_lights_going_to_none_never_wake_while_holding_a_rule_position():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig(signal_hold_ms=5_000))
    start = {"market": "UP", "model": "UP", "crowd": "UP"}
    ev.observe(t0, rsnap(view(t0, leans=start, positions=[pos()])))
    for i in range(1, 8):  # every light goes to None (SKIP) and stays there
        now = t0 + i * 1_000
        ev.observe(now, rsnap(view(now, leans={"market": None, "model": None, "crowd": None}, positions=[pos()])))
    assert ev.pending() == []
    for i in range(8, 15):  # a real flip still wakes
        now = t0 + i * 1_000
        ev.observe(now, rsnap(view(now, leans={"market": None, "model": "DOWN", "crowd": None}, positions=[pos()])))
    (e,) = ev.pending()
    assert e["type"] == "signal_changed" and e["latest"] == {"model": "DOWN"}


def test_lights_going_to_none_still_wake_for_a_position_the_agent_opened_itself():
    t0 = W0 + 60_000
    ev = AgentEvents(EventsConfig(signal_hold_ms=5_000))
    start = {"market": "UP", "model": "UP", "crowd": "UP"}
    ev.observe(t0, rsnap(view(t0, leans=start, positions=[pos()]), rule_ids=()))
    for i in range(1, 8):
        now = t0 + i * 1_000
        ev.observe(now, rsnap(view(now, leans={"market": None, "model": None, "crowd": None}, positions=[pos()]),
                              rule_ids=()))
    assert types(ev.pending()) == ["signal_changed"]
