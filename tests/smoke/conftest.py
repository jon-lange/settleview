"""The deploy gate's smoke suite (pytest + Playwright, the same tools as tests/e2e) against a RUNNING stack, read-only.

scripts/smoke.py (`python scripts/smoke.py --project <compose project>`) starts it in the dev image, in a
throwaway container that shares the app container's network namespace, so `localhost:<port>` is the app's own
listeners and the Host check passes as on this PC. It mounts, read-only: the app's data volume at /data (the PC
secret, to sign a cookie: adapters/pc_credentials.cookie_from_secret_file, nothing written) and /smoke, the stack as
the host's Docker saw it (`docker inspect` of the project's containers, `docker logs --since 30m` of the app and the
recorders). Without SETTLEVIEW_SMOKE_DIR every test here is skipped: the ordinary suite never runs it.

Read-only by construction: the HTTP client sends GET/HEAD only (any other method raises before it is sent), and the
browser aborts every request that is not a GET/HEAD to the app's own origin (the page's presence heartbeat included).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

SAFE = ("GET", "HEAD")
SMOKE_DIR = os.environ.get("SETTLEVIEW_SMOKE_DIR")


def pytest_collection_modifyitems(config, items):
    if SMOKE_DIR:
        return
    skip = pytest.mark.skip(reason="the smoke suite runs against a live stack: python scripts/smoke.py --project P")
    for item in items:
        if "tests/smoke/" in item.nodeid.replace("\\", "/"):
            item.add_marker(skip)


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

    def stream(self, path: str, **kw):
        return self._c.stream("GET", path, **kw)

    def request(self, method: str, path: str, **kw):
        return self._c.request(method, path, **kw)

    def close(self) -> None:
        self._c.close()


@pytest.fixture(scope="session")
def stack() -> dict:
    """{"project", "services": {service: docker inspect}, "logs": {service: text}} as the host saw it."""
    d = Path(SMOKE_DIR or "")
    services = json.loads((d / "containers.json").read_text(encoding="utf-8"))
    logs = {p.stem: p.read_text(encoding="utf-8", errors="replace") for p in (d / "logs").glob("*.log")}
    return {"project": json.loads((d / "project.json").read_text(encoding="utf-8"))["project"],
            "services": services, "logs": logs}


def env_of(container: dict) -> dict[str, str]:
    """A container's settings. compose.yaml keeps every secret in a file, so its environment holds none."""
    return dict(e.split("=", 1) for e in container["Config"].get("Env") or [] if "=" in e)


@pytest.fixture(scope="session")
def app_env(stack) -> dict[str, str]:
    return env_of(stack["services"]["app"])


@pytest.fixture(scope="session")
def staging(app_env) -> bool:
    return app_env.get("BTCFLOW_STAGING", "").lower() in ("1", "true", "yes", "on")


@pytest.fixture(scope="session")
def ports(app_env) -> dict[str, int | None]:
    get = app_env.get
    return {"api": int(get("BTCFLOW_API_PORT", "8000")),
            "pc": int(get("BTCFLOW_PC_PORT")) if get("BTCFLOW_PC_PORT") else None,
            "agent": int(get("BTCFLOW_AGENT_PORT")) if get("BTCFLOW_AGENT_PORT") else None}


@pytest.fixture(scope="session")
def pc_cookie() -> str:
    from btc_signal_engine.adapters.pc_credentials import cookie_from_secret_file
    return cookie_from_secret_file(Path(os.environ.get("SETTLEVIEW_SMOKE_DATA", "/data")))


@pytest.fixture(scope="session")
def pc(ports, pc_cookie):
    from btc_signal_engine.adapters.pc_credentials import PC_COOKIE
    if ports["pc"] is None:
        pytest.fail("the app has no BTCFLOW_PC_PORT: the PC checks need it")
    c = ReadOnlyClient(f"http://localhost:{ports['pc']}", cookies={PC_COOKIE: pc_cookie})
    yield c
    c.close()
