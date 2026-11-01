"""Play's live data in a real browser (review P1, B6#1-5, #8, #9; B5#1-2): the window is pushed over one stream (no
polling of /api/play/now), with one presence heartbeat; a failed read greys the slide and turns it off, with a line
saying since when, until the stream is back; a hidden page stops the stream and the timers; the slide approves only a
ticket that matches what it showed (another one opens the confirm sheet with what changed, and nothing is approved);
a signed-out phone gets one clear prompt; Record states break-even with fees and the zone its days run in. The app is
tests/e2e/server.py (shadow exchange, no network)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from tests.application.test_your_record import bracket
from tests.e2e.server import PIN
from tests.e2e.test_agent_panel import tab
from tests.e2e.test_copilot import drag, green, setup_page

pytestmark = pytest.mark.e2e

ONE_LOOK = pytest.mark.parametrize("look", ["phone-light", "desktop-dark"], indirect=True)


def knob(ui):
    return ui.page.locator("#panel-play .pl-slide-knob")


def slide_text(ui):
    return ui.page.locator("#panel-play .pl-slide-text > span")


def live(ui):
    expect(slide_text(ui)).to_contain_text("Slide to buy", timeout=8000)
    expect(knob(ui)).to_be_enabled()


def api_paths(reqs) -> list[str]:
    return [urlparse(u).path for _, u in reqs]


@ONE_LOOK
def test_play_is_pushed_over_one_stream_with_one_heartbeat_and_no_polling(ui):
    reqs: list = []
    ui.page.on("request", lambda r: reqs.append((r.method, r.url)))
    setup_page(ui, mode=None)
    live(ui)
    before = api_paths(reqs)
    assert before.count("/api/play/stream") == 1, before
    assert before.count("/api/trading/heartbeat") == 1, before  # the app's one presence heartbeat
    reqs.clear()
    green(ui)  # the market moves on the server: the stream brings it, no request is made for it
    expect(ui.page.locator("#panel-play .pl-sidebtn").first).to_contain_text("86¢", timeout=4000)
    ui.page.wait_for_timeout(3000)
    after = [p for p in api_paths(reqs) if p.startswith("/api/")]
    assert "/api/play/now" not in after and "/api/play/stream" not in after, after  # no polling, one stream
    assert "/api/play/scoreboard" not in after, after  # read only while an outcome is awaited, or on Record
    assert "/api/trading/heartbeat" not in after, after  # every 30 s, not per screen


@ONE_LOOK
def test_a_failed_read_greys_the_slide_and_turns_it_off_until_the_stream_is_back(ui):
    ui.guard.allow("/api/play/now")
    setup_page(ui, mode=None)
    live(ui)
    play = ui.server.world.r.play

    def boom(*a, **kw):
        raise RuntimeError("the database is away")

    ui.server.call(lambda: setattr(play, "now_view", boom))
    note = ui.page.locator("#panel-play .pl-stale-note")
    expect(note).to_contain_text(re.compile(r"Live prices stopped at \d\d:\d\d:\d\d \(The server could not read the market just now\)"))
    expect(note).to_contain_text("buying and selling are off until they are back")
    expect(ui.page.locator("#panel-play .pl-bottom")).to_have_class(re.compile(r"\bpl-stale\b"))
    expect(slide_text(ui)).to_have_text("Live prices stopped")
    expect(knob(ui)).to_be_disabled()
    ui.no_horizontal_scroll()
    ui.shot("stale")
    ui.server.call(lambda: delattr(play, "now_view"))  # the read works again: the next frame is live
    expect(note).to_be_hidden(timeout=5000)
    live(ui)


@ONE_LOOK
def test_a_hidden_page_closes_the_stream_and_stops_every_timer(ui):
    reqs: list = []
    ui.page.on("request", lambda r: reqs.append((r.method, r.url)))
    setup_page(ui, mode=None)
    live(ui)
    ui.page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    expect(knob(ui)).to_be_disabled()
    reqs.clear()
    ui.page.wait_for_timeout(6000)  # past the kill switch's 5 s poll and the heartbeat's re-check
    assert [p for p in api_paths(reqs) if p.startswith("/api/")] == []
    ui.page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    live(ui)
    assert api_paths(reqs).count("/api/play/stream") == 1  # one new stream on the way back


@ONE_LOOK
def test_a_slide_whose_ticket_differs_opens_the_sheet_and_approves_nothing(ui):
    ui.page.add_init_script("localStorage.setItem('pl.order', 'limit');")
    setup_page(ui, mode=None)
    live(ui)
    page = ui.page

    def tamper(route):
        resp = route.fetch()
        body = resp.json()
        body["ticket"]["limit_price"] = 0.97  # not the max price the slide showed
        route.fulfill(response=resp, json=body)

    page.route("**/api/trading/prepare", tamper)
    approves: list = []
    page.on("request", lambda r: approves.append(r.post_data_json) if r.url.endswith("/api/trading/approve") else None)
    drag(ui, 1.0)
    why = page.locator("#panel-play .pl-ticketwhy")
    expect(why).to_contain_text(re.compile(r"This order is not what the slide showed: its max price is 97¢, not \d+¢"))
    expect(page.locator("#pl-sheet-title")).to_have_text("Buy ▲ Up?")
    page.wait_for_timeout(500)
    assert approves == []  # nothing was approved unseen
    ui.shot("ticket-differs")
    page.locator("#panel-play .pl-sheet[aria-modal='true']").get_by_role("button", name="Cancel").click()
    expect(why).to_be_hidden()
    assert [a["decision"] for a in approves] == ["no"]
    assert not [p for p in ui.server.world.r.ex.posts() if p]  # no order reached the exchange


@ONE_LOOK
def test_a_matching_slide_approves_the_values_it_showed(ui):
    ui.page.add_init_script("localStorage.setItem('pl.order', 'limit');")
    setup_page(ui, mode=None)
    live(ui)
    page = ui.page
    with page.expect_request("**/api/trading/prepare") as prep, page.expect_request("**/api/trading/approve") as appr:
        drag(ui, 1.0)
    sent, shown = prep.value.post_data_json, appr.value.post_data_json
    assert shown["decision"] == "yes"
    t = shown["ticket"]
    assert (t["side"], t["stake"], t["price_mode"], t["limit_price"]) == ("UP", sent["stake"], "limit", sent["max_price"])
    expect(page.locator("#panel-play p.pl-msg[role='status']")).to_contain_text("Order sent")


@pytest.mark.parametrize("look", ["phone-light", "phone-dark"], indirect=True)
@pytest.mark.peer("192.168.1.50")
def test_a_phone_whose_session_ended_is_told_to_sign_in_again(ui, pc_request):
    assert pc_request.post("/api/access/pin", data={"pin": PIN, "pin_again": PIN}).json()["ok"]
    page = ui.page
    ui.guard.allow("/api/")  # the phone's 403 on the PC-only card, then every 401 once it is signed out
    login = page.request.post("/login", form={"pin": PIN}, max_redirects=0)
    assert login.status == 303, login.status
    setup_page(ui, mode=None)
    live(ui)
    assert pc_request.post("/api/access/signout").ok  # the PC signs every phone out
    prompt = page.get_by_role("alertdialog")
    expect(prompt).to_be_visible(timeout=8000)
    expect(prompt.get_by_role("heading")).to_have_text("You are signed out")
    expect(prompt.get_by_role("link", name="Sign in again")).to_have_attribute("href", "/login")
    expect(page.locator("#panel-play .pl-stale-note")).to_have_text("Live prices are off: you are signed out.")
    expect(knob(ui)).to_be_disabled()
    ui.no_horizontal_scroll()
    ui.shot("signed-out")


@pytest.mark.parametrize("look", ["phone-light", "desktop-light"], indirect=True)
def test_record_states_break_even_with_fees_and_the_zone_its_days_run_in(ui):
    def trades():
        rows = [{**bracket(1, proceeds=2.0, env="shadow"), "id": "be1"},  # won: 2 at 60c + 2c fee = 61c a contract
                {**bracket(2, proceeds=0.0, env="shadow"), "id": "be2"}]  # lost
        ui.server.world.store.insert("trading_brackets", rows)
    ui.server.call(trades)
    setup_page(ui, mode=None)
    tab(ui, "Record").click()
    expect(ui.page.locator("#panel-record .rc-foot").first).to_contain_text("Days run midnight to midnight UTC")
    ui.page.locator("#rc-trades summary").click()
    grid = ui.page.locator("#rc-trades .pl-grid3")
    expect(grid.locator("div").nth(0)).to_contain_text("50%")
    expect(grid.locator("div").nth(1)).to_have_text(re.compile(r"Break-even\s*61%"))
    expect(grid.locator("div").nth(2)).to_contain_text("-11 pts")  # 50% won against a 61% break-even
    expect(ui.page.locator("#rc-trades")).to_contain_text("Break-even = what you paid a contract, fees included.")
    ui.shot("break-even")
