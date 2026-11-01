"""The web app runs Alpine's CSP build (web/vendor/alpine-csp-*.min.js), so the Content-Security-Policy needs no
'unsafe-eval' (B6#6). That build evaluates no JavaScript from the page: every directive's value must be a property
path (`open`, `pickerLabel`, `$store.app.view`, `it.name`) that names a value, a getter or a method on the component
(registered with Alpine.data in the view's script). A path that names a method is called with the component as
`this` (an event handler's with the event as its argument). x-model needs a {get, set} pair (BTC.model, web/app.js):
the build cannot assign to a path, so an x-model path names such a pair and ends in `Model` (`qtyModel`). This test
reads web/index.html and fails on any directive that is not a path, with its line, so an inline expression can never
slip back in."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"
PATH = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
FOR = re.compile(r"(?:[A-Za-z_$][\w$]*|\(\s*[A-Za-z_$][\w$]*\s*(?:,\s*[A-Za-z_$][\w$]*\s*){0,2}\))\s+(?:in|of)\s+(.+)")
# Directives whose value is not an expression (or that take none).
NOT_EXPRESSIONS = {"x-ref", "x-cloak", "x-ignore", "x-teleport", "x-id"}


def _directive(name: str) -> str | None:
    """The directive's name for an Alpine attribute (x-on:click / @click -> 'x-on'), None for any other attribute."""
    if name.startswith("@"):
        return "x-on"
    if name.startswith(":"):
        return "x-bind"
    if name.startswith("x-"):
        return name.split(":", 1)[0].split(".", 1)[0]
    return None


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag, attrs):
        line = self.getpos()[0]
        for name, value in attrs:
            directive = _directive(name)
            if directive is None or directive.startswith("x-transition") or directive in NOT_EXPRESSIONS:
                continue
            self.found.append((line, name, value or ""))


def directives(html: str) -> list[tuple[int, str, str]]:
    parser = _Collect()
    parser.feed(html)
    return parser.found


def violations(html: str) -> list[str]:
    bad = []
    for line, name, value in directives(html):
        v = value.strip()
        if name == "x-for":
            m = FOR.fullmatch(v)
            ok = bool(m) and bool(PATH.fullmatch(m.group(1).strip()))
        elif name.split(".", 1)[0] == "x-model":
            ok = bool(PATH.fullmatch(v)) and v.endswith("Model")
        else:
            ok = bool(PATH.fullmatch(v))
        if not ok:
            bad.append(f"index.html:{line}: {name}=\"{value}\"")
    return bad


def test_every_directive_is_a_property_path():
    bad = violations((WEB / "index.html").read_text(encoding="utf-8"))
    assert not bad, f"{len(bad)} directive(s) the CSP build cannot evaluate:\n" + "\n".join(bad)


def test_the_page_loads_the_csp_build_only():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)
    alpine = [s for s in scripts if "alpine" in s]
    assert alpine == ["vendor/alpine-csp-3.14.9.min.js"], alpine
    assert (WEB / alpine[0]).is_file()


def test_the_checker_catches_expressions():
    page = ('<div x-data="playView"><b x-text="a && b"></b><i @click="go(1)"></i><p x-show="!x"></p>'
            '<template x-for="(it, i) in items"></template><template x-for="it in list()"></template>'
            '<input x-model="form.qty"><input x-model.number="qtyModel"><a :class="cls" x-ref="r" x-cloak></a></div>')
    assert [b.split(": ", 1)[1] for b in violations(page)] == [
        'x-text="a && b"', '@click="go(1)"', 'x-show="!x"', 'x-for="it in list()"', 'x-model="form.qty"']
