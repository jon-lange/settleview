"""Assist proposals over Telegram: Approve takes the card's path (AgentProposals.approve: prepare -> OrderGate ->
approve, on the shadow exchange, no network) with the Telegram user as the human actor for that one approval; a
second or late tap answers "expired / already handled" and sends nothing; the message is edited in place. A real
AgentSessions / TradingService rig (tests/agent/test_rules.RuleRig), nobody at the Play screen."""

from __future__ import annotations

import asyncio
import re

from btc_signal_engine.adapters.http.agent_feed import AgUiFeed
from btc_signal_engine.adapters.http.agent_routes import AgentActions, KeyState
from btc_signal_engine.adapters.http.push import FeedHub
from btc_signal_engine.application.agent_fleet import AgentFleet
from btc_signal_engine.application.notifier import EXPIRED, RemoteControl
from tests.agent.test_rules import RuleRig
from tests.agent.test_tools import SID
from tests.telegram.fakes import USER, Clock, FakeMessenger, message, pair, tap
from tests.trading.helpers import NOW


def run(coro):
    return asyncio.run(coro)


class Rig:
    def __init__(self, store, present=False):
        self.r = RuleRig(store, level="assist")  # nobody at the screen unless `present`
        if present:
            self.r.svc.heartbeat(NOW)
        self.clock = Clock()
        feed = AgUiFeed(FeedHub(), lambda: {}, now_fn=self.clock)
        self.actions = AgentActions(sessions=self.r.ag, prompts=None, feed=feed, now_fn=self.clock, llm=None,
                                    fleet=AgentFleet(self.r.ag, self.r.play, lambda: self.r.runner,
                                                     clock=self.r.runner.clock),
                                    start_task=lambda *a: None, limits=None,
                                    proposals=self.r.proposals, key=KeyState(False))
        self.c = RemoteControl(store, now_fn=self.clock, sessions=self.r.ag, proposals=self.r.proposals,
                               actions=self.actions, configured=True)
        self.m = FakeMessenger()
        self.c.messenger = self.m
        self.r.proposals._notify = lambda n, v: (self.r.events_out.append((n, v)), self.c.on_proposal(n, v))
        run(pair(self.c))

    def propose(self) -> dict:
        self.r.tick(1, step=0)
        run(self.c.pump())
        (p,) = self.r.proposals.pending(SID)
        return p


def test_a_proposal_arrives_with_approve_and_skip_and_the_cards_line(store):
    g = Rig(store)
    p = g.propose()
    sent = g.m.sent[-1]
    assert sent["text"].startswith(f"🟣 <b>{p['line']}</b> · Kalshi 15 min · H4 · Kalshi 15m agent\n"
                                   "<blockquote expandable>")  # every alert names its agent
    assert sent["silent"] is False  # a proposal notifies
    data = [d for row in sent["buttons"] for _, d in row]
    assert [lab for row in sent["buttons"] for lab, _ in row] == ["Approve", "Skip"]
    assert data[0].startswith(f"a:{p['id']}:") and data[1].startswith(f"s:{p['id']}:")
    assert data[0].split(":")[2] == data[1].split(":")[2]  # one nonce per proposal message


def test_approve_from_telegram_goes_through_the_gate_with_the_telegram_user_as_the_actor(store):
    g = Rig(store)  # nobody at the Play screen: the card's own Approve would be refused not_present
    p = g.propose()
    msg_id = g.m.sent[-1]["id"]
    run(g.c.handle_tap(tap(g.m.button("Approve"), message_id=msg_id)))
    assert len(g.r.buys()) == 1  # one entry with its BTC stop, through prepare -> OrderGate -> approve
    (b,) = store.select("trading_brackets")
    assert b["session_id"] == SID and b["stop_index_below"] is not None
    (o,) = g.r.journal("proposal_outcome")
    assert o["state"] == "approved" and o["by"] == f"telegram:{USER}"
    approved = [a for a in store.select("trading_audit") if a["event"] == "approved"]
    assert approved and all(a["actor"] == f"telegram:{USER}" for a in approved)
    assert g.m.edits[-1]["id"] == msg_id and g.m.edits[-1]["text"] == f"<b>Approved</b> · {p['line']}"
    assert g.m.edits[-1]["buttons"] is None
    # the outcome event that follows does not edit the message again
    n = len(g.m.edits)
    run(g.c.pump())
    assert len(g.m.edits) == n


def test_the_telegram_presence_is_for_that_one_approval_only(store):
    g = Rig(store)
    p = g.propose()
    out = run(g.r.proposals.approve(p["id"], NOW + 1_000))  # the card's path, nobody at the screen
    assert out["ok"] is False and out["code"] == "not_present" and g.r.buys() == []


def test_a_second_tap_sends_nothing(store):
    g = Rig(store)
    g.propose()
    data = g.m.button("Approve")
    run(g.c.handle_tap(tap(data, callback_id="one")))
    run(g.c.handle_tap(tap(data, callback_id="two")))
    run(g.c.handle_tap(tap(g.m.button("Skip"), callback_id="three")))
    assert len(g.r.buys()) == 1
    assert ("two", EXPIRED) in g.m.answers and ("three", EXPIRED) in g.m.answers


def test_a_late_tap_sends_nothing(store):
    g = Rig(store)
    p = g.propose()
    g.clock.t = p["expires_ts"]
    run(g.c.handle_tap(tap(g.m.button("Approve"), callback_id="late")))
    assert g.r.buys() == [] and g.m.answers == [("late", EXPIRED)]


def test_a_forged_nonce_sends_nothing(store):
    g = Rig(store)
    p = g.propose()
    run(g.c.handle_tap(tap(f"a:{p['id']}:forged", callback_id="x")))
    assert g.r.buys() == [] and g.m.answers == [("x", EXPIRED)]
    assert g.r.proposals.pending(SID)  # still open for the real button


def test_skip_from_telegram_sends_nothing_and_answers_the_proposal(store):
    g = Rig(store)
    p = g.propose()
    run(g.c.handle_tap(tap(g.m.button("Skip"), message_id=g.m.sent[-1]["id"])))
    assert g.r.buys() == [] and g.r.proposals.pending(SID) == []
    (o,) = g.r.journal("proposal_outcome")
    assert o["state"] == "skipped" and o["by"] == f"telegram:{USER}"
    assert g.m.edits[-1]["text"] == f"<b>Skipped</b> · {p['line']}"


def test_a_proposal_answered_on_the_pc_is_edited_and_its_buttons_die(store):
    g = Rig(store, present=True)
    p = g.propose()
    data = g.m.button("Approve")
    assert run(g.r.proposals.skip(p["id"], NOW + 1_000))["ok"]  # the card's Skip
    run(g.c.pump())
    assert g.m.edits[-1]["text"] == f"<b>Skipped</b> · {p['line']}"
    run(g.c.handle_tap(tap(data, callback_id="after")))
    assert g.r.buys() == [] and ("after", EXPIRED) in g.m.answers


def test_stop_from_telegram_ends_the_live_session_at_once(store):
    g = Rig(store)
    g.propose()
    run(g.c.handle_message(message("/status")))
    lines = g.m.texts()[-1].split("\n")
    assert lines[0] == "<b>1 agent</b> · total <b>+$0.00</b> · 0 trades · ⚠️ 1 need you"  # its proposal waits
    assert re.fullmatch(r"<blockquote expandable>1\. <b>Kalshi 15m agent</b> · Proposal · \+\$0\.00 · 0 trades · "
                        r"ends \d\d:\d\d</blockquote>", lines[1])
    run(g.c.handle_message(message("/stop")))  # one agent running: stopped at once, as before
    assert g.r.ag.latest() is None and g.m.texts()[-1] == "Stopped Kalshi 15m agent."
    run(g.c.handle_message(message("/status")))
    lines = g.m.texts()[-1].split("\n")
    assert lines[0] == "<b>No agent running</b> · no trades today"
    assert re.fullmatch(r"<blockquote expandable>Last agent \(Kalshi 15m agent\) ended \d\d:\d\d: \+\$0\.00 · "
                        r"0 trades</blockquote>", lines[1])
    assert len(lines) == 2  # no presets object in this rig


def test_stop_works_while_paused_and_resume_is_refused(store):
    g = Rig(store)
    run(g.c.handle_message(message("/pause")))
    assert g.r.ag.latest()["state"] == "paused"
    run(g.c.handle_message(message("/resume")))
    assert g.r.ag.latest()["state"] == "paused" and g.m.texts()[-1] == "Resuming stays on the PC."
    run(g.c.handle_message(message("/stop")))
    assert g.r.ag.latest() is None


def test_a_note_from_telegram_is_journaled_as_the_telegram_user(store):
    g = Rig(store)
    run(g.c.handle_message(message("/note skip the next window")))
    (n,) = g.r.journal("note")
    assert n["text"] == "skip the next window" and n["by"] == f"telegram:{USER}"
