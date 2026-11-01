"""Give the services an ingest-only OpenObserve credential (review B4#14): `python deploy/openobserve/ingest_token.py`
(any OS, standard library only), once OpenObserve runs (`docker compose --profile observability up -d`).

OpenObserve's organization ingestion token (its "passcode") is accepted on the ingestion endpoints only: with it the
OTLP exporters can send logs, metrics and traces, and every other API (search, streams, users, settings) answers 401.
This reads the token with the root login (its password from secrets/openobserve.env, or O2_ROOT_PASSWORD in the
environment, e.g. on Swarm), writes the OTLP header into secrets/otlp_headers (`Authorization=Basic ...` of
`otlp@settleview.local:<token>`; the e-mail is only a label, any name works with the token), then checks both halves:
an empty OTLP batch is accepted with the new header, and a read (the stream list) is refused. The services mount
secrets/otlp_headers, so they never hold the root password. Prints file names and results only, never a value.

  --url URL        OpenObserve's address from where this runs (default http://127.0.0.1:5080, compose's loopback
                   publish; an SSH tunnel on Swarm)
  --org ORG        the organization (default `default`, as OTEL_EXPORTER_OTLP_ENDPOINT's /api/default)
  --secrets DIR    the secrets folder (default: secrets/ next to compose.yaml)
  --rotate         issue a new token first (the old one stops working: run this, then restart the services)

Idempotent: run again, it rewrites the same header (the file is untouched when it already holds it). Then restart
the services that read it: `docker compose up -d --force-recreate app rec-pm-quotes rec-pm-settle`. Exit 0 when the
header works and is ingest-only, 1 otherwise (nothing is written when the token cannot be read).
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
INGEST_LABEL = "otlp@settleview.local"  # shown as the sender; the token, not the name, is checked
O2_ENV = "openobserve.env"
OTLP_HEADERS = "otlp_headers"
TIMEOUT_S = 15


def _basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")


def _call(method: str, url: str, auth: str, body: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers={"Authorization": auth})
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


def header_line(token: str) -> str:
    """The OTEL_EXPORTER_OTLP_HEADERS form: key=value, the value percent-encoded (its one space)."""
    return "Authorization=" + _basic(INGEST_LABEL, token).replace(" ", "%20") + "\n"


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
    f = a.secrets / OTLP_HEADERS
    line = header_line(token)
    if f.exists() and f.read_text(encoding="ascii", errors="replace") == line:
        print(f"kept    {f} (already the ingest-only header)")
    else:
        tmp = f.with_name(f.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)  # readable by the containers' users
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as out:
            out.write(line)
        os.replace(tmp, f)
        print(f"wrote   {f} (ingest-only)")
        print("next    docker compose up -d --force-recreate app rec-pm-quotes rec-pm-settle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
