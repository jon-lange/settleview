"""One agent on the Agents tab in a real browser (U9, the 2026-10-30 refactor to the approved wireframe, and the
fleet's U6: the per-session view is now one agent's screen, for its live run, else its last). The fleet is the tab's
page (its own tests: tests/e2e/test_fleet_ui.py); New agent is a sheet: one market first, then name, level, rules,
limits and model (Model collapsed), presets, the typed START and a slide. One agent's run: the status card (with its
own Stop), tiles, open positions, Latest (trades and acting decisions only), rows that open their own screens (note,
run settings, scorecard, instructions, Telegram) and All activity with its filters (the full live feed of each wake
under Thinking). Ended: the run's summary and "Run this agent again". Paused states, Stop from the tab bar's
accessory on another tab, the prompt editor; desktop and phone sizes, light and dark. The app is tests/e2e/server.py
(shadow exchange, scripted fake harness: no network, no model, no key; presets and Telegram by their contract from
tests/e2e/stubs.py); fixtures in tests/e2e/conftest.py."""

from __future__ import annotations

import re
import threading

import pytest
from playwright.sync_api import expect

from tests.agent.fake_harness import text, thinking, tool_use, turn
from tests.agent.test_tools import decision, entry
from tests.e2e.server import DEFAULT_PROMPT, PIN
from tests.trading.helpers import NOW

pytestmark = pytest.mark.e2e

LAUNCH = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 20.0, "per_trade": 10.0, "minutes": 60,
          "spend_cap": 5.0, "model": "claude-haiku-5-5", "level": "auto", "confirm": "START"}


def launch_api(ui, **kw) -> dict:
    out = ui.page.request.post("/api/agent/launch", data={k: v for k, v in {**LAUNCH, **kw}.items() if v is not None}).json()
    assert out["ok"], out
    return out


def open_agent(ui):
    """The Agents tab: the fleet (from one agent's screen: its "‹ Agents")."""
    ui.page.goto("/#agent")
    expect(ui.page.locator("#panel-agent")).to_be_visible()
    if ui.page.locator("#panel-agent .ag-top-back").is_visible():
        ui.page.locator("#panel-agent .ag-top-back").click()
    expect(ui.page.locator("#panel-agent .ag-fleet")).to_be_visible()


def open_run(ui, name: str | None = None):
    """One agent's screen (its live run, else its last), from its row on the fleet."""
    open_agent(ui)
    rows = ui.page.locator("#panel-agent .ag-fleet .ag-ag")
    (rows.filter(has_text=name) if name else rows).first.click()
    expect(ui.page.locator("#panel-agent .ag-live:visible, #panel-agent .ag-ended:visible")).to_have_count(1)


def open_new(ui):
    """New agent (the fleet's +)."""
    open_agent(ui)
    ui.page.get_by_role("button", name="New agent").first.click()
    expect(new_form(ui)).to_be_visible()


def status_card(ui):
    return ui.page.locator("#panel-agent .ag-status")


def new_form(ui):
    return ui.page.locator("form.ag-launch")


def wide(ui) -> bool:
    return not ui.look["phone"]


def advanced(form):
    """The Model group (collapsed): who trades, the key, the model, the API cap, instructions."""
    d = form.locator("details.ag-adv")
    if not d.evaluate("d => d.open"):
        d.locator("summary").click()
    return d


def row(ui, name: str):
    """A visible row button of the Agents tab (rows open their own screens)."""
    return ui.page.locator("#panel-agent button.ag-row:visible").filter(has_text=name).first


def back(ui):
    ui.page.locator("#panel-agent .ag-bar .ag-back:visible").first.click()


def activity(ui, chip: str | None = None):
    """All activity: its own screen on the phone, the middle column on a wide screen while the run is live."""
    page = ui.page
    sec = page.locator("#panel-agent .ag-activity")
    if not sec.is_visible():
        page.locator("#panel-agent .ag-more:visible, #panel-agent button.ag-row:visible:has-text('All activity')").first.click()
    expect(sec).to_be_visible()
    if chip:
        sec.locator(".ag-chips").get_by_role("button", name=chip, exact=True).click()
    return sec


def tab(ui, name: str):
    """A tab of the tab bar (Play · Agents · Record)."""
    return ui.page.get_by_role("navigation", name="Sections").get_by_role("tab", name=name)


def accessory(ui):
    """The tab bar's fleet accessory (on every tab while any agent runs)."""
    return ui.page.locator("#ag-acc")


def open_perps(ui):
    """Kalshi's market picker -> BTC Perps (Perps is a market of Kalshi, not a tab)."""
    page = ui.page
    page.locator("#panel-play .nv-pick").click()
    page.locator("#panel-play .nv-menu").get_by_role("menuitemradio", name="BTC Perps").click()
    expect(page.locator("#panel-perps")).to_be_visible()


def pick_market(form, label="Kalshi 15 min"):
    form.locator(".ag-mk").filter(has_text=label).click()


def slide_to_start(ui, fraction: float = 1.0):
    """Drag the slide's knob `fraction` of the way (the whole way starts the agent)."""
    page = ui.page
    knob = new_form(ui).locator(".ag-slide-knob")
    knob.scroll_into_view_if_needed()
    track = new_form(ui).locator(".ag-slide").bounding_box()
    k = knob.bounding_box()
    x0, y = k["x"] + k["width"] / 2, k["y"] + k["height"] / 2
    page.mouse.move(x0, y)
    page.mouse.down()
    page.mouse.move(x0 + (track["width"] - k["width"]) * fraction, y, steps=12)
    page.mouse.up()


def fill_new(ui, *, model="claude-haiku-5-5", confirm="START", market="Kalshi 15 min"):
    form = new_form(ui)
    if market and form.locator(".ag-mk[aria-checked=true]").count() == 0:
        pick_market(form, market)
    if model:
        advanced(form).locator("select").select_option(model)
    form.get_by_label("Max loss (budget) $").fill("20")
    form.get_by_label("Per-trade cap $").fill("10")
    form.get_by_label("Runs for (minutes)").fill("60")
    advanced(form).get_by_label("API spend cap $").fill("5")
    form.locator("#ag-confirm").fill(confirm)
    return form


# -- New agent -----------------------------------------------------------------------------------------------------
def test_the_agents_tab_shows_the_fleet_and_new_agent_picks_one_market_first(ui):
    page = ui.page
    page.goto("/")
    # a first visit (no venue picked yet): the chooser asks for a venue; the tab bar reaches Agents from anywhere
    expect(page.locator("#panel-play .pl-choose").first).to_be_visible()
    agents_tab = tab(ui, "Agents")
    expect(agents_tab).to_have_attribute("aria-selected", "false")
    agents_tab.click()
    expect(agents_tab).to_have_attribute("aria-selected", "true")
    expect(page).to_have_url(re.compile(r"#agent$"))
    fl = page.locator("#panel-agent .ag-fleet")
    expect(fl).to_be_visible()
    expect(page.locator("#panel-agent .nv-title")).to_have_text("Agents")
    expect(fl.locator(".ag-sum .ag-hero")).to_have_text("$0.00")
    expect(fl.locator(".ag-sum .ag-sumhead span")).to_have_text("today · 0 agents")
    expect(fl.locator(".ag-ask-t")).to_have_text("No agent is running.")
    expect(fl.locator(".ag-limits-row .ag-val")).to_have_text("off")
    expect(status_card(ui)).to_be_hidden()
    expect(accessory(ui)).to_be_hidden()
    ui.shot("fleet-empty")
    page.get_by_role("button", name="New agent").first.click()
    form = new_form(ui)
    expect(form).to_be_visible()
    expect(page.locator("#panel-agent .nv-title")).to_have_text("New agent")
    status = page.request.get("/api/agent/status").json()
    cards = form.locator(".ag-mk")
    expect(cards).to_have_count(len(status["venues"]))
    for i, (_k, v) in enumerate(status["venues"].items()):
        expect(cards.nth(i).locator("b")).to_have_text(v["label"])
        if v["mode"] is None:
            expect(cards.nth(i).locator("span")).to_have_text("Off in this app")
            expect(cards.nth(i)).to_be_disabled()
    expect(cards.nth(0).locator("span")).to_have_text("3 rules · Shadow")
    # nothing else until a market is picked; the slide waits for it
    expect(form.get_by_label("Agent name")).to_be_hidden()
    expect(form.get_by_label("Max loss (budget) $")).to_be_hidden()
    expect(form.locator(".ag-slide-text small")).to_have_text("Pick a market first.")
    expect(form.locator(".ag-slide-knob")).to_be_disabled()
    pick_market(form)
    expect(cards.nth(0)).to_have_attribute("aria-checked", "true")
    expect(form.get_by_label("Agent name")).to_have_value("Kalshi 15m agent")  # the server's name for it
    expect(form.locator(".ag-foot").filter(has_text="The server’s name for the next Kalshi 15 min agent")).to_be_visible()
    level = form.get_by_role("radiogroup", name="How much control")
    expect(level.get_by_role("radio", name="Auto")).to_have_attribute("aria-checked", "true")
    expect(form.get_by_text("Auto: the agent trades on its own inside these limits.")).to_be_visible()
    expect(form.get_by_label("Proposal window (10 to 60 seconds)")).to_be_hidden()
    # the market's rules, off by default; a retired rule (HPANIC, retired by the user 2026-10-30) is never offered
    shown = [x for x in status["rules"] if "kalshi_15m" in x["markets"] and x["status"]["status"] != "retired"]
    assert [x["id"] for x in shown] == ["H4", "CUSHION100", "LATERESID"]
    rules = form.locator(".ag-rules label.ag-check:not(.ag-one-bet)")
    expect(rules).to_have_count(len(shown))
    for i, x in enumerate(shown):
        expect(rules.nth(i)).to_contain_text(x["id"])
        expect(rules.nth(i).locator("small")).to_have_text(x["description"])
        expect(rules.nth(i).locator(".ag-rule-rec")).to_have_text("no live trades")  # the scorecard's live record
        expect(rules.nth(i).locator("input")).not_to_be_checked()
    one = form.locator(".ag-rules label.ag-one-bet")
    expect(one).to_contain_text("One bet per window")
    expect(one.locator("input")).to_be_checked()  # on by default
    # Model is collapsed: model, key, API cap, instructions and the external agent are one tap deeper
    adv = form.locator("details.ag-adv")
    assert not adv.evaluate("d => d.open")
    expect(adv.locator("summary .ag-val")).to_have_text("no model picked")
    ui.shot("new-agent")
    advanced(form)
    expect(form.locator(".ag-key b")).to_have_text("present ✓ · checked with a cheap call at launch")
    options = form.locator("select option")
    expect(options).to_have_count(1 + len(status["models"]))
    for m in status["models"]:
        p = m["usd_per_mtok"]
        expect(form.locator(f"select option[value='{m['id']}']")).to_have_text(
            f"{m['id']} · ${p['input']:g} in / ${p['output']:g} out per MTok")  # JS number text
    expect(adv.get_by_role("radio", name="External (MCP)")).to_be_visible()
    expect(form.locator(".ag-slide-text small")).to_have_text("Type START, then slide.")
    ui.no_horizontal_scroll()
    page.locator("#panel-agent .ag-top-back").click()
    expect(page.locator("#panel-agent .ag-fleet")).to_be_visible()


def test_launch_needs_START_and_shows_the_servers_refusal_verbatim(ui):
    open_new(ui)
    form = fill_new(ui, model=None, confirm="")
    knob = form.locator(".ag-slide-knob")
    for typed in ("", "start", "yes", "STAR"):
        form.locator("#ag-confirm").fill(typed)
        expect(knob).to_be_disabled()  # nothing is sent without the typed START
    form.locator("#ag-confirm").fill("START")
    with ui.page.expect_response("**/api/agent/launch") as resp:
        slide_to_start(ui)
    out = resp.value.json()
    assert out["ok"] is False and out["code"] == "unknown_model"  # no model was picked for the user
    expect(form.locator("p.ag-launch-msg")).to_have_text(out["reason"])
    assert ui.server.world.r.ag.latest() is None and ui.server.world.started == []
    ui.no_horizontal_scroll()
    ui.shot("refused")


def test_launch_is_refused_while_the_market_is_in_production(ui):
    ui.server.call(lambda: setattr(ui.server.world.r.svc, "mode", "prod"))
    open_new(ui)
    form = new_form(ui)
    expect(form.locator(".ag-mk").first.locator("span")).to_have_text("3 rules · Live · real money")
    fill_new(ui)
    expect(form).to_contain_text("a market in production is refused")
    with ui.page.expect_response("**/api/agent/launch") as resp:
        slide_to_start(ui)
    out = resp.value.json()
    assert out["code"] == "production_locked"
    expect(form.locator("p.ag-launch-msg")).to_have_text(out["reason"])
    assert ui.server.world.r.ag.latest() is None
    ui.shot("production-locked")


def test_a_launch_sends_the_ticked_rule_and_the_one_bet_per_window_choice(ui):
    open_new(ui)
    form = fill_new(ui)
    form.locator(".ag-rules label.ag-check").filter(has_text="LATERESID").locator("input").check()
    form.locator(".ag-rules label.ag-one-bet input").uncheck()
    with ui.page.expect_response("**/api/agent/launch") as resp:
        slide_to_start(ui)
    out = resp.value.json()
    assert out["ok"], out
    lim = ui.server.world.r.ag.latest()["limits"]
    assert lim["rules"] == ["LATERESID"] and lim["one_bet_per_window"] is False and lim["level"] == "auto"
    assert lim["market"] == "kalshi_15m" and lim["name"] == "Kalshi 15m agent"
    expect(row(ui, "Run settings").locator(".ag-val")).to_have_text("Auto · 1 rule")
    row(ui, "Run settings").click()
    sheet = ui.page.locator("#panel-agent .ag-settings")
    expect(sheet).to_be_visible()
    expect(sheet).to_contain_text("rules: LATERESID · may bet the same window again")
    expect(sheet).to_contain_text("realized $0.00 · at risk $0.00 · left $20.00 of $20.00")
    ui.shot("settings")
    back(ui)
    expect(status_card(ui)).to_be_visible()


def test_an_assist_launch_sends_the_level_and_the_proposal_window(ui):
    open_new(ui)
    form = fill_new(ui)
    level = form.get_by_role("radiogroup", name="How much control")
    level.get_by_role("radio", name="Assist").click()
    expect(level.get_by_role("radio", name="Assist")).to_have_attribute("aria-checked", "true")
    win = form.get_by_label("Proposal window (10 to 60 seconds)")
    expect(win).to_be_visible()
    expect(win).to_have_attribute("placeholder", "20")
    expect(form.locator(".ag-foot").filter(has_text="Assist: each rule match is proposed to you")).to_be_visible()
    win.fill("30")
    form.locator(".ag-rules label.ag-check").filter(has_text="H4").locator("input").check()
    ui.shot("assist")
    with ui.page.expect_response("**/api/agent/launch") as resp:
        slide_to_start(ui)
    assert resp.value.json()["ok"], resp.value.json()
    lim = ui.server.world.r.ag.latest()["limits"]
    assert lim["level"] == "assist" and lim["proposal_ttl_s"] == 30 and lim["rules"] == ["H4"]
    expect(ui.page.locator("#panel-agent .nv-titlesub")).to_have_text("In-app agent · Assist · Kalshi 15 min")


def _seed_rule_trades(store, rule, market, results, *, cost=0.70):
    """Closed rule trades of an older session (journal rule rows + their brackets): results are proceeds per trade."""
    t0 = NOW - 20 * 3_600_000
    for i, proceeds in enumerate(results):
        bid, created = f"e2e-{rule}-{i}", t0 + i * 900_000
        store.insert("trading_brackets", [{
            "id": bid, "env": "demo", "strategy": "you", "session_id": "old00001", "kind": "entry", "ticker": "T",
            "side": "UP", "window_end": created + 900_000, "stake": 1.0, "contracts": 1.0, "limit_price": 0.95,
            "take_profit": 0.99, "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h", "state": "closed", "filled": 1.0,
            "entry_avg": cost, "entry_cost": cost, "held": 0.0, "tp_filled": 0.0, "tp_fees": 0.0, "exit_fees": 0.0,
            "proceeds": proceeds, "exit_reason": "settled", "created_ts": created, "updated_ts": created + 900_000,
            "closed_ts": created + 900_000}])
        store.insert("agent_journal", [{"session_id": "old00001", "ts": created, "kind": "rule",
                                        "data": {"rule": rule, "market": market, "ok": True, "bracket_id": bid}}])


def test_the_scorecard_sits_behind_its_row_and_retires_and_reactivates_on_the_pc(ui):
    page = ui.page
    _seed_rule_trades(ui.server.world.store, "CUSHION100", "kalshi_15m", [1.0, 1.0, 0.0])  # 2/3, net -$0.10
    open_new(ui)
    form = new_form(ui)
    card = page.locator("#panel-agent .ag-score")
    expect(card).to_be_hidden()  # one tap deeper
    pick_market(form)
    expect(form.locator(".ag-rules label.ag-check").filter(has_text="CUSHION100").locator(".ag-rule-rec")).to_have_text(
        "2 of 3 won live")
    row(ui, "Rule scorecard").click()
    expect(card).to_be_visible()
    expect(form).to_be_hidden()
    rules = card.locator(".ag-score-rule")
    expect(rules).to_have_count(4)
    cu = rules.filter(has=page.locator(".ag-score-head b", has_text="CUSHION100"))
    r = cu.locator(".ag-score-rows li").filter(has_text="Kalshi 15 min")
    expect(r.locator(".ag-score-win")).to_have_text("2/3 won (66.7%) vs 70.0% break-even")
    expect(r.locator(".ag-score-net")).to_have_text("−$0.10 · −3.3¢/contract")
    expect(cu.locator(".ag-score-barline")).to_contain_text("3 / 30 trades to the review bar")
    expect(cu.locator(".ag-score-bt")).to_contain_text("backtest (EXP-CUSHION100)")
    hp = rules.filter(has=page.locator(".ag-score-head b", has_text="HPANIC"))
    expect(hp).to_have_class(re.compile(r"is-retired"))
    expect(hp.locator(".ag-score-reason")).to_have_text("0/3 live, killed in backtest")
    expect(hp.locator(".ag-score-bt")).to_contain_text("−4.09¢")
    ui.shot("scorecard")
    # retire CUSHION100 from the PC: New agent stops offering it
    with page.expect_response("**/api/agent/rules/status") as resp:
        cu.get_by_role("button", name="Retire").click()
    assert resp.value.json()["ok"]
    expect(cu).to_have_class(re.compile(r"is-retired"))
    assert ui.server.world.r.ag.rule_book.retired("CUSHION100")
    back(ui)
    expect(form.locator(".ag-rules label.ag-check:not(.ag-one-bet)")).to_have_count(2)  # H4, LATERESID
    # reactivating needs the typed phrase
    row(ui, "Rule scorecard").click()
    react = cu.get_by_role("button", name="Reactivate")
    expect(react).to_be_disabled()
    cu.locator(".ag-score-react input").fill("REACTIVATE CUSHION100")
    with page.expect_response("**/api/agent/rules/status") as resp:
        react.click()
    assert resp.value.json()["ok"]
    expect(cu).not_to_have_class(re.compile(r"is-retired"))
    back(ui)
    expect(form.locator(".ag-rules label.ag-check:not(.ag-one-bet)")).to_have_count(3)
    ui.no_horizontal_scroll()


# -- running ----------------------------------------------------------------------------------------------------------
def test_a_launch_shows_the_running_view_and_the_accessory_on_play_and_perps(ui):
    page = ui.page
    open_new(ui)
    fill_new(ui)
    slide_to_start(ui)
    card = status_card(ui)
    # the state in words with its time; the mode pill on its own (Shadow: no money); the now line says what's next
    expect(card.locator(".ag-state")).to_have_text("Watching")
    expect(card.locator(".ag-since")).to_have_text(re.compile(r"^since \d\d:\d\d$"))
    expect(page.locator("#panel-agent .nv-title")).to_have_text("Kalshi 15m agent")
    expect(page.locator("#panel-agent .nv-titlesub")).to_have_text("In-app agent · Auto · Kalshi 15 min")
    expect(card.locator(".ag-pill")).to_have_text("Shadow")
    expect(card.locator(".ag-pill")).to_have_attribute("aria-label", "Shadow: no money")
    expect(card.locator(".ag-nowline")).to_have_text("No position. First look at the next market event.")
    expect(card.locator(".ag-t-budget b")).to_have_text("$0.00")
    expect(card.locator(".ag-t-budget")).to_contain_text("of $20.00")
    expect(card.locator(".ag-meter")).to_have_attribute("aria-valuenow", "0")
    expect(card.locator(".ag-t-net b")).to_have_text("$0.00")
    expect(card.locator(".ag-t-net")).to_contain_text("0 trades")
    expect(card.locator(".ag-t-ends b")).to_have_text(re.compile(r"^(59:\d\d|1h 00m)$"))
    expect(card.locator(".ag-t-ends")).to_contain_text(re.compile(r"at \d\d:\d\d"))
    expect(card.locator(".ag-quiet").first).to_have_text("API $0.00 of $5.00 est. · wakes 0 · claude-haiku-5-5")
    # this agent's own Stop on its card; the fleet's in the accessory; "… started." is a toast
    expect(card.get_by_role("button", name="Stop Kalshi 15m agent now")).to_be_visible()
    expect(accessory(ui).get_by_role("button", name="Stop the agent now")).to_be_visible()
    expect(page.locator(".nv-toast")).to_have_text("Kalshi 15m agent started.")
    expect(page.locator("#panel-agent > p.pl-msg")).to_be_hidden()
    expect(new_form(ui)).to_be_hidden()
    expect(page.locator("#panel-agent #proposal-cards")).to_be_attached()
    for name in ("Note to the agent", "Run settings", "Instructions", "Telegram"):
        expect(row(ui, name)).to_be_visible()
    assert ui.server.world.started and ui.server.world.r.ag.latest()["state"] == "running"
    if wide(ui):  # three columns: status | activity | scorecard
        expect(page.locator("#panel-agent .ag-page")).to_have_class(re.compile(r"\bag-3col\b"))
        expect(page.locator("#panel-agent .ag-activity")).to_be_visible()
        expect(page.locator("#panel-agent .ag-score")).to_be_visible()
        expect(page.locator("#panel-agent .ag-latest")).to_be_hidden()  # the activity column is beside it
    else:
        expect(page.locator("#panel-agent .ag-activity")).to_be_hidden()
        expect(page.locator("#panel-agent .ag-score")).to_be_hidden()
        expect(page.locator("#panel-agent .ag-latest .ag-ev-empty")).to_contain_text("No trades or decisions yet")
        expect(row(ui, "Rule scorecard")).to_be_visible()
    ui.no_horizontal_scroll()
    ui.shot("running")

    acc = accessory(ui)
    expect(acc).to_be_visible()  # on every tab, Agents included; no stacked strip anywhere
    expect(page.locator("#ag-strip")).to_have_count(0)
    tab(ui, "Play").click()
    expect(page.locator("#panel-play")).to_be_visible()
    expect(acc.locator(".nv-acc-open b")).to_have_text("1 agent")
    expect(acc.locator(".nv-acc-t")).to_have_text("· $0.00")
    expect(acc.locator(".nv-acc-dot")).to_have_class(re.compile(r"\bis-watching\b"))
    expect(acc.get_by_role("button", name="Stop the agent now")).to_be_visible()
    ui.no_horizontal_scroll()
    ui.shot("accessory-play")
    page.locator("#panel-play .pl-choose").get_by_role("button", name="Trade on Kalshi").click()  # a first visit
    open_perps(ui)
    expect(acc.get_by_role("button", name="Stop the agent now")).to_be_visible()
    ui.no_horizontal_scroll()
    ui.shot("accessory-perps")
    tab(ui, "Record").click()
    expect(page.locator("#panel-record")).to_be_visible()
    expect(acc).to_be_visible()
    acc.locator(".nv-acc-open").click()  # the accessory opens the Agents tab (the fleet)
    expect(page.locator("#panel-agent .ag-fleet")).to_be_visible()
    expect(page).to_have_url(re.compile(r"#agent$"))


def test_the_proposal_card_is_mounted_above_the_status_card(ui):
    from tests.e2e.test_proposal_card import MATCH  # (that module imports this one)
    launch_api(ui, budget=5.0, per_trade=1.0, level="assist", rules=["H4"])
    open_run(ui)
    expect(status_card(ui)).to_be_visible()
    w = ui.server.world
    out = ui.server.run(lambda: w.proposals.propose(w.clock.now_ms(), w.r.ag.latest(), "H4", "kalshi_15m", MATCH))
    assert out["ok"], out
    card = ui.page.locator("#panel-agent #proposal-cards article.pc-card")
    expect(card).to_have_count(1)
    expect(card.locator(".pc-o")).to_contain_text("Buy Up $1.00 · max ")
    above = ui.page.evaluate("""() => {
        const a = document.querySelector('#proposal-cards'), b = document.querySelector('#panel-agent .ag-status');
        return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    }""")
    assert above
    expect(status_card(ui).locator(".ag-state")).to_have_text("Waiting for you")
    ui.no_horizontal_scroll()
    ui.shot("proposal")
    card.get_by_role("button", name="Skip").click()
    expect(card).to_have_count(0)


# -- the live feed --------------------------------------------------------------------------------------------------
P1 = "First the book: YES asks 58c and BTC is $45 over the strike."
P2 = "A 50c limit cannot fill; a market buy up to 60c fits the $10 per-trade cap."


class Gate:
    """Holds the wake between two model turns until the test releases it (polled on the server's loop)."""

    def __init__(self, then) -> None:
        self.open, self.then = threading.Event(), then

    async def __call__(self, _req):
        import asyncio
        while not self.open.is_set():
            await asyncio.sleep(0.02)
        return self.then


GATE = Gate(turn(thinking(P2), text("Buying UP at market."), tool_use("t2", "place_entry", entry(request_id="r2")),
                 deltas=tuple(("thinking", w + " ") for w in P2.split(" ")[:-1]) + (("thinking", P2.split(" ")[-1]),)
                 + (("text", "Buying UP "), ("text", "at market."), ("tool_use", "place_entry"))))


def spend(x: float) -> str:
    """The panel's API-spend text (agent.js `spend`): under a cent with four decimals."""
    return f"${x:.4f}" if 0 < x < 0.01 else f"${x:.2f}"


def feed_run(ui):
    return ui.page.locator("#ag-feed article.ag-run").first


def run_snapshot(run) -> dict:
    return run.evaluate("""el => ({
        thinking: el.querySelector('.ag-think-body').textContent,
        items: Array.from(el.querySelectorAll('.ag-items > *')).map(x => x.classList.contains('ag-tool')
            ? 'tool:' + x.querySelector('.ag-tool-h b').textContent + (x.classList.contains('ag-tool-no') ? ':no' : ':ok')
            : 'text:' + x.textContent),
        decision: el.querySelector('.ag-dec').hidden ? null : el.querySelector('.ag-dec b').textContent,
    })""")


@pytest.mark.script(
    turn(thinking(P1), text("Trying a 50c limit."), tool_use("t1", "place_entry", entry(max_price=0.50)),
         deltas=tuple(("thinking", w + " ") for w in P1.split(" ")[:-1]) + (("thinking", P1.split(" ")[-1]),)
         + (("text", "Trying a 50c limit."), ("tool_use", "place_entry"))),
    GATE,
    turn(tool_use("t3", "record_decision", decision())),
    turn(text("Holding UP; next check 2 min before close.")))
def test_a_scripted_wake_streams_in_order_then_is_rebuilt_from_the_journal_after_a_reload(ui):
    GATE.open.clear()
    page = ui.page
    launch_api(ui)
    open_run(ui)
    expect(status_card(ui)).to_be_visible()
    sec = activity(ui, "Thinking")
    expect(sec).to_contain_text("Waiting for the first event: no wake yet.")
    ui.server.push("agent_alarm", kind="at_time", note="e2e")
    pending = ui.server.tick_async()
    run = feed_run(ui)
    # mid-wake: the first turn's thinking (in order), its text and the refused order; the run is live
    expect(run).to_have_class(re.compile(r"\bis-live\b"))
    expect(run.locator(".ag-think-body")).to_have_text(P1)
    expect(run.locator(".ag-think summary")).to_have_text("Thinking…")
    assert run.locator(".ag-think").evaluate("d => d.open")
    no = run.locator(".ag-tool").first
    expect(no).to_have_class(re.compile(r"\bag-tool-no\b"))
    expect(no.locator(".ag-tool-res")).to_contain_text(re.compile(r"^The app said no: .+"))
    expect(no.locator(".ag-tool-h b")).to_have_text("place_entry")
    expect(run.locator(".ag-run-st")).to_have_text("working…")
    expect(run.locator(".ag-dec")).to_be_hidden()  # no decision yet: no empty decision bar either
    expect(status_card(ui).locator(".ag-state")).to_have_text("Thinking")  # a wake is running
    expect(status_card(ui).locator(".ag-nowline")).to_have_text("No position. Next look at the next market event.")
    ui.shot("mid-wake")
    GATE.open.set()
    pending.result(10)
    # finished: both turns' thinking in order, folded to one line; the order that went through, with its note;
    # the decision; the run's status and estimated cost
    expect(run).not_to_have_class(re.compile(r"\bis-live\b"))
    expect(run.locator(".ag-think-body")).to_have_text(P1 + "\n\n" + P2)
    # the folded line keeps the break between the two turns' thinking (as the journal rebuild does)
    expect(run.locator(".ag-think summary")).to_have_text(re.compile(r"^Thought: First the book: .* strike\. A 50c limit"))
    assert not run.locator(".ag-think").evaluate("d => d.open")
    tools = run.locator(".ag-tool")
    expect(tools).to_have_count(3)
    yes = tools.nth(1)
    expect(yes).not_to_have_class(re.compile(r"\bag-tool-no\b"))
    expect(yes.locator(".ag-tool-note")).to_have_text("Note: " + entry()["note"])
    expect(yes.locator(".ag-tool-res summary")).to_contain_text('"ok":true')
    expect(run.locator(".ag-dec")).to_be_visible()
    expect(run.locator(".ag-dec b")).to_contain_text("No trade · ")
    expect(run.locator(".ag-dec")).to_contain_text("Wrong if: " + decision()["invalidation"])
    est = ui.server.world.r.ag.journal_rows(ui.server.world.r.ag.latest()["id"])
    wake = next(j["data"] for j in est if j["kind"] == "wake")
    assert wake["est_usd"] > 0
    cost = spend(wake["est_usd"])
    expect(run.locator(".ag-run-st")).to_have_text(f"done · {cost} est.")
    expect(status_card(ui).locator(".ag-quiet").first).to_have_text(f"API {cost} of $5.00 est. · wakes 1 · claude-haiku-5-5")
    expect(run.locator(".ag-trig")).to_have_text("agent_alarm")
    expect(status_card(ui).locator(".ag-state")).to_have_text("Watching")
    expect(status_card(ui).locator(".ag-nowline")).to_contain_text("Next check: ")
    expect(status_card(ui).locator(".ag-last")).to_contain_text("Last: No trade · ")
    live = run_snapshot(run)
    folded = run.locator(".ag-think summary").text_content()
    assert live["items"] == ["text:Trying a 50c limit.", "tool:place_entry:no", "text:Buying UP at market.",
                             "tool:place_entry:ok", "tool:record_decision:ok",
                             "text:Holding UP; next check 2 min before close."], live
    ui.no_horizontal_scroll()
    ui.shot("finished")

    # the filters: Trades (the position the entry opened), Decisions, Everything (with the wake), Thinking (the card);
    # every run of the agent under its divider ("Run 1 · since …")
    rows = sec.locator(".ag-feedlist details.ag-ev")
    sec.locator(".ag-chips").get_by_role("button", name="Trades", exact=True).click()
    expect(rows).to_have_count(1)
    expect(sec.locator(".ag-run-div")).to_have_text(re.compile(r"^Run 1 · since \d\d:\d\d · running"))
    expect(rows.first).to_have_class(re.compile(r"\bis-trade\b"))
    expect(rows.first.locator(".ag-ev-t b")).to_have_text(re.compile(r"^Bought Up \$\d+\.\d\d at \d+¢$"))
    rows.first.locator("summary").click()  # tap to expand
    expect(rows.first.locator(".ag-ev-more")).to_contain_text("worst case")
    ui.shot("trades")
    sec.locator(".ag-chips").get_by_role("button", name="Decisions", exact=True).click()
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".ag-ev-t b")).to_contain_text("No trade · ")
    rows.first.locator("summary").click()
    expect(rows.first.locator(".ag-ev-more")).to_contain_text("Wrong if: " + decision()["invalidation"])
    sec.locator(".ag-chips").get_by_role("button", name="Everything", exact=True).click()
    expect(rows.filter(has_text="Wake 1 · agent_alarm")).to_have_count(1)
    expect(rows.filter(has_text="No trade · ")).to_have_count(1)
    expect(rows.filter(has_text="Launched by you.")).to_have_count(1)
    expect(sec.locator(".ag-chips button[aria-pressed=true]")).to_have_text("Everything")
    # Latest (the running page): the trade only; the no-trade decision and the wake stay under All activity
    latest = page.locator("#panel-agent .ag-latest .ag-ev")
    expect(latest).to_have_count(1)
    expect(latest.first).to_have_class(re.compile(r"\bis-trade\b"))
    expect(status_card(ui).locator(".ag-t-net")).to_contain_text("1 trade")
    if not wide(ui):
        back(ui)
        expect(page.locator("#panel-agent .ag-latest")).to_be_visible()
        expect(page.locator("#panel-agent .ag-pos")).to_have_count(1)  # the open position's card
        ui.shot("latest")

    page.reload()  # a reload opens the fleet: the agent's row opens its run again
    open_run(ui)
    expect(status_card(ui)).to_be_visible()
    activity(ui, "Thinking")
    run = feed_run(ui)
    expect(run).to_be_visible()
    expect(run.locator(".ag-run-st")).to_have_text(f"done · {cost} est.")
    expect(run.locator(".ag-dec")).to_be_visible()
    assert run_snapshot(run) == live
    expect(run.locator(".ag-think summary")).to_have_text(folded)
    expect(page.locator("#ag-feed article.ag-run")).to_have_count(1)
    ui.shot("reloaded")


@pytest.mark.script(turn(text("Noted: only UP today.")))
def test_a_note_shows_queued_then_delivered_when_the_next_wake_starts(ui):
    launch_api(ui)
    open_run(ui)
    row(ui, "Note to the agent").click()
    box = ui.page.locator("#panel-agent .ag-note")
    expect(box).to_be_visible()
    box.locator("textarea").fill("Only trade UP today.")
    box.get_by_role("button", name="Send note").click()
    item = box.locator(".ag-notes li")
    expect(item).to_have_count(1)
    expect(item).to_contain_text("“Only trade UP today.”")
    expect(item.locator("small")).to_have_text("queued")
    expect(box.locator("textarea")).to_have_value("")
    ui.shot("queued")
    ui.server.tick()
    expect(item.locator("small")).to_have_text(re.compile(r"^delivered \d\d:\d\d:\d\d$"))
    ui.no_horizontal_scroll()
    ui.shot("delivered")
    back(ui)
    expect(row(ui, "Note to the agent").locator(".ag-val")).to_have_text("1 sent")
    sec = activity(ui, "Thinking")
    expect(feed_run(ui).locator(".ag-trig")).to_have_text("human_note")
    sec.locator(".ag-chips").get_by_role("button", name="Everything", exact=True).click()
    expect(sec.locator("details.ag-ev.is-note")).to_contain_text("Your note: “Only trade UP today.”")


# -- paused --------------------------------------------------------------------------------------------------------
def test_the_kill_switch_pauses_the_agent_and_resume_waits_for_it(ui):
    page = ui.page
    launch_api(ui)
    open_run(ui)
    assert page.request.post("/api/trading/kill").json()["killed"]
    ui.server.tick()  # the runner's review pauses the session
    card = page.locator("#panel-agent .ag-paused")
    expect(card).to_be_visible()
    expect(card.locator("b")).to_have_text("The kill switch is on. Turn it off first, then resume.")
    expect(status_card(ui).locator(".ag-state")).to_have_text("Paused")
    expect(status_card(ui).locator(".ag-chip")).to_have_class(re.compile(r"\bis-paused\b"))
    expect(status_card(ui).locator(".ag-since")).to_have_text(re.compile(r"^since \d\d:\d\d$"))
    act = card.locator(".ag-act").filter(has_text="Turn the kill switch off first")
    expect(act).to_be_visible()
    expect(act.get_by_role("button", name="Resume")).to_be_disabled()
    expect(card.get_by_role("button", name="End run")).to_be_hidden()
    expect(accessory(ui)).to_have_class(re.compile(r"\bis-amber\b"))  # a paused agent needs you
    ui.no_horizontal_scroll()
    ui.shot("kill")
    page.request.post("/api/trading/resume")
    resume = card.locator(".ag-act").filter(has_not_text="kill switch").get_by_role("button", name="Resume")
    expect(resume).to_be_enabled(timeout=12_000)  # the paused panel re-reads the kill switch every 5 s
    resume.click()
    expect(status_card(ui).locator(".ag-state")).to_have_text("Watching")
    expect(card).to_be_hidden()


def test_a_mode_change_pauses_the_agent_and_only_end_run_is_offered(ui):
    page = ui.page
    launch_api(ui)
    open_run(ui)
    ui.server.call(lambda: setattr(ui.server.world.r.svc, "mode", "sandbox"))
    ui.server.tick()
    card = page.locator("#panel-agent .ag-paused")
    expect(card).to_be_visible()
    expect(card.locator("b")).to_contain_text("switched from shadow to sandbox")
    buttons = card.locator("button:visible")
    expect(buttons).to_have_count(1)
    expect(buttons).to_have_text("End run")
    ui.shot("mode")
    buttons.click()
    ended = page.locator("#panel-agent .ag-ended")
    expect(ended).to_be_visible()
    expect(ended.locator(".ag-endwhy")).to_have_text("Stopped by you.")  # the journal's end reason
    expect(card).to_be_hidden()


# -- ended -----------------------------------------------------------------------------------------------------------
def test_the_ended_summary_and_running_the_agent_again_with_the_same_settings(ui):
    page = ui.page
    out = launch_api(ui, rules=["H4"], level="assist", proposal_ttl_s=30)
    assert page.request.post(f"/api/agent/sessions/{out['id']}/stop").json()["ok"]
    open_agent(ui)
    idle = page.locator("#panel-agent .ag-idle .ag-ag")
    expect(idle).to_have_count(1)
    expect(idle.locator(".ag-agsub")).to_have_text(re.compile(r"^Not running · last run ended \d\d:\d\d · stopped by you · 1 run$"))
    idle.click()
    ended = page.locator("#panel-agent .ag-ended")
    expect(ended).to_be_visible()
    expect(new_form(ui)).to_be_hidden()  # one job per screen: the summary
    expect(ended.locator(".ag-state")).to_have_text("Stopped")  # ended by the user's stop
    expect(ended.locator(".ag-since")).to_have_text(re.compile(r"^at \d\d:\d\d$"))
    expect(ended.locator(".ag-endwhy")).to_have_text("Stopped by you.")
    expect(ended.locator(".ag-big b")).to_have_text("$0.00")
    tiles = ended.locator(".ag-tile")
    expect(tiles.nth(0)).to_contain_text("Trades0")
    expect(tiles.nth(0)).to_contain_text("0 won")
    expect(tiles.nth(1)).to_contain_text("of 1 h 00")
    expect(tiles.nth(2)).to_contain_text("API$0.00")
    expect(tiles.nth(2)).to_contain_text("0 wakes")
    expect(ended.locator(".ag-rule-results .ag-row")).to_have_text(["H4no match"])
    expect(ended.locator(".ag-row").filter(has_text="Open positions handed back").locator(".ag-val")).to_have_text("none")
    ui.no_horizontal_scroll()
    ui.shot("ended")
    activity(ui)
    expect(page.locator("#panel-agent .ag-activity .ag-ev-empty")).to_have_text("No trades yet.")
    back(ui)
    ended.get_by_role("button", name="Run this agent again").click()
    form = new_form(ui)
    expect(form).to_be_visible()
    expect(ended).to_be_hidden()
    # prefilled from the run's launch settings, the same agent (its name fixed); nothing starts without START
    expect(form.get_by_text("Runs this agent again with its last launch’s settings.")).to_be_visible()
    expect(form.get_by_label("Agent name")).to_have_value("Kalshi 15m agent")
    expect(form.get_by_label("Agent name")).not_to_be_editable()
    expect(form.locator(".ag-mk[aria-checked=true] b")).to_have_text("Kalshi 15 min")
    expect(form.get_by_role("radiogroup", name="How much control").get_by_role("radio", name="Assist")).to_have_attribute(
        "aria-checked", "true")
    expect(form.get_by_label("Proposal window (10 to 60 seconds)")).to_have_value("30")
    expect(form.get_by_label("Max loss (budget) $")).to_have_value("20")
    expect(form.get_by_label("Per-trade cap $")).to_have_value("10")
    expect(form.get_by_label("Runs for (minutes)")).to_have_value("60")
    expect(form.locator(".ag-rules label.ag-check").filter(has_text="H4").locator("input")).to_be_checked()
    expect(advanced(form).locator("select")).to_have_value("claude-haiku-5-5")
    expect(form.locator("#ag-confirm")).to_have_value("")
    ui.shot("prefilled")
    form.locator("#ag-confirm").fill("START")
    with page.expect_response("**/api/agent/launch") as resp:
        slide_to_start(ui)
    again = resp.value.json()
    assert again["ok"] and again["agent_id"] == out["agent_id"] and again["id"] != out["id"], again
    expect(status_card(ui).locator(".ag-state")).to_have_text("Watching")
    expect(ui.page.locator("#panel-agent .nv-titlesub")).to_have_text("In-app agent · Assist · Kalshi 15 min")


# -- Stop from another tab: the tab bar's accessory ----------------------------------------------------------------
def test_stop_on_the_accessory_over_perps_stops_the_one_agent_in_one_tap(ui):
    page = ui.page
    launch_api(ui)
    page.add_init_script("localStorage.setItem('play.venue','kalshi');localStorage.setItem('play.span.kalshi','15m')")
    page.goto("/#play")
    open_perps(ui)
    acc = accessory(ui)
    stop = acc.get_by_role("button", name="Stop the agent now")
    expect(stop).to_be_visible()
    stop.click()  # one agent: no confirmation (as before): the guard fails the test on any dialog
    expect(page.locator(".nv-toast")).to_contain_text("Kalshi 15m agent stopped.")  # the outcome, as a toast
    expect(acc).to_be_hidden()
    assert ui.server.world.r.ag.latest() is None
    ui.no_horizontal_scroll()
    ui.shot("stopped")


# -- presets ---------------------------------------------------------------------------------------------------------
def test_a_preset_is_saved_from_new_agent_and_starts_an_agent(ui):
    page = ui.page
    open_new(ui)
    form = fill_new(ui, confirm="")
    form.locator(".ag-rules label.ag-check").filter(has_text="H4").locator("input").check()
    expect(form.locator(".ag-presets")).to_be_hidden()  # no presets yet
    form.get_by_label("Preset name").fill("Quick H4")
    with page.expect_response(lambda r: r.url.endswith("/api/agent/presets") and r.request.method == "POST"):
        form.get_by_role("button", name="Save as preset").click()
    expect(form.get_by_text("Saved as “Quick H4”.")).to_be_visible()
    posted = ui.server.world.stubs.posted
    assert posted and posted[0]["name"] == "Quick H4", posted
    launch = posted[0]["launch"]
    assert "confirm" not in launch and "venues" not in launch and "agent_id" not in launch
    assert {k: launch[k] for k in ("kind", "market", "budget", "per_trade", "minutes", "spend_cap", "model", "rules",
                                   "one_bet_per_window", "level", "proposal_ttl_s")} == {
        "kind": "in_app", "market": "kalshi_15m", "budget": 20, "per_trade": 10, "minutes": 60, "spend_cap": 5,
        "model": "claude-haiku-5-5", "rules": ["H4"], "one_bet_per_window": True, "level": "auto",
        "proposal_ttl_s": None}
    item = form.locator(".ag-presets .ag-preset")
    expect(item).to_have_count(1)
    expect(item.locator("b")).to_have_text("Quick H4")
    expect(item.locator("small")).to_have_text("Auto · Kalshi 15 min · max loss $20.00 · H4")
    ui.shot("preset-saved")
    page.reload()  # a fresh page: the fleet; New agent starts from the preset
    open_new(ui)
    item.get_by_role("button", name="Start from preset Quick H4").click()
    expect(form.get_by_label("Max loss (budget) $")).to_have_value("20")
    expect(form.locator(".ag-mk[aria-checked=true] b")).to_have_text("Kalshi 15 min")
    expect(form.locator(".ag-rules label.ag-check").filter(has_text="H4").locator("input")).to_be_checked()
    expect(form.get_by_text("Filled in from the preset “Quick H4”.")).to_be_visible()
    form.locator("#ag-confirm").fill("START")
    with page.expect_response("**/api/agent/launch") as resp:
        slide_to_start(ui)
    assert resp.value.json()["ok"], resp.value.json()
    assert ui.server.world.r.ag.latest()["limits"]["rules"] == ["H4"]
    expect(status_card(ui)).to_be_visible()
    ui.no_horizontal_scroll()


def test_presets_and_telegram_show_not_available_when_the_app_has_no_routes(ui):
    ui.server.world.stubs.off.update({"presets", "telegram"})
    ui.guard.allow("/api/agent/presets")  # the 404s are the real state of such an app
    ui.guard.allow("/api/telegram/status")
    open_agent(ui)
    expect(row(ui, "Telegram").locator(".ag-val")).to_have_text("Not available")
    row(ui, "Telegram").click()
    expect(ui.page.locator("#panel-agent .ag-tg")).to_contain_text("Not available in this app")
    back(ui)
    open_new(ui)
    form = new_form(ui)
    expect(form.locator(".ag-presets .ag-row.is-off")).to_have_text(re.compile(r"Presets\s*Not available in this app"))
    pick_market(form)
    expect(form.get_by_label("Preset name")).to_be_hidden()
    ui.no_horizontal_scroll()
    ui.shot("not-available")


# -- Telegram --------------------------------------------------------------------------------------------------------
def test_telegram_without_a_bot_token_says_how_to_set_it_up(ui):
    w = ui.server.world
    w.stubs.telegram = {**w.stubs.telegram, "configured": False, "running": False, "bot_username": None}
    open_agent(ui)
    expect(row(ui, "Telegram").locator(".ag-val")).to_have_text("Not set up")
    row(ui, "Telegram").click()
    sheet = ui.page.locator("#panel-agent .ag-tg")
    expect(sheet.locator(".ag-tg-off")).to_have_text("Add a bot token in .env to use Telegram.")
    expect(sheet.get_by_role("button", name="Pair")).to_have_count(0)
    ui.no_horizontal_scroll()
    ui.shot("telegram-off")


def test_telegram_pairs_with_a_code_and_sets_alerts(ui):
    page = ui.page
    w = ui.server.world
    launch_api(ui)
    open_run(ui)
    expect(row(ui, "Telegram").locator(".ag-val")).to_have_text("Not paired")
    row(ui, "Telegram").click()
    sheet = page.locator("#panel-agent .ag-tg")
    expect(sheet.locator(".ag-tg-on .ag-row").filter(has_text="Bot")).to_contain_text("running")
    sheet.get_by_role("button", name="Pair").click()
    code = sheet.locator(".ag-tg-code")
    expect(code).to_contain_text(f"Send /start {w.stubs.code} to @example_settleview_bot")
    expect(code.locator(".ag-code")).to_have_css("user-select", "all")  # the code is easy to select
    expect(code.locator("small")).to_have_text(re.compile(r"^expires in (10:00|9:\d\d)$"))
    ui.no_horizontal_scroll()
    ui.shot("telegram-code")
    alerts = sheet.locator(".ag-tg-alerts label.ag-check")
    expect(alerts).to_have_count(9)  # data recorders added 2026-10-31
    activity_sw = sheet.get_by_label("Session activity alerts")
    expect(activity_sw).to_be_checked()
    with page.expect_response("**/api/telegram/alerts"):
        activity_sw.uncheck()
    assert w.stubs.telegram["alerts"]["activity"] is False
    expect(activity_sw).not_to_be_checked()
    expect(sheet.get_by_label("Daily summary alerts")).to_be_checked()
    at = sheet.get_by_label("Daily summary time")
    expect(at).to_have_value("22:00")
    with page.expect_response("**/api/telegram/alerts"):
        at.fill("21:30")
        at.dispatch_event("change")
    assert w.stubs.telegram["daily_at"] == "21:30"
    expect(sheet.locator("p.ag-foot[role=status]")).to_have_text("Daily summary at 21:30.")
    trades = sheet.get_by_label("Trades alerts")
    expect(trades).to_be_checked()
    with page.expect_response("**/api/telegram/alerts"):
        trades.uncheck()
    assert w.stubs.telegram["alerts"]["trades"] is False
    expect(trades).not_to_be_checked()
    # the user sends /start: the sheet sees the pairing on its next read
    w.stubs.telegram = {**w.stubs.telegram, "paired": True, "user_label": "@alex_example"}
    expect(sheet.locator(".ag-tg-who")).to_have_text("@alex_example", timeout=8_000)
    expect(code).to_be_hidden()
    expect(sheet.get_by_role("button", name="Unpair")).to_be_visible()
    ui.shot("telegram-paired")
    sheet.get_by_role("button", name="Unpair").click()
    expect(sheet.locator(".ag-tg-who")).to_have_text("not paired")


def _session_trade(store, sid, bid, *, created, avg, cost, fees, proceeds, exit_reason, exit_fees=0.0, filled=1.0):
    store.insert("trading_brackets", [{
        "id": bid, "env": "shadow", "strategy": "you", "session_id": sid, "kind": "entry", "ticker": "KXBTC15M-T",
        "side": "UP", "window_end": created + 600_000, "stake": 1.0, "contracts": filled, "limit_price": 0.9,
        "take_profit": 0.99, "no_tp": True, "stop": 0.0, "max_cost": 1.0, "ticket_hash": "h", "state": "closed",
        "filled": filled, "held": 0.0, "tp_filled": 0.0, "entry_avg": avg, "entry_cost": cost, "entry_fees": fees,
        "tp_fees": 0.0, "exit_fees": exit_fees, "proceeds": proceeds, "exit_reason": exit_reason,
        "created_ts": created, "updated_ts": created + 60_000, "closed_ts": created + 60_000}])
    store.insert("agent_journal", [{"session_id": sid, "ts": created, "kind": "decision", "data": {
        **decision(action="ENTRY", market="kalshi_15m", side="UP", p_source="experimental",
                   thesis="[rule:CUSHION100] UP at ask 0.86"),
        "ts": created, "wake_id": None, "source": "rule", "rule": "CUSHION100", "bracket_id": bid}}])


def test_trade_rows_show_the_exact_cost_fees_and_net_per_trade_and_per_rule(ui):
    page = ui.page
    w = ui.server.world
    sid = launch_api(ui, rules=["CUSHION100"])["session"]["id"]
    _session_trade(w.store, sid, "e2e-win", created=NOW - 120_000, avg=0.86, cost=1.0, fees=0.0024, proceeds=1.16,
                   exit_reason="settled", filled=1.16)
    _session_trade(w.store, sid, "e2e-loss", created=NOW - 60_000, avg=0.80, cost=0.81, fees=0.01, proceeds=0.50,
                   exit_reason="stop", exit_fees=0.01)
    open_run(ui)
    titles = ["Closed Up (stop) · −$0.32", "Settled Up · +$0.16"]
    if not wide(ui):  # Latest (the activity column replaces it on a wide screen)
        latest = page.locator("#panel-agent .ag-latest .ag-ev-t b")
        expect(latest).to_have_text(titles)
        expect(latest.nth(0)).to_have_class(re.compile(r"\bred\b"))
        expect(latest.nth(1)).to_have_class(re.compile(r"\bgreen\b"))
    sec = activity(ui, "Trades")
    rows = sec.locator(".ag-feedlist details.ag-ev")
    expect(rows.locator("summary .ag-ev-t b")).to_have_text(titles)
    expect(rows.nth(1).locator("summary small")).to_have_text("Bought Up $1.00 at 86¢ · CUSHION100 · Kalshi 15 min")
    rows.nth(1).locator("summary").click()
    expect(rows.nth(1).locator(".ag-ev-more p").nth(1)).to_have_text(
        "Paid $1.00 (1.16 × 86¢ + $0.00 fees), returned $1.16: net +$0.16.")
    rows.nth(0).locator("summary").click()
    expect(rows.nth(0).locator(".ag-ev-more p").nth(1)).to_have_text(
        "Paid $0.81 (1 × 80¢ + $0.01 fees), returned $0.50 less $0.01 exit fees: net −$0.32.")
    expect(sec).not_to_contain_text("fees are not in")
    ui.no_horizontal_scroll()
    ui.shot("trades-exact")
    if not wide(ui):
        back(ui)
    assert page.request.post(f"/api/agent/sessions/{sid}/stop").json()["ok"]
    open_run(ui)
    ended = page.locator("#panel-agent .ag-ended")
    expect(ended).to_be_visible()
    expect(ended.locator(".ag-big b")).to_have_text("−$0.16")
    expect(ended.locator(".ag-tile").nth(0)).to_contain_text("1 won")
    expect(ended.locator(".ag-rule-results .ag-row")).to_have_text(["CUSHION100" + "2 trades · 1 won · −$0.16"])
    expect(ended).to_contain_text("Won: made money after every fee.")
    ui.no_horizontal_scroll()
    ui.shot("ended-exact")


# -- prompts ---------------------------------------------------------------------------------------------------------
def open_prompts(ui):
    open_new(ui)
    form = new_form(ui)
    pick_market(form)
    advanced(form).locator("button.ag-row").filter(has_text="Instructions").click()
    box = ui.page.locator("#panel-agent .ag-prompts")
    expect(box).to_be_visible()
    return box


def test_the_prompt_editor_saves_a_version_and_resets_to_the_default(ui):
    box = open_prompts(ui)
    system = box.locator("section.ag-layer").first
    expect(system.locator("h3")).to_have_text("System prompt v1 · default")
    area = system.get_by_role("textbox", name="System prompt")
    expect(area).to_have_value(DEFAULT_PROMPT)
    # loopback is always this PC (the access middleware), so the phone-sized window edits as the PC does
    expect(box.get_by_text("Change this on the PC.")).to_be_hidden()
    expect(area).to_be_editable()
    area.fill("Trade less. (e2e)")
    expect(system.locator("h3")).to_have_text("System prompt v1 · default · unsaved changes")
    system.get_by_role("button", name="Save as new version").click()
    expect(box.locator("p[role=status]")).to_have_text("Saved as system v2.")
    expect(system.locator("h3")).to_have_text("System prompt v2")
    system.locator("details.ag-hist summary").click()
    rows = system.locator("details.ag-hist li")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("v2 · ")
    expect(rows.first).to_contain_text("Trade less. (e2e)")
    expect(rows.first.locator("b")).to_have_text("current")
    expect(rows.nth(1).get_by_role("button", name="Restore")).to_be_visible()
    ui.no_horizontal_scroll()
    ui.shot("saved")
    ui.guard.accept_dialogs = True
    system.get_by_role("button", name="Reset to default").click()
    expect(box.locator("p[role=status]")).to_have_text("Saved as system v3.")
    assert ui.guard.dialogs and ui.guard.dialogs[-1].startswith("Reset the system prompt to the default?")
    expect(area).to_have_value(DEFAULT_PROMPT)
    expect(system.locator("h3")).to_have_text("System prompt v3 · default")
    expect(rows).to_have_count(3)
    back(ui)
    expect(new_form(ui)).to_be_visible()


@pytest.mark.parametrize("look", ["phone-light", "phone-dark"], indirect=True)
@pytest.mark.peer("192.168.1.50")
def test_a_phone_on_the_lan_reads_the_prompts_but_cannot_edit_them(ui, pc_request):
    assert pc_request.post("/api/access/pin", data={"pin": PIN, "pin_again": PIN}).json()["ok"]
    page = ui.page
    ui.guard.allow("/api/access")  # the phone is refused the PC-only access route (403): that is how it knows
    login = page.request.post("/login", form={"pin": PIN}, max_redirects=0)
    assert login.status == 303, login.status
    box = open_prompts(ui)
    form = new_form(ui)
    expect(box.get_by_text("Change this on the PC.")).to_be_visible()
    system = box.locator("section.ag-layer").first
    area = system.get_by_role("textbox", name="System prompt")
    expect(area).to_have_value(DEFAULT_PROMPT)
    expect(area).not_to_be_editable()
    expect(system.get_by_role("button", name="Save as new version")).to_be_disabled()
    expect(system.get_by_role("button", name="Reset to default")).to_be_disabled()
    ui.no_horizontal_scroll()
    ui.shot("phone-readonly")
    back(ui)
    # PC-only actions are hidden on the phone, with a one-line reason
    expect(form.get_by_text("Save and delete presets on the PC.")).to_be_visible()
    expect(form.get_by_label("Preset name")).to_be_hidden()
    page.locator("#panel-agent .ag-top-back").click()
    row(ui, "Fleet limits").click()
    expect(page.locator("#panel-agent .ag-glim").get_by_text("Change the fleet limits on the PC.")).to_be_visible()
    expect(page.locator("#panel-agent .ag-glim").get_by_role("button", name="Save fleet limits")).to_be_hidden()
    page.locator("#panel-agent .ag-top-back").click()
    row(ui, "Telegram").click()
    sheet = page.locator("#panel-agent .ag-tg")
    expect(sheet.get_by_text("Pair, unpair and alert changes are made on the PC.")).to_be_visible()
    expect(sheet.get_by_role("button", name="Pair")).to_be_hidden()
    expect(sheet.get_by_label("Trades alerts")).to_be_disabled()
