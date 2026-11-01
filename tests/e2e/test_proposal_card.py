"""The Assist proposal card (web/views/proposal.js) in a real browser, mounted on an agent's screen for its run: shown from
the run's feed CUSTOM proposal_new, Approve sends one entry, Skip and expiry send nothing. The app is tests/e2e/server.py
(shadow exchange, scripted fake harness); the proposal is the real AgentProposals over a test-built rule match."""

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
    expect(card.locator(".pc-note")).to_have_text("One Approve sends the entry and its stop. Unanswered proposals "
                                                  "expire; nothing is sent.")
    ui.no_horizontal_scroll()
    ui.shot("proposal")
    assert buys(ui) == []
    ui.server.call(lambda: ui.server.world.r.svc.heartbeat(ui.server.world.clock.now_ms()))  # someone at the screen
    card.get_by_role("button", name="Approve").click()
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
