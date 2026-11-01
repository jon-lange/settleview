"""The Agents tab as a fleet in a real browser (docs/plans/2026-10-31-0010-feat-multi-agent-plan.md U6 / U6b and the
approved fleet wireframe): two agents (Kalshi 15m and Polymarket 15m) together, "Needs you" first, many agents by
market, All activity across an agent's runs with run dividers, the fleet's audit, New agent (one market first, the
typed START and the slide), the accessory's total and its on-screen "Stop all?", Play's "agents here" line, Fleet
limits, Record "By agent" with Clone and Retire; and the screenshots for the README (docs/images/agents-*,
new-agent-*, record-by-agent-*). The app is tests/e2e/server.py (shadow exchange, scripted fake harness: no network,
no model, no key); the second market is a test-only shadow stand-in (server.ShadowMarket)."""

from __future__ import annotations

import re
from datetime import datetime, UTC

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ARTIFACTS, Guard, pair_context
from tests.e2e.test_agent_panel import (accessory, fill_new, launch_api, new_form, open_agent, open_new,
                                        open_run, pick_market, slide_to_start, tab)
from tests.e2e.test_design_pass import DOCS_IMAGES, KALSHI, bracket
from tests.trading.helpers import NOW

pytestmark = pytest.mark.e2e


def fleet(ui):
    return ui.page.locator("#panel-agent .ag-fleet")


def agent_rows(ui):
    """The agents' rows of the fleet that are not in "Needs you" (a few agents: one row each)."""
    return fleet(ui).locator(".ag-group.ag-agents:not(.ag-idle) .ag-ag:visible")


def two_agents(ui) -> tuple[dict, dict]:
    """A Kalshi 15m agent (through the launch route: a real runner) and a Polymarket 15m agent beside it."""
    w = ui.server.world
    ui.server.call(w.add_polymarket)
    k = launch_api(ui, rules=["CUSHION100", "H4"])
    p = launch_api(ui, venues=None, market="polymarket_15m", rules=["LATERESID"])
    return k, p


def held(ui, sid: str, bid: str, *, side="UP", cost=1.0, rule="CUSHION100", market="kalshi_15m", strategy="you"):
    """An open, filled position of an agent session that a rule entry opened (its "rule" journal row)."""
    w = ui.server.world

    def go():
        w.store.insert("trading_brackets", [{
            "id": bid, "env": "shadow", "strategy": strategy, "session_id": sid, "kind": "entry", "ticker": "KXBTC15M-T",
            "side": side, "window_end": NOW + 600_000, "stake": cost, "contracts": 1.16, "limit_price": 0.9,
            "take_profit": 0.99, "stop": 0.0, "max_cost": cost, "ticket_hash": "h", "state": "open", "filled": 1.16,
            "held": 1.16, "tp_filled": 0.0, "entry_avg": 0.86, "entry_cost": cost, "entry_fees": 0.0,
            "tp_fees": 0.0, "exit_fees": 0.0, "proceeds": 0.0, "created_ts": NOW - 60_000, "updated_ts": NOW - 60_000}],
            ignore_conflicts=False)
        w.r.ag.journal("rule", {"rule": rule, "market": market, "ok": True, "bracket_id": bid, "inputs": {}},
                       NOW - 60_000, session_id=sid)
    ui.server.call(go)


def closed(ui, sid: str, bid: str, *, cost: float, proceeds: float, ts: int):
    ui.server.call(lambda: ui.server.world.store.insert("trading_brackets", [
        {**bracket(bid, closed_ts=ts, cost=cost, proceeds=proceeds), "session_id": sid}], ignore_conflicts=False))


# -- the fleet ------------------------------------------------------------------------------------------------------
def test_two_agents_kalshi_and_polymarket_show_together_with_the_total_and_stop_all_asks_on_screen(ui):
    page = ui.page
    k, p = two_agents(ui)
    closed(ui, k["id"], "k-win", cost=0.86, proceeds=1.0, ts=NOW - 30_000)  # +$0.14 for the Kalshi agent
    open_agent(ui)
    expect(page.locator("#panel-agent .nv-title")).to_have_text("Agents")
    expect(tab(ui, "Agents")).to_have_attribute("aria-selected", "true")
    card = fleet(ui).locator(".ag-sum")
    expect(card.locator(".ag-hero")).to_have_text("+$0.14")
    expect(card.locator(".ag-sumhead span")).to_have_text("today · 2 agents")
    expect(card.locator(".ag-t-win")).to_contain_text("Winning1of 2")
    expect(card.locator(".ag-t-budget")).to_contain_text("of $40.00")
    rows = agent_rows(ui)
    expect(rows).to_have_count(2)
    expect(rows.locator(".ag-nm")).to_have_text(["Kalshi 15m agent", "Polymarket 15m agent"])
    expect(rows.nth(0).locator(".ag-agsub")).to_have_text(re.compile(r"^Watching · CUSHION100, H4 · (59m|1h) left$"))
    expect(rows.nth(1).locator(".ag-agsub")).to_have_text(re.compile(r"^Watching · LATERESID · (59m|1h) left$"))
    expect(rows.nth(0).locator(".ag-r b")).to_have_text("+$0.14")
    expect(rows.nth(0).locator(".ag-r b")).to_have_class(re.compile(r"\bgreen\b"))
    expect(rows.nth(0).locator("svg.ag-spark")).to_have_class(re.compile(r"\bis-up\b"))
    expect(rows.nth(1).locator(".ag-r b")).to_have_text("$0.00")
    expect(fleet(ui).locator(".ag-need")).to_be_hidden()
    expect(fleet(ui).locator(".ag-limits-row .ag-val")).to_have_text("off")
    acc = accessory(ui)
    expect(acc.locator(".nv-acc-open b")).to_have_text("2 agents")
    expect(acc.locator(".nv-acc-t")).to_have_text("· +$0.14")
    expect(acc).not_to_have_class(re.compile(r"\bis-amber\b"))
    ui.no_horizontal_scroll()
    ui.shot("two-agents")
    # Stop all: the screen asks first (the server asks nothing); Cancel keeps both running
    acc.get_by_role("button", name="Stop all agents").click()
    ask = acc.get_by_role("alertdialog", name="Stop all agents?")
    expect(ask.locator("b")).to_have_text("Stop all 2 agents?")
    ui.shot("stop-all-ask")
    ask.get_by_role("button", name="Cancel").click()
    expect(ask).to_have_count(0)
    w = ui.server.world
    assert len(ui.server.call(w.r.ag.live)) == 2
    acc.get_by_role("button", name="Stop all agents").click()
    with page.expect_response("**/api/agent/stop_all") as resp:
        acc.get_by_role("button", name="Stop all").click()
    assert resp.value.json()["ok"] and len(resp.value.json()["stopped"]) == 2
    expect(page.locator(".nv-toast")).to_contain_text("Stopped 2 agents.")
    expect(acc).to_be_hidden()
    assert ui.server.call(w.r.ag.live) == []
    # both are "Not running" now, with their last run
    idle = fleet(ui).locator(".ag-idle .ag-ag")
    expect(idle).to_have_count(2)
    expect(idle.nth(0).locator(".ag-agsub")).to_contain_text("stopped by you")


def test_an_agent_that_needs_you_comes_first_in_amber_and_the_accessory_says_so(ui):
    page = ui.page
    w = ui.server.world
    a = ui.server.call(lambda: w.run_agent(name="Steady"))
    b = ui.server.call(lambda: w.run_agent(name="Stuck one"))
    ui.server.call(lambda: w.run_agent(name="Calm"))
    ui.server.call(lambda: w.r.ag.pause(b["id"], NOW, "The model is unavailable: overloaded"))
    open_agent(ui)
    head = fleet(ui).locator(".ag-need-h")
    expect(head).to_have_text("Needs you · 1")
    need = fleet(ui).locator(".ag-need .ag-ag")
    expect(need).to_have_count(1)
    expect(need.locator(".ag-nm")).to_have_text("Stuck one")
    expect(need.locator(".ag-agsub")).to_have_text("Paused: The model is unavailable: overloaded")
    first = page.evaluate("""() => {
        const n = document.querySelector('#panel-agent .ag-need'), r = document.querySelector('#panel-agent .ag-group.ag-agents:not(.ag-need):not(.ag-idle)');
        return !!(n.compareDocumentPosition(r) & Node.DOCUMENT_POSITION_FOLLOWING);
    }""")
    assert first  # "Needs you" sits above the others
    expect(agent_rows(ui).locator(".ag-nm")).to_have_text(["Steady", "Calm"])
    acc = accessory(ui)
    expect(acc).to_have_class(re.compile(r"\bis-amber\b"))
    expect(acc.locator(".nv-acc-t")).to_have_text("· $0.00 · 1 needs you")
    ui.no_horizontal_scroll()
    ui.shot("needs-you")
    need.click()  # its own screen, with the reason and the one action it allows
    expect(page.locator("#panel-agent .ag-paused b")).to_have_text("The model is unavailable: overloaded")
    expect(page.locator("#panel-agent .nv-title")).to_have_text("Stuck one")
    assert a["agent_id"]


def test_many_agents_fold_into_markets_with_one_sentence_and_a_market_opens_its_agents(ui):
    w = ui.server.world
    ui.server.call(w.add_polymarket)
    for i in range(7):
        ui.server.call(lambda i=i: w.run_agent(name=f"K{i + 1}"))
    for i in range(3):
        ui.server.call(lambda i=i: w.run_agent(name=f"P{i + 1}", market="polymarket_15m"))
    best = ui.server.call(w.r.ag.live)[0]["id"]
    closed(ui, best, "best-win", cost=0.5, proceeds=1.0, ts=NOW - 20_000)
    open_agent(ui)
    expect(fleet(ui).locator(".ag-sumhead span")).to_have_text("today · 10 agents")
    bym = fleet(ui).locator(".ag-bymkt .ag-row")
    expect(bym).to_have_count(2)
    expect(bym.nth(0)).to_contain_text("Kalshi 15 min")
    expect(bym.nth(0).locator(".ag-val")).to_have_text("7 · +$0.50")
    expect(bym.nth(1).locator(".ag-val")).to_have_text("3 · $0.00")
    expect(fleet(ui).locator(".ag-normal")).to_have_text("10 running normally · Best: K1 +$0.50.")
    expect(agent_rows(ui)).to_have_count(0)
    ui.no_horizontal_scroll()
    ui.shot("many")
    bym.nth(1).click()
    expect(fleet(ui).locator(".ag-mktbar")).to_contain_text("Only Polymarket 15 min")
    expect(agent_rows(ui).locator(".ag-nm")).to_have_text(["P1", "P2", "P3"])
    fleet(ui).locator(".ag-mktbar").get_by_role("button", name="Show all").click()
    expect(bym).to_have_count(2)


# -- one agent across its runs; the audit ------------------------------------------------------------------------------
def test_all_activity_spans_every_run_of_the_agent_with_a_divider_per_run(ui):
    page = ui.page
    w = ui.server.world
    first = launch_api(ui)
    note = page.request.post(f"/api/agent/sessions/{first['id']}/note", data={"text": "First run note"}).json()
    assert note["ok"], note
    assert page.request.post(f"/api/agent/sessions/{first['id']}/stop").json()["ok"]
    w.clock.t += 120_000
    again = launch_api(ui, venues=None, agent_id=first["agent_id"])
    assert again["agent_id"] == first["agent_id"] and again["id"] != first["id"]
    open_run(ui, "Kalshi 15m agent")
    expect(page.locator("#panel-agent .ag-status .ag-state")).to_have_text("Watching")
    sec = page.locator("#panel-agent .ag-activity")
    if not sec.is_visible():
        page.locator("#panel-agent .ag-more:visible").first.click()
    sec.locator(".ag-chips").get_by_role("button", name="Everything", exact=True).click()
    divs = sec.locator(".ag-run-div")
    expect(divs).to_have_count(2)
    expect(divs.nth(0)).to_have_text(re.compile(r"^Run 2 · since \d\d:\d\d · running · \$0\.00 · 0 trades$"))
    expect(divs.nth(1)).to_have_text(re.compile(r"^Run 1 · \d\d:\d\d–\d\d:\d\d · stopped by you · \$0\.00 · 0 trades$"))
    expect(sec.locator("details.ag-ev.is-note")).to_contain_text("Your note: “First run note”")
    order = page.evaluate("""() => Array.from(document.querySelectorAll('#panel-agent .ag-jr > .ag-jr-item, #panel-agent .ag-jr .ag-run-div, #panel-agent .ag-jr details.ag-ev'))
        .map(e => e.classList.contains('ag-run-div') ? 'run' : e.classList.contains('is-note') ? 'note' : e.tagName === 'DETAILS' ? 'row' : '')
        .filter(Boolean)""")
    assert order.index("note") > order.index("run", 1), order  # the note is under Run 1's divider
    ui.no_horizontal_scroll()
    ui.shot("cross-run")
    sec.locator(".ag-chips").get_by_role("button", name="Decisions", exact=True).click()
    expect(sec.locator(".ag-ev-empty")).to_have_text("No decisions recorded yet.")


def test_the_fleet_audit_lists_every_agents_rows_with_the_same_filters(ui):
    page = ui.page
    k, p = two_agents(ui)
    for x in (k, p):
        assert page.request.post(f"/api/agent/sessions/{x['id']}/note", data={"text": "hi " + x["id"][-4:]}).json()["ok"]
    open_agent(ui)
    fleet(ui).locator(".ag-audit-row").click()
    expect(page.locator("#panel-agent .nv-title")).to_have_text("All agents")
    sec = page.locator("#panel-agent .ag-activity")
    expect(sec.locator(".ag-chips button[aria-pressed=true]")).to_have_text("Everything")
    divs = sec.locator(".ag-run-div")  # newest first across agents: a divider wherever the agent changes
    expect(divs.first).to_contain_text("Polymarket 15m agent · Run 1")
    expect(divs.nth(1)).to_contain_text("Kalshi 15m agent · Run 1")
    expect(sec.locator("details.ag-ev.is-note")).to_have_count(2)
    sec.locator(".ag-chips").get_by_role("button", name="Trades", exact=True).click()
    expect(sec.locator(".ag-ev-empty")).to_have_text("No trades yet.")
    ui.no_horizontal_scroll()
    ui.shot("audit")
    page.locator("#panel-agent .ag-top-back").click()
    expect(fleet(ui)).to_be_visible()


# -- New agent -------------------------------------------------------------------------------------------------------
def test_new_agent_picks_one_market_and_a_short_slide_snaps_back_a_full_one_starts_it(ui):
    page = ui.page
    w = ui.server.world
    ui.server.call(w.add_polymarket)
    k = launch_api(ui, rules=["H4"])
    open_new(ui)
    form = new_form(ui)
    cards = form.locator(".ag-mk")
    expect(cards.locator("b")).to_have_text(["Kalshi 15 min", "Polymarket 15 min", "Polymarket 5 min"])
    expect(cards.nth(0).locator("span")).to_have_text("3 rules · Shadow · also traded by “Kalshi 15m agent”")
    expect(cards.nth(1).locator("span")).to_have_text("3 rules · Shadow")
    expect(cards.nth(2).locator("span")).to_have_text("Off in this app")
    expect(cards.nth(2)).to_be_disabled()
    pick_market(form, "Kalshi 15 min")
    expect(cards.nth(0)).to_have_attribute("aria-checked", "true")
    expect(form.get_by_label("Agent name")).to_have_value("Kalshi 15m agent 2")  # the server's next name
    fill_new(ui, confirm="")
    knob = form.locator(".ag-slide-knob")
    expect(knob).to_be_disabled()
    expect(form.locator(".ag-slide-text small")).to_have_text("Type START, then slide.")
    form.locator("#ag-confirm").fill("START")
    expect(knob).to_be_enabled()
    ui.shot("new-agent")
    slide_to_start(ui, fraction=0.4)  # not far enough: nothing is sent
    expect(knob).to_have_attribute("style", re.compile(r"translateX\(0px\)"))
    assert len(ui.server.call(w.r.ag.live)) == 1
    form.get_by_label("Agent name").fill("Night H4")
    with page.expect_response("**/api/agent/launch") as resp:
        slide_to_start(ui)
    out = resp.value.json()
    assert out["ok"], out
    assert out["session"]["name"] == "Night H4" and out["agent_id"] != k["agent_id"]
    expect(page.locator("#panel-agent .nv-title")).to_have_text("Night H4")
    expect(page.locator("#panel-agent .ag-also")).to_have_text(
        "Also traded by Kalshi 15m agent. Each agent touches only its own positions.")
    expect(page.locator(".nv-toast")).to_have_text("Night H4 started.")


def test_new_agent_on_a_market_without_rules_says_the_model_judges_alone(ui):
    ui.server.call(ui.server.world.add_polymarket)
    ui.server.call(lambda: ui.server.world.r.ag.services.__setitem__("you_poly5", ui.server.world.r.ag.services["you_poly"]))
    open_new(ui)
    form = new_form(ui)
    card = form.locator(".ag-mk").filter(has_text="Polymarket 5 min")
    expect(card.locator("span")).to_have_text("No rules yet: the model’s judgment only · Shadow")
    card.click()
    expect(form.locator(".ag-norules")).to_have_text("No rules yet for this market: the model’s judgment only.")
    expect(form.get_by_label("Agent name")).to_have_value("Polymarket 5m agent")


# -- Play: the agents on the market you are looking at -------------------------------------------------------------
def test_play_says_which_agents_are_here_and_what_they_hold_and_opens_them(ui):
    page = ui.page
    k = launch_api(ui, rules=["CUSHION100"])
    ui.server.call(lambda: ui.server.world.run_agent(name="Second look"))
    held(ui, k["id"], "k-held")
    page.add_init_script(KALSHI)
    page.goto("/#play")
    line = page.locator("#panel-play .sv-here")
    expect(line).to_have_text("2 agents here · one holds Up $1.00 (CUSHION100)›", use_inner_text=False)
    ui.no_horizontal_scroll()
    ui.shot("agents-here")
    line.click()
    expect(page.locator("#panel-agent .ag-fleet")).to_be_visible()
    expect(page.locator("#panel-agent .ag-mktbar")).to_contain_text("Only Kalshi 15 min")
    expect(agent_rows(ui).locator(".ag-nm")).to_have_text(["Kalshi 15m agent", "Second look"])
    expect(agent_rows(ui).nth(0).locator(".ag-agsub")).to_contain_text("Holding Up $1.00 · CUSHION100")


# -- Fleet limits ----------------------------------------------------------------------------------------------------
def test_fleet_limits_are_off_until_set_on_the_pc(ui):
    page = ui.page
    open_agent(ui)
    fleet(ui).locator(".ag-limits-row").click()
    box = page.locator("#panel-agent .ag-glim")
    expect(page.locator("#panel-agent .nv-title")).to_have_text("Fleet limits")
    expect(box.get_by_label("Fleet max loss per day $ (empty: off)")).to_have_value("")
    box.get_by_label("Fleet max loss per day $ (empty: off)").fill("abc")
    box.get_by_role("button", name="Save fleet limits").click()
    expect(box.locator("p[role=status]")).to_have_text("Enter a dollar amount above $0, or leave it empty for off.")
    box.get_by_label("Fleet max loss per day $ (empty: off)").fill("3")
    with page.expect_response("**/api/agent/global_limits") as resp:
        box.get_by_role("button", name="Save fleet limits").click()
    assert resp.value.json()["limits"] == {"max_loss_day": 3.0, "api_day": None}
    expect(box.locator("p[role=status]")).to_have_text("Saved.")
    ui.shot("fleet-limits")
    page.locator("#panel-agent .ag-top-back").click()
    expect(fleet(ui).locator(".ag-limits-row .ag-val")).to_have_text("$3.00 loss a day")


# -- Record: by agent ------------------------------------------------------------------------------------------------
def seed_by_agent(ui) -> tuple[dict, dict]:
    """Two agents today: Kalshi 15m agent +$0.14 (API $0.02), Polymarket 15m agent −$0.30 (API $0.01); both stopped."""
    w = ui.server.world
    k, p = two_agents(ui)
    closed(ui, k["id"], "k1", cost=0.86, proceeds=1.0, ts=NOW - 50_000)
    closed(ui, p["id"], "p1", cost=0.30, proceeds=0.0, ts=NOW - 40_000)
    ui.server.call(lambda: w.r.ag.note_usage(k["id"], NOW, api_usd=0.02))
    ui.server.call(lambda: w.r.ag.note_usage(p["id"], NOW, api_usd=0.01))
    for x in (k, p):
        assert ui.page.request.post(f"/api/agent/sessions/{x['id']}/stop").json()["ok"]
    return k, p


def test_record_by_agent_ranks_net_after_api_and_clones_and_retires(ui):
    page = ui.page
    k, p = seed_by_agent(ui)
    page.goto("/#record")
    rec = page.locator("#panel-record")
    by = rec.get_by_role("radiogroup", name="Group results")
    expect(by.get_by_role("radio")).to_have_text(["By rule", "By agent"])
    by.get_by_role("radio", name="By agent").click()
    rows = rec.locator(".rc-agents details.rc-ag")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".rc-ag-name")).to_have_text("Kalshi 15m agent")
    expect(rows.nth(0).locator(".rc-ag-t small")).to_have_text("1 trade · 1 won · API $0.02")
    expect(rows.nth(0).locator(".rc-ag-r b")).to_have_text("+$0.12")
    expect(rows.nth(0).locator(".rc-ag-r small")).to_have_text("after API")
    expect(rows.nth(1).locator(".rc-ag-name")).to_have_text("Polymarket 15m agent")
    expect(rows.nth(1).locator(".rc-ag-r b")).to_have_text("−$0.31")
    expect(rec.locator(".rc-clone-best")).to_have_text("Clone the best agent (Kalshi 15m agent)›", use_inner_text=False)
    ui.no_horizontal_scroll()
    ui.shot("by-agent")
    # Retire the Polymarket agent: asked on screen, then archived (it keeps its runs)
    rows.nth(1).locator("summary").click()
    rows.nth(1).get_by_role("button", name="Retire").click()
    expect(rows.nth(1).locator(".rc-ask")).to_contain_text("Retire Polymarket 15m agent? It keeps its runs")
    with page.expect_response("**/archive") as resp:
        rows.nth(1).locator(".rc-ask").get_by_role("button", name="Retire").click()
    assert resp.value.json()["ok"], resp.value.json()
    expect(rec.locator(".rc-byagent p.rc-foot")).to_have_text("Polymarket 15m agent is retired. Its runs stay in the record.")
    expect(rows.nth(1).locator(".rc-ag-t small")).to_contain_text("retired")
    # Clone the Kalshi agent: New agent, filled in from its last launch, under a new name
    rows.nth(0).locator("summary").click()
    rows.nth(0).get_by_role("button", name="Clone").click()
    form = new_form(ui)
    expect(form).to_be_visible()
    expect(form.get_by_text("A clone: the same setup under a new name (a new agent).")).to_be_visible()
    expect(form.locator(".ag-mk[aria-checked=true] b")).to_have_text("Kalshi 15 min")
    expect(form.get_by_label("Agent name")).to_have_value("Kalshi 15m agent copy")
    expect(form.locator(".ag-rules label.ag-check").filter(has_text="CUSHION100").locator("input")).to_be_checked()
    expect(form.get_by_label("Max loss (budget) $")).to_have_value("20")
    form.locator("#ag-confirm").fill("START")
    with page.expect_response("**/api/agent/launch") as resp:
        slide_to_start(ui)
    out = resp.value.json()
    assert out["ok"] and out["session"]["name"] == "Kalshi 15m agent copy" and out["agent_id"] != k["agent_id"]
    agents = page.request.get("/api/agent/agents").json()["agents"]
    assert "Polymarket 15m agent" not in [a["name"] for a in agents]  # retired: not in the fleet any more


def test_retire_is_for_the_pc_and_a_running_agent_is_refused(ui):
    page = ui.page
    k = launch_api(ui)
    closed(ui, k["id"], "k1", cost=0.86, proceeds=1.0, ts=NOW - 50_000)
    page.goto("/#record")
    rec = page.locator("#panel-record")
    rec.get_by_role("radiogroup", name="Group results").get_by_role("radio", name="By agent").click()
    row = rec.locator(".rc-agents details.rc-ag").first
    row.locator("summary").click()
    row.get_by_role("button", name="Retire").click()
    row.locator(".rc-ask").get_by_role("button", name="Retire").click()
    expect(rec.locator(".rc-byagent p.rc-foot")).to_have_text("Kalshi 15m agent is running; stop it first.")


# -- the README's pictures: Agents (a few, many), New agent, Record by agent in the four looks --------------------------
def _fleet_world(server, screen):
    w = server.world
    server.call(w.add_polymarket)
    if screen == "agents-many":
        for i in range(9):
            server.call(lambda i=i: w.run_agent(name=f"Kalshi 15m · H4 #{i + 1}", rules=["H4"]))
        for i in range(4):
            server.call(lambda i=i: w.run_agent(name=f"Polymarket 15m · L{i + 1}", market="polymarket_15m",
                                                rules=["LATERESID"]))
        live = server.call(w.r.ag.live)
        server.call(lambda: w.r.ag.pause(live[3]["id"], NOW, "92% of its budget used"))
        return
    a = server.call(lambda: w.run_agent(rules=["CUSHION100", "H4"]))
    server.call(lambda: w.run_agent(market="polymarket_15m", rules=["LATERESID"]))
    server.call(lambda: w.store.insert("trading_brackets", [
        {**bracket("shot1", closed_ts=NOW - 300_000, cost=0.86, proceeds=1.0), "session_id": a["id"]},
        {**bracket("shot2", closed_ts=NOW - 120_000, cost=0.80, proceeds=1.0), "session_id": a["id"]}],
        ignore_conflicts=False))
    server.call(lambda: w.r.ag.note_usage(a["id"], NOW, api_usd=0.03))


SHOTS = {"agents-few": ("/#agent", "#panel-agent .ag-fleet .ag-ag"),
         "agents-many": ("/#agent", "#panel-agent .ag-bymkt .ag-row"),
         "new-agent": ("/#agent", "#panel-agent form.ag-launch .ag-slide"),
         "record-by-agent": ("/#record", "#panel-record .rc-agents .rc-ag")}


@pytest.mark.parametrize("screen", list(SHOTS))
def test_the_fleet_screens_in_each_look(browser, app_server, look, screen):
    """Saved to docs/images/<screen>-<phone|desktop>-<light|dark>.png for the README (and tests/e2e/.artifacts/).
    Deterministic: the test app's data only (shadow, no real balance, account or order id), the clock frozen at the
    test clock's NOW, UTC, no motion."""
    ctx = browser.new_context(viewport=look["viewport"], color_scheme=look["color_scheme"], is_mobile=look["phone"],
                              has_touch=look["phone"], base_url=app_server.url, timezone_id="UTC", locale="en-US",
                              reduced_motion="reduce")
    pair_context(ctx, app_server)  # the paired PC browser
    try:
        page = ctx.new_page()
        guard = Guard()
        guard.watch(page)
        page.clock.set_fixed_time(datetime.fromtimestamp(NOW / 1000, UTC))
        _fleet_world(app_server, screen)
        if screen == "record-by-agent":
            page.add_init_script("localStorage.setItem('record.by', 'agent')")
        path, ready = SHOTS[screen]
        page.goto(path)
        if screen == "new-agent":
            page.get_by_role("button", name="New agent").first.click()
            form = page.locator("#panel-agent form.ag-launch")
            form.locator(".ag-mk").filter(has_text="Kalshi 15 min").click()
            form.locator(".ag-rules label.ag-check").filter(has_text="H4").locator("input").check()
        expect(page.locator(ready).first).to_be_visible()
        page.mouse.move(0, 0)
        page.wait_for_timeout(900)
        name = f"{screen}-{'phone' if look['phone'] else 'desktop'}-{look['color_scheme']}.png"
        DOCS_IMAGES.mkdir(parents=True, exist_ok=True)
        ARTIFACTS.mkdir(exist_ok=True)
        shot = page.screenshot(full_page=False, animations="disabled", caret="hide")
        (DOCS_IMAGES / name).write_bytes(shot)
        (ARTIFACTS / f"fleet-{name}").write_bytes(shot)
        if look["phone"]:
            w = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
            assert w[0] <= w[1], w
        guard.check()
    finally:
        ctx.close()


def test_the_launch_body_is_the_one_market_and_the_fleet_reads_the_per_session_routes(ui):
    """The page names its session on every call: no deprecated single-session route is requested."""
    page = ui.page
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.method + " " + r.url.split(ui.server.url, 1)[-1].split("?")[0]))
    open_new(ui)
    form = fill_new(ui)
    with page.expect_request("**/api/agent/launch") as req:
        slide_to_start(ui)
    body = req.value.post_data_json
    assert body["market"] == "kalshi_15m" and "venues" not in body and body["confirm"] == "START"
    assert "name" not in body and "agent_id" not in body  # the server's default name: sent as neither
    expect(ui.page.locator("#panel-agent .ag-status")).to_be_visible()
    page.wait_for_timeout(500)
    old = {"POST /api/agent/stop", "POST /api/agent/resume", "POST /api/agent/note", "GET /api/agent/journal",
           "GET /api/agent/decisions", "GET /api/agent/stream"}
    assert not old & set(seen), sorted(old & set(seen))
    assert any(s.startswith("GET /api/agent/sessions/") and s.endswith("/stream") for s in seen), seen
    assert form is not None
