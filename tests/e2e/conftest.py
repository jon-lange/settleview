"""Browser fixtures for the end-to-end tests (marker `e2e`; `-m "not e2e"` leaves them out).

The browser: SETTLEVIEW_E2E_BROWSER_CHANNEL picks one of `msedge`, `chrome` (the installed Microsoft Edge / Google
Chrome, launched by Playwright through its channel) or `chromium` (Playwright's own build: `playwright install
chromium`, as on Linux CI). Unset: msedge, then chrome, then chromium, and the tests are skipped with the reasons when
none starts. A channel named explicitly that does not start fails the tests (never a silent fallback); any other value
is an error. The dev container (scripts/dev.py) sets `chromium`.

Playwright's sync API is started per module and stopped after it: while it runs, its event loop is "running" on this
thread and asyncio.run() (used by many other tests) would fail. That is also why pytest-playwright (session-scoped
browser fixtures) is not used.

Each test gets a fresh app (tests/e2e/server.py) and a fresh browser context in one of four looks: desktop 1280x900 or
phone 400x860, light or dark (prefers-color-scheme emulation). Any console error, uncaught page error or unexpected
dialog fails the test; a failed resource load is allowed only for URLs the test names (`guard.allow`). Screenshots go to
tests/e2e/.artifacts/ (git-ignored)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (pip install -e .[dev])")

from btc_signal_engine.adapters.pc_credentials import PC_COOKIE  # noqa: E402
from tests.e2e.server import LiveServer  # noqa: E402

ARTIFACTS = Path(__file__).resolve().parent / ".artifacts"
CHANNEL_ENV = "SETTLEVIEW_E2E_BROWSER_CHANNEL"
CHANNELS = {"msedge": "msedge", "chrome": "chrome", "chromium": None}
LOOKS = {
    "desktop-light": {"viewport": {"width": 1280, "height": 900}, "color_scheme": "light", "phone": False},
    "desktop-dark": {"viewport": {"width": 1280, "height": 900}, "color_scheme": "dark", "phone": False},
    "phone-light": {"viewport": {"width": 400, "height": 860}, "color_scheme": "light", "phone": True},
    "phone-dark": {"viewport": {"width": 400, "height": 860}, "color_scheme": "dark", "phone": True},
}


def _first_line(e: BaseException) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:200]


@pytest.fixture(scope="module")
def browser():
    want = os.environ.get(CHANNEL_ENV, "").strip()
    if want and want not in CHANNELS:
        pytest.fail(f"{CHANNEL_ENV}={want!r} is not one of {', '.join(CHANNELS)}", pytrace=False)
    names = [want] if want else list(CHANNELS)
    pw = sync_api.sync_playwright().start()
    tried, chosen = [], None
    for name in names:
        try:
            ch = CHANNELS[name]
            chosen = pw.chromium.launch(channel=ch) if ch else pw.chromium.launch()
            break
        except sync_api.Error as e:
            tried.append(f"{name}: {_first_line(e)}")
    if chosen is None:
        pw.stop()
        if want:
            pytest.fail(f"{CHANNEL_ENV}={want} did not start: {tried[0]}", pytrace=False)
        pytest.skip("no browser for the e2e tests (" + "; ".join(tried) + ")")
    try:
        yield chosen
    finally:
        chosen.close()
        pw.stop()


@pytest.fixture(params=list(LOOKS))
def look(request) -> dict:
    return {"name": request.param, **LOOKS[request.param]}


class Guard:
    """Collects console errors, page errors and dialogs of one page; `check()` fails the test on any."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.allowed: list[str] = []
        self.accept_dialogs = False
        self.dialogs: list[str] = []

    def allow(self, url_part: str) -> None:
        """A failed load of a URL containing `url_part` is expected (e.g. a phone's 403 on a PC-only route)."""
        self.allowed.append(url_part)

    def watch(self, page) -> None:
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.problems.append(f"page error: {e}"))
        page.on("dialog", self._dialog)

    def _console(self, msg) -> None:
        # Alpine reports a directive it cannot evaluate (the CSP build: anything but a property path, or a path to
        # nothing) and an expression that threw as console warnings: both are failures.
        if msg.type == "warning" and msg.text.startswith("Alpine"):
            self.problems.append(f"console warning: {msg.text.strip()}")
            return
        if msg.type != "error":
            return
        url = (msg.location or {}).get("url") or ""
        if msg.text.startswith("Failed to load resource") and any(a in url for a in self.allowed):
            return
        self.problems.append(f"console error: {msg.text} ({url})")

    def _dialog(self, dialog) -> None:
        self.dialogs.append(dialog.message)
        if self.accept_dialogs:
            dialog.accept()
        else:
            self.problems.append(f"unexpected dialog: {dialog.message}")
            dialog.dismiss()

    def check(self) -> None:
        assert not self.problems, "\n".join(self.problems)


def make_server(store, script) -> LiveServer:
    return LiveServer(store, list(script)).start()


@pytest.fixture
def app_server(request, store):
    """A started app with the script from the test's `script` marker (`@pytest.mark.script(*turns)`), else empty."""
    marker = request.node.get_closest_marker("script")
    srv = make_server(store, marker.args if marker else ())
    yield srv
    srv.stop()


def pair_context(ctx, server: LiveServer) -> None:
    """The context's browser is the paired PC: the PC cookie, as `ops pair-pc` + /pair would set it
    (test_pairing.py runs that flow for real)."""
    ctx.add_cookies([{"name": PC_COOKIE, "value": server.world.pc.issue(), "url": server.url, "httpOnly": True,
                      "sameSite": "Strict"}])


@pytest.fixture
def pc_request(browser, app_server):
    """Requests from this PC (the paired browser on loopback), beside a test's own page (e.g. to set the phone PIN)."""
    ctx = browser.new_context(base_url=app_server.url)
    pair_context(ctx, app_server)
    yield ctx.request
    ctx.close()


@pytest.fixture
def ui(browser, look, app_server, request):
    """(page, guard, server) on a fresh context in this test's look, at the app's base URL, as the paired PC. With the
    test's `peer` marker (`@pytest.mark.peer("192.168.1.50")`) every request of the page arrives from that address, as
    a phone's does (tests/e2e/server.py PeerShim), and the context holds no PC cookie. With the `unpaired` marker it
    is this PC's browser before pairing."""
    peer = request.node.get_closest_marker("peer")
    ctx = browser.new_context(viewport=look["viewport"], color_scheme=look["color_scheme"],
                              is_mobile=look["phone"], has_touch=look["phone"], base_url=app_server.url,
                              extra_http_headers={"X-E2E-Peer": peer.args[0]} if peer else None)
    ctx.set_default_timeout(8_000)
    if not peer and not request.node.get_closest_marker("unpaired"):
        pair_context(ctx, app_server)
    page = ctx.new_page()
    guard = Guard()
    guard.watch(page)
    yield Ui(page, guard, app_server, look, request.node.name)
    try:
        guard.check()
    finally:
        ctx.close()


class Ui:
    def __init__(self, page, guard: Guard, server: LiveServer, look: dict, test_name: str) -> None:
        self.page, self.guard, self.server, self.look = page, guard, server, look
        self._name = re.sub(r"[^A-Za-z0-9_.-]+", "_", test_name.split("[")[0].removeprefix("test_"))

    def shot(self, label: str) -> Path:
        ARTIFACTS.mkdir(exist_ok=True)
        path = ARTIFACTS / f"{self._name}-{label}-{self.look['name']}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def no_horizontal_scroll(self) -> None:
        if not self.look["phone"]:
            return
        w = self.page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        assert w[0] <= w[1], f"the page scrolls sideways at {self.look['viewport']['width']}px: {w[0]} > {w[1]}"
