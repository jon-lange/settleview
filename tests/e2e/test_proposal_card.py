"""The Assist proposal card (web/views/proposal.js) in a real browser, mounted on an agent's screen for its run: shown from
the run's feed CUSTOM proposal_new, Approve sends one entry, Skip and expiry send nothing. The app is tests/e2e/server.py
(shadow exchange, scripted fake harness); the proposal is the real AgentProposals over a test-built rule match.
Approve is press-and-hold (B6#11): held to the end it sends (a mouse on desktop, a real touch on the phone looks, Space
held on the keyboard); a tap, an early release or a short key press sends nothing; assistive activation (a click with
no press behind it) needs an explicit second confirm."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.test_agent_panel import LAUNCH, open_agent
from tests.trading.helpers import CLOSE, NOW, TICKER, W0

pytestmark = pytest.mark.e2e

K = 80_000.0
MATCH = {"rule": "H4", "market": "kalshi_15m", "window_start": W0, "window_end": CLOSE, "ticker": TICKER,
         "elapsed_s": 300.0, "left_s": 600.0, "btc": K + 45, "strike": K, "gap": 45.0, "gap_pct": 0.0563,
         "side": "UP", "ask": 0.58, "ask_cap": 0.95, "stop_index_below": K, "stop_index_above": None}


def assist_session(ui) -> str:
    out = ui.page.request.post("/api/agent/launch", data={**LAUNCH, "budget": 5.0, "per_trade": 1.0,
                                                          "level": "assist", "rules": ["H4"]}).json()
    assert out["ok"], out
    return out["id"]


def mount_card(ui, sid: str):
    """The card for one agent's run (the Agents tab mounts it on that agent's screen for its run)."""
    page = ui.page
    open_agent(ui)
    page.add_style_tag(url="/proposal.css")
    page.add_script_tag(url="/views/proposal.js")
    page.evaluate("sid => { window.__e2eSid = sid; }", sid)
    page.evaluate("""() => {
        const el = document.createElement('div');
        el.id = 'pc-mount';
        const panel = document.querySelector('#panel-agent');
        panel.insertBefore(el, panel.firstChild);
        window.__pc = window.proposalCards.mount(el, { session: () => window.__e2eSid });
    }""")
    return page.locator("#pc-mount")


def propose(ui) -> dict:
    w = ui.server.world
    out = ui.server.run(lambda: w.proposals.propose(w.clock.now_ms(), w.r.ag.latest(), "H4", "kalshi_15m", MATCH))
    assert out["ok"], out
    reload_cards(ui)
    return out["proposal"]


def reload_cards(ui):
    """The card reads its run's proposals on every proposal event of that run's stream (this bare mount has no
    stream open) and every 5 s; the test asks at once."""
    ui.page.evaluate("() => window.__pc.reload()")


HOLD_S = 0.8  # proposal.js HOLD_MS


def hold(ui, button, seconds: float) -> None:
    """Press the button for `seconds`, then let go: a mouse on desktop; on the phone looks a real touch (Chromium's
    touch input over CDP, the events a finger makes), since Playwright's touchscreen only taps."""
    button.scroll_into_view_if_needed()
    box = button.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page = ui.page
    if ui.look["phone"]:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        page.wait_for_timeout(seconds * 1000)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        cdp.detach()
    else:
        page.mouse.move(x, y)
        page.mouse.down()
        page.wait_for_timeout(seconds * 1000)
        page.mouse.up()


def someone_at_the_screen(ui) -> None:
    ui.server.call(lambda: ui.server.world.r.svc.heartbeat(ui.server.world.clock.now_ms()))


def buys(ui) -> list:
    return ui.server.call(lambda: [p for p in ui.server.world.r.ex.posts()
                                   if p and "-entry" in p.get("client_order_id", "")])


def test_the_card_shows_a_proposal_and_approve_sends_the_entry(ui):
    root = mount_card(ui, assist_session(ui))
    p = propose(ui)
    card = root.locator("article.pc-card")
    expect(card).to_have_count(1)
    expect(card.locator(".pc-t")).to_contain_text("Ask Settleview proposes")
    expect(card.locator(".pc-o")).to_have_text("Buy Up $1.00 · max 60¢")
    expect(card.locator(".pc-why")).to_have_text(p["reason"])
    expect(card.locator(".pc-make")).to_have_text(f"Could make +${p['could_make']:.2f}")
    expect(card.locator(".pc-lose")).to_have_text(f"Could lose ${p['could_lose']:.2f}")
    expect(card.locator(".pc-left")).to_have_text("0:20")  # the server's clock stands still in this world
    note = ("Hold Approve to send the entry and its stop. Unanswered proposals expire; nothing is sent.")
    expect(card.locator(".pc-note")).to_have_text(note)
    ok = card.get_by_role("button", name="Hold to approve")
    expect(ok).to_have_accessible_description(note)
    ui.no_horizontal_scroll()
    ui.shot("proposal")
    assert buys(ui) == []
    someone_at_the_screen(ui)
    hold(ui, ok, HOLD_S + 0.4)
    expect(root.locator(".pc-status")).to_have_text("Approved: the entry and its stop were sent.")
    expect(card).to_have_count(0)
    assert len(buys(ui)) == 1


def test_a_tap_or_an_early_release_sends_nothing(ui):
    root = mount_card(ui, assist_session(ui))
    propose(ui)
    someone_at_the_screen(ui)
    card = root.locator("article.pc-card")
    ok = card.get_by_role("button", name="Hold to approve")
    status = root.locator(".pc-status")
    if ui.look["phone"]:
        ok.tap()  # a stray tap
    else:
        ok.click()
    expect(status).to_have_text("Released early: nothing was sent. Hold until it fills to approve.")
    ui.page.evaluate("() => { document.querySelector('#pc-mount .pc-status').textContent = ''; }")
    # held, but let go before the fill completes: cancelled, the fill resets, nothing is sent
    hold(ui, ok, HOLD_S / 3)
    expect(status).to_have_text("Released early: nothing was sent. Hold until it fills to approve.")
    assert ok.evaluate("b => b.classList.contains('pc-holding')") is False
    assert ok.locator(".pc-fill").evaluate("f => f.style.transform") == ""
    ui.page.wait_for_timeout(HOLD_S * 1000)  # longer than a hold: the cancelled hold must not complete later
    expect(card).to_have_count(1)
    assert buys(ui) == []


def test_mid_hold_progress_shows_and_sliding_off_cancels(ui):
    if ui.look["phone"]:
        pytest.skip("a mouse drag; the touch path is covered by the hold tests above")
    root = mount_card(ui, assist_session(ui))
    propose(ui)
    someone_at_the_screen(ui)
    card = root.locator("article.pc-card")
    ok = card.get_by_role("button", name="Hold to approve")
    box = ok.bounding_box()
    page = ui.page
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(HOLD_S * 1000 / 2)
    scale = ok.locator(".pc-fill").evaluate("f => parseFloat(f.style.transform.slice(7))")
    assert 0.2 < scale < 1, scale  # the fill shows how far the hold has come
    assert ok.evaluate("b => b.classList.contains('pc-holding')") is True
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] - 80)  # slid off the button, still pressed
    page.wait_for_timeout(HOLD_S * 1000)
    page.mouse.up()
    expect(root.locator(".pc-status")).to_have_text("Released early: nothing was sent. Hold until it fills to approve.")
    expect(card).to_have_count(1)
    assert buys(ui) == []


def test_keyboard_hold_approves_and_a_short_press_does_not(ui):
    root = mount_card(ui, assist_session(ui))
    propose(ui)
    someone_at_the_screen(ui)
    card = root.locator("article.pc-card")
    ok = card.get_by_role("button", name="Hold to approve")
    status = root.locator(".pc-status")
    kb = ui.page.keyboard
    ok.focus()
    kb.press("Enter")  # a short press: no native click, nothing sent
    expect(status).to_have_text("Released early: nothing was sent. Hold until it fills to approve.")
    kb.press("Space")
    ui.page.wait_for_timeout(HOLD_S * 1000)
    expect(card).to_have_count(1)
    assert buys(ui) == []
    kb.down("Space")  # held: the fill completes and the proposal is approved
    expect(status).to_have_text("Approved: the entry and its stop were sent.", timeout=HOLD_S * 1000 + 4000)
    kb.up("Space")
    expect(card).to_have_count(0)
    assert len(buys(ui)) == 1


def test_assistive_activation_needs_an_explicit_confirm(ui):
    """A screen reader's or voice control's activation is a click with no press behind it (detail 0): the first one
    arms "Confirm approve", only a second one sends."""
    root = mount_card(ui, assist_session(ui))
    propose(ui)
    someone_at_the_screen(ui)
    card = root.locator("article.pc-card")
    ok = card.get_by_role("button", name="Hold to approve")
    ok.evaluate("b => b.click()")
    confirm = card.get_by_role("button", name="Confirm approve")
    expect(confirm).to_be_visible()
    expect(root.locator(".pc-status")).to_have_text("Activate Confirm approve again to send; nothing is sent otherwise.")
    assert buys(ui) == []
    confirm.evaluate("b => b.click()")
    expect(root.locator(".pc-status")).to_have_text("Approved: the entry and its stop were sent.")
    expect(card).to_have_count(0)
    assert len(buys(ui)) == 1


def test_skip_and_expiry_send_nothing(ui):
    root = mount_card(ui, assist_session(ui))
    propose(ui)
    card = root.locator("article.pc-card")
    expect(card).to_have_count(1)
    card.get_by_role("button", name="Skip").click()
    expect(root.locator(".pc-status")).to_have_text("Skipped; nothing was sent.")
    expect(card).to_have_count(0)
    # a second window's proposal, unanswered: it expires and the card goes away
    w = ui.server.world
    out = ui.server.run(lambda: w.proposals.propose(w.clock.now_ms(), w.r.ag.latest(), "H4", "kalshi_15m",
                                                    {**MATCH, "window_start": W0 + 1}))
    assert out["ok"], out
    reload_cards(ui)
    expect(card).to_have_count(1)
    ui.server.call(lambda: w.proposals.expire(NOW + 25_000))
    reload_cards(ui)
    expect(card).to_have_count(0)
    assert buys(ui) == []
