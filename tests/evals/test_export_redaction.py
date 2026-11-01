"""The eval export's redaction, verified by its own adversarial scan (house rule 7: distrust the sanitization label).

A source database is planted with a secret, ids, balances, a write token, a device id, an e-mail, a path, an IP and
the tables an export must never read. The full export must contain none of them; with any ONE redaction rule switched
off the deny-list scan must FAIL the export (the mutation check: a redaction whose removal the scan does not notice
is unverified); the scan alone (every rule off) must catch every planted kind."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event

from evals import export as ex
from evals.redact import RULES, Redactor, scan

SESSION = "c0ffee00c0ffee00c0ffee00c0ffee00"
AGENT = "a9a9a9a9b8b8b8b8c7c7c7c7d6d6d6d6"
BRACKET = "b1b2b3b4b5b6b7b8b9b0b1b2b3b4b5b6"
SECRET = "sk-ant-api03-PLANTEDsecretVALUE0123456789abcdef"
TOKEN = "svtZx9Qk2LmN8pR4tY7wE1uI0oP3aS6dF5gHjK"  # gitleaks:allow - planted fake
PHONE = "phone:842d8b09"
MSG_ID = "msg_01PLANTEDmessageIDxyz"
EMAIL = "someone@example.com"
PATH = r"C:\Users\Someone\secrets\key.pem"
IP = "10.20.30.40"
UUID = "123e4567-e89b-42d3-a456-426614174000"
BALANCES = ("123.45", "99.10", "5,000.00", "77.70")
NEVER = ("PLANTEDTICKETHASH", "PLANTEDTPORDER", "PLANTEDORDERID", "PLANTEDAUDITDETAIL", "deadbeefdeadbeef")
T0 = 1_793_458_000_000


def _wake(wake_id: int, ts: int) -> dict:
    return {
        "wake_id": wake_id, "status": "done", "reason": "urgent", "model": "claude-haiku-5-5", "duration_ms": 9000,
        "trigger": [{"type": "human_note", "class": "urgent", "value": {"text": "check it", "by": PHONE}},
                    {"type": "settled", "class": "urgent", "venue": "kalshi", "span": "15m",
                     "latest": {"brackets": [{"id": BRACKET, "side": "UP", "pnl": -0.2}]}}],
        "decision": f"Held. Kalshi equity {BALANCES[2]} and cash ${BALANCES[0]}, Polymarket ${BALANCES[3]}. "
                    f"Mail {EMAIL}. "
                    f"Ref {MSG_ID}.",
        "thinking": [f"The key file is {PATH} at {IP}; order {UUID}."],
        "tools": [
            {"name": "update_notes",
             "args": {"text": f"Bracket {BRACKET} open. balance: {BALANCES[1]}. {SECRET} {TOKEN}"},
             "ok": True, "code": None, "result": {"ok": True, "chars": 80}},
            {"name": "observe", "args": {}, "ok": True, "code": None,
             "result": {"ok": True, "balances": {"kalshi": {"cash": 123.45}},
                        "markets": {"kalshi_15m": {"positions": [{"id": BRACKET, "side": "UP"}]}}}},
            {"name": "record_decision", "ok": True, "code": None, "result": {"ok": True},
             "args": {"market": "kalshi_15m", "action": "HOLD", "side": "UP", "p_estimate": None, "p_low": None,
                      "p_high": None, "p_source": "experimental", "market_p": 0.6, "edge_cents": None,
                      "pass_reason": "NO_MATERIAL_CHANGE", "invalidation": "BTC through the stop.",
                      "thesis": f"Session {SESSION[:8]} holds {BRACKET}.", "next_check": None}}],
        "segments": [], "refusal": None, "stop_reason": "end_turn", "turns": 2, "message_ids": [MSG_ID],
        "tokens": {"input": 4, "output": 900, "cache_write": 9000, "cache_read": 30000}, "est_usd": 0.003,
        "prompt_versions": {"system": 1, "user": 3}, "runtime": {"runtime": "claude-agent-sdk 0.2.168"}}


@pytest.fixture
def source(store, pg_url, request):
    """The planted database on the run's store (SQLite by default; Postgres in the CI job with --db-url), returned as
    what the exporter is given: the SQLite file, or the Postgres URL (read through readonly_engine)."""
    store.insert("agent_sessions", [{
        "id": SESSION, "kind": "in_app", "state": "ended", "pinned": {"you": "prod"}, "model": "claude-haiku-5-5",
        "limits": {"venues": ["kalshi_15m"], "budget": 10.0, "per_trade": 1.0, "spend_cap": 2.0,
                   "rules": ["CUSHION100"], "level": "auto"},
        "prompt_versions": {"system": 1, "user": 3}, "token_hash": "deadbeef" * 8, "taken_over": [],
        "usage": {"wakes": 2}, "started_ts": T0, "ends_ts": T0 + 3_600_000, "updated_ts": T0, "agent_id": AGENT}],
        ignore_conflicts=False)
    rows = [("notes", T0 + 1_000, {"text": f"Polymarket cash {BALANCES[0]}; keep {BRACKET}; {SECRET}"}),
            ("note", T0 + 2_000, {"text": "hello", "by": PHONE, "state": "delivered"}),
            ("rule", T0 + 3_000, {"rule": "CUSHION100", "ok": True, "bracket_id": BRACKET,
                                  "request_id": f"rule:CUSHION100:{T0}"}),
            ("decision", T0 + 19_000, {"wake_id": 1, "source": "agent", "action": "HOLD", "mid_up": 0.61,
                                       "bracket_id": BRACKET}),
            ("wake", T0 + 20_000, _wake(1, T0 + 20_000)),
            ("wake", T0 + 60_000, _wake(2, T0 + 60_000))]
    for kind, ts, data in rows:
        store.insert("agent_journal", [{"session_id": SESSION, "ts": ts, "kind": kind, "data": data,
                                        "agent_id": AGENT}], ignore_conflicts=False)
    last = max(r["id"] for r in store.select("agent_journal", {"kind": "wake"}))
    observe = {"markets": {"kalshi_15m": {"market": {"mid_up": 0.61, "fresh": True}}},
               "balances": {"kalshi": {"cash": float(BALANCES[0])}}}
    msg = ("<memory>" + json.dumps({"notes": f"cash ${BALANCES[0]} at {IP}", "trades": [{"id": BRACKET}]})
           + "</memory>\n\n<events>[]</events>\n\n<observe>" + json.dumps(observe) + "</observe>")
    store.insert("agent_wake_inputs", [{
        "journal_id": last, "session_id": SESSION, "wake_id": 2, "ts": T0 + 60_000, "message": msg,
        "message_sha256": "0" * 64, "fingerprint": {"system_prompt_sha256": "a" * 64, "git_sha": "unknown"},
        "sensitive": {}}], ignore_conflicts=False)
    store.insert("trading_brackets", [{
        "id": BRACKET, "env": "prod", "session_id": SESSION, "kind": "entry", "ticker": "KXBTC15M-26OCT311000-00",
        "side": "UP", "window_end": T0 + 900_000, "stake": 1.0, "contracts": 1.0, "limit_price": 0.6,
        "take_profit": 0.99, "stop": 0.0, "max_cost": 0.62, "ticket_hash": "PLANTEDTICKETHASH" * 3,
        "state": "closed", "filled": 1.0, "held": 0.0, "tp_filled": 0.0, "tp_order_id": "PLANTEDTPORDER",
        "entry_cost": 0.6, "proceeds": 0.0, "created_ts": T0, "updated_ts": T0}], ignore_conflicts=False)
    store.insert("trading_orders", [{
        "client_order_id": "PLANTEDORDERID-0001", "bracket_id": BRACKET, "role": "entry", "ticker": "KXBTC15M",
        "side": "yes", "action": "buy", "count": 1.0, "price": 0.6, "tif": "ioc", "status": "executed",
        "filled": 1.0, "created_ts": T0, "updated_ts": T0}], ignore_conflicts=False)
    store.insert("trading_audit", [{"ts": T0, "event": "sent", "env": "prod", "bracket_id": BRACKET,
                                    "session_id": SESSION, "detail": {"x": "PLANTEDAUDITDETAIL"}}],
                 ignore_conflicts=False)
    if pg_url is not None:
        return pg_url.render_as_string(hide_password=False)
    return str(request.getfixturevalue("db_path"))


def _load(path):
    return ex.load(ex.open_engine(path), "planted")


def _planted() -> list[str]:
    return [SESSION, SESSION[:8], AGENT, BRACKET, SECRET, TOKEN, PHONE.split(":")[1], MSG_ID, EMAIL, "Someone", IP,
            UUID, *BALANCES, *NEVER]


def test_the_full_export_contains_none_of_the_planted_values(source):
    src = _load(source)
    items = ex.export_items(src, ex.wakes(src), Redactor(b"fixed-salt"))
    blob = json.dumps(items)
    leaked = [p for p in _planted() if p in blob]
    assert not leaked, leaked
    assert scan(items, extra=tuple(_planted())) == []
    assert '"balances"' not in blob and '"message_ids"' not in blob and "phone:" not in blob
    # the exact input (agent_wake_inputs) is used for the second wake: memory and observe exact, balances dropped
    second = items[1]
    assert second["fidelity"]["memory"] == "exact" and second["input"]["message_verbatim"]
    assert "balances" not in second["input"]["observe"] and second["context"]["stale_data"] is False
    assert items[0]["fidelity"]["memory"] == "reconstructed"
    # pseudonyms are stable within an export: one bracket, one session, one agent
    h = {o["bracket_id"] for it in items for o in it["outcome"]["trades"]}
    assert len(h) == 1 and h.pop().startswith("h_")
    assert {it["source"]["session"] for it in items} == {"s01"} and {it["source"]["agent"] for it in items} == {"a01"}
    assert items[0]["outcome"]["trades"][0]["rule"] == "CUSHION100" and items[0]["outcome"]["trades"][0]["won"] is False
    assert items[0]["input"]["events"][0]["value"]["by"] == "user"


@pytest.mark.parametrize("rule", [r for r in RULES])
def test_switching_off_any_one_redaction_makes_the_scan_fail_the_export(source, rule):
    src = _load(source)
    with pytest.raises(ex.ExportScanError) as e:
        ex.export_items(src, ex.wakes(src), Redactor(b"fixed-salt", disabled=frozenset({rule})))
    assert e.value.hits, rule


def test_the_scan_alone_catches_every_planted_kind(source):
    src = _load(source)
    with pytest.raises(ex.ExportScanError) as e:
        ex.export_items(src, ex.wakes(src), Redactor(b"fixed-salt", disabled=frozenset(RULES)))
    rules = {h.rule for h in e.value.hits}
    for want in ("api_key", "high_entropy", "email", "ip", "path", "anthropic_id", "uuid", "hex_id", "phone_id",
                 "balance", "balance_field", "dropped_key_present", "planted_value", "key_file"):
        assert want in rules, (want, sorted(rules))


def test_the_exporter_never_reads_order_tables_audit_detail_or_tokens(source):
    engine = ex.open_engine(source)
    seen: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda conn, cur, stmt, *a: seen.append(stmt.lower()))
    ex.load(engine, "planted")
    assert len(seen) == 4
    for stmt in seen:
        for bad in ex.FORBIDDEN:
            assert bad not in stmt, (bad, stmt)
    for q in ex.QUERIES:
        assert not any(b in q.lower() for b in ex.FORBIDDEN)


def test_the_source_is_opened_read_only(source):
    engine = ex.open_engine(source)
    with pytest.raises(Exception, match="readonly|read-only|attempt to write"):
        with engine.begin() as c:
            c.exec_driver_sql("delete from agent_journal")


def test_a_failed_scan_writes_nothing_and_exits_2(source, tmp_path, monkeypatch, capsys):
    out = tmp_path / "x.jsonl"
    monkeypatch.setattr(ex, "Redactor", lambda *a, **k: Redactor(b"s", disabled=frozenset({"secrets"})))
    assert ex.main(["--source", source, "--all", "--out", str(out)]) == 2
    assert not out.exists() and "export refused" in capsys.readouterr().err
    monkeypatch.undo()
    assert ex.main(["--source", source, "--all", "--out", str(out)]) == 0
    assert out.exists() and len(out.read_text(encoding="utf-8").splitlines()) == 2
    manifest = json.loads(out.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["scan"] == "clean" and manifest["wake_inputs_table"] is True


def test_ids_resolve_a_decision_row_to_its_wake(source):
    src = _load(source)
    dec = next(r["id"] for rows in src.rows.values() for r in rows if r["kind"] == "decision")
    (w,) = ex.wakes(src, [dec])
    assert w["kind"] == "wake" and w["id"] > dec
    with pytest.raises(KeyError):
        ex.wakes(src, [10_000])


def test_text_redaction_keeps_market_data_and_drops_account_figures():
    r = Redactor(b"s")
    t = r.text("BTC 77429.11 vs strike 77385.19 on cpc-btc-updown-15m-2026-10-29-1500z; "
               "(Kalshi $41.27, Polymarket $88.40 cash). Budget $20.")
    assert "77429.11" in t and "cpc-btc-updown-15m-2026-10-29-1500z" in t and "Budget $20." in t
    assert "41.27" not in t and "88.40" not in t
