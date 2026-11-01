"""The Telegram remote's message format (application/notifier.py): HTML with every dynamic string escaped, the
headline-first shape with details in an expandable quote, Telegram's 4096-character limit, silent sends per kind,
the pinned live status (pin on start, throttled edits, unpin at the end, failures tolerated, a stale pin closed by
the next session) and the Status / Lights / Stop menu with its two-step Stop. Fakes only (no network)."""

from __future__ import annotations

import logging

from btc_signal_engine.application.notifier import (ACTIVITY_GAP_MS, DIGEST_MS, EXPIRED, LIVE_PIN, MAX_CHARS,
                                                    PIN_EDIT_MS, STOP_CONFIRM_MS, Note, RemoteControl, compose, esc)
from tests.telegram.fakes import OTHER, FakeActions, message, tap
from tests.telegram.test_activity import LAUNCH, SID, WINDOW_END, World, msg, run
from tests.trading.helpers import NOW

MENU = [[("Status", "v:st"), ("Lights", "v:li"), ("Stop…", "v:sx")]]  # Stop…: which agent (or all), then confirm


class StopActions(FakeActions):
    """/stop's path on the real AgentSessions."""

    def __init__(self, w: World) -> None:
        super().__init__()
        self.w = w

    async def stop(self, session_id):
        self.calls.append(("stop", session_id))
        return self.w.ag.stop(session_id, self.w.clock.t, "stopped")


def started(store, **kw) -> World:
    w = World(store, **kw)
    w.c.actions = StopActions(w)
    w.pump()  # the start: its message, and the pinned live status
    return w


def pinned(w: World) -> dict:
    (chat, mid), = w.m.pins
    return next(m for m in w.m.sent if m["id"] == mid)


# -- escaping and shape -------------------------------------------------------------------------------------------
def test_every_dynamic_string_is_html_escaped(store):
    w = started(store)
    w.new()
    w.journal("decision", {"action": "EXIT", "source": "agent", "side": "UP", "market": "kalshi_15m",
                           "thesis": "BTC <76,600 & falling > fast"})
    w.journal("rule", {"rule": "CUSHION100", "market": "kalshi_15m", "ok": False, "code": "over_budget",
                       "reason": "Cost <$1> & more"})
    w.ag.pause(w.ag.latest()["id"], w.clock.t, "Paused <by> the kill & switch.", "kill_switch")
    w.pump(ACTIVITY_GAP_MS)
    (sent,) = w.new()
    assert "BTC &lt;76,600 &amp; falling &gt; fast" in sent
    assert "Cost &lt;$1&gt; &amp; more." in sent
    assert "⏸ <b>Paused</b> · Kalshi 15m agent · Paused &lt;by&gt; the kill &amp; switch." in sent
    assert "<76" not in sent and "<$1>" not in sent and "<by>" not in sent


def test_an_agent_note_and_preset_names_are_escaped(store):
    w = started(store)
    w.new()
    w.bracket("b2", stop_index_below=None)
    w.journal("tool", {"name": "place_entry", "ok": True, "args": {"market": "kalshi_15m", "note": "a<b & c>d"},
                       "result": {"ok": True, "bracket": {"id": "b2"}}})
    w.bracket("b2", filled=2.0, entry_avg=0.5, entry_cost=1.02)
    w.pump(ACTIVITY_GAP_MS)
    assert w.new() == [msg("🔵 <b>Bought Up $1.02 at 50¢</b> · Kalshi 15m agent", "a&lt;b &amp; c&gt;d", "No stop")]
    assert esc("<b>&") == "&lt;b&gt;&amp;"


def test_long_details_are_cut_under_telegrams_limit_and_the_headline_stays():
    notes = [Note(f"🔵 <b>Head {i}</b>", [f"detail {i}.{k} " + "x" * 200 for k in range(40)]) for i in range(5)]
    text = compose(notes)
    assert len(text) <= MAX_CHARS
    assert text.startswith("🔵 <b>Head 0</b>\n<blockquote expandable>")
    assert text.count("<blockquote expandable>") == text.count("</blockquote>")
    assert "…" in text
    many = compose([Note(f"<b>Head {i}</b> " + "y" * 100) for i in range(100)])
    assert len(many) <= MAX_CHARS and many.startswith("<b>Head 0</b>") and many.endswith("more")
    assert compose([Note("<b>Short</b>", ["one", "two"])]) == msg("<b>Short</b>", "one", "two")


# -- silent sends -----------------------------------------------------------------------------------------------
def test_silent_and_notifying_kinds(store):
    w = started(store)
    (start,) = w.new_sent()
    assert start["silent"] is True and pinned(w)["silent"] is True  # session started, live status
    w.rule_entry()
    w.pump(ACTIVITY_GAP_MS)
    (entry,) = w.new_sent()
    assert entry["silent"] is False
    w.view["btc"] = 76_590.0  # near the stop: a heads-up
    w.pump(ACTIVITY_GAP_MS)
    (heads,) = w.new_sent()
    assert heads["silent"] is True
    # a heads-up waiting with a refused rule match: one message, and it notifies (not all of it is quiet)
    w.clock.t = WINDOW_END - 80_000
    w.journal("rule", {"rule": "H4", "market": "kalshi_15m", "ok": False, "code": "over_budget", "reason": "No."})
    w.pump(ACTIVITY_GAP_MS)
    (mixed,) = w.new_sent()
    assert mixed["text"].startswith("⚠️ <b>H4 not sent</b>")  # what notifies leads (the preview line)
    assert "left · losing" in mixed["text"] and mixed["silent"] is False
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=0.0, held=0.0)
    w.pump(1_000)
    (close,) = w.new_sent()
    assert close["text"].startswith("🔴 <b>−$1.00</b> · Settled Up · Kalshi 15m agent") and close["silent"] is False
    w.ag.pause(w.ag.latest()["id"], w.clock.t, "The kill switch is on.", "kill_switch")
    w.pump(ACTIVITY_GAP_MS)
    (pause,) = w.new_sent()
    assert pause["silent"] is False
    w.ag.stop(w.ag.latest()["id"], w.clock.t, "time", "The end time.")
    w.pump(1_000)
    (end,) = w.new_sent()
    assert end["text"].startswith("⏹ <b>−$1.00</b> · Kalshi 15m agent ended · end time") and end["silent"] is False


def test_in_a_coalesced_message_what_notifies_comes_first(store):
    w = started(store)
    w.rule_entry()
    w.pump(ACTIVITY_GAP_MS)
    w.new()
    w.journal("tool", {"name": "set_btc_stop", "ok": True, "args": {"bracket_id": "b1", "level": 76_620.0}})
    w.journal("rule", {"rule": "H4", "market": "kalshi_15m", "ok": False, "code": "over_budget", "reason": "No."})
    w.pump(ACTIVITY_GAP_MS)
    (sent,) = w.new_sent()
    assert sent["text"] == ("⚠️ <b>H4 not sent</b> · Kalshi 15m agent\n<blockquote expandable>No.</blockquote>\n\n"
                            "<b>Stop tightened to BTC $76,620</b> · Kalshi 15m agent")
    assert sent["silent"] is False


def test_the_digest_is_silent_and_carries_the_menu(store):
    w = started(store)
    w.rule_entry()
    w.pump(ACTIVITY_GAP_MS)
    w.new()
    w.clock.t = NOW + DIGEST_MS
    w.pump()
    (digest,) = w.new_sent()
    assert digest["text"].startswith("📊 Fleet <b>+$0.00</b> · 17:05–18:05 · 0 trades\n<blockquote expandable>")
    assert digest["silent"] is True and digest["buttons"] == MENU


# -- the pinned live status ------------------------------------------------------------------------------------------
def test_a_session_start_sends_pins_and_stores_the_live_status(store):
    w = started(store)
    p = pinned(w)
    # the fleet line (multi-agent KD8), one line per agent below it
    assert p["text"] == ("📌 <b>1 agent</b> · <b>+$0.00</b> · 0 winning · 0 holding · 0 need you\n"
                         "Kalshi 15m agent · Watching · +$0.00 (0)")
    assert p["silent"] is True and p["buttons"] == MENU
    stored = store.select("strategy_settings", LIVE_PIN)[0]["value"]
    assert stored == {"fleet": True, "chat_id": w.m.sent[0]["chat"], "message_id": p["id"]}


def test_the_live_status_is_edited_only_on_change_at_most_once_a_minute_and_at_once_after_an_entry(store):
    w = started(store)
    mid = pinned(w)["id"]
    w.pump(PIN_EDIT_MS)
    assert w.m.edits == []  # nothing changed: no edit
    w.rule_entry()
    w.pump(1_000)  # an entry: at once
    live = [e for e in w.m.edits if e["id"] == mid]
    assert live[-1]["text"] == ("📌 <b>1 agent</b> · <b>+$0.00</b> · 0 winning · 1 holding · 0 need you\n"
                                "Kalshi 15m agent · Holding · +$0.00 (0)")
    assert live[-1]["buttons"] == MENU
    w.pump(10_000)
    assert len([e for e in w.m.edits if e["id"] == mid]) == 1
    w.pump(PIN_EDIT_MS)
    assert len([e for e in w.m.edits if e["id"] == mid]) == 1  # unchanged: no edit
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16, held=0.0)
    w.pump(1_000)  # a close: at once
    live = [e for e in w.m.edits if e["id"] == mid]
    assert live[-1]["text"] == ("📌 <b>1 agent</b> · <b>+$0.16</b> · 1 winning · 0 holding · 0 need you\n"
                                "Kalshi 15m agent · Watching · +$0.16 (1)")


def test_the_session_end_edits_the_live_status_to_the_result_and_unpins_it(store):
    w = started(store)
    mid = pinned(w)["id"]
    w.rule_entry()
    w.bracket("b1", state="closed", exit_reason="settled", proceeds=1.16, held=0.0)
    w.pump(1_000)
    w.ag.stop(w.ag.latest()["id"], w.clock.t, "time", "The end time.")
    w.pump(1_000)
    assert w.m.edits[-1] == {"chat": w.m.sent[0]["chat"], "id": mid, "buttons": None,
                             "text": "⏹ <b>No agents running</b> · today <b>+$0.16</b> (1)"}
    assert w.m.unpins == [(w.m.sent[0]["chat"], mid)]
    assert store.select("strategy_settings", LIVE_PIN)[0]["value"] == {}
    n = len(w.m.edits)
    w.pump(PIN_EDIT_MS)
    assert len(w.m.edits) == n and len(w.m.unpins) == 1


def test_pin_and_edit_failures_are_logged_once_and_the_bot_carries_on(store, caplog):
    caplog.set_level("DEBUG", logger="btc_signal_engine.application.notifier")
    w = World(store)
    w.m.boom = {"pin", "edit"}
    w.m.fail = {"unpin"}
    w.pump()
    sent = w.new()  # the live status was sent but not pinned; the start message still went
    assert w.m.pins == [] and sent[0].startswith("📌 ") and sent[1].startswith("▶️ ")
    w.rule_entry()
    w.pump(ACTIVITY_GAP_MS)  # an entry: an edit tried at once, and it fails
    w.view["btc"] = 76_650.0
    w.pump(PIN_EDIT_MS)  # and again
    assert w.new() == [msg("🔵 <b>Bought Up $1 at 86¢</b> · Kalshi 15m agent",
                           "CUSHION100 · BTC +$118 vs strike · 6:40 left · stop BTC $76,560")]  # the entry still went
    w.ag.stop(w.ag.latest()["id"], w.clock.t, "time", "The end time.")
    w.pump(1_000)
    assert any(t.startswith("⏹ <b>+$0.00</b> · Kalshi 15m agent ended") for t in w.m.texts())  # the summary went
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "live status" in r.getMessage()]
    assert sorted(warn) == sorted([f"telegram: the live status {k} failed (rights, or the message is too old); "
                                   f"carrying on" for k in ("pin", "edit", "unpin")])
    assert store.select("strategy_settings", LIVE_PIN)[0]["value"] == {}


def test_a_stale_pin_is_unpinned_by_the_next_session(store):
    # a pin left by an earlier run (the app restarted mid-session: its session ended without the bot)
    store.insert("strategy_settings", [{**LIVE_PIN, "value": {"session_id": "old00001", "chat_id": 4242,
                                                              "message_id": 55}, "updated_ts": NOW}],
                 ignore_conflicts=False)
    w = World(store)  # its baseline pump runs before the launch: no session, so the stale pin is closed there
    assert w.m.unpins == [(4242, 55)] and w.m.edits[-1]["id"] == 55 and w.m.edits[-1]["text"].startswith("⏹ ")
    w.pump()
    assert len(w.m.pins) == 1 and w.m.pins[0][1] != 55


def test_a_new_session_closes_a_stored_pin_first(store):
    w = World(store, launch=False)
    # a pin stored by an earlier run that this remote has not looked at yet (a restart between the two)
    store.insert("strategy_settings", [{**LIVE_PIN, "value": {"session_id": "old00001", "chat_id": 4242,
                                                              "message_id": 55}, "updated_ts": NOW}],
                 ignore_conflicts=False)
    w.c = RemoteControl(store, now_fn=w.clock, configured=True, sessions=w.ag, tz=w.c.tz)
    w.c.messenger = w.m
    w.ag._notify = w.c.on_session
    assert w.ag.launch(LAUNCH, w.clock.t)["ok"]
    w.pump()
    assert w.m.unpins == [(4242, 55)]
    assert len(w.m.pins) == 1 and w.m.pins[0][1] != 55
    assert store.select("strategy_settings", LIVE_PIN)[0]["value"]["fleet"] is True


# -- the menu: Status, Lights, a two-step Stop -----------------------------------------------------------------------
def test_status_and_lights_buttons_answer_like_the_commands(store):
    w = started(store, lights_fn=lambda now: [])
    run(w.c.handle_message(message("/status", sent_ts=w.clock.t)))
    status = w.m.texts()[-1]
    run(w.c.handle_tap(tap("v:st", callback_id="s1")))
    assert w.m.texts()[-1] == status and ("s1", None) in w.m.answers
    run(w.c.handle_tap(tap("v:li", callback_id="l1")))
    assert w.m.texts()[-1] == "<b>All grey</b> · nothing is setting up."


def test_stop_needs_a_second_tap_and_yes_stops_at_once(store):
    w = started(store)
    mid = pinned(w)["id"]
    run(w.c.handle_tap(tap("v:sx", message_id=mid, callback_id="x1")))
    assert w.ag.latest() is not None and w.c.actions.calls == []  # the first tap stops nothing
    (row,) = w.m.button_edits[-1]["buttons"]
    assert [lab for lab, _ in row] == ["Yes, stop", "Cancel"] and w.m.button_edits[-1]["id"] == mid
    assert w.m.answers[-1] == ("x1", "Stop Kalshi 15m agent? Tap Yes, stop within 10 s.")
    yes = row[0][1]
    assert yes.startswith("v:y:")
    w.clock.t += STOP_CONFIRM_MS - 1
    run(w.c.handle_tap(tap(yes, message_id=mid, callback_id="y1")))
    assert w.c.actions.calls == [("stop", SID)] and w.ag.latest() is None  # /stop's path
    assert w.m.texts()[-1] == "Stopped Kalshi 15m agent." and w.m.button_edits[-1]["buttons"] == MENU
    run(w.c.handle_tap(tap(yes, message_id=mid, callback_id="y2")))  # single use
    assert w.m.answers[-1] == ("y2", EXPIRED) and w.c.actions.calls == [("stop", SID)]


def test_an_expired_or_cancelled_stop_does_nothing(store):
    w = started(store)
    mid = pinned(w)["id"]
    run(w.c.handle_tap(tap("v:sx", message_id=mid)))
    yes = w.m.button_edits[-1]["buttons"][0][0][1]
    w.clock.t += STOP_CONFIRM_MS
    run(w.c.handle_tap(tap(yes, message_id=mid, callback_id="late")))
    assert w.m.answers[-1] == ("late", EXPIRED) and w.c.actions.calls == [] and w.ag.latest() is not None
    assert w.m.button_edits[-1]["buttons"] == MENU
    # untapped: the pump puts the row back after 10 s
    run(w.c.handle_tap(tap("v:sx", message_id=mid)))
    assert w.m.button_edits[-1]["buttons"] != MENU
    w.pump(STOP_CONFIRM_MS)
    assert w.m.button_edits[-1]["buttons"] == MENU
    # Cancel
    run(w.c.handle_tap(tap("v:sx", message_id=mid)))
    cancel = w.m.button_edits[-1]["buttons"][0][1][1]
    run(w.c.handle_tap(tap(cancel, message_id=mid, callback_id="no")))
    assert w.m.answers[-1] == ("no", "Cancelled.") and w.c.actions.calls == [] and w.ag.latest() is not None
    assert w.m.button_edits[-1]["buttons"] == MENU


def test_stale_menu_taps_and_strangers_do_nothing(store):
    w = started(store)
    run(w.c.handle_tap(tap("v:x:another-session", message_id=7, callback_id="old")))
    run(w.c.handle_tap(tap("v:y:forged", message_id=7, callback_id="forged")))
    run(w.c.handle_tap(tap("v:zz", callback_id="zz")))
    assert [a for a in w.m.answers if a[0] in ("old", "forged", "zz")] == [("old", EXPIRED), ("forged", EXPIRED),
                                                                            ("zz", EXPIRED)]
    n = len(w.m.answers)
    run(w.c.handle_tap(tap(f"v:x:{SID}", user=OTHER, message_id=7)))
    run(w.c.handle_tap(tap("v:st", user=OTHER)))
    assert len(w.m.answers) == n and w.m.button_edits == [] and w.c.actions.calls == []
    # the menu's callback data never collides with the proposals' ("a:" / "s:") or /launch's ("p:" / "c:")
    assert all(d.startswith("v:") for row in MENU for _, d in row)
