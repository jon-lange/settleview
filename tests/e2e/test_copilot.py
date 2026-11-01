"""Ask Settleview on the Play screen in a real browser (Manual level; wireframe v2 "Phone" 1-5 and 8): the one line
and its tap-to-expand why, the setup lights, Fill in ticket + Undo (and the slide sending the filled-in ticket through
the normal prepare -> approve path, with what was shown recorded), the check while you slide, the hold note, and the
Settings modes. Desktop and phone, light and dark. The app is tests/e2e/server.py (shadow exchange, no network)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.agent.test_tools import K
from tests.application.test_your_record import bracket
from tests.trading.helpers import NOW

pytestmark = pytest.mark.e2e


def setup_page(ui, mode: str | None = "full"):
    """Play on Kalshi (remembered; no auto-sell prompt after a buy), with Ask Settleview's mode stored (None = never
    set: Off)."""
    script = "localStorage.setItem('play.venue', 'kalshi'); localStorage.setItem('pl.autosellPrompt', 'off');"
    if mode is not None:
        script += f"localStorage.setItem('pl.copilot', '{mode}');"
    ui.page.add_init_script(script)
    ui.page.goto("/")
    expect(ui.page.locator("#panel-play .pl-verdict")).to_be_visible()


def green(ui):
    """CUSHION100 ready: BTC $120 over the strike, Up at 86c, 10:00 left."""
    def go():
        w = ui.server.world
        w.r.play.index_fn = lambda: (NOW, K + 120)
        w.r.ex.market.set(0.84, 0.14)
    ui.server.call(go)


def amber(ui):
    """CUSHION100 setting up: BTC $72 over the strike, Up at 78c."""
    def go():
        w = ui.server.world
        w.r.play.index_fn = lambda: (NOW, K + 72)
        w.r.ex.market.set(0.76, 0.22)
    ui.server.call(go)


def line(ui):
    return ui.page.locator("#panel-play .pl-pre details.sv-ask")


def lights(ui):
    return ui.page.locator("#panel-play .sv-lights .sv-lt")


def drag(ui, fraction: float, release: bool = True):
    knob = ui.page.locator("#panel-play .pl-slide-knob")
    knob.scroll_into_view_if_needed()
    track = ui.page.locator("#panel-play .pl-slide").bounding_box()
    box = knob.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    ui.page.mouse.move(x, y)
    ui.page.mouse.down()
    steps = 8
    for i in range(1, steps + 1):
        ui.page.mouse.move(x + track["width"] * fraction * i / steps, y)
    if release:
        ui.page.mouse.up()


# -- the line, the lights, tap to expand --------------------------------------------------------------------------------
def test_the_line_names_a_rule_that_is_setting_up_then_ready(ui):
    amber(ui)
    setup_page(ui)
    expect(line(ui).locator(".sv-say")).to_have_text("CUSHION100 is setting up: BTC needs $28 more.")
    expect(lights(ui)).to_have_count(3)  # H4, CUSHION100, LATERESID; HPANIC is retired: no light
    expect(lights(ui).nth(1)).to_have_text("CUSHION100")
    expect(lights(ui).nth(1)).to_have_class("sv-lt sv-form")
    expect(lights(ui).nth(0)).to_have_class("sv-lt")
    expect(line(ui).locator(".sv-why")).to_be_hidden()  # the why sits behind a tap
    line(ui).locator("summary").click()
    why = line(ui).locator(".sv-why li")
    expect(why.nth(0)).to_have_text("Needs BTC $100 past the strike, Up at 70¢–90¢, 5:00–10:00 left.")
    expect(why.nth(1)).to_have_text("Have: $72, 78¢, 10:00 left.")
    expect(why.nth(2)).to_have_text("No live record yet.")
    expect(line(ui).locator(".sv-prog span")).to_have_attribute("style", "width:72%")
    ui.no_horizontal_scroll()
    ui.shot("amber")
    green(ui)
    expect(line(ui).locator(".sv-say")).to_have_text("CUSHION100 fits this window.", timeout=6000)
    expect(lights(ui).nth(1)).to_have_class("sv-lt sv-go")
    lights(ui).nth(1).click()  # a light's record on tap
    expect(ui.page.locator("#panel-play .sv-tip")).to_have_text("CUSHION100 fits this window. No live record yet.")
    expect(ui.page.get_by_role("button", name="Fill in ticket")).to_be_visible()
    ui.shot("green")


# -- Fill in ticket, Undo, the slide sends it, the hold note ------------------------------------------------------------
def test_fill_in_ticket_with_undo_then_the_slide_sends_it_and_the_rule_says_hold(ui):
    green(ui)
    setup_page(ui)
    page = ui.page
    fill = page.get_by_role("button", name="Fill in ticket")
    filled = page.locator("#panel-play .sv-filled")
    fill.click()
    expect(filled).to_contain_text("Filled in")
    expect(filled).to_contain_text("CUSHION100 · ▲ Up · Max 88¢ · BTC stop $80,040")
    expect(fill).to_be_hidden()
    expect(page.locator("#panel-play .pl-slide-text small")).to_contain_text("Max price 88¢ · filled in")
    ui.shot("filled")
    filled.get_by_role("button", name="Undo").click()
    expect(filled).to_be_hidden()
    expect(fill).to_be_visible()
    assert not [p for p in ui.server.world.r.ex.posts() if p]  # nothing was ever sent by filling in
    fill.click()
    with page.expect_request("**/api/trading/prepare") as req, page.expect_response("**/api/copilot/shown") as shown:
        drag(ui, 1.0)
    body = req.value.post_data_json
    assert (body["side"], body["price_mode"], body["max_price"]) == ("UP", "limit", 0.88)
    assert body["stop_index_below"] == K + 40 and body["stop_index_above"] is None
    assert shown.value.json() == {"ok": True, "repeat": False}
    rows = ui.server.call(lambda: ui.server.world.store.select("trading_audit", {"event": "copilot_shown"}))
    d = rows[0]["detail"]
    assert d["filled"] == {"rule": "CUSHION100", "side": "UP", "max_price": 0.88, "stop": K + 40}
    assert d["line"]["kind"] == "ready" and {"rule": "CUSHION100", "state": "green"} in d["lights"]
    assert d["btc_gap"] == 120
    hold = page.locator("#panel-play details.sv-ask").filter(has_text="The rule says hold")
    expect(hold.locator(".sv-say")).to_have_text("The rule says hold. Your stop is set.", timeout=6000)
    hold.locator("summary").click()
    expect(hold.locator(".sv-why li").first).to_have_text(
        "CUSHION100 holds to settlement. Your BTC stop is at $80,040.")
    ui.no_horizontal_scroll()
    ui.shot("hold")


# -- the check while you slide ------------------------------------------------------------------------------------------
def test_the_check_while_you_slide_folds_warnings_and_never_blocks(ui):
    def losing_record():
        st = ui.server.world.store
        rows = [{**bracket(i, side="UP", proceeds=0.0, env="shadow"), "id": f"old{i}"} for i in (1, 2)]
        st.insert("trading_brackets", rows)
        st.insert("trading_audit", [{"ts": 1, "event": "copilot_shown", "env": "shadow", "bracket_id": r["id"],
                                     "strategy": "you", "detail": {"btc_gap": -20.0}} for r in rows])
    ui.server.call(losing_record)  # two UP buys with BTC below the strike, both lost
    setup_page(ui, mode="quiet")
    page = ui.page
    check = page.locator("#panel-play .pl-bottom details.sv-check")
    page.locator("#panel-play .pl-sidebtn").filter(has_text="Down").click()  # BTC is $45 over: Down is against it
    expect(check).to_have_count(0)  # nothing until the slide moves
    expect(line(ui)).to_have_count(1)  # Quiet: the warning for the side you picked
    expect(line(ui).locator(".sv-say")).to_have_text("This is against BTC.")
    drag(ui, 0.3, release=False)
    expect(check.locator(".sv-say")).to_have_text("This is against BTC.")
    ui.shot("sliding")
    page.mouse.up()  # under 90%: it snaps back, nothing is sent; the check stays to be read
    check.locator("summary").click()
    expect(check.locator(".sv-why li")).to_have_text("Against BTC: you've won 0 of 2, −$2.44 (only 2 trades).")
    assert not [p for p in ui.server.world.r.ex.posts() if p]
    # Up is with BTC, but at 58c it is in the 50-75c band, where those two losses were bought
    page.locator("#panel-play .pl-sidebtn").filter(has_text="Up").click()
    expect(check).to_have_count(0)  # a new side: a new check, once the slide moves
    drag(ui, 0.3, release=False)
    expect(check.locator(".sv-say")).to_have_text("This is at 50–75¢.")
    page.mouse.up()
    ui.no_horizontal_scroll()


# -- Settings ----------------------------------------------------------------------------------------------------------
def test_settings_off_quiet_full_and_the_warning_switches(ui):
    green(ui)
    asked = []
    ui.page.on("request", lambda r: asked.append(r.url) if "/api/copilot/" in r.url else None)
    setup_page(ui, mode=None)
    page = ui.page
    page.wait_for_timeout(2500)  # at least one more Play poll
    expect(lights(ui)).to_have_count(0)
    expect(line(ui)).to_have_count(0)
    assert asked == []  # Off: nothing is asked
    page.locator("#panel-play .pl-top").get_by_role("button", name="Settings").click()
    sheet = page.locator("#pl-settings")
    mode = sheet.get_by_role("radiogroup", name="Ask Settleview mode")
    expect(mode.get_by_role("radio", name="Off")).to_have_attribute("aria-checked", "true")
    expect(sheet.get_by_text("Never places, approves or cancels orders. Runs only while Play is open.")).to_be_visible()
    mode.get_by_role("radio", name="Quiet").click()
    expect(sheet.get_by_text("Quiet: warnings only. Full adds rule matches, tickets and hold advice.")).to_be_visible()
    warn = sheet.locator("details.sv-warnset")
    expect(warn.locator("summary small")).to_have_text("3 on")
    warn.locator("summary").click()
    warn.get_by_label("Against BTC").uncheck()
    expect(warn.locator("summary small")).to_have_text("2 on")
    assert page.evaluate("localStorage.getItem('pl.copilot.warn')") == "late,band_50_75"
    ui.no_horizontal_scroll()
    ui.shot("settings")
    sheet.get_by_role("button", name="Close").click()
    page.wait_for_timeout(2500)
    expect(lights(ui)).to_have_count(0)  # Quiet: no lights, no rule line
    expect(line(ui)).to_have_count(0)
    assert any("mode=quiet" in u and "warn=late%2Cband_50_75" in u for u in asked)
    page.locator("#panel-play .pl-top").get_by_role("button", name="Settings").click()
    mode.get_by_role("radio", name="Full").click()
    sheet.get_by_role("button", name="Close").click()
    expect(lights(ui)).to_have_count(3)
    expect(line(ui).locator(".sv-say")).to_have_text("CUSHION100 fits this window.")
    page.reload()
    expect(lights(ui)).to_have_count(3)  # kept on this device
    assert page.evaluate("localStorage.getItem('pl.copilot')") == "full"
