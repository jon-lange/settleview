"""The Telegram remote's application side (application/notifier.py) with a fake Messenger: pairing, who is answered,
stale messages, the commands (forbidden ones refused, /stop always, /launch only from a preset with a 60 s confirm),
the alert preferences and the alerts."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.application.agent_presets import AgentPresets
from btc_signal_engine.application.notifier import (CODE_TRIES, CODE_TTL_MS, CONFIRM_MS, DEFAULT_ALERTS, EXPIRED,
                                                    HELP, STALE_MS, RemoteControl)
from tests.telegram.fakes import (CHAT, OTHER, USER, Clock, FakeActions, FakeMessenger, live_sessions, message,
                                  pair, tap)
from tests.trading.helpers import NOW

SID1 = "sess0001abcd"


def run(coro):
    return asyncio.run(coro)


def remote(store, **kw) -> tuple[RemoteControl, FakeMessenger, Clock]:
    clock = Clock()
    kw.setdefault("configured", True)
    c = RemoteControl(store, now_fn=clock, **kw)
    m = FakeMessenger()
    c.messenger = m
    return c, m, clock


# -- pairing --------------------------------------------------------------------------------------------------------
def test_pairing_code_is_one_time_and_pairs_the_user_and_chat(store):
    c, m, clock = remote(store)
    code = c.new_code(NOW)
    assert code["ok"] and len(code["code"]) == 8 and code["expires_ts"] == NOW + CODE_TTL_MS
    run(c.handle_message(message(f"/start {code['code']}")))
    p = c.paired()
    assert p["user_id"] == USER and p["chat_id"] == CHAT and p["user_label"] == "@you"
    assert m.texts() == ["Paired. Proposals and trades will arrive here."]
    st = c.status()
    assert st["paired"] and st["user_label"] == "@you" and st["configured"] and st["running"] is False
    # the same code again (another user): nothing, no reply, the pairing unchanged
    run(c.handle_message(message(f"/start {code['code']}", user=OTHER)))
    assert c.paired()["user_id"] == USER and len(m.sent) == 1


def test_a_pairing_code_expires_after_10_minutes_and_a_new_one_replaces_the_old(store):
    c, m, clock = remote(store)
    old = c.new_code(NOW)["code"]
    new = c.new_code(NOW)["code"]
    if old != new:
        run(c.handle_message(message(f"/start {old}")))
        assert c.paired() is None
    clock.t = NOW + CODE_TTL_MS
    run(c.handle_message(message(f"/start {new}", sent_ts=clock.t)))
    assert c.paired() is None and m.sent == []


def test_wrong_codes_are_ignored_and_five_kill_the_code(store):
    c, m, clock = remote(store)
    code = c.new_code(NOW)["code"]
    for _ in range(CODE_TRIES):
        run(c.handle_message(message("/start WRONGCOD")))
    run(c.handle_message(message(f"/start {code}")))
    assert c.paired() is None and m.sent == []


def test_pairing_only_in_a_private_chat(store):
    c, m, clock = remote(store)
    code = c.new_code(NOW)["code"]
    run(c.handle_message(message(f"/start {code}", chat=-100123, private=False)))
    assert c.paired() is None and m.sent == []


def test_unpair_forgets_the_user(store):
    c, m, clock = remote(store)
    run(pair(c))
    assert c.unpair(NOW) == {"ok": True, "was_paired": True}
    assert c.paired() is None and c.status()["paired"] is False
    run(c.handle_message(message("/status")))
    assert len(m.sent) == 1  # only the earlier "Paired."


# -- who is answered ------------------------------------------------------------------------------------------------
def test_anyone_but_the_paired_user_in_the_paired_chat_is_ignored_and_logged(store, caplog):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts)
    run(pair(c))
    n = len(m.sent)
    caplog.set_level("INFO", logger="btc_signal_engine.application.notifier")
    run(c.handle_message(message("/stop", user=OTHER)))
    run(c.handle_message(message("/stop", user=USER, chat=-100555, private=False)))  # the right user, another chat
    run(c.handle_tap(tap("a:x:y", user=OTHER)))
    assert len(m.sent) == n and m.answers == [] and acts.calls == []
    assert sum("ignored" in r.getMessage() for r in caplog.records) == 3


def test_unpaired_nobody_is_answered(store):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts)
    run(c.handle_message(message("/stop")))
    run(c.handle_message(message("/start NOCODE")))
    assert m.sent == [] and acts.calls == []


def test_a_message_older_than_60_s_is_dropped(store):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts, sessions=live_sessions(store))
    run(pair(c))
    n = len(m.sent)
    clock.t = NOW + 10 * 60_000
    run(c.handle_message(message("/stop", sent_ts=clock.t - STALE_MS - 1)))
    assert acts.calls == [] and len(m.sent) == n
    run(c.handle_message(message("/stop", sent_ts=clock.t - STALE_MS)))  # 60 s exactly still counts
    assert acts.calls == [("stop", SID1)]


# -- commands --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", ["/resume", "/limits 50", "/raise", "/mode prod", "/prompt be bold", "/reactivate H4"])
def test_forbidden_commands_are_refused_with_a_plain_sentence_and_do_nothing(store, cmd):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts)
    run(pair(c))
    run(c.handle_message(message(cmd)))
    reply = m.texts()[-1]
    assert reply.endswith("on the PC.") and acts.calls == []


def test_stop_works_and_needs_no_confirmation(store):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts, sessions=live_sessions(store))
    run(pair(c))
    run(c.handle_message(message("/stop")))  # one agent running: at once, no confirmation
    run(c.handle_message(message("/stop@example_settleview_bot")))
    assert acts.calls == [("stop", SID1), ("stop", SID1)] and m.texts()[-1] == "Stopped Kalshi 15m agent."


def test_with_several_agents_stop_asks_which_and_names_or_all_act_at_once(store):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts, sessions=live_sessions(store, 2))
    run(pair(c))
    run(c.handle_message(message("/stop")))
    assert acts.calls == [] and m.texts()[-1] == "Stop which agent?"
    assert m.sent[-1]["buttons"] == [[("Kalshi 15m agent", "v:x:sess0001abcd")],
                                     [("Kalshi 15m agent 2", "v:x:sess0002abcd")], [("All agents", "v:x:all")]]
    run(c.handle_message(message("/stop 2")))
    assert acts.calls == [("stop", "sess0002abcd")] and m.texts()[-1] == "Stopped Kalshi 15m agent 2."
    run(c.handle_message(message("/stop Kalshi 15m agent")))
    assert acts.calls[-1] == ("stop", SID1)
    run(c.handle_message(message("/stop all")))
    assert acts.calls[-1] == ("stop_all",)
    run(c.handle_message(message("/stop 9")))
    assert m.texts()[-1] == "There is no agent 9; /status lists them."


def test_pause_and_note(store):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts, sessions=live_sessions(store))
    run(pair(c))
    run(c.handle_message(message("/pause")))
    run(c.handle_message(message("/note watch the 5 min market")))
    run(c.handle_message(message("/note")))
    assert acts.calls[0][:2] == ("pause", SID1) and "Resume on the PC" in acts.calls[0][2]
    assert acts.calls[1] == ("note", SID1, "watch the 5 min market")
    assert m.texts()[-3:] == ["Paused Kalshi 15m agent. Resume on the PC.", "Noted for Kalshi 15m agent.",
                              "Write the note after /note."]


def test_with_several_agents_pause_and_note_name_their_agent(store):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts, sessions=live_sessions(store, 2))
    run(pair(c))
    run(c.handle_message(message("/pause")))
    run(c.handle_message(message("/note hold off")))
    assert acts.calls == []  # which one? the agents are listed
    assert m.texts()[-2].startswith("Which agent should I pause?") and "2. Kalshi 15m agent 2" in m.texts()[-2]
    run(c.handle_message(message("/note 2 hold off")))
    run(c.handle_message(message("/pause all")))
    assert acts.calls[0] == ("note", "sess0002abcd", "hold off")
    assert [x[:2] for x in acts.calls[1:]] == [("pause", SID1), ("pause", "sess0002abcd")]


def test_without_agent_mode_the_agent_commands_say_so(store):
    c, m, clock = remote(store)
    run(pair(c))
    for cmd in ("/stop", "/pause", "/note x", "/launch", "/status"):
        run(c.handle_message(message(cmd)))
    assert m.texts()[1:] == ["Agent mode is off on this PC."] * 5


def test_unknown_text_gets_the_command_list(store):
    c, m, clock = remote(store)
    run(pair(c))
    run(c.handle_message(message("hello")))
    assert m.texts()[-1] == HELP


def test_lights_lists_the_lit_rules(store):
    def lights(now):
        return [{"market": "kalshi_15m", "market_label": "Kalshi 15 min", "window_end": 1,
                 "lights": [{"rule": "H4", "state": "green", "side": "UP", "ask": 0.86},
                            {"rule": "CUSHION100", "state": "amber",
                             "line": "CUSHION100 is setting up: BTC needs $28 more."},
                            {"rule": "LATERESID", "state": "grey"}]},
                {"market": "polymarket_5m", "market_label": "Polymarket 5 min", "window_end": 1, "lights": []}]
    c, m, clock = remote(store, lights_fn=lights)
    run(pair(c))
    run(c.handle_message(message("/lights")))
    assert m.texts()[-1] == ("<b>Lights</b> · 1 ready · 1 setting up\n"
                             "<blockquote expandable><b>Kalshi 15 min</b>\n"
                             "H4 is ready: <b>Up 86¢</b>\n"
                             "CUSHION100 is setting up: BTC needs $28 more.</blockquote>")


def test_lights_all_grey_says_so(store):
    c, m, clock = remote(store, lights_fn=lambda now: [{"market": "kalshi_15m", "market_label": "Kalshi 15 min",
                                                         "lights": [{"rule": "H4", "state": "grey"}]}])
    run(pair(c))
    run(c.handle_message(message("/lights")))
    assert m.texts()[-1] == "<b>All grey</b> · nothing is setting up."


# -- /launch: from a preset only, a 60 s confirm, the launch path ------------------------------------------------
LAUNCH = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 5.0, "per_trade": 1.0, "minutes": 60,
          "spend_cap": 2.0, "model": "claude-haiku-5-5", "rules": ["H4"], "level": "assist"}


def presets_for(store, modes=None):
    modes = modes if modes is not None else {"kalshi_15m": "shadow"}
    return AgentPresets(store, lambda spec, now: None, lambda: modes)


def test_launch_lists_presets_then_a_60s_confirm_launches_through_the_launch_path(store):
    acts = FakeActions()
    presets = presets_for(store)
    assert presets.save("H4 assist", LAUNCH, NOW)["ok"]
    c, m, clock = remote(store, actions=acts, presets=presets)
    run(pair(c))
    run(c.handle_message(message("/launch")))
    assert m.texts()[-1] == "Pick a preset."
    run(c.handle_tap(tap(m.button("H4 assist"))))
    assert acts.calls == [] and m.texts()[-1].startswith("Launch H4 assist: assist · H4 · Kalshi 15 min")
    confirm = m.button("Launch")
    clock.t += CONFIRM_MS - 1
    run(c.handle_tap(tap(confirm, message_id=m.sent[-1]["id"])))
    assert acts.calls == [("launch", LAUNCH, False)]  # the preset's body, no confirm phrase, not from the PC
    assert m.edits[-1]["text"] == "Launched <b>H4 assist</b> · assist · 60 min."
    run(c.handle_tap(tap(confirm, callback_id="again")))  # the confirm is one time
    assert m.answers[-1] == ("again", EXPIRED) and len(acts.calls) == 1


def test_the_launch_confirm_expires_at_60_s(store):
    acts = FakeActions()
    presets = presets_for(store)
    presets.save("H4 assist", LAUNCH, NOW)
    c, m, clock = remote(store, actions=acts, presets=presets)
    run(pair(c))
    run(c.handle_message(message("/launch")))
    run(c.handle_tap(tap(m.button("H4 assist"))))
    confirm = m.button("Launch")
    clock.t += CONFIRM_MS
    run(c.handle_tap(tap(confirm, message_id=m.sent[-1]["id"])))
    assert acts.calls == [] and m.answers[-1][1] == EXPIRED and "nothing launched" in m.edits[-1]["text"]


def test_launch_refuses_a_preset_whose_venue_changed_mode(store):
    acts = FakeActions()
    modes = {"kalshi_15m": "shadow"}
    presets = presets_for(store, modes)
    presets.save("H4 assist", LAUNCH, NOW)
    modes["kalshi_15m"] = "prod"
    c, m, clock = remote(store, actions=acts, presets=presets)
    run(pair(c))
    run(c.handle_message(message("/launch")))
    run(c.handle_tap(tap(m.button("H4 assist"))))
    run(c.handle_tap(tap(m.button("Launch"), message_id=m.sent[-1]["id"])))
    assert acts.calls == [] and "Launch from the PC" in m.edits[-1]["text"]


def test_launch_takes_no_free_form_arguments_and_forged_taps_do_nothing(store):
    acts = FakeActions()
    c, m, clock = remote(store, actions=acts, presets=presets_for(store))
    run(pair(c))
    run(c.handle_message(message("/launch budget=100")))
    assert m.texts()[-1].startswith("No presets yet")
    run(c.handle_tap(tap("c:made-up")))
    run(c.handle_tap(tap("zz")))
    assert acts.calls == [] and [a[1] for a in m.answers] == [EXPIRED, EXPIRED]


# -- alert preferences ---------------------------------------------------------------------------------------------
def test_alert_defaults_and_changes_persist_and_unknown_kinds_are_refused(store):
    c, m, clock = remote(store)
    assert c.alerts() == DEFAULT_ALERTS
    assert DEFAULT_ALERTS == {"proposals": True, "trades": True, "activity": True, "pauses": False,
                              "session_end": True, "settle_warnings": False, "setups": False, "data": True,
                              "daily": True}
    out = c.set_alerts({"setups": True, "trades": False}, NOW)
    assert out["ok"] and out["alerts"]["setups"] is True and out["alerts"]["trades"] is False
    again = RemoteControl(store, now_fn=Clock())
    assert again.alerts() == out["alerts"]
    assert c.set_alerts({"sms": True}, NOW)["code"] == "unknown_alert"
    assert c.set_alerts({"setups": "yes"}, NOW)["code"] == "invalid"
    assert c.set_alerts({}, NOW)["code"] == "invalid"


# -- alerts: sessions, trades, settle warnings, setups --------------------------------------------------------------
def bracket(store, bid, **kw):
    row = {"id": bid, "env": "shadow", "session_id": "s1", "kind": "entry", "ticker": "KXBTC15M-T", "side": "UP",
           "window_end": NOW + 600_000, "stake": 1.0, "contracts": 1.6, "limit_price": 0.6, "take_profit": 0.0,
           "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h", "state": "open", "filled": 0.0, "held": 0.0,
           "tp_filled": 0.0, "created_ts": NOW, "updated_ts": NOW, "strategy": "you"}
    if store.select("trading_brackets", {"id": bid}):
        store.update("trading_brackets", {"id": bid}, {**kw, "updated_ts": NOW})
    else:
        store.insert("trading_brackets", [{**row, **kw}], ignore_conflicts=False)


def test_trade_alerts_fill_and_settlement_after_a_baseline(store):
    c, m, clock = remote(store)
    c.set_alerts({"activity": False}, NOW)  # the trades alert's own lines (activity replaces them when on)
    bracket(store, "old", state="closed", filled=1.0, entry_cost=0.6, proceeds=1.0, exit_reason="settled")
    run(pair(c))
    run(c.pump())  # the baseline: nothing old is replayed
    n = len(m.sent)
    bracket(store, "b1")
    run(c.pump())
    assert len(m.sent) == n  # sent, not filled yet
    bracket(store, "b1", filled=1.6, held=1.6, entry_avg=0.61, entry_cost=0.99)
    run(c.pump())
    assert m.texts()[-1] == "🔵 <b>Filled Up 1.6 at 61¢</b> · Kalshi 15 min" and m.sent[-1]["silent"] is False
    bracket(store, "b1", state="closed", exit_reason="settled", proceeds=1.6)
    run(c.pump())
    assert m.texts()[-1] == "🟢 <b>+$0.61</b> · Settled Up · Kalshi 15 min" and m.sent[-1]["silent"] is False
    run(c.pump())
    assert len(m.sent) == n + 2


def test_trade_alerts_off_send_nothing(store):
    c, m, clock = remote(store)
    run(pair(c))
    c.set_alerts({"trades": False}, NOW)
    run(c.pump())
    n = len(m.sent)
    bracket(store, "b1", filled=1.0, entry_avg=0.6)
    run(c.pump())
    assert len(m.sent) == n


def test_session_alerts_follow_the_preferences_and_skip_the_bots_own_stop(store):
    c, m, clock = remote(store)
    c.set_alerts({"activity": False}, NOW)  # activity's end summary stands in for these lines when on
    run(pair(c))
    n = len(m.sent)
    c.on_session({"id": "s1", "state": "paused", "pause_reason": "The kill switch is on."})
    run(c.pump())
    assert len(m.sent) == n  # pauses are off by default
    bracket(store, "b1", filled=1.0)  # an open position of session s1
    c.on_session({"id": "s1", "state": "ended", "end_reason": "time"})
    run(c.pump())
    assert "⏹ <b>Agent ended</b> · end time · 1 open position" in m.texts()[n:]  # session_end: on by default
    c.set_alerts({"pauses": True}, NOW)
    c.on_session({"id": "s1", "state": "paused", "pause_reason": "The kill switch is on."})
    c.on_session({"id": "s2", "state": "ended", "end_reason": "stopped", "token_hash": "never read"})
    run(c.pump())
    assert m.texts()[-2:] == ["⏸ <b>Paused</b> · Agent · The kill switch is on.",
                              "⏹ <b>Agent ended</b> · stopped by you"]
    c._own = {"s2"}  # the bot's own /stop of s2 answers for itself...
    c.on_session({"id": "s2", "state": "ended", "end_reason": "stopped"})
    c.on_session({"id": "s3", "state": "paused", "pause_reason": "Mode changed."})  # ...not for another agent
    c._own = set()
    k = len(m.sent)
    run(c.pump())
    assert m.texts()[k:] == ["⏸ <b>Paused</b> · Agent · Mode changed."]



def test_settle_warnings_are_opt_in_and_sent_once(store):
    warns = []
    c, m, clock = remote(store, settle_fn=lambda now: list(warns))
    run(pair(c))
    c.set_alerts({"settle_warnings": True}, NOW)
    run(c.pump())
    n = len(m.sent)
    warns.append({"id": "b9", "ticker": "KXBTC15M-T", "minutes": 75, "venue_label": "Kalshi 15 min"})
    clock.t += 60_000
    run(c.pump())
    clock.t += 60_000
    run(c.pump())
    assert m.texts()[n:] == ["⚠️ <b>Check Kalshi 15 min</b> · KXBTC15M-T still settling after 75 min"]


def test_setup_alerts_are_opt_in_and_only_without_a_session(store):
    state = {"s": "amber"}

    def lights(now):
        return [{"market": "kalshi_15m", "market_label": "Kalshi 15 min", "window_end": 1,
                 "lights": [{"rule": "CUSHION100", "state": state["s"]}]}]
    c, m, clock = remote(store, lights_fn=lights)
    run(pair(c))
    run(c.pump())
    n = len(m.sent)
    state["s"] = "green"
    clock.t += 5_000
    run(c.pump())
    assert len(m.sent) == n  # off by default
    c.set_alerts({"setups": True}, NOW)
    state["s"] = "amber"
    clock.t += 5_000
    run(c.pump())
    state["s"] = "green"
    clock.t += 5_000
    run(c.pump())
    clock.t += 5_000
    run(c.pump())
    assert m.texts()[n:] == ["<b>CUSHION100 ready</b> · Kalshi 15 min"]


def test_nothing_is_sent_while_unpaired_and_pairing_replays_nothing(store):
    c, m, clock = remote(store)
    c.on_proposal("proposal_new", {"id": "p1", "state": "pending", "line": "Buy Up $1.00 · max 60¢"})
    bracket(store, "b1", filled=1.0)
    run(c.pump())
    assert m.sent == []
    run(pair(c))
    run(c.pump())
    assert m.texts() == ["Paired. Proposals and trades will arrive here."]


def test_the_pinned_line_and_the_digest_are_fleet_level_with_a_line_per_agent(store):
    from btc_signal_engine.application.notifier import DIGEST_MS, PIN_AGENTS, compose
    c, m, clock = remote(store, sessions=live_sessions(store, PIN_AGENTS + 2))
    lines = c.live_text(NOW).split("\n")
    assert lines[0] == "📌 <b>7 agents</b> · <b>+$0.00</b> · 0 winning · 0 holding · 0 need you"
    assert lines[1] == "Kalshi 15m agent · Watching · +$0.00 (0)" and lines[2].startswith("Kalshi 15m agent 2 ·")
    assert len(lines) == 1 + PIN_AGENTS + 1 and lines[-1] == "+ 2 more"  # collapsed beyond 5 (KD8)
    note = c.digest_note(NOW, NOW - DIGEST_MS, NOW)
    text = compose([note])
    assert text.startswith("📊 Fleet <b>+$0.00</b>") and text.count(" agent") >= 7 and note.menu == "fleet"
