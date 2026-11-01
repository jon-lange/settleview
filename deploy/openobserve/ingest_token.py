"""Give the OpenTelemetry Collector an ingest-only OpenObserve credential (review B4#14):
`python deploy/openobserve/ingest_token.py` (any OS, standard library only), once OpenObserve runs (`docker compose
--profile observability up -d`).

OpenObserve's organization ingestion token (its "passcode") is accepted on the ingestion endpoints only: with it the
collector can send logs, metrics and traces, and every other API (search, streams, users, settings) answers 401.
This reads the token with the root login (its password from secrets/openobserve.env, or O2_ROOT_PASSWORD in the
environment, e.g. on Swarm), checks both halves with it (an empty OTLP batch is accepted with Basic auth of
`otlp@settleview.local:<token>`, the user name the collector sends, deploy/otel-collector/config.yaml; the e-mail is
only a label), and only then writes the token alone into secrets/openobserve_ingest_token, the collector's
`basicauth` password file. Only the collector mounts it; the services hold no backend credential at all. Prints file
names and results only, never a value.

  --url URL        OpenObserve's address from where this runs (default http://127.0.0.1:5080, compose's loopback
                   publish; an SSH tunnel on Swarm)
  --org ORG        the organization (default `default`, as the collector's endpoint .../api/default)
  --secrets DIR    the secrets folder (default: secrets/ next to compose.yaml)
  --rotate         issue a new token first (the old one stops working: run this, then recreate the collector)

Idempotent: run again, it changes nothing (the file is untouched when it already holds the token). After a change,
recreate the collector (its secret is mounted at its start): `docker compose up -d --force-recreate otel-collector`.
Exit 0 when the token works and is ingest-only, 1 otherwise (nothing is written when it cannot be read or checked).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The root login's e-mail: the same literal as ZO_ROOT_USER_EMAIL in compose.yaml (a login name, not a secret).
ROOT_EMAIL = "admin@settleview.local"
INGEST_LABEL = "otlp@settleview.local"  # the collector's basicauth username; the token, not the name, is checked
O2_ENV = "openobserve.env"
INGEST_TOKEN = "openobserve_ingest_token"  # the collector's password_file
TIMEOUT_S = 15


def _basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")


def _call(method: str, url: str, auth: str, body: bytes | None = None) -> tuple[int, bytes]:
    # main() refuses any --url that is not http(s), so no file: or custom scheme reaches urlopen.
    req = urllib.request.Request(url, data=body, method=method, headers={"Authorization": auth})  # noqa: S310
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 - an http(s) URL the operator names
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def root_password(secrets_dir: Path) -> str:
    pw = os.environ.get("O2_ROOT_PASSWORD", "")
    if pw:
        return pw
    f = secrets_dir / O2_ENV
    try:
        lines = f.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise SystemExit(f"error   cannot read {f} ({type(exc).__name__}); or set O2_ROOT_PASSWORD") from None
    pw = next((ln.split("=", 1)[1].strip() for ln in lines if ln.startswith("ZO_ROOT_USER_PASSWORD=")), "")
    if not pw:
        raise SystemExit(f"error   {f} has no ZO_ROOT_USER_PASSWORD line")
    return pw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingest_token.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:5080")
    ap.add_argument("--org", default="default")
    ap.add_argument("--secrets", type=Path, default=None)
    ap.add_argument("--rotate", action="store_true")
    a = ap.parse_args(argv)
    if not a.url.startswith(("http://", "https://")):
        print("error   --url must be an http(s) address", file=sys.stderr)
        return 2
    if a.secrets is None:  # secrets/ next to compose.yaml (this file is deploy/openobserve/ingest_token.py)
        a.secrets = Path(__file__).resolve().parents[2] / "secrets"
    base = a.url.rstrip("/") + f"/api/{a.org}"
    root = _basic(ROOT_EMAIL, root_password(a.secrets))
    status, body = _call("PUT" if a.rotate else "GET", base + "/passcode", root)
    if status != 200:
        print(f"error   reading the ingestion token: HTTP {status} (is OpenObserve up at {a.url}, and the root "
              "password the one it first started with?)", file=sys.stderr)
        return 1
    try:
        token = json.loads(body)["data"]["passcode"]
    except (ValueError, KeyError, TypeError):
        print("error   unexpected answer from /passcode", file=sys.stderr)
        return 1
    ingest = _basic(INGEST_LABEL, token)
    sent, _ = _call("POST", base + "/v1/logs", ingest, b'{"resourceLogs":[]}')
    read, _ = _call("GET", base + "/streams", ingest)
    print(f"check   OTLP ingestion with the token: HTTP {sent} (want 200)")
    print(f"check   reading streams with the token: HTTP {read} (want 401: ingest-only)")
    if sent != 200 or read != 401:
        print("error   the token is not an ingest-only credential here: nothing written", file=sys.stderr)
        return 1
    f = a.secrets / INGEST_TOKEN
    if f.exists() and f.read_text(encoding="ascii", errors="replace").strip() == token:
        print(f"kept    {f} (already the ingest-only token)")
    else:
        tmp = f.with_name(f.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)  # readable by the containers' users
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as out:
            out.write(token)
        os.replace(tmp, f)
        print(f"wrote   {f} (ingest-only)")
        print("next    docker compose up -d --force-recreate otel-collector")
    return 0


if __name__ == "__main__":
    sys.exit(main())
