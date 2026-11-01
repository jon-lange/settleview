"""deploy/openobserve/alerts.py and its definitions (P3 item 2): the SLO alerts as OpenObserve's own export format,
applied idempotently through its HTTP API, delivered by its HTTP destination to Telegram's sendMessage. Against a fake
OpenObserve on a local port; the real v1.1.1 image was checked on 2026-11-01 (docs/solutions/
2026-11-01-openobserve-alerting.md). No Telegram call is ever made."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("o2_alerts", ROOT / "deploy" / "openobserve" / "alerts.py")
al = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(al)

ROOT_PW, BOT, CHAT = "Root-Pass-1x", "123456:fake-alerts-bot-token", "4242"
ALERTS = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(al.ALERTS_DIR.glob("*.json"))]
TEMPLATES = {json.loads(p.read_text(encoding="utf-8"))["name"]: json.loads(p.read_text(encoding="utf-8"))
             for p in al.TEMPLATES_DIR.glob("*.json")}


# -- the definitions ---------------------------------------------------------------------------------------------------
def _anchors() -> set[str]:
    return set(re.findall(r'<a id="([a-z0-9-]+)"></a>', (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")))


@pytest.mark.parametrize("alert", ALERTS, ids=[a["name"] for a in ALERTS])
def test_each_alert_is_a_complete_openobserve_definition(alert):
    path = al.ALERTS_DIR / f"{alert['name']}.json"
    assert path.exists() and alert["name"].startswith(al.PREFIX)
    assert not set(al.SERVER_FIELDS) & set(alert), "server-side fields stay out of the repo"
    ctx = alert["context_attributes"]
    assert ctx["severity"] in ("critical", "warning") and ctx["slo"]
    assert alert["destinations"] == [f"settleview_telegram_{ctx['severity']}"]
    assert ctx["runbook"].startswith(al.RUNBOOK_BASE + "#")
    assert ctx["runbook"].split("#", 1)[1] in _anchors(), "the runbook anchor exists in docs/operations.md"
    q = alert["query_condition"]
    assert q["type"] == "sql" and f'"{alert["stream_name"]}"' in q["sql"]
    t = alert["trigger_condition"]
    assert t["silence"] == (10 if ctx["severity"] == "critical" else 30)  # repeat while firing, not every run
    assert t["frequency"] <= t["period"], "every minute of the period is evaluated"
    if alert["name"] == al.TEST_ALERT:
        assert alert["enabled"] is False and not alert["notify_on_recovery"]
    else:
        assert alert["enabled"] and alert["notify_on_recovery"]
        assert alert["recovery_destinations"] == ["settleview_telegram_resolved"]
    assert len(alert["description"]) <= 90, "the message's first line stays short"


def test_every_stream_an_alert_reads_is_exported_by_the_code():
    """A metric renamed in the code must not leave an alert reading nothing (OTel dots -> OpenObserve underscores)."""
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "btc_signal_engine").rglob("*.py"))
    for a in ALERTS:
        if a["stream_type"] != "metrics":
            continue
        name = a["stream_name"]
        candidates = re.findall(r'"((?:settleview|process)\.[a-z_.]+)"', src)
        assert name in {c.replace(".", "_") for c in candidates}, f"{name} is not exported by any service"


def test_templates_are_telegram_sendmessage_bodies():
    assert set(TEMPLATES) == {f"settleview_telegram_{x}" for x in al.LEVELS}
    for name, t in TEMPLATES.items():
        assert t["type"] == "http"
        body = json.loads(t["body"])
        assert "chat_id" not in body, "the chat id is added at apply time, never committed"
        assert body["disable_notification"] is (not name.endswith("critical"))
        assert "{alert_description}" in body["text"]
        assert "parse_mode" not in body, "plain text: no markup a row could break"


# -- apply / test / export against a fake OpenObserve -----------------------------------------------------------------
class FakeO2:
    def __init__(self, missing_streams=()) -> None:
        self.templates, self.destinations, self.alerts = {}, {}, {}
        self.missing = set(missing_streams)
        self.triggered: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self._n = 0
        o2 = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                body = json.dumps(obj or {}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def _authed(self):
                want = "Basic " + base64.b64encode(f"{al.ROOT_EMAIL}:{ROOT_PW}".encode()).decode()
                return self.headers.get("Authorization") == want

            def _route(self, method):
                o2.calls.append((method, self.path))
                if not self._authed():
                    return self._send(401, {"message": "Unauthorized"})
                p, body = self.path, self._body() if method in ("POST", "PUT") else None
                if m := re.fullmatch(r"/api/default/alerts/(templates|destinations)(?:/([\w]+))?", p):
                    store = o2.templates if m[1] == "templates" else o2.destinations
                    if method == "PUT":
                        if m[2] not in store:
                            return self._send(404, {"message": "not found"})
                        store[m[2]] = body
                        return self._send(200, {"message": "updated"})
                    store[body["name"]] = body
                    return self._send(200, {"message": "saved"})
                if p == "/api/v2/default/alerts" and method == "GET":
                    return self._send(200, {"list": [{"name": a["name"], "alert_id": i}
                                                     for i, a in o2.alerts.items()]})
                if p == "/api/v2/default/alerts" and method == "POST":
                    if body["stream_name"] in o2.missing:
                        return self._send(404, {"message": f"Stream {body['stream_name']} not found"})
                    o2._n += 1
                    o2.alerts[f"id{o2._n}"] = body
                    return self._send(200, {"message": "Alert saved"})
                if m := re.fullmatch(r"/api/v2/default/alerts/(id\d+)(/trigger|/export)?", p):
                    if m[1] not in o2.alerts:
                        return self._send(404, {"message": "Alert not found"})
                    if m[2] == "/trigger":
                        o2.triggered.append(o2.alerts[m[1]]["name"])
                        return self._send(200, {"message": "Alert triggered"})
                    if m[2] == "/export":
                        return self._send(200, {**o2.alerts[m[1]], "id": m[1], "org_id": "default",
                                                "owner": "admin", "last_triggered_at": 1})
                    if method == "PUT":
                        o2.alerts[m[1]] = body
                    elif method == "DELETE":
                        del o2.alerts[m[1]]
                    return self._send(200, {"message": "ok"})
                return self._send(404, {"message": "no route"})

            def do_GET(self):  # noqa: N802
                self._route("GET")

            def do_POST(self):  # noqa: N802
                self._route("POST")

            def do_PUT(self):  # noqa: N802
                self._route("PUT")

            def do_PATCH(self):  # noqa: N802
                self._route("PATCH")

            def do_DELETE(self):  # noqa: N802
                self._route("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    for v in ("O2_ROOT_PASSWORD", "ALERTS_TELEGRAM_BOT_TOKEN", "ALERTS_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / "openobserve.env").write_text(f"ZO_ROOT_USER_PASSWORD={ROOT_PW}\n")
    (tmp_path / "alerts_telegram_bot_token").write_text(BOT + "\n")
    (tmp_path / "alerts_telegram_chat_id").write_text(CHAT + "\n")
    return tmp_path


def run(o2, secrets, *args):
    return al.main([*args, "--url", o2.url, "--secrets", str(secrets)])


def test_apply_installs_everything_and_is_idempotent(secrets, capsys):
    o2 = FakeO2()
    o2.alerts["id900"] = {"name": "settleview_retired", "stream_name": "x"}  # ours, no longer in alerts/
    o2.alerts["id901"] = {"name": "someone_elses", "stream_name": "x"}  # never touched
    assert run(o2, secrets, "apply") == 0
    assert set(o2.templates) == set(TEMPLATES)
    assert json.loads(o2.templates["settleview_telegram_critical"]["body"])["chat_id"] == 4242
    d = o2.destinations["settleview_telegram_warning"]
    assert d["url"] == f"https://api.telegram.org/bot{BOT}/sendMessage" and d["template"] == "settleview_telegram_warning"
    names = sorted(a["name"] for a in o2.alerts.values())
    assert names == sorted([a["name"] for a in ALERTS] + ["someone_elses"])
    first = dict(o2.alerts)
    assert run(o2, secrets, "apply") == 0
    assert o2.alerts == first, "a second apply replaces each alert in place: same ids, same content"
    out = capsys.readouterr()
    assert BOT not in out.out + out.err and ROOT_PW not in out.out + out.err


def test_an_alert_on_a_stream_not_exported_yet_is_pending_then_applied(secrets, capsys):
    o2 = FakeO2(missing_streams={"settleview_perps_book_live"})
    assert run(o2, secrets, "apply") == 2
    assert "pending alert settleview_feed_perps" in capsys.readouterr().out
    o2.missing.clear()
    assert run(o2, secrets, "apply") == 0
    assert "settleview_feed_perps" in {a["name"] for a in o2.alerts.values()}


def test_runbook_base_is_rewritten_for_a_fork(secrets):
    o2 = FakeO2()
    assert al.main(["apply", "--url", o2.url, "--secrets", str(secrets),
                    "--runbook-base", "https://example.org/ops.md"]) == 0
    assert all(a["context_attributes"]["runbook"].startswith("https://example.org/ops.md#")
               for a in o2.alerts.values())


def test_test_triggers_the_test_alert(secrets):
    o2 = FakeO2()
    assert run(o2, secrets, "test") == 1  # not applied yet
    assert run(o2, secrets, "apply") == 0
    assert run(o2, secrets, "test") == 0
    assert o2.triggered == [al.TEST_ALERT]


def test_export_writes_the_definition_without_server_fields(secrets, tmp_path, monkeypatch):
    o2 = FakeO2()
    assert run(o2, secrets, "apply") == 0
    monkeypatch.setattr(al, "ALERTS_DIR", tmp_path)
    assert run(o2, secrets, "export", "settleview_writer_outage") == 0
    got = json.loads((tmp_path / "settleview_writer_outage.json").read_text(encoding="utf-8"))
    assert got == json.loads((ROOT / "deploy/openobserve/alerts/settleview_writer_outage.json").read_text("utf-8"))


def test_credentials_come_from_the_environment_first(secrets, monkeypatch):
    o2 = FakeO2()
    monkeypatch.setenv("ALERTS_TELEGRAM_CHAT_ID", "-100777")
    assert run(o2, secrets, "apply") == 0
    assert json.loads(o2.templates["settleview_telegram_resolved"]["body"])["chat_id"] == -100777


def test_refuses_a_malformed_bot_token_and_wrong_password(secrets):
    (secrets / "alerts_telegram_bot_token").write_text("not a token\n")
    o2 = FakeO2()
    assert run(o2, secrets, "apply") == 1 and not o2.destinations
    (secrets / "alerts_telegram_bot_token").write_text(BOT)
    (secrets / "openobserve.env").write_text("ZO_ROOT_USER_PASSWORD=wrong\n")
    with pytest.raises(SystemExit):
        run(o2, secrets, "apply")
