"""Settleview's SLO alerts as code, applied to OpenObserve through its own HTTP API (P3 item 2; docs/operations.md,
"Alerting"). Standard library only, any OS: `python deploy/openobserve/alerts.py apply`.

The definitions are OpenObserve's own formats, nothing of ours: `alerts/*.json` are alerts as OpenObserve's
`POST /api/v2/{org}/alerts/{id}/export` returns them (minus the server's ids and timestamps; the UI's Import takes them
as they are), `templates/*.json` its alert templates. OpenObserve runs every query, deduplicates (`silence`: one
notification per alert, repeated every 10 min for critical and 30 min for warnings while it still fires), sends the
recovery (`notify_on_recovery` to the `resolved` destination) and holds the history. Delivery is OpenObserve's
standard HTTP destination straight to the Telegram Bot API's sendMessage: critical alerts notify, warnings and
resolutions arrive silently (Telegram's `disable_notification`).

  apply     templates, then the three destinations (settleview_telegram_critical / _warning / _resolved), then every
            alert: created, or replaced by name when it exists (idempotent; run again after any change). An alert
            whose metric stream does not exist yet (nothing exported it so far: perps off, a fresh install) is
            reported `pending` and applied by a later run. `settleview_*` alerts no longer in alerts/ are deleted;
            other alerts are never touched.
  test      triggers the disabled `settleview_test` alert by hand: one message through the whole path.
  export    writes the named alerts (or every settleview_* one) from OpenObserve back into alerts/ (an edit made in
            the UI, kept as code).

Credentials, read at run time only and never printed or stored by this script beyond OpenObserve's own config:
  OpenObserve root login   O2_ROOT_PASSWORD, else ZO_ROOT_USER_PASSWORD in secrets/openobserve.env (as ingest_token.py)
  Telegram bot token       ALERTS_TELEGRAM_BOT_TOKEN, else secrets/alerts_telegram_bot_token: use a bot made for
                           alerts only (not the app's BTCFLOW_TELEGRAM_BOT_TOKEN). The token is part of the
                           destination's URL, so it lives in OpenObserve's configuration (readable by its admins);
                           a separate bot keeps the app's remote control out of it, and revoking it (@BotFather)
                           stops only the alerts.
  Telegram chat id         ALERTS_TELEGRAM_CHAT_ID, else secrets/alerts_telegram_chat_id (your user id: message the
                           alerts bot once, then read it from getUpdates). Added to each template's body.

Options: --url (default http://127.0.0.1:5080), --org (default), --secrets DIR, --runbook-base URL (replaces the
runbook links' https://github.com/jon-lange/settleview/blob/main/docs/operations.md for a fork), --telegram-api URL
(default https://api.telegram.org; a test points it at a fake). Exit 0 done, 1 an error, 2 done with alerts pending.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALERTS_DIR, TEMPLATES_DIR = HERE / "alerts", HERE / "templates"
ROOT_EMAIL = "admin@settleview.local"  # ZO_ROOT_USER_EMAIL in compose.yaml (a login name, not a secret)
PREFIX = "settleview_"
TEST_ALERT = "settleview_test"
LEVELS = ("critical", "warning", "resolved")
RUNBOOK_BASE = "https://github.com/jon-lange/settleview/blob/main/docs/operations.md"
TELEGRAM_API = "https://api.telegram.org"
# What OpenObserve adds to an alert that is not part of its definition (dropped on export).
SERVER_FIELDS = ("id", "org_id", "owner", "last_edited_by", "last_triggered_at", "last_satisfied_at", "updated_at",
                 "tz_offset", "workflows", "creates_incident", "pending_period_sec", "keep_firing_for")
TIMEOUT_S = 20


class O2:
    def __init__(self, url: str, org: str, password: str) -> None:
        self.base, self.org = url.rstrip("/"), org
        self.auth = "Basic " + base64.b64encode(f"{ROOT_EMAIL}:{password}".encode()).decode("ascii")

    def call(self, method: str, path: str, body: object | None = None) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path.replace("{org}", self.org), data=data, method=method,  # noqa: S310
                                     headers={"Authorization": self.auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 - main() allows http(s) only
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {"message": raw[:200].decode("utf-8", "replace")}

    def alerts(self) -> dict[str, str]:
        """Every alert's id by name."""
        status, body = self.call("GET", "/api/v2/{org}/alerts")
        if status != 200:
            raise SystemExit(f"error   listing alerts: HTTP {status} {_msg(body)}")
        return {a["name"]: a["alert_id"] for a in body.get("list", [])}


def _msg(body: object) -> str:
    return str(body.get("message", ""))[:200] if isinstance(body, dict) else ""


def _secret(env: str, secrets: Path, name: str, key: str | None = None) -> str:
    """A value from the environment, else from a file in the secrets folder (a KEY= line when `key`)."""
    v = os.environ.get(env, "").strip()
    if v:
        return v
    f = secrets / name
    try:
        text = f.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error   cannot read {f} ({type(exc).__name__}); or set {env}") from None
    if key is not None:
        text = next((ln.split("=", 1)[1] for ln in text.splitlines() if ln.startswith(f"{key}=")), "")
    v = text.strip()
    if not v:
        raise SystemExit(f"error   {f} is empty; or set {env}")
    return v


def load(folder: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


def with_runbook(alert: dict, base: str) -> dict:
    ctx = dict(alert.get("context_attributes") or {})
    if ctx.get("runbook", "").startswith(RUNBOOK_BASE + "#"):
        ctx["runbook"] = base.rstrip("#") + ctx["runbook"][len(RUNBOOK_BASE):]
    return {**alert, "context_attributes": ctx}


def put_template(o2: O2, tpl: dict, chat_id: str) -> None:
    body = json.loads(tpl["body"])
    body["chat_id"] = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
    t = {**tpl, "body": json.dumps(body, ensure_ascii=False)}
    status, out = o2.call("PUT", f"/api/{{org}}/alerts/templates/{tpl['name']}", t)
    if status == 404:
        status, out = o2.call("POST", "/api/{org}/alerts/templates", t)
    if status != 200:
        raise SystemExit(f"error   template {tpl['name']}: HTTP {status} {_msg(out)}")
    print(f"ok      template {tpl['name']}")


def put_destination(o2: O2, level: str, url: str) -> None:
    name = f"{PREFIX}telegram_{level}"
    d = {"name": name, "type": "http", "method": "post", "url": url, "template": name, "skip_tls_verify": False,
         "headers": {}}
    status, out = o2.call("PUT", f"/api/{{org}}/alerts/destinations/{name}", d)
    if status == 404:
        status, out = o2.call("POST", "/api/{org}/alerts/destinations", d)
    if status != 200:  # the message never holds the URL (OpenObserve names the destination)
        raise SystemExit(f"error   destination {name}: HTTP {status} {_msg(out).replace(url, '<url>')}")
    print(f"ok      destination {name}")


def apply(o2: O2, a: argparse.Namespace) -> int:
    token = _secret("ALERTS_TELEGRAM_BOT_TOKEN", a.secrets, "alerts_telegram_bot_token")
    chat = _secret("ALERTS_TELEGRAM_CHAT_ID", a.secrets, "alerts_telegram_chat_id")
    if ":" not in token or any(c.isspace() or c == "/" for c in token):
        print("error   the alerts bot token does not look like a Telegram bot token (<id>:<secret>)", file=sys.stderr)
        return 1
    templates = {t["name"]: t for t in load(TEMPLATES_DIR)}
    for level in LEVELS:
        put_template(o2, templates[f"{PREFIX}telegram_{level}"], chat)
    url = f"{a.telegram_api.rstrip('/')}/bot{token}/sendMessage"
    for level in LEVELS:
        put_destination(o2, level, url)
    existing = o2.alerts()
    wanted = [with_runbook(x, a.runbook_base) for x in load(ALERTS_DIR)]
    pending = 0
    for alert in wanted:
        name = alert["name"]
        if name in existing:
            status, out = o2.call("PUT", f"/api/v2/{{org}}/alerts/{existing[name]}", alert)
            verb = "updated"
        else:
            status, out = o2.call("POST", "/api/v2/{org}/alerts", alert)
            verb = "created"
        if status == 200:
            print(f"ok      alert {name} {verb}")
        elif status == 404 and "not found" in _msg(out).lower() and alert["stream_name"] in _msg(out):
            pending += 1
            print(f"pending alert {name}: stream {alert['stream_name']} does not exist yet (nothing exported it); "
                  "run apply again later")
        else:
            print(f"error   alert {name}: HTTP {status} {_msg(out)}", file=sys.stderr)
            return 1
    names = {x["name"] for x in wanted}
    for name, alert_id in existing.items():
        if name.startswith(PREFIX) and name not in names:
            status, out = o2.call("DELETE", f"/api/v2/{{org}}/alerts/{alert_id}")
            print(f"{'ok     ' if status == 200 else 'error  '} alert {name} deleted (no longer in alerts/)")
    return 2 if pending else 0


def test(o2: O2) -> int:
    alert_id = o2.alerts().get(TEST_ALERT)
    if alert_id is None:
        print(f"error   {TEST_ALERT} is not in OpenObserve: run apply first", file=sys.stderr)
        return 1
    status, out = o2.call("PATCH", f"/api/v2/{{org}}/alerts/{alert_id}/trigger")
    print(f"{'ok' if status == 200 else 'error'}      test alert: HTTP {status} {_msg(out)}")
    return 0 if status == 200 else 1


def export(o2: O2, names: list[str]) -> int:
    existing = o2.alerts()
    for name in names or sorted(n for n in existing if n.startswith(PREFIX)):
        if name not in existing:
            print(f"error   no alert {name}", file=sys.stderr)
            return 1
        status, body = o2.call("POST", f"/api/v2/{{org}}/alerts/{existing[name]}/export")
        if status != 200 or not isinstance(body, dict):
            print(f"error   export {name}: HTTP {status}", file=sys.stderr)
            return 1
        for k in SERVER_FIELDS:
            body.pop(k, None)
        (ALERTS_DIR / f"{name}.json").write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n",
                                                  encoding="utf-8", newline="\n")
        print(f"wrote   {ALERTS_DIR.name}/{name}.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="alerts.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=("apply", "test", "export"))
    ap.add_argument("names", nargs="*", help="export: the alerts to write back (default: every settleview_* one)")
    ap.add_argument("--url", default="http://127.0.0.1:5080")
    ap.add_argument("--org", default="default")
    ap.add_argument("--secrets", type=Path, default=None)
    ap.add_argument("--runbook-base", default=RUNBOOK_BASE)
    ap.add_argument("--telegram-api", default=TELEGRAM_API)
    a = ap.parse_args(argv)
    for opt in (a.url, a.telegram_api, a.runbook_base):
        if not opt.startswith(("http://", "https://")):
            print(f"error   {opt!r} is not an http(s) address", file=sys.stderr)
            return 2
    if a.secrets is None:  # secrets/ next to compose.yaml (this file is deploy/openobserve/alerts.py)
        a.secrets = HERE.parents[1] / "secrets"
    o2 = O2(a.url, urllib.parse.quote(a.org, safe=""),
            _secret("O2_ROOT_PASSWORD", a.secrets, "openobserve.env", "ZO_ROOT_USER_PASSWORD"))
    if a.cmd == "apply":
        return apply(o2, a)
    if a.cmd == "test":
        return test(o2)
    return export(o2, a.names)


if __name__ == "__main__":
    sys.exit(main())
