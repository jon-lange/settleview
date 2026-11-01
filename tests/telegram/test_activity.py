"""The Telegram remote's activity commentary, digest, daily summary and the richer /status (application/notifier.py),
with a real AgentSessions on the test store, journal and bracket rows written as the runner / trading services write
them, and the fake Messenger (no network). Local times are UTC here (`tz`)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC

from btc_signal_engine.application.agent_presets import AgentPresets
from btc_signal_engine.application.agent_sessions import AgentSessions
from btc_signal_engine.application.notifier import ACTIVITY_GAP_MS, DIGEST_MS, FILL_WAIT_MS, RemoteControl
from tests.telegram.fakes import Clock, FakeMessenger, message, pair
from tests.trading.helpers import NOW

SID = "sess0001"
WINDOW_END = NOW + 600_000
LAUNCH = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 5.0, "per_trade": 1.0, "minutes": 120,
          "spend_cap": 2.0, "model": "claude-haiku-5-5", "rules": ["CUSHION100"], "level": "auto"}


def run(coro):
    return asyncio.run(coro)


def msg(head: str, *details: str) -> str:
    """A message as the remote sends it: the head line, then the details in an expandable quote."""
    return head + ("\n<blockquote expandable>" + "\n".join(details) + "</blockquote>" if details else "")


def both(*messages: str) -> str:
    return "\n\n".join(messages)


ENTRY = msg("🔵 <b>Bought Up $1 at 86¢</b> · Kalshi 15m agent",  # every alert names its agent (multi-agent)
            "CUSHION100 · BTC +$118 vs strike · 6:40 left · stop BTC $76,560")


class Svc:
    mode = "shadow"

    def killed(self) -> bool:
        return False

    def present(self, now: int) -> bool:
        return False


class World:
    def __init__(self, store, *, launch=True, view=None, presets=None, **kw):
        self.store = store
        self.clock = Clock()
        self.view = view if view is not None else {"btc": 76_718.0, "strike": 76_600.0, "window_end": WINDOW_END}
        self.c = RemoteControl(store, now_fn=self.clock, configured=True, tz=UTC, presets=presets,
                               view_fn=lambda key, now: dict(self.view) if self.view else None, **kw)
        self.ag = AgentSessions(store, {"you": Svc(), "you_poly": Svc()}, id_fn=lambda: SID,
                                notify_fn=self.c.on_session)
        self.c.sessions = self.ag
        self.m = FakeMessenger()
        self.c.messenger = self.m
        run(pair(self.c))
        self.pump()  # the baseline
        self.n0 = len(self.m.sent)
        if launch:
            assert self.ag.launch(LAUNCH, self.clock.t)["ok"]

    def pump(self, advance_ms: int = 0):
        self.clock.t += advance_ms
        run(self.c.pump())

    def new_sent(self) -> list[dict]:
        """The messages sent since the last look, without the pinned live status (its own tests look at it)."""
        pinned = {mid for _, mid in self.m.pins}
        out = [m for m in self.m.sent[self.n0:] if m["id"] not in pinned]
        self.n0 = len(self.m.sent)
        return out

    def new(self) -> list[str]:
        return [m["text"] for m in self.new_sent()]

    def journal(self, kind, data):
        self.ag.journal(kind, data, self.clock.t, session_id=SID)

    def bracket(self, bid, **kw):
        row = {"id": bid, "env": "shadow", "session_id": SID, "kind": "entry", "ticker": "KXBTC15M-T", "side": "UP",
               "window_end": WINDOW_END, "stake": 1.0, "contracts": 1.16, "limit_price": 0.9, "take_profit": 0.0,
               "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h", "state": "open", "filled": 0.0, "held": 0.0,
               "tp_filled": 0.0, "tp_fees": 0.0, "exit_fees": 0.0, "proceeds": 0.0, "created_ts": self.clock.t,
               "updated_ts": self.clock.t, "strategy": "you", "stop_index_below": 76_560.0}
        if self.store.select("trading_brackets", {"id": bid}):
            self.store.update("trading_brackets", {"id": bid}, {**kw, "updated_ts": self.clock.t})
        else:
            self.store.insert("trading_brackets", [{**row, **kw}], ignore_conflicts=False)

    def rule_entry(self, bid="b1", **inputs):
        """CUSHION100's entry as the runner writes it: the bracket, its rule row, then the fill."""
        self.bracket(bid)
        self.journal("rule", {"rule": "CUSHION100", "market": "kalshi_15m", "ok": True, "bracket_id": bid,
                              "inputs": {"gap": 118.0, "left_s": 400.0, "btc": 76_718.0, "strike": 76_600.0,
                                         **inputs}})
        self.journal("decision", {"action": "ENTRY", "source": "rule", "rule": "CUSHION100", "market": "kalshi_15m",
                                  "bracket_id": bid, "thesis": "[rule:CUSHION100] UP at ask 0.86 ..."})
        self.bracket(bid, filled=1.16, held=1.16, entry_avg=0.86, entry_cost=1.0, entry_fees=0.0024)


def test_a_session_start_is_told_once_with_its_limits(store):
    w = World(store)
    w.pump()
    (start,) = w.new_sent()
    assert start["text"] == msg("▶️ <b>Kalshi 15m agent started</b> · Auto · Kalshi 15 min",
                                "Max loss <b>$5</b> · $1 a trade · ends 19:05", "Rules: CUSHION100")
    assert start["silent"] is True
    w.ag.pause(w.ag.latest()["id"], w.clock.t, "The kill switch is on.", "kill_switch")
    w.pump(ACTIVITY_GAP_MS)
    (paused,) = w.new_sent()  # a resume is not a second start
    assert paused["text"] == "⏸ <b>Paused</b> · Kalshi 15m agent · The kill switch is on."
    assert paused["silent"] is False
    w.ag.resume(w.ag.latest()["id"], w.clock.t)
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == []


def test_a_rule_entry_reads_like_play_and_replaces_the_trades_fill_line(store):
    w = World(store)  # trades and activity both on (the defaults): one message for the entry
    w.pump()
    w.new()
    w.clock.t += ACTIVITY_GAP_MS
    w.rule_entry()
    w.pump()
    (entry,) = w.new_sent()
    assert entry["text"] == ENTRY and entry["silent"] is False


def test_a_settlement_is_one_message_with_the_session_net_and_never_waits(store):
    w = World(store)
    w.pump()
    w.rule_entry()
    w.pump()  # the entry waits in the outbox (the start message just went)
    assert len(w.new()) == 1
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16, held=0.0)
    w.pump(1_000)
    # the close goes at once, carrying the waiting entry line: one message, not a Filled + a Settled
    # the close's own news is the first line (the lock-screen preview)
    (close,) = w.new_sent()
    assert close["text"] == both(msg("🟢 <b>+$0.16</b> · Settled Up · Kalshi 15m agent",
                                     "CUSHION100 · Agent <b>+$0.16</b> · 1 trade · 1 won"), ENTRY)
    assert close["silent"] is False
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == []


def test_activity_off_and_trades_on_keeps_the_trades_lines(store):
    w = World(store)
    w.c.set_alerts({"activity": False}, NOW)
    w.pump()
    w.rule_entry()
    w.pump()
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16)
    w.pump()
    assert w.new() == ["🔵 <b>Filled Up 1.16 at 86¢</b> · Kalshi 15 min",
                       "🟢 <b>+$0.16</b> · Settled Up · Kalshi 15 min"]


def test_a_burst_is_coalesced_and_sent_at_most_every_20_s(store):
    w = World(store)
    w.pump()
    assert len(w.new()) == 1  # the start: nothing sent before, so at once
    w.journal("rule", {"rule": "CUSHION100", "market": "kalshi_15m", "ok": False, "code": "over_budget",
                       "reason": "Over the session budget: this order could cost $1.00 and $0.40 of the $5.00 budget "
                                 "is left; nothing was sent."})
    w.journal("decision", {"action": "EXIT", "source": "agent", "side": "UP", "market": "kalshi_15m",
                           "thesis": "BTC lost the cushion in one minute. The stop is close."})
    w.pump(5_000)
    assert w.new() == []  # within 20 s of the last message: they wait
    w.pump(ACTIVITY_GAP_MS - 5_000)
    (burst,) = w.new_sent()
    assert burst["text"] == both(
        msg("⚠️ <b>CUSHION100 not sent</b> · Kalshi 15m agent",
            "Over the session budget: this order could cost $1.00 and $0.40 of the $5.00 budget is left."),
        msg("<b>Agent: exit Up</b> · Kalshi 15m agent", "BTC lost the cushion in one minute."))
    assert burst["silent"] is False  # a refused rule match notifies


def test_quiet_wakes_send_nothing(store):
    w = World(store)
    w.pump()
    w.new()
    for action, why in (("NO_TRADE", "NO_VALIDATED_EDGE"), ("NO_TRADE", "NO_MATERIAL_CHANGE"), ("HOLD", None)):
        w.journal("decision", {"action": action, "source": "agent", "pass_reason": why, "thesis": "Nothing new."})
    w.journal("decision", {"action": "UNRECORDED", "source": "runner"})
    w.journal("wake", {"status": "ok", "trigger": "price_move"})
    w.journal("wake_start", {"wake_id": 3})
    w.journal("tool", {"name": "snooze", "ok": True, "args": {"until_ms": NOW + 600_000}})
    w.journal("tool", {"name": "set_alarms", "ok": True, "args": {"alarms": []}})
    w.journal("rule", {"rule": "CUSHION100", "market": "kalshi_15m", "ok": False, "code": "proposed"})
    w.pump(ACTIVITY_GAP_MS)
    w.pump(DIGEST_MS)  # nor a digest for that hour
    assert w.new() == []


def test_stop_and_take_profit_moves_say_what_changed(store):
    w = World(store)
    w.pump()
    w.rule_entry()
    w.pump(ACTIVITY_GAP_MS)
    w.new()
    w.journal("tool", {"name": "set_btc_stop", "ok": True, "args": {"bracket_id": "b1", "level": 76_620.0,
                                                                     "note": "Locking in the cushion. More text."}})
    w.bracket("b1", stop_index_below=76_620.0)
    w.journal("tool", {"name": "set_take_profit", "ok": True, "args": {"bracket_id": "b1", "price": 0.95,
                                                                        "note": "Near the top."}})
    w.journal("tool", {"name": "set_btc_stop", "ok": False, "args": {"bracket_id": "b1", "level": 76_500.0}})
    w.pump(ACTIVITY_GAP_MS)
    (moves,) = w.new_sent()
    assert moves["text"] == both(msg("<b>Stop tightened to BTC $76,620</b> · Kalshi 15m agent", "Locking in the cushion."),
                                 msg("<b>Take profit moved to 95¢</b> · Kalshi 15m agent", "Near the top."))
    assert moves["silent"] is True


def test_the_agents_own_entry_carries_its_note(store):
    w = World(store)
    w.pump()
    w.new()
    w.bracket("b2", stop_index_below=None)
    w.journal("tool", {"name": "place_entry", "ok": True, "args": {"market": "kalshi_15m", "note": "Momentum and "
                                                                    "a wide gap. Wrong if BTC drops $50."},
                       "result": {"ok": True, "bracket": {"id": "b2"}}})
    w.bracket("b2", filled=2.0, entry_avg=0.5, entry_cost=1.02)
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == [msg("🔵 <b>Bought Up $1.02 at 50¢</b> · Kalshi 15m agent", "Momentum and a wide gap.",
                           "No stop")]


def test_a_fill_waits_briefly_for_its_rule_row(store):
    w = World(store)
    w.pump()
    w.new()
    w.clock.t += ACTIVITY_GAP_MS
    w.bracket("b1", filled=1.16, entry_avg=0.86, entry_cost=1.0)  # filled before the runner journaled its rule row
    w.pump(1_000)
    assert w.new() == []
    w.journal("rule", {"rule": "CUSHION100", "market": "kalshi_15m", "ok": True, "bracket_id": "b1",
                       "inputs": {"gap": 118.0, "left_s": 400.0}})
    w.pump(1_000)
    assert w.new() == [ENTRY]
    # with no row at all it is told without one after FILL_WAIT_MS
    w.clock.t += ACTIVITY_GAP_MS
    w.bracket("b3", filled=1.0, entry_avg=0.5, entry_cost=0.51, created_ts=w.clock.t)
    w.pump(1_000)
    assert w.new() == [] and w.c._outbox == []  # still waiting for a row
    w.pump(FILL_WAIT_MS)
    assert w.new() == [msg("🔵 <b>Bought Up $0.51 at 50¢</b> · Kalshi 15m agent", "Stop BTC $76,560")]


def test_heads_ups_near_the_stop_and_in_the_last_90_s_once_per_position(store):
    w = World(store)
    w.pump()
    w.rule_entry()  # BTC 76,718 at entry, stop 76,560: $158 away
    w.pump(ACTIVITY_GAP_MS)
    w.new()
    w.view["btc"] = 76_610.0  # $50 from the stop: more than 25% of $158 left
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == []
    w.view["btc"] = 76_590.0  # $30 left: under 25%
    w.pump(ACTIVITY_GAP_MS)
    (heads,) = w.new_sent()
    assert heads["text"] == msg("⚠️ <b>$30 from the stop</b> · Up $1 at 86¢ · Kalshi 15m agent",
                                "BTC $76,590 · stop BTC $76,560")
    assert heads["silent"] is True
    w.view["btc"] = 76_580.0
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == []  # once
    w.clock.t = WINDOW_END - 80_000
    w.view["btc"] = 76_650.0
    w.pump()
    (last,) = w.new_sent()
    assert last["text"] == msg("⚠️ <b>1:20 left · winning</b> · Up $1 at 86¢ · Kalshi 15m agent",
                               "BTC $76,650 · +$50 vs strike")
    assert last["silent"] is True
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == []


def test_the_hourly_digest_says_how_its_going_and_is_skipped_for_a_quiet_hour(store):
    w = World(store)
    w.pump()
    w.rule_entry()
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16)
    w.pump(ACTIVITY_GAP_MS)
    w.new()
    w.ag.note_usage(w.ag.latest()["id"], w.clock.t, api_usd=0.08)
    w.clock.t = NOW + DIGEST_MS
    w.pump()
    (digest,) = w.new_sent()
    # fleet-level (multi-agent KD8): the fleet's total, one line per agent inside the quote
    assert digest["text"] == msg("📊 Fleet <b>+$0.16</b> · 17:05–18:05 · 1 trade",
                                 "Kalshi 15m agent +$0.16 · 1 trade · 1h 00m left", "Budget $0 / $5 · API $0.08")
    assert digest["silent"] is True
    assert digest["buttons"] == [[("Status", "v:st"), ("Lights", "v:li"), ("Stop…", "v:sx")]]
    w.pump(DIGEST_MS)  # nothing happened in hour 2
    assert w.new() == []


def test_the_end_summary_replaces_the_session_end_line(store):
    w = World(store)
    w.pump()
    w.rule_entry()
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16)
    w.journal("rule", {"rule": "CUSHION100", "market": "kalshi_15m", "ok": True, "bracket_id": "b2", "inputs": {}})
    w.bracket("b2", filled=1.0, entry_avg=0.7, entry_cost=0.71)
    w.pump(ACTIVITY_GAP_MS)
    w.new()
    w.ag.note_usage(w.ag.latest()["id"], w.clock.t, api_usd=0.12)
    w.ag.stop(w.ag.latest()["id"], w.clock.t, "time", "The session reached its end time.")
    w.pump(1_000)
    (end,) = w.new_sent()
    assert end["text"] == both(msg("⏹ <b>+$0.16</b> · Kalshi 15m agent ended · end time",
                                   "1 trade · 1 won · API $0.12", "CUSHION100 1/1 +$0.16",
                                   "1 position handed back to you"),
                               # it waited: it rides along
                               msg("🔵 <b>Bought Up $0.71 at 70¢</b> · Kalshi 15m agent", "CUSHION100 · stop BTC $76,560",
                                   "CUSHION100 this session: 1/1 +$0.16"))
    assert end["silent"] is False


def test_the_daily_summary_at_its_time_once_and_only_after_trades(store):
    w = World(store, launch=False)
    hh = int(re.match(r"(\d\d)", w.c.hm(NOW)).group(1))
    assert w.c.set_alerts({"daily_at": f"{hh + 1:02d}:00"}, NOW)["daily_at"] == f"{hh + 1:02d}:00"
    assert w.c.set_alerts({"daily_at": "25:00"}, NOW)["code"] == "invalid"
    assert w.c.set_alerts({"daily_at": "9:00"}, NOW)["code"] == "invalid"
    assert w.c.status()["daily_at"] == f"{hh + 1:02d}:00"
    # your own Play trade (no session) and an agent rule trade, both closed today
    w.store.insert("trading_brackets", [{
        "id": "mine", "env": "shadow", "session_id": None, "kind": "entry", "ticker": "T", "side": "DOWN",
        "window_end": NOW, "stake": 1.0, "contracts": 1.0, "limit_price": 0.6, "take_profit": 0.0, "stop": 0.0,
        "max_cost": 1.0, "ticket_hash": "h", "state": "closed", "filled": 1.0, "held": 0.0, "tp_filled": 0.0,
        "entry_avg": 0.6, "entry_cost": 0.61, "proceeds": 0.0, "exit_reason": "settled", "created_ts": NOW - 900_000,
        "updated_ts": NOW, "closed_ts": NOW, "strategy": "you"}], ignore_conflicts=False)
    w.ag.launch(LAUNCH, NOW)
    w.rule_entry()
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16, closed_ts=NOW)
    w.ag.note_usage(w.ag.latest()["id"], NOW, api_usd=0.05)
    w.ag.stop(w.ag.latest()["id"], NOW, "stopped")
    w.pump()
    w.new()
    w.clock.t = NOW + 3_600_000 - (NOW % 3_600_000) - 1_000  # a second before the time
    w.pump()
    assert w.new() == []
    w.pump(1_000)
    (daily,) = w.new_sent()
    assert daily["text"] == msg("🌙 <b>−$0.45</b> · Today · 2 trades · 1 won",
                                "1 session · API $0.05 · paper", "CUSHION100 1/1 +$0.16", "yours 0/1 −$0.61",
                                "By agent: Kalshi 15m agent 1/1 +$0.16",
                                "Best +$0.16 (CUSHION100 Up) · worst −$0.61 (yours Down)")
    assert daily["silent"] is True
    w.pump(60_000)
    again = RemoteControl(store, now_fn=w.clock, tz=UTC)  # a restart never sends it twice
    again.messenger, again.sessions = w.m, w.ag
    run(again.pump())
    assert w.new() == []


def test_the_daily_summary_is_skipped_when_nothing_traded(store):
    w = World(store, launch=False)
    w.c.set_alerts({"daily_at": "00:00"}, NOW)
    w.pump()
    assert w.new() == []


def test_status_with_a_running_session_is_one_block(store):
    w = World(store)
    w.rule_entry()
    w.ag.note_usage(w.ag.latest()["id"], w.clock.t, api_usd=0.08)
    run(w.c.handle_message(message("/status", sent_ts=w.clock.t)))
    # the fleet's /status (KD8): the total first, then one numbered line per agent with its positions
    assert w.m.texts()[-1] == msg("<b>1 agent</b> · total <b>+$0.00</b> · 0 trades",
                                  "1. <b>Kalshi 15m agent</b> · Holding · +$0.00 · 0 trades · ends 19:05",
                                  "   <b>Up $1 at 86¢</b> · CUSHION100 · Kalshi 15 min · BTC +$118 vs strike · "
                                  "stop BTC $76,560")


def test_status_with_no_session_tells_how_today_is_going(store):
    presets = AgentPresets(store, lambda spec, now: None, lambda: {"kalshi_15m": "shadow"})
    assert presets.save("Cushion auto", LAUNCH, NOW)["ok"]
    w = World(store, presets=presets)
    w.rule_entry()
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16, closed_ts=w.clock.t)
    w.ag.stop(w.ag.latest()["id"], w.clock.t, "stopped")
    run(w.c.handle_message(message("/status", sent_ts=w.clock.t)))
    assert w.m.texts()[-1] == msg("<b>No agent running</b> · today <b>+$0.16</b> · 1 trade · 1 won",
                                  "Last agent (Kalshi 15m agent) ended 17:05: +$0.16 · 1 trade",
                                  "Presets: Cushion auto (CUSHION100)")


def test_a_muted_agent_sends_no_trade_alerts_but_its_pause_still_comes(store):
    w = World(store)
    w.pump()
    w.new()
    assert w.c.set_muted(SID, True, w.clock.t) == {"ok": True, "muted": [SID]}
    assert w.c.set_muted("nope", True, w.clock.t)["code"] == "not_found"
    assert w.c.set_muted(SID, "yes", w.clock.t)["code"] == "invalid"
    w.rule_entry()
    w.pump(ACTIVITY_GAP_MS)
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16, held=0.0)
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == []  # neither the entry nor the close
    w.ag.pause(SID, w.clock.t, "The kill switch is on.", "kill_switch")
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == ["⏸ <b>Paused</b> · Kalshi 15m agent · The kill switch is on."]
    assert w.c.status()["muted"] == [SID]
    assert w.c.set_muted(SID, False, w.clock.t)["muted"] == []


def test_two_agents_on_opposite_sides_send_one_warning(store):
    w = World(store)
    w.pump()
    w.new()
    w.c.on_overlap({"strategy": "you", "market": "kalshi_15m", "ticker": "T", "window_end": 0,
                    "sides": {"UP": [{"session_id": SID, "name": "Kalshi 15m agent", "bracket_id": "a"}],
                              "DOWN": [{"session_id": "s2", "name": "Kalshi 15m agent 2", "bracket_id": "b"}]}})
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == [msg("⚠️ <b>Opposite sides</b> · Kalshi 15 min",
                           "Kalshi 15m agent Up vs Kalshi 15m agent 2 Down. Both stay open; a warning, not a block.")]
