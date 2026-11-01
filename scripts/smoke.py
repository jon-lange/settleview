"""The deploy gate's smoke suite: is a running Settleview stack whole? READ-ONLY, so it runs against staging and, right
after a deploy, against production (docs/operations.md, "Staging and the deploy gate").

    python scripts/dev.py smoke --project settleview-staging            # the usual way (builds the dev image first)
    python scripts/smoke.py --project settleview-staging --wait 300     # the same, the dev image already built

It never places an order, never changes a setting and never pairs a browser:
  - On the host (standard library only), Docker is only asked: `docker ps`, `docker inspect` and `docker logs` of the
    project's containers. Checks: the migrate job exited 0; postgres, the app and every recorder running and healthy
    (the backup service running: it reports healthy only after its first nightly dump); no OTLP export error in the
    last 30 minutes of the app's and recorders' logs; on staging (BTCFLOW_STAGING), no production key file mounted.
  - The HTTP and browser checks run in the dev image (httpx, Playwright's Chromium) in a throwaway container that
    shares the app container's network namespace (`--network container:<app>`), so it reaches the app's own
    listeners at localhost:<port> with no port published (an offline staging has none) and passes the Host check as
    this PC does. It mounts the app's data volume READ-ONLY and signs a PC cookie under the secret the app already uses
    (adapters/pc_credentials.cookie_from_secret_file: nothing is written). Its HTTP client sends GET only (any other
    method raises before it is sent), and the browser aborts every request that is not a GET/HEAD to the app's own
    origin (the page's presence heartbeat included: it is listed, not sent). The container has a read-only root,
    the checkout read-only, no capabilities.
    Checks: /api/health on the phone listener ({status} without a session) and on the PC listener (the full health;
    `degraded` parts are listed, not failures: an offline staging has no venue feeds); the trading status (on staging:
    production off and not ready); Play's live stream sends a frame; the agent API lists sessions (skipped when agent
    mode is off); the agent listener answers its tool API only; the Play, Record, Agents and Perps screens load in
    headless Chromium with no page error.

Prints one line per check (PASS / FAIL / SKIP) and exits 0 only when nothing failed (2: bad arguments, no such stack).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV_IMAGE = "settleview-dev:local"  # deploy/dev/compose.yaml's image (scripts/dev.py builds it)
APP_UID = "10001:10001"  # the app's user (Dockerfile): the PC secret file is readable by it only
# Production credentials' secret files: never mounted in a staging stack (deploy/staging/compose.staging.yaml).
PROD_SECRET_FILES = ("trading_prod_key_id", "polymarket_key_id", "polymarket_secret_key", "kalshi_key_id",
                     "telegram_bot_token")
# The OpenTelemetry SDK's own lines when an export fails (opentelemetry.exporter.otlp.proto.http and the SDK's
# batch processors): one is enough to fail the check.
OTLP_ERROR = re.compile(r"(?i)(failed to export|exception while exporting|transient error .* exporting|"
                        r"exporter.*(error|failed)|max retries exceeded.*(v1/traces|v1/metrics|v1/logs))")
LOG_WINDOW = "30m"
VIEWS = ("play", "record", "agent", "perps")
SAFE = ("GET", "HEAD")
STRATEGIES = ("you", "you_poly", "you_poly5")  # the Play tabs' trading services (domain/markets.py)


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def line(self, status: str, name: str, detail: str = "") -> None:
        if status == "FAIL":
            self.failed += 1
        print(f"{status:4} {name}{': ' + detail if detail else ''}", flush=True)

    def ok(self, cond: bool, name: str, good: str, bad: str) -> bool:
        self.line("PASS" if cond else "FAIL", name, good if cond else bad)
        return cond


# -- host half: Docker, read-only -------------------------------------------------------------------------------------
def docker_env() -> dict[str, str]:
    # Git Bash rewrites absolute container paths into Windows paths (docs/operations.md, "Deploying"): never for us.
    return {**{k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")},
            "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    exe = shutil.which("docker")
    if exe is None:
        raise SystemExit("smoke: docker is not on PATH")
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",  # noqa: S603
                          timeout=timeout, env=docker_env())


def containers(project: str) -> dict[str, dict]:
    """service -> `docker inspect` of its container (compose one-off `run` containers are not services)."""
    ps = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}")
    if ps.returncode != 0:
        raise SystemExit(f"smoke: docker ps failed: {ps.stderr.strip()[:300]}")
    ids = ps.stdout.split()
    if not ids:
        return {}
    got = docker("inspect", *ids)
    if got.returncode != 0:
        raise SystemExit(f"smoke: docker inspect failed: {got.stderr.strip()[:300]}")
    out: dict[str, dict] = {}
    for c in json.loads(got.stdout):
        labels = c["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.oneoff") == "True":
            continue
        out[labels.get("com.docker.compose.service", c["Name"].lstrip("/"))] = c
    return out


def env_of(c: dict) -> dict[str, str]:
    """The container's settings. compose.yaml keeps every secret in a file, so its environment holds none."""
    return dict(e.split("=", 1) for e in c["Config"].get("Env") or [] if "=" in e)


def health(c: dict) -> str:
    st = c["State"]
    if not st.get("Running"):
        return f"not running ({st.get('Status')}, exit {st.get('ExitCode')})"
    return (st.get("Health") or {}).get("Status", "running")


def required(services: dict[str, dict]) -> list[str]:
    return [s for s in services if s in ("postgres", "app") or s.startswith("rec-")]


def wait_ready(project: str, wait_s: float) -> dict[str, dict]:
    """The project's containers once the migrate job has finished and every required service is healthy, or at the
    deadline (the checks then report what is wrong)."""
    deadline = time.monotonic() + wait_s
    while True:
        services = containers(project)
        mig = services.get("migrate")
        done = mig is not None and not mig["State"].get("Running")
        ready = done and all(health(services[s]) in ("healthy", "running") for s in required(services))
        if ready or time.monotonic() >= deadline or (done and mig["State"].get("ExitCode") != 0):
            return services
        time.sleep(5)


def host_checks(r: Report, project: str, services: dict[str, dict]) -> None:
    mig = services.get("migrate")
    if mig is None:
        r.line("FAIL", "migrate", "no migrate container in this project")
    else:
        st = mig["State"]
        r.ok(not st.get("Running") and st.get("ExitCode") == 0, "migrate", "exited 0",
             f"{st.get('Status')}, exit {st.get('ExitCode')} (docker compose -p {project} logs migrate)")
    for name in required(services):
        h = health(services[name])
        r.ok(h == "healthy", name, h, f"{h} (docker compose -p {project} logs {name})")
    if "backup" in services:
        h = health(services["backup"])
        r.ok(not h.startswith("not running") and h != "unhealthy", "backup", h, h)
    for name in ("app", *[s for s in services if s.startswith("rec-")]):
        c = services.get(name)
        if c is None:
            continue
        if not env_of(c).get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            r.line("PASS", f"otlp {name}", "no exporter configured")
            continue
        logs = docker("logs", "--since", LOG_WINDOW, c["Id"], timeout=120)
        bad = [ln for ln in (logs.stdout + logs.stderr).splitlines() if OTLP_ERROR.search(ln)]
        r.ok(logs.returncode == 0 and not bad, f"otlp {name}", f"no export error in the last {LOG_WINDOW}",
             f"{len(bad)} export error line(s), e.g. {bad[0][:200] if bad else logs.stderr.strip()[:200]}")


def staging_checks(r: Report, app: dict) -> None:
    mounted = {Path(m.get("Destination", "")).name for m in app.get("Mounts") or []
               if str(m.get("Destination", "")).startswith("/run/secrets/")}
    prod = sorted(mounted & set(PROD_SECRET_FILES))
    r.ok(not prod, "staging: no production key file", f"mounted secrets: {', '.join(sorted(mounted))}",
         f"production key file(s) mounted: {', '.join(prod)}")
    named = sorted(k for k in env_of(app) if k.removesuffix("_FILE").lower().removeprefix("btcflow_")
                   in PROD_SECRET_FILES)
    r.ok(not named, "staging: no production key setting", "none named", f"named: {', '.join(named)}")


def inside_command(project: str, app: dict, args: argparse.Namespace) -> list[str] | None:
    env = env_of(app)
    data = next((m for m in app.get("Mounts") or [] if m.get("Destination") == "/data"), None)
    if data is None:
        return None
    vol = (["--mount", f"type=volume,source={data['Name']},target=/data,readonly"] if data.get("Type") == "volume"
           else ["--mount", f"type=bind,source={data['Source']},target=/data,readonly"])
    ports = ["--api-port", env.get("BTCFLOW_API_PORT", "8000")]
    if env.get("BTCFLOW_PC_PORT"):
        ports += ["--pc-port", env["BTCFLOW_PC_PORT"]]
    if env.get("BTCFLOW_AGENT_PORT"):
        ports += ["--agent-port", env["BTCFLOW_AGENT_PORT"]]
    return ["run", "--rm", "--network", f"container:{app['Id']}", *vol,
            "--mount", f"type=bind,source={ROOT},target=/app,readonly", "-w", "/app",
            "--read-only", "--tmpfs", "/tmp:size=512m",  # noqa: S108 - the throwaway container's own tmpfs
            "--shm-size", "1g", "--user", APP_UID,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--label", f"settleview.smoke={project}",
            args.image, "python", "scripts/smoke.py", "--inside", *ports,
            *(["--staging"] if env.get("BTCFLOW_STAGING", "").lower() in ("1", "true", "yes", "on") else []),
            *(["--no-browser"] if args.no_browser else [])]


def host_main(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project):
        print(f"smoke: {args.project!r} is not a compose project name", file=sys.stderr)
        return 2
    print(f"smoke: project {args.project} (read-only)", flush=True)
    services = wait_ready(args.project, args.wait)
    if not services:
        print(f"smoke: the project {args.project} has no containers", file=sys.stderr)
        return 2
    r = Report()
    host_checks(r, args.project, services)
    app = services.get("app")
    if app is None or not app["State"].get("Running"):
        r.line("FAIL", "app", "not running: no HTTP checks")
        return 1
    if env_of(app).get("BTCFLOW_STAGING", "").lower() in ("1", "true", "yes", "on"):
        staging_checks(r, app)
    cmd = inside_command(args.project, app, args)
    if cmd is None:
        r.line("FAIL", "app data volume", "the app container mounts nothing at /data")
        return 1
    print("+ docker " + " ".join(cmd), flush=True)
    inside = subprocess.run([shutil.which("docker") or "docker", *cmd], env=docker_env(), timeout=600)  # noqa: S603
    if inside.returncode != 0:
        r.failed += 1
    print(f"smoke: {'FAILED' if r.failed else 'passed'} ({args.project})", flush=True)
    return 1 if r.failed else 0


# -- inside half: the app's own listeners, GET only ----------------------------------------------------------------
class ReadOnlyClient:
    """An httpx client that can only GET: any other method raises before anything is sent."""

    def __init__(self, base_url: str, cookies: dict[str, str] | None = None) -> None:
        import httpx

        def refuse(request) -> None:
            if request.method not in SAFE:
                raise PermissionError(f"the smoke suite is read-only: {request.method} {request.url.path} refused")

        self._c = httpx.Client(base_url=base_url, cookies=cookies, timeout=15, follow_redirects=False,
                               event_hooks={"request": [refuse]}, trust_env=False)

    def get(self, path: str, **kw):
        return self._c.get(path, **kw)

    def first_event(self, path: str, params: dict, timeout_s: float = 20.0) -> tuple[str, dict]:
        """(event, data) of the first server-sent event."""
        deadline = time.monotonic() + timeout_s
        event = ""
        with self._c.stream("GET", path, params=params, timeout=timeout_s) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    return event, json.loads(line[5:])
                if time.monotonic() > deadline:
                    break
        raise RuntimeError("no frame")

    def request(self, method: str, path: str, **kw):  # the guard's own test
        return self._c.request(method, path, **kw)

    def close(self) -> None:
        self._c.close()


def _json(resp) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def http_checks(r: Report, args: argparse.Namespace, cookie: str) -> None:
    from btc_signal_engine.adapters.pc_credentials import PC_COOKIE

    phone = ReadOnlyClient(f"http://localhost:{args.api_port}")
    try:
        resp = phone.get("/api/health")
        r.ok(resp.status_code == 200 and "status" in _json(resp), "health (phone listener)",
             f"{_json(resp).get('status')}", f"HTTP {resp.status_code}")
    finally:
        phone.close()
    pc = ReadOnlyClient(f"http://localhost:{args.pc_port}", cookies={PC_COOKIE: cookie})
    try:
        resp = pc.get("/api/health")
        h = _json(resp)
        if r.ok(resp.status_code == 200 and h.get("status") in ("live", "degraded"), "health (PC)",
                f"{h.get('status')}" + (f"; degraded: {', '.join(h.get('degraded') or [])}" if h.get("degraded")
                                        else ""), f"HTTP {resp.status_code} {str(h)[:200]}"):
            r.line("PASS", "health: loops", f"{len(h.get('loops') or {})} loop(s) reporting")
        for strategy in STRATEGIES:
            resp = pc.get("/api/trading/status", params={"strategy": strategy})
            t = _json(resp)
            if t.get("code") == "unknown_strategy":
                r.line("SKIP", f"trading {strategy}", "not wired on this server")
                continue
            r.ok(resp.status_code == 200 and "mode" in t, f"trading {strategy}", f"mode {t.get('mode')}",
                 f"HTTP {resp.status_code}")
            if args.staging:
                prod = (t.get("modes") or {}).get("prod") or {}
                r.ok(t.get("enabled") is False and t.get("mode") != "prod" and prod.get("ready") is not True,
                     f"staging: {strategy} has no production",
                     f"enabled {t.get('enabled')}, prod ready {prod.get('ready')} ({prod.get('reason')})",
                     f"enabled {t.get('enabled')}, mode {t.get('mode')}, prod {prod}")
        try:
            event, data = pc.first_event("/api/play/stream", {"venue": "kalshi", "span": "15m"})
            r.ok(event in ("view", "stale"), "play stream", f"first frame: {event} (seq {data.get('seq')})",
                 f"unexpected first event {event!r}")
        except Exception as e:
            r.line("FAIL", "play stream", f"{type(e).__name__}: {e}")
        resp = pc.get("/api/agent/sessions")
        if resp.status_code == 404:
            r.line("SKIP", "agent sessions", "agent mode is off on this server")
        else:
            body = _json(resp)
            r.ok(resp.status_code == 200 and body.get("ok") is True and isinstance(body.get("sessions"), list),
                 "agent sessions", f"{len(body.get('sessions') or [])} live session(s)", f"HTTP {resp.status_code}")
    finally:
        pc.close()
    if args.agent_port:
        agent = ReadOnlyClient(f"http://localhost:{args.agent_port}")
        try:
            other = agent.get("/api/health")
            tools = agent.get("/api/agent/tools")
            r.ok(other.status_code == 404 and tools.status_code in (200, 401, 403, 404), "agent listener",
                 f"tool API answers {tools.status_code}, other paths 404",
                 f"/api/health {other.status_code}, /api/agent/tools {tools.status_code}")
        finally:
            agent.close()


def browser_checks(r: Report, args: argparse.Namespace, cookie: str) -> None:
    from playwright import sync_api

    from btc_signal_engine.adapters.pc_credentials import PC_COOKIE

    base = f"http://localhost:{args.pc_port}"
    blocked: list[str] = []

    def guard(route) -> None:
        req = route.request
        if req.method in SAFE and req.url.startswith(base + "/"):
            route.continue_()
        else:
            blocked.append(f"{req.method} {req.url.removeprefix(base)}")
            route.abort()

    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            ctx = browser.new_context(base_url=base)
            ctx.add_cookies([{"name": PC_COOKIE, "value": cookie, "url": base, "httpOnly": True,
                              "sameSite": "Strict"}])
            ctx.route("**/*", guard)
            ctx.set_default_timeout(15_000)
            for view in VIEWS:
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)[:200]))
                try:
                    page.goto(f"/#{view}", wait_until="domcontentloaded")
                    page.locator(f"#panel-{view}").wait_for(state="visible")
                    page.wait_for_timeout(1_500)  # its first reads and the live stream
                    r.ok(not errors, f"page {view}", f"loaded ({page.title()})", "; ".join(errors))
                except sync_api.Error as e:
                    r.line("FAIL", f"page {view}", (str(e).strip().splitlines() or ["?"])[0][:200])
                finally:
                    page.close()
            ctx.close()
        finally:
            browser.close()
    kinds = sorted({b.split("?")[0] for b in blocked})
    r.line("PASS", "browser: read-only", f"{len(blocked)} non-GET or foreign request(s) aborted"
           + (f" ({', '.join(kinds[:6])})" if kinds else ""))


def inside_main(args: argparse.Namespace) -> int:
    from btc_signal_engine.adapters.pc_credentials import cookie_from_secret_file

    r = Report()
    try:
        cookie = cookie_from_secret_file(Path(args.data_dir))
    except (OSError, ValueError) as e:
        r.line("FAIL", "PC cookie", f"the app's PC secret is not readable ({type(e).__name__}): has it started?")
        return 1
    if not args.pc_port:
        r.line("FAIL", "PC listener", "the app has no BTCFLOW_PC_PORT: the PC checks need it")
        return 1
    http_checks(r, args, cookie)
    if args.no_browser:
        r.line("SKIP", "pages", "--no-browser")
    else:
        browser_checks(r, args, cookie)
    return 1 if r.failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The deploy gate's read-only smoke suite.")
    ap.add_argument("--project", help="the compose project to check (host mode), e.g. settleview-staging")
    ap.add_argument("--wait", type=float, default=0.0, help="seconds to wait for the stack to become healthy")
    ap.add_argument("--image", default=DEV_IMAGE, help="the image the HTTP and browser checks run in")
    ap.add_argument("--no-browser", action="store_true", help="skip the Playwright page checks")
    ap.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--api-port", type=int, default=8000, help=argparse.SUPPRESS)
    ap.add_argument("--pc-port", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--agent-port", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--staging", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--data-dir", default="/data", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.inside:
        return inside_main(args)
    if not args.project:
        ap.error("--project is required")
    return host_main(args)


if __name__ == "__main__":
    sys.exit(main())
