"""Telegram remote fixes from the 2026-10-31 review (docs/plans/2026-10-31-1800-review-fix-plan.md, B2): the bot's own
/stop hides only its own sessions' alerts, /note's agent number, a proposal tap that failed can be retried, HTML-safe
truncation, the pinned status throttled before it is built, one read of each session's stats per pump, and bounded
memory. Each test failed on main. Fakes only (no network)."""

from __future__ import annotations

import re

from btc_signal_engine.application import notifier as N
from btc_signal_engine.application.notifier import MAX_CHARS, Note, RemoteControl, compose
from tests.telegram.fakes import Clock, FakeActions, FakeMessenger, live_sessions, message, pair, tap
from tests.telegram.test_activity import run
from tests.telegram.test_approve import Rig
from tests.telegram.test_format import started
from tests.trading.helpers import NOW

A1, A2, A3 = "sess0001abcd", "sess0002abcd", "sess0003abcd"


def remote(store, n: int, **kw):
    ag = live_sessions(store, n)
    c = RemoteControl(store, now_fn=Clock(), sessions=ag, configured=True, **kw)
    ag._notify = c.on_session
    m = FakeMessenger()
    c.messenger = m
    run(pair(c))
    return c, m, ag


def test_a_stop_from_telegram_does_not_hide_another_agents_pause_meanwhile(store):
    c, m, ag = remote(store, 2)
    c.set_alerts({"activity": False, "pauses": True}, NOW)

    class Actions(FakeActions):
        async def stop(self, session_id):
            ag.pause(A2, NOW, "Mode changed.", "mode_changed")  # the other agent pauses while this stop runs
            return ag.stop(session_id, NOW)
    c.actions = Actions()
    run(c.handle_message(message("/stop 1")))
    k = len(m.sent)
    run(c.pump())
    assert m.texts()[k:] == ["⏸ <b>Paused</b> · Kalshi 15m agent 2 · Mode changed."]  # main: hidden as "own"


def test_note_with_one_agent_keeps_a_leading_number_as_its_text(store):
    c, m, ag = remote(store, 1)
    c.actions = acts = FakeActions()
    run(c.handle_message(message("/note 5 minutes left: hold")))
    assert acts.calls[-1] == ("note", A1, "5 minutes left: hold")  # main: "There is no agent 5"
    run(c.handle_message(message("/note #1 careful")))  # the explicit form works with one agent too
    assert acts.calls[-1] == ("note", A1, "careful")


def test_note_with_several_agents_takes_a_number_or_hash(store):
    c, m, ag = remote(store, 2)
    c.actions = acts = FakeActions()
    run(c.handle_message(message("/note 2 careful")))
    run(c.handle_message(message("/note #1 hold")))
    assert acts.calls == [("note", A2, "careful"), ("note", A1, "hold")]



def test_a_proposal_tap_that_failed_can_be_tapped_again(store):
    g = Rig(store)
    g.propose()
    data, mid = g.m.button("Approve"), g.m.sent[-1]["id"]
    real, tries = g.r.proposals.approve, []

    async def flaky(pid, now):
        tries.append(pid)
        if len(tries) == 1:
            raise RuntimeError("a database hiccup")
        return await real(pid, now)
    g.r.proposals.approve = flaky
    run(g.c.handle_tap(tap(data, message_id=mid, callback_id="one")))  # main: raised, and the entry was spent
    assert g.r.buys() == [] and "Tap again" in dict(g.m.answers)["one"]
    run(g.c.handle_tap(tap(data, message_id=mid, callback_id="two")))
    assert len(tries) == 2 and len(g.r.buys()) == 1 and g.m.edits[-1]["text"].startswith("<b>Approved</b>")
    run(g.c.handle_tap(tap(data, message_id=mid, callback_id="three")))
    assert len(g.r.buys()) == 1 and dict(g.m.answers)["three"] == N.EXPIRED  # answered: single use again


def test_a_head_over_the_limit_is_cut_on_text_never_inside_a_tag_or_an_entity():
    head = "<b>" + "Tom &amp; Jerry " * 400 + "</b> <i>tail</i>"
    t = compose([Note(head)])
    assert len(t) <= MAX_CHARS and t.endswith("…</b>")  # main: cut mid-text with the <b> left open
    assert t.count("<b>") == t.count("</b>") == 1 and "<i>" not in t
    assert not re.search(r"&[#a-zA-Z0-9]*…", t) and not re.search(r"<[^>]*$", t[:-4])
    for limit in range(5, 60):  # every cut point parses
        s = N.cut_html('<a href="https://x.example/?a=1&amp;b=2">A &lt;link&gt;</a> and <b>more</b>', limit)
        assert len(s) <= limit or s == "…", (limit, s)
        assert s.count("<a ") == s.count("</a>") and s.count("<b>") == s.count("</b>"), (limit, s)


def test_the_pinned_status_is_not_rebuilt_while_its_edit_is_throttled(store, monkeypatch):
    w = started(store)
    built = []
    real = N.FleetView.rows
    monkeypatch.setattr(N.FleetView, "rows", lambda self, now: built.append(now) or real(self, now))
    for _ in range(5):
        w.pump(1_000)
    assert built == []  # main: the fleet rows were built every pump, then thrown away by the throttle


def _brackets(store, sid, bid, **kw):
    row = {"id": bid, "env": "shadow", "session_id": sid, "kind": "entry", "ticker": "KXBTC15M-T", "side": "UP",
           "window_end": NOW + 600_000, "stake": 1.0, "contracts": 1.0, "limit_price": 0.6, "take_profit": 0.0,
           "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h", "state": "open", "filled": 1.0, "held": 1.0,
           "entry_avg": 0.6, "entry_cost": 0.6, "tp_filled": 0.0, "tp_fees": 0.0, "exit_fees": 0.0, "proceeds": 0.0,
           "created_ts": NOW, "updated_ts": NOW, "strategy": "you"}
    store.insert("trading_brackets", [{**row, **kw}], ignore_conflicts=False)


def test_closes_in_one_pump_read_each_sessions_stats_once(store):
    c, m, ag = remote(store, 3)
    for sid in (A1, A2, A3):
        _brackets(store, sid, f"b-{sid}")
    run(c.pump())  # the baseline
    for sid in (A1, A2, A3):
        store.update("trading_brackets", {"id": f"b-{sid}"}, {"state": "closed", "exit_reason": "settled",
                                                            "proceeds": 1.0, "held": 0.0, "updated_ts": NOW + 1})
    reads = []
    real = store.select

    def select(table, *a, **kw):
        where = a[0] if a else None
        if table == "trading_brackets" and isinstance(where, dict) and set(where) == {"session_id"} \
                and kw.get("order_by") == ["created_ts"]:
            reads.append(where["session_id"])
        return real(table, *a, **kw)

    store.select = select
    c.now_fn.t = NOW + 2
    run(c.pump())
    closes = [t for t in m.texts() if "Settled" in t]
    assert len(closes) >= 1 and "fleet" in closes[0]
    assert sorted(reads) == [A1, A2, A3]  # main: every close re-read every other agent's stats (9+ reads)


def test_taps_answered_proposals_and_closed_brackets_are_pruned(store):
    c, m, ag = remote(store, 1)
    c._taps.update({"old": {"kind": "pick", "name": "x", "expires_ts": NOW - 1},
                    "new": {"kind": "pick", "name": "y", "expires_ts": NOW + 60_000}})
    c._props.update({"p1": {"nonce": "n", "message_id": 1, "done": True, "edited": True},
                     "p2": {"nonce": "n", "message_id": 2, "done": False, "edited": False}})
    run(c.pump())  # the baseline
    c._brackets.update({"gone": (True, True), "still-open": (True, False)})
    run(c.pump())
    assert set(c._taps) == {"new"} and set(c._props) == {"p2"}  # main: kept forever
    assert "gone" not in c._brackets and c._brackets["still-open"] == (True, False)
