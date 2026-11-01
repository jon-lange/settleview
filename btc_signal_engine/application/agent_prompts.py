"""Agent prompt versions (Agent mode U7; R15, KTD13).

Two layers, each a list of numbered versions in the agent_prompts table (layer, version, text, created_ts):
- "system": the default is the user's text in docs/agent/default-system-prompt.md, handed in by the app's wiring at
  start-up; version 1 is that text, stored once when the table has no system row yet.
- "user": further customization, sent after the system prompt; the default is empty. Version 0 is that empty default
  and has no row.
Saving, restoring an old version and resetting to the default each add a NEW version (history is never rewritten);
saving text identical to the current version adds nothing. A session pins the versions it launched with, and its
wakes read exactly those (`pinned`), so an edit applies to the next session only.

An unknown layer or a version that does not exist is an error (ValueError), never read as some other one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from btc_signal_engine.application.ports import Repository

TABLE = "agent_prompts"
LAYERS = ("system", "user")
MAX_CHARS = 60_000
PREVIEW_CHARS = 160


class PromptError(ValueError):
    """A prompt request refused for a stated reason (`code`)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


def _layer(layer: Any) -> str:
    if layer not in LAYERS:
        raise PromptError("unknown_layer", f"layer must be one of {', '.join(LAYERS)} (got {layer!r}).")
    return layer


class AgentPrompts:
    def __init__(self, repo: Repository, default_system: str) -> None:
        if not isinstance(default_system, str) or not default_system.strip():
            raise ValueError("the default system prompt is empty")
        self.repo = repo
        self.defaults = {"system": default_system, "user": ""}

    def seed(self, now: int) -> None:
        """Store the default system prompt as version 1 when no system version exists yet (first start)."""
        if not self.repo.select(TABLE, {"layer": "system"}, limit=1):
            self.repo.insert(TABLE, [{"layer": "system", "version": 1, "text": self.defaults["system"],
                                      "created_ts": now}], ignore_conflicts=False)

    # -- reads ------------------------------------------------------------------------------------------------------
    def _latest(self, layer: str) -> dict | None:
        rows = self.repo.select(TABLE, {"layer": layer}, order_by=["-version"], limit=1)
        return rows[0] if rows else None

    def current(self, layer: str) -> dict:
        """{layer, version, text, created_ts, is_default}; the user layer with no row is version 0, empty."""
        layer = _layer(layer)
        row = self._latest(layer)
        if row is None:
            if layer == "system":
                raise PromptError("not_seeded", "No system prompt is stored yet (the app seeds it at start-up).")
            return {"layer": layer, "version": 0, "text": "", "created_ts": None, "is_default": True}
        return {"layer": layer, "version": row["version"], "text": row["text"], "created_ts": row["created_ts"],
                "is_default": row["text"] == self.defaults[layer]}

    def versions(self) -> dict:
        return {layer: self.current(layer)["version"] for layer in LAYERS}

    def text(self, layer: str, version: Any) -> str:
        layer = _layer(layer)
        if isinstance(version, bool) or not isinstance(version, int):
            raise PromptError("unknown_version", f"version must be a whole number (got {version!r}).")
        if layer == "user" and version == 0:
            return ""
        rows = self.repo.select(TABLE, {"layer": layer, "version": version}, limit=1)
        if not rows:
            raise PromptError("unknown_version", f"There is no {layer} prompt version {version}.")
        return rows[0]["text"]

    def history(self, layer: str, limit: int = 50) -> list[dict]:
        """Newest first: {version, created_ts, chars, preview, is_default}; the user layer ends with version 0."""
        layer = _layer(layer)
        out = [{"version": r["version"], "created_ts": r["created_ts"], "chars": len(r["text"]),
                "preview": r["text"][:PREVIEW_CHARS], "is_default": r["text"] == self.defaults[layer]}
               for r in self.repo.select(TABLE, {"layer": layer}, order_by=["-version"], limit=limit)]
        if layer == "user" and len(out) < limit:
            out.append({"version": 0, "created_ts": None, "chars": 0, "preview": "", "is_default": True})
        return out

    def pinned(self, versions: dict | None) -> tuple[str, str, dict]:
        """(system text, user text, versions) for a session's pinned versions; None = the current ones."""
        if versions is None:
            versions = self.versions()
        return self.text("system", versions.get("system")), self.text("user", versions.get("user")), dict(versions)

    def prompts_fn(self, session_fn: Callable[[], dict | None]) -> Callable[[], tuple[str, str, dict]]:
        """The runner's `prompts_fn`: the live session's pinned versions (the current ones when it pinned none)."""
        def fn() -> tuple[str, str, dict]:
            s = session_fn()
            return self.pinned((s or {}).get("prompt_versions"))
        return fn

    # -- writes ------------------------------------------------------------------------------------------------------
    def save(self, layer: str, text: Any, now: int) -> dict:
        """Add `text` as the layer's next version. Returns {ok, changed, prompt}."""
        layer = _layer(layer)
        if not isinstance(text, str):
            raise PromptError("invalid", "text must be a string.")
        if len(text) > MAX_CHARS:
            raise PromptError("too_long", f"A prompt is at most {MAX_CHARS:,} characters (got {len(text):,}).")
        if layer == "system" and not text.strip():
            raise PromptError("empty", "The system prompt cannot be empty; reset it to the default instead.")
        cur = self.current(layer)
        if text == cur["text"]:
            return {"ok": True, "changed": False, "prompt": cur}
        self.repo.insert(TABLE, [{"layer": layer, "version": cur["version"] + 1, "text": text, "created_ts": now}],
                         ignore_conflicts=False)
        return {"ok": True, "changed": True, "prompt": self.current(layer)}

    def restore(self, layer: str, version: Any, now: int) -> dict:
        """Make an old version current again, as a new version (a copy)."""
        return self.save(layer, self.text(layer, version), now)

    def reset(self, layer: str, now: int) -> dict:
        """Back to the default (the repo's system prompt file; an empty user prompt), as a new version."""
        layer = _layer(layer)
        return self.save(layer, self.defaults[layer], now)
