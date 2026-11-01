"""The Telegram remote and presets in the app's wiring: no token -> the bot is off and the app runs (status
configured: false, not an error); a token -> the bot's loop is registered (never started here: no network). Presets
are validated by launch's own checks (a dry run). PC-only routes answer a phone 403."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.app import create_app
from btc_signal_engine.adapters.http.telegram_routes import register_preset_routes, register_telegram_routes
from btc_signal_engine.adapters.telegram.bot import TelegramBot
from btc_signal_engine.application.agent_presets import MAX_PRESETS, AgentPresets
from btc_signal_engine.application.notifier import DEFAULT_ALERTS, RemoteControl
from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import Settings
from tests.telegram.fakes import Clock
from tests.pc import paired

HOST = "http://127.0.0.1:8000"
GOOD = {"kind": "in_app", "venues": ["kalshi_15m"], "budget": 5.0, "per_trade": 1.0, "minutes": 60,
        "spend_cap": 2.0, "model": "claude-haiku-5-5", "rules": ["H4"], "level": "assist"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY", "BTCFLOW_AGENT_ENABLED", "BTCFLOW_ANTHROPIC_API_KEY",
              "BTCFLOW_AGENT_CLI_PATH", "BTCFLOW_TELEGRAM_BOT_TOKEN",
              "BTCFLOW_TELEGRAM_BOT_USERNAME"):
        monkeypatch.delenv(k, raising=False)


def runtime(tmp_path, **kw) -> Runtime:
    return Runtime(Settings(_env_file=None, db_path=tmp_path / "tg.db", trading_stream=False, **kw))


def pc(rt) -> TestClient:
    return paired(TestClient(create_app(rt.services()), client=("127.0.0.1", 1), base_url=HOST), rt.settings.data_dir)


def test_no_token_the_bot_is_off_and_the_status_says_so(tmp_path):
    rt = runtime(tmp_path, agent_enabled=True)
    try:
        assert rt.telegram_bot is None and "telegram" not in rt.loops()
        c = pc(rt)
        st = c.get("/api/telegram/status").json()
        assert st == {"ok": True, "configured": False, "running": False, "bot_username": None, "paired": False,
                      "user_label": None,
                      "alerts": DEFAULT_ALERTS, "daily_at": "22:00", "muted": []}
        out = c.post("/api/telegram/pair").json()
        assert out["ok"] is False and out["code"] == "not_configured"
        # the remote's sources read the app's own services (no market data yet: no lights, no warnings)
        now = rt.clock.now_ms()
        assert isinstance(rt._telegram_lights(now), list) and rt._telegram_settle_warnings(now) == []
        assert rt._venue_modes()["kalshi_15m"] == rt.you_trading.mode
    finally:
        rt.writer.close()


def test_with_agent_mode_off_telegram_status_exists_and_presets_do_not(tmp_path):
    rt = runtime(tmp_path)
    try:
        c = pc(rt)
        assert c.get("/api/telegram/status").json()["configured"] is False
        assert c.get("/api/agent/presets").status_code == 404
        assert rt.remote.actions is None and rt.agent_presets is None
    finally:
        rt.writer.close()


def test_a_token_registers_the_bot_loop_without_contacting_telegram(tmp_path):
    rt = runtime(tmp_path, agent_enabled=True, telegram_bot_token="123456:not-a-real-token")
    try:
        assert isinstance(rt.telegram_bot, TelegramBot) and rt.loops()["telegram"] == rt.telegram_bot.run
        c = pc(rt)
        st = c.get("/api/telegram/status").json()
        assert st["configured"] is True and st["running"] is False
        assert st["bot_username"] is None  # not set and the bot has not started: the UI says "your bot"
        assert "not-a-real-token" not in c.get("/api/telegram/status").text
        out = c.post("/api/telegram/pair").json()
        assert out["ok"] and len(out["code"]) == 8 and out["expires_ts"] > 0
        again = c.post("/api/telegram/pair").json()
        assert rt.remote._code["code"] == again["code"]  # the new code replaced the old one
        assert rt.remote.actions is rt.agent_actions is not None  # the launch route's own path
        al = c.post("/api/telegram/alerts", json={"setups": True}).json()
        assert al["ok"] and al["alerts"]["setups"] is True
        assert c.post("/api/telegram/alerts", json={"nope": True}).json()["code"] == "unknown_alert"
        assert c.post("/api/telegram/unpair").json() == {"ok": True, "was_paired": False}
    finally:
        rt.writer.close()


def test_presets_are_validated_by_launchs_own_checks(tmp_path):
    rt = runtime(tmp_path, agent_enabled=True)
    try:
        c = pc(rt)
        out = c.post("/api/agent/presets", json={"name": "  H4 assist ", "launch": GOOD}).json()
        assert out["ok"] and out["preset"]["name"] == "H4 assist" and out["replaced"] is False
        assert out["preset"]["modes"] == {"kalshi_15m": rt.you_trading.mode}
        lst = c.get("/api/agent/presets").json()
        assert lst["ok"] and [p["name"] for p in lst["presets"]] == ["H4 assist"]
        assert set(lst["presets"][0]) >= {"name", "launch", "created_ts"}

        def code(launch, name="x"):
            return c.post("/api/agent/presets", json={"name": name, "launch": launch}).json().get("code")
        assert code({**GOOD, "budget": 0}) == "invalid"
        assert code({**GOOD, "venues": ["kalshi_1h"]}) == "unknown_venue"
        assert code({k: v for k, v in GOOD.items() if k != "level"}) == "invalid_level"
        assert code({**GOOD, "rules": []}) == "invalid_level"  # assist needs a rule
        assert code({**GOOD, "model": "gpt-9"}) == "unknown_model"
        assert code({**GOOD, "sell_on_stop": True}) == "not_available"
        assert code({**GOOD, "rules": ["HPANIC"]}) == "rule_retired"
        assert code({**GOOD, "minutes": None, "ends_ts": 2_000_000_000_000}) == "invalid"
        assert code({**GOOD, "kind": "external"}) == "in_app_only"
        assert code({**GOOD, "confirm": "START"}) == "invalid"
        assert code(GOOD, name="") == "invalid_name" and code(GOOD, name="x" * 41) == "invalid_name"
        assert code("not an object") == "invalid"
        # a live session does not matter to a preset (launch would refuse session_live)
        assert rt.agent_sessions.launch({**GOOD, "level": "auto", "rules": []}, rt.clock.now_ms())["ok"]
        assert c.post("/api/agent/presets", json={"name": "again", "launch": GOOD}).json()["ok"]
        assert c.delete("/api/agent/presets/again").json() == {"ok": True}
        assert c.delete("/api/agent/presets/again").json()["code"] == "not_found"
        assert c.delete("/api/agent/presets/H4%20assist").json() == {"ok": True}
    finally:
        rt.writer.close()


def test_at_most_ten_presets_and_saving_a_name_again_replaces_it(store):
    p = AgentPresets(store, lambda spec, now: None, lambda: {"kalshi_15m": "shadow"})
    for i in range(MAX_PRESETS):
        assert p.save(f"p{i}", GOOD, i)["ok"]
    assert p.save("eleven", GOOD, 99)["code"] == "too_many"
    again = p.save("p3", {**GOOD, "budget": 4.0}, 100)
    assert again["ok"] and again["replaced"] and p.get("p3")["launch"]["budget"] == 4.0
    assert len(p.all()) == MAX_PRESETS


def phone_app(store) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def as_phone(request, call_next):
        request.scope.setdefault("state", {})["actor"] = "phone:abcdef12"
        return await call_next(request)
    control = RemoteControl(store, now_fn=Clock(), configured=True)
    register_telegram_routes(app, control, Clock())
    register_preset_routes(app, AgentPresets(store, lambda s, n: None, lambda: {}), Clock())
    return TestClient(app)


def test_pc_only_routes_answer_a_phone_403_and_reads_work(store):
    c = phone_app(store)
    assert c.get("/api/telegram/status").json()["ok"] is True
    assert c.get("/api/agent/presets").json() == {"ok": True, "presets": []}
    for method, path, body in (("post", "/api/telegram/pair", None), ("post", "/api/telegram/unpair", None),
                               ("post", "/api/telegram/alerts", {"setups": True}),
                               ("post", "/api/agent/presets", {"name": "x", "launch": GOOD}),
                               ("delete", "/api/agent/presets/x", None)):
        r = getattr(c, method)(path, json=body) if body is not None else getattr(c, method)(path)
        assert r.status_code == 403, path


def test_the_app_watches_the_research_recorders_read_only(tmp_path):
    """P4: the remote gets the recorder watch (BTCFLOW_RESEARCH_DIR); /api/health says plainly that there is no
    research database (and the app never creates one)."""
    rt = runtime(tmp_path, research_dir=tmp_path / "research")
    try:
        assert rt.remote.recorders is rt.recorder_watch
        h = pc(rt).get("/api/health").json()
        assert h["recorders"] == {"available": False, "reason": "polymarket_us.db not found", "recorders": {}}
        assert "recorders" not in h["degraded"]  # research data never degrades the app's own status
        assert not (tmp_path / "research").exists()
    finally:
        rt.writer.close()


def test_the_bot_username_setting_is_shown_and_a_bad_one_is_an_error(tmp_path):
    rt = runtime(tmp_path, telegram_bot_token="123456:not-a-real-token", telegram_bot_username="@example_settleview_bot")
    try:
        assert pc(rt).get("/api/telegram/status").json()["bot_username"] == "example_settleview_bot"
    finally:
        rt.writer.close()
    assert Settings(_env_file=None, telegram_bot_username="  ").telegram_bot_username is None  # empty = not set
    for bad in ("settleview", "@xbot", "my bot", "1example_bot", "a" * 30 + "_bot"):
        with pytest.raises(ValueError, match="BTCFLOW_TELEGRAM_BOT_USERNAME"):
            Settings(_env_file=None, telegram_bot_username=bad)
