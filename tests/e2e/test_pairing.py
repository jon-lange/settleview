"""Pairing this PC's browser (P0 #1, adapters/http/access.py) in a real browser: before pairing every page is "Pair
this browser" and /api answers 401; the one-time link from `ops pair-pc` fills the code in, Pair sets the PC cookie and
opens Play as the PC (the Phone access card shows); the same code never pairs a second browser; "Sign out PC browsers"
on the card brings the pairing page back. The app is tests/e2e/server.py (no PC listener: a loopback browser is in
the PC's place)."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.unpaired]


def new_code(ui) -> str:
    code, _ = ui.server.world.pc.new_code()  # what `python -m btc_signal_engine.ops pair-pc` prints
    return code


@pytest.mark.parametrize("look", ["desktop-light", "phone-dark"], indirect=True)
def test_an_unpaired_browser_sees_only_the_pairing_page_then_pairs_with_the_link(ui, browser):
    page = ui.page
    ui.guard.allow("/")  # the pairing page itself answers 401 until the browser is paired
    page.goto("/#play")
    expect(page.get_by_role("heading", name="Pair this browser")).to_be_visible()
    expect(page.get_by_text("python -m btc_signal_engine.ops pair-pc", exact=True)).to_be_visible()
    assert page.request.get("/api/play/limits").status == 401
    ui.no_horizontal_scroll()
    ui.shot("unpaired")

    code = new_code(ui)
    page.goto(f"/pair?code={code}")
    expect(page.get_by_label("Pairing code")).to_have_value(code)  # the link only fills it in: nothing paired yet
    assert page.request.get("/api/access").status == 401
    page.get_by_role("button", name="Pair").click()
    expect(page).to_have_url(f"{ui.server.url}/#play")
    assert page.request.get("/api/access").ok  # the PC-only card's route: this browser is the PC now
    page.get_by_role("button", name="Settings").first.click()
    card = page.locator("section[aria-labelledby='pa-title']")
    expect(card.get_by_role("button", name="Sign out PC browsers")).to_be_visible()
    ui.shot("paired")

    # the same code in another browser: used up
    other = browser.new_context(base_url=ui.server.url)
    try:
        p2 = other.new_page()
        p2.goto(f"/pair?code={code}")
        p2.get_by_role("button", name="Pair").click()
        expect(p2.get_by_role("alert")).to_contain_text("No pairing code is waiting")
        assert p2.request.get("/api/access").status == 401
    finally:
        other.close()

    # Sign out PC browsers: this one too; the page comes back as the pairing page
    ui.guard.accept_dialogs = True
    card.get_by_role("button", name="Sign out PC browsers").click()
    expect(page.get_by_role("heading", name="Pair this browser")).to_be_visible()
    assert page.request.get("/api/access").status == 401


@pytest.mark.parametrize("look", ["desktop-dark"], indirect=True)
def test_a_wrong_code_says_so_and_pairs_nothing(ui):
    page = ui.page
    ui.guard.allow("/")
    new_code(ui)
    page.goto("/pair")
    page.get_by_label("Pairing code").fill("AAAA-BBBB-CCCC")
    page.get_by_role("button", name="Pair").click()
    expect(page.get_by_role("alert")).to_have_text("Wrong pairing code.")
    assert page.request.get("/api/access").status == 401
