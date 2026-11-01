"""The design pass (2026-10-31) in a real browser: the tab bar (Play · Agents · Record) and the address it keeps; the
venue segment and the market picker (Kalshi -> BTC 15m · BTC Perps, "off" with one empty-state card; Polymarket ->
BTC 15m · BTC 5m); the quiet KILL and its engaged state; the Record tab's numbers from a seeded database (the daily
summary's computation); the logo files; no sideways scroll at 390 px; and the four screens in the four looks
(screenshots in tests/e2e/.artifacts/). The app is tests/e2e/server.py; the agent accessory and its Stop are in
tests/e2e/test_agent_panel.py (test_a_launch_shows_the_running_view..., test_stop_on_the_accessory...)."""

from __future__ import annotations

import re
import struct
from datetime import datetime, UTC
from pathlib import Path

import pytest
from playwright.sync_api import expect

from btc_signal_engine.application import record_summary
from btc_signal_engine.application.play import PlayVenue
from tests.e2e.conftest import ARTIFACTS, Guard, pair_context
from tests.e2e.test_agent_panel import LAUNCH, open_perps, tab
from tests.trading.helpers import NOW

pytestmark = pytest.mark.e2e

# a first visit lands on Kalshi 15m (only when nothing is stored, so a reload keeps what the test picked)
KALSHI = ("if (!localStorage.getItem('play.venue')) { localStorage.setItem('play.venue', 'kalshi');"
          " localStorage.setItem('play.span.kalshi', '15m'); }")


def on_kalshi(ui, path="/#play"):
    ui.page.add_init_script(KALSHI)
    ui.page.goto(path)
    expect(ui.page.locator("#panel-play .pl-verdict")).to_be_visible()


def picker(ui, panel="#panel-play"):
    return ui.page.locator(f"{panel} .nv-pick")


def menu(ui, panel="#panel-play"):
    return ui.page.locator(f"{panel} .nv-menu")


# -- the tab bar ----------------------------------------------------------------------------------------------------
def test_the_tab_bar_moves_between_play_agent_and_record_and_the_address_follows(ui):
    page = ui.page
    on_kalshi(ui)
    bar = page.get_by_role("navigation", name="Sections")
    expect(bar.get_by_role("tab")).to_have_text(["Play", "Agents", "Record"])  # one word each, always there
    for t in bar.get_by_role("tab").all():
        box = t.bounding_box()
        assert box["height"] >= 44 and box["width"] >= 44, box  # 44 pt targets
    expect(tab(ui, "Play")).to_have_attribute("aria-selected", "true")
    tab(ui, "Record").click()
    expect(page.locator("#panel-record")).to_be_visible()
    expect(page.locator("#panel-play")).to_be_hidden()
    expect(page).to_have_url(re.compile(r"#record$"))
    expect(tab(ui, "Record")).to_have_attribute("aria-selected", "true")
    tab(ui, "Agents").click()
    expect(page.locator("#panel-agent")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#agent$"))
    page.reload()  # the address brings the same tab back
    expect(page.locator("#panel-agent")).to_be_visible()
    expect(tab(ui, "Agents")).to_have_attribute("aria-selected", "true")
    # Play's tab reopens the last Play market: BTC Perps once it was picked there
    tab(ui, "Play").click()
    expect(page.locator("#panel-play")).to_be_visible()
    open_perps(ui)
    expect(page).to_have_url(re.compile(r"#perps$"))
    tab(ui, "Record").click()
    tab(ui, "Play").click()
    expect(page.locator("#panel-perps")).to_be_visible()
    expect(tab(ui, "Play")).to_have_attribute("aria-selected", "true")  # Perps is Play's
    page.goto("/#nowhere")  # an unknown address: Play, with a note (never a silent redirect)
    expect(page.locator("#panel-play .pl-route")).to_contain_text('There is no "#nowhere" screen')
    # the glass is on the chrome only (with a solid fallback under Reduce Transparency, in the CSS)
    blur = page.locator("#tabbar").evaluate("e => getComputedStyle(e).backdropFilter || getComputedStyle(e).webkitBackdropFilter")
    assert "blur" in blur, blur
    assert page.locator("#panel-play .pl-bottom").evaluate("e => getComputedStyle(e).backdropFilter") in ("none", "")
    ui.guard.check()


# -- the market picker ------------------------------------------------------------------------------------------------
def test_kalshis_picker_offers_btc_15m_and_btc_perps_which_is_off_here(ui):
    page = ui.page
    on_kalshi(ui)
    expect(page.locator("#panel-play .nv-seg").get_by_role("radio", name="Kalshi")).to_have_attribute("aria-checked", "true")
    expect(picker(ui).locator("b")).to_have_text("BTC 15m▾")
    expect(picker(ui).locator(".nv-sub")).to_have_text(re.compile(r"^\d\d:\d\d–\d\d:\d\d · beat \$\d{2},\d{3}$"))
    # Perps is no longer a tab of its own: no venue radio and no tab named Perps
    expect(page.get_by_role("radio", name="Perps")).to_have_count(0)
    expect(page.get_by_role("tab", name="Perps")).to_have_count(0)
    picker(ui).click()
    items = menu(ui).get_by_role("menuitemradio")
    expect(items).to_have_count(2)
    expect(items.nth(0)).to_contain_text("BTC 15m")
    expect(items.nth(0)).to_have_attribute("aria-checked", "true")
    expect(items.nth(1)).to_contain_text("BTC Perps")
    expect(items.nth(1).locator("small")).to_have_text("off")  # the server has perps switched off
    ui.no_horizontal_scroll()
    ui.shot("kalshi-picker")
    items.nth(1).click()
    perps = page.locator("#panel-perps")
    expect(perps).to_be_visible()
    expect(picker(ui, "#panel-perps").locator("b")).to_have_text("BTC Perps · off▾")
    # ONE empty-state card says why; nothing else on the screen (no money line, banners, chart or slide)
    expect(perps.locator(".pl-choose:visible")).to_have_count(1)
    expect(perps.locator(".nv-empty h2")).to_have_text("Perps are switched off")
    expect(perps.locator(".nv-money")).to_be_hidden()
    expect(perps.locator(".pl-bottom")).to_be_hidden()
    expect(perps.locator(".pv-chartcard")).to_be_hidden()
    expect(perps.locator(".pv-banners")).to_be_hidden()
    ui.no_horizontal_scroll()
    ui.shot("perps-off")
    perps.get_by_role("button", name="Open BTC 15m").click()
    expect(page.locator("#panel-play")).to_be_visible()
    expect(picker(ui).locator("b")).to_have_text("BTC 15m▾")


def add_polymarket(ui):
    """The e2e world has Kalshi only: add Polymarket's 15 min and 5 min tabs on the same (shadow) service, as a server
    with Polymarket offers them (test-only)."""
    def go():
        w = ui.server.world
        k = w.r.play.venues["kalshi"]
        w.r.play.venues["polymarket"] = PlayVenue(k.svc, quote_fn=k.quote_fn)
        w.r.play.venues["polymarket_5m"] = PlayVenue(k.svc, quote_fn=k.quote_fn, window_ms=300_000)
    ui.server.call(go)


def test_polymarkets_picker_switches_between_btc_15m_and_btc_5m(ui):
    page = ui.page
    add_polymarket(ui)
    on_kalshi(ui)
    page.locator("#panel-play .nv-seg").get_by_role("radio", name="Polymarket").click()
    # no remembered length: the choice is made in plain sight (never a silent default)
    expect(picker(ui).locator("b")).to_have_text("Pick a market▾")
    expect(page.locator("#panel-play .pl-choose:visible h2")).to_have_text("Polymarket: pick a market length.")
    picker(ui).click()
    items = menu(ui).get_by_role("menuitemradio")
    expect(items).to_have_count(2)
    expect(items).to_have_text(["BTC 15m", "BTC 5m"], use_inner_text=True)
    ui.shot("polymarket-picker")
    items.nth(1).click()
    expect(picker(ui).locator("b")).to_have_text("BTC 5m▾")
    expect(page.locator("#panel-play .pl-verdict")).to_be_visible()
    assert page.evaluate("localStorage.getItem('play.span.polymarket')") == "5m"
    # the slide names the market length first: a 5-min buy is never mistaken for a 15-min one
    expect(page.locator("#panel-play .pl-slide-text small")).to_contain_text(re.compile(r"^5 min · "))
    picker(ui).click()
    expect(menu(ui).get_by_role("menuitemradio", name="BTC 5m")).to_have_attribute("aria-checked", "true")
    menu(ui).get_by_role("menuitemradio", name="BTC 15m").click()
    expect(picker(ui).locator("b")).to_have_text("BTC 15m▾")
    assert page.evaluate("localStorage.getItem('play.span.polymarket')") == "15m"
    page.reload()  # the venue and its length are remembered (the same keys as before the redesign)
    expect(page.locator("#panel-play .nv-seg").get_by_role("radio", name="Polymarket")).to_have_attribute("aria-checked", "true")
    expect(picker(ui).locator("b")).to_have_text("BTC 15m▾")
    ui.no_horizontal_scroll()


# -- KILL -------------------------------------------------------------------------------------------------------------
def test_kill_is_quiet_until_engaged_then_red_and_resume_asks_first(ui):
    page = ui.page
    on_kalshi(ui)
    kill = page.locator("#panel-play .pl-kill")
    expect(kill).to_have_text("KILL")
    expect(kill).not_to_have_class(re.compile(r"\bis-on\b"))
    quiet = kill.evaluate("e => getComputedStyle(e).backgroundColor")
    red = page.evaluate("() => { const d = document.createElement('i'); d.style.color = 'var(--down-strong)';"
                        " document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; }")
    assert quiet != red, (quiet, red)  # neutral, not a red fill
    box = kill.bounding_box()
    assert box["width"] >= 44 and box["height"] >= 44
    ui.shot("kill-quiet")
    kill.click()  # no confirmation to turn it on (the guard fails the test on any dialog)
    expect(kill).to_have_class(re.compile(r"\bis-on\b"))
    expect(kill).to_have_text("Resume")
    expect(kill).to_have_attribute("aria-pressed", "true")
    assert kill.evaluate("e => getComputedStyle(e).backgroundColor") == red  # the one red fill: it is engaged
    expect(page.locator("#panel-play .pl-killed")).to_be_visible()
    assert page.request.get("/api/trading/status", params={"strategy": "you"}).json()["killed"] is True
    ui.no_horizontal_scroll()
    ui.shot("kill-engaged")
    ui.guard.accept_dialogs = True
    kill.click()  # Resume asks first, as before
    expect(kill).to_have_text("KILL")
    assert ui.guard.dialogs and ui.guard.dialogs[-1].startswith("Resume?")
    assert page.request.get("/api/trading/status", params={"strategy": "you"}).json()["killed"] is False


# -- Record -----------------------------------------------------------------------------------------------------------
def bracket(bid, *, closed_ts, cost, proceeds, session_id=None, side="UP"):
    return {"id": bid, "env": "shadow", "session_id": session_id, "kind": "entry", "ticker": "KXBTC15M-T", "side": side,
            "window_end": closed_ts, "stake": cost, "contracts": 1.0, "limit_price": 0.6, "take_profit": 0.0,
            "stop": 0.0, "max_cost": cost, "ticket_hash": "h", "state": "closed", "filled": 1.0, "held": 0.0,
            "tp_filled": 0.0, "entry_avg": 0.6, "entry_cost": cost, "proceeds": proceeds, "exit_reason": "settled",
            "created_ts": closed_ts - 600_000, "updated_ts": closed_ts, "closed_ts": closed_ts, "strategy": "you"}


def seed_record(ui) -> str:
    """Today: your win (+$0.40) and an agent CUSHION100 loss (−$0.61); three days ago: your loss (−$0.50)."""
    def go():
        w = ui.server.world
        spec = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 20.0, "per_trade": 10.0, "ends_ts": NOW + 3_600_000,
                "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto", "rules": ["CUSHION100", "H4"]}
        out = w.r.ag.launch(spec, NOW)
        assert out["ok"], out
        sid = out["id"]
        w.store.insert("trading_brackets", [bracket("mine1", closed_ts=NOW - 120_000, cost=0.60, proceeds=1.00),
                                            bracket("ag1", closed_ts=NOW - 60_000, cost=0.61, proceeds=0.0,
                                                    session_id=sid, side="DOWN"),
                                            bracket("old1", closed_ts=NOW - 3 * 86_400_000, cost=0.50, proceeds=0.0)],
                       ignore_conflicts=False)
        w.r.ag.journal("rule", {"rule": "CUSHION100", "market": "kalshi_15m", "ok": True, "bracket_id": "ag1",
                                "inputs": {}}, NOW - 600_000, session_id=sid)
        w.r.ag.note_usage(sid, NOW, api_usd=0.05, wakes=3)
        w.r.ag.stop(sid, NOW, "stopped")
        return sid
    return ui.server.call(go)


def test_record_shows_the_daily_summarys_numbers_for_today_week_and_all(ui):
    page = ui.page
    seed_record(ui)
    want = {r: ui.server.call(lambda r=r: record_summary.summary(ui.server.world.store, NOW, r)) for r in record_summary.RANGES}
    assert (want["today"]["net"], want["today"]["trades"], want["today"]["wins"]) == (-0.21, 2, 1)
    page.goto("/#record")
    rec = page.locator("#panel-record")
    expect(rec.locator(".nv-title")).to_have_text("Record")
    seg = rec.get_by_role("radiogroup", name="Range")
    expect(seg.get_by_role("radio")).to_have_text(["Today", "Week", "All"])
    expect(seg.get_by_role("radio", name="Today")).to_have_attribute("aria-checked", "true")
    card = rec.locator(".rc-card")
    expect(card.locator(".pl-eyebrow")).to_have_text("Today · all venues")
    expect(card.locator(".rc-hero")).to_have_text("−$0.21")
    expect(card.locator(".rc-hero")).to_have_class(re.compile(r"\bpl-down\b"))  # colour and a sign
    expect(card.locator(".rc-hero-row > span")).to_have_text("2 trades · 1 won")
    expect(card.locator(".rc-you")).to_have_text("You+$0.401 trade")
    expect(card.locator(".rc-agent")).to_have_text("Agent−$0.611 trade")
    expect(card.locator(".rc-api")).to_have_text("API$0.053 wakes")
    rules = rec.locator(".rc-rules .rc-row:visible")
    expect(rules).to_have_count(2)
    expect(rules.nth(0)).to_have_text("CUSHION100 0/1 won · −$0.61", use_inner_text=True)
    expect(rules.nth(0).locator(".rc-val.pl-down")).to_have_text("−$0.61")
    expect(rules.nth(1)).to_have_text("H4 no trades", use_inner_text=True)
    ui.no_horizontal_scroll()
    ui.shot("record-today")
    seg.get_by_role("radio", name="Week").click()
    expect(card.locator(".pl-eyebrow")).to_have_text("Week · all venues")
    expect(card.locator(".rc-hero")).to_have_text("−$0.71")
    expect(card.locator(".rc-you")).to_have_text("You−$0.102 trades")
    assert want["week"]["net"] == -0.71 and want["all"]["trades"] == 3
    seg.get_by_role("radio", name="All").click()
    expect(card.locator(".rc-hero-row > span")).to_have_text("3 trades · 1 won")
    page.reload()  # the range is remembered on this device
    expect(seg.get_by_role("radio", name="All")).to_have_attribute("aria-checked", "true")
    # Your trades (Play's record view) and each market's mode moved here from Play
    expect(page.locator("#panel-play .pl-hist")).to_have_count(0)
    expect(rec.locator("#rc-trades summary")).to_contain_text("Your trades")
    modes = rec.locator(".rc-modes details")
    expect(modes).to_have_count(1)
    expect(modes.nth(0).locator("summary")).to_have_text("Kalshi · BTC 15mShadow")
    modes.nth(0).locator("summary").click()
    expect(modes.nth(0).locator("select")).to_have_value("shadow")
    expect(modes.nth(0).get_by_role("button", name="Switch mode")).to_be_disabled()  # nothing changed yet


def test_an_unknown_stored_range_is_said_and_today_is_shown(ui):
    ui.page.add_init_script("localStorage.setItem('record.range', 'month')")
    ui.page.goto("/#record")
    rec = ui.page.locator("#panel-record")
    expect(rec.get_by_role("radio", name="Today")).to_have_attribute("aria-checked", "true")
    expect(rec.get_by_text("The saved range (“month”) was not recognised")).to_be_visible()


# -- the logo -------------------------------------------------------------------------------------------------------
def png_size(body: bytes) -> tuple[int, int]:
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", body[16:24])


def test_the_logo_files_are_served_and_linked(ui):
    page = ui.page
    on_kalshi(ui)
    for path, size in (("/icons/favicon-32.png", 32), ("/icons/apple-touch-icon.png", 180),
                       ("/icons/settleview-512.png", 512), ("/icons/icon-192.png", 192), ("/icons/icon-512.png", 512)):
        r = page.request.get(path)
        assert r.ok and r.headers["content-type"] == "image/png", path
        assert png_size(r.body()) == (size, size), path
    for path in ("/icons/favicon.svg", "/icon.svg", "/icons/mark.svg"):
        r = page.request.get(path)
        assert r.ok and "<svg" in r.text(), path
    head = {k: page.locator(f"head link[rel='{k}']").evaluate_all("ls => ls.map(l => l.getAttribute('href'))")
            for k in ("icon", "apple-touch-icon", "manifest")}
    assert head == {"icon": ["icons/favicon.svg", "icons/favicon-32.png"], "apple-touch-icon": ["icons/apple-touch-icon.png"],
                    "manifest": ["manifest.json"]}
    icons = page.request.get("/manifest.json").json()["icons"]
    assert [i["src"] for i in icons] == ["icon.svg", "icons/icon-192.png", "icons/icon-512.png"]
    # the mark: the Agent tab's icon and Ask Settleview's identifier; no sparkle anywhere
    expect(tab(ui, "Agents").locator("use")).to_have_attribute("href", "#sv-mark")
    assert "✦" not in page.content()


# -- 390 px and the four screens in the four looks ---------------------------------------------------------------------
def test_no_sideways_scroll_at_390px_on_any_screen(browser, app_server):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True,
                              base_url=app_server.url)
    pair_context(ctx, app_server)  # the paired PC browser
    try:
        page = ctx.new_page()
        page.add_init_script(KALSHI)
        assert page.request.post("/api/agent/launch", data=LAUNCH).json()["ok"]
        for h, sel in (("play", "#panel-play .pl-verdict"), ("perps", "#panel-perps .nv-empty"),
                       ("agent", "#panel-agent .ag-fleet .ag-ag"), ("record", "#panel-record .rc-card")):
            page.goto("/#" + h)
            expect(page.locator(sel)).to_be_visible()
            expect(page.locator("#ag-acc")).to_be_visible()
            w = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
            assert w[0] <= w[1] == 390, (h, w)
    finally:
        ctx.close()


DOCS_IMAGES = Path(__file__).resolve().parents[2] / "docs" / "images"
READY = {"play": "#panel-play .pl-verdict", "perps": "#panel-perps .nv-empty", "agent": "#panel-agent .ag-fleet .ag-ag",
         "record": "#panel-record .rc-card"}


@pytest.mark.parametrize("screen", list(READY))
def test_the_four_screens_in_each_look(browser, app_server, look, screen):
    """Play, Perps, Agents (the fleet with one agent) and Record in the four looks, saved to docs/images/<screen>-<phone|desktop>-<light|dark>.png
    for the README (and to tests/e2e/.artifacts/). Deterministic: the test app's data only (shadow exchange, no real
    balance, account or order id), the clock frozen at the test clock's NOW, UTC, no motion."""
    ctx = browser.new_context(viewport=look["viewport"], color_scheme=look["color_scheme"], is_mobile=look["phone"],
                              has_touch=look["phone"], base_url=app_server.url, timezone_id="UTC", locale="en-US",
                              reduced_motion="reduce")
    pair_context(ctx, app_server)  # the paired PC browser
    try:
        page = ctx.new_page()
        guard = Guard()
        guard.watch(page)
        page.clock.set_fixed_time(datetime.fromtimestamp(NOW / 1000, UTC))
        if screen == "record":  # two of your own test trades today, so the card shows figures
            app_server.call(lambda: app_server.world.store.insert("trading_brackets", [
                bracket("mine1", closed_ts=NOW - 120_000, cost=0.60, proceeds=1.00),
                bracket("mine2", closed_ts=NOW - 60_000, cost=0.45, proceeds=0.0, side="DOWN")], ignore_conflicts=False))
        assert page.request.post("/api/agent/launch", data=LAUNCH).json()["ok"]
        page.add_init_script(KALSHI)
        page.goto("/#" + screen)
        expect(page.locator(READY[screen])).to_be_visible()
        expect(page.locator("#ag-acc")).to_be_visible()
        if screen == "play":
            page.locator("#panel-play .pl-sidebtn").first.click()  # a side picked: the dock shows its terms
            expect(page.locator("#panel-play .pl-slide-text small")).to_be_visible()
            expect(page.locator("#panel-play .pl-ptb-chart canvas").first).to_be_visible()
        page.mouse.move(0, 0)
        page.wait_for_timeout(2500 if screen == "play" else 800)  # the chart's polls have landed
        if look["phone"]:
            w = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
            assert w[0] <= w[1], w
        name = f"{screen}-{'phone' if look['phone'] else 'desktop'}-{look['color_scheme']}.png"
        DOCS_IMAGES.mkdir(parents=True, exist_ok=True)
        ARTIFACTS.mkdir(exist_ok=True)
        shot, last = None, None
        for _ in range(12):  # settled: two shots in a row are the same picture (charts draw on their own frames)
            shot = page.screenshot(full_page=False, animations="disabled", caret="hide")
            if shot == last:
                break
            last = shot
            page.wait_for_timeout(500)
        (DOCS_IMAGES / name).write_bytes(shot)
        (ARTIFACTS / f"design_pass-{name}").write_bytes(shot)
        guard.check()
    finally:
        ctx.close()
