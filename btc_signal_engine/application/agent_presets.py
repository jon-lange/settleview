"""Saved launch presets (U11): a launch body the user saved on the PC, so a session can start from Telegram's /launch
without typing limits on the phone. The preset fixes everything: level, limits, rules, markets and each venue's
trading mode at save time.

Rules:
- A preset is {name, launch, modes, created_ts}: `launch` is the launch body without its confirm phrase, `modes` the
  trading mode of each of its venues when it was saved (venue key -> mode).
- Saving validates the body with launch's own checks as a dry run (`validate_fn(spec, now)` -> refusal or None: the
  route's checks and AgentSessions.validate; a live session does not matter). Refused: an invalid body (launch's
  code), a fixed end time (`ends_ts`: a preset gives `minutes`, code invalid), an external session (its token would
  have to travel over Telegram, code in_app_only), more than MAX_PRESETS, a name outside 1-40 characters after
  trimming. Saving an existing name replaces it (its created_ts is the new save's).
- `launchable(name, modes_now)`: the preset's body if every venue is still in its saved mode, else a refusal naming the
  venue (the mode is never changed for you, and a preset never launches on another mode).
- Multi-agent (2026-10-31): a preset is ONE market (`launch.market`; a preset saved before carries the old
  `venues: [one market]`, still launched as that market; more than one is refused at save, code one_market) and may
  carry a name template (`launch.name`, else the market's default agent): `launchable` runs the idle agent of that
  name again (agent_id), else a new agent with the name ("H4 agent", or "H4 agent 2" ... while that one runs;
  `target_fn`), so a preset can run several agents and an agent keeps one journal across its runs.
- Stored in strategy_settings (strategy "agent", key "presets") as {name: preset}; nothing is migrated.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from btc_signal_engine.application.agent_sessions import LaunchError, market_of
from btc_signal_engine.application.ports import Repository
from btc_signal_engine.application.settings_store import get_setting, put_setting
from btc_signal_engine.domain.markets import market as market_spec

SETTING = {"strategy": "agent", "key": "presets"}
MAX_PRESETS = 10
NAME_MAX = 40
CONFIRM_KEY = "confirm"


def _refused(code: str, reason: str) -> dict:
    return {"ok": False, "code": code, "reason": reason}


class AgentPresets:
    def __init__(self, repo: Repository, validate_fn: Callable[[dict, int], dict | None],
                 modes_fn: Callable[[], Mapping[str, str | None]],
                 target_fn: Callable[[str | None, str], dict] | None = None) -> None:
        """`validate_fn(spec, now)` -> launch's refusal for this body or None. `modes_fn()` -> venue key -> the
        venue's trading mode now (None: not in this app). `target_fn(template, market)` -> the agent the preset runs
        (AgentSessions.preset_target: {agent_id} of that idle agent, {name} for a new one, {} the market's default
        agent); None: the body as saved."""
        self.repo = repo
        self._validate = validate_fn
        self._modes = modes_fn
        self._target = target_fn

    # -- store ----------------------------------------------------------------------------------------------------
    def _stored(self) -> dict[str, dict]:
        v = get_setting(self.repo, SETTING, {})
        return dict(v) if isinstance(v, dict) else {}

    def _write(self, value: dict, now: int) -> None:
        put_setting(self.repo, SETTING, value, now)

    # -- reads --------------------------------------------------------------------------------------------------------
    def all(self) -> list[dict]:
        """Every preset, oldest first."""
        return sorted((dict(p) for p in self._stored().values()), key=lambda p: (p.get("created_ts") or 0,
                                                                                    p.get("name") or ""))

    def get(self, name: Any) -> dict | None:
        """The preset by name, the name normalized as save stores it (clean_name)."""
        n = self.clean_name(name)
        p = self._stored().get(n) if n is not None else None
        return dict(p) if p is not None else None

    # -- writes ---------------------------------------------------------------------------------------------------
    @staticmethod
    def clean_name(name: Any) -> str | None:
        if not isinstance(name, str):
            return None
        n = " ".join(name.split())
        return n if 1 <= len(n) <= NAME_MAX else None

    def save(self, name: Any, launch: Any, now: int) -> dict:
        n = self.clean_name(name)
        if n is None:
            return _refused("invalid_name", f"A preset's name is 1 to {NAME_MAX} characters.")
        if not isinstance(launch, dict):
            return _refused("invalid", "launch must be the launch form's fields as an object.")
        if CONFIRM_KEY in launch:
            return _refused("invalid", "A preset holds no confirm phrase; the launch asks for it each time.")
        if launch.get("ends_ts") is not None:
            return _refused("invalid", "A preset gives the session's length in minutes, not a fixed end time.")
        if launch.get("kind") != "in_app":
            return _refused("in_app_only", "Presets are for in-app agent sessions (an external session's token "
                                           "would have to travel over Telegram).")
        bad = self._validate(dict(launch), now)
        if bad is not None:
            return bad
        stored = self._stored()
        if n not in stored and len(stored) >= MAX_PRESETS:
            return _refused("too_many", f"At most {MAX_PRESETS} presets; delete one first.")
        try:
            market = market_of(launch)
        except LaunchError as e:  # validate_fn already refused these; kept so a preset is never stored without one
            return _refused(e.code, e.reason)
        modes = {market: self._modes().get(market)}
        preset = {"name": n, "launch": dict(launch), "modes": modes, "created_ts": now}
        self._write({**stored, n: preset}, now)
        return {"ok": True, "preset": preset, "replaced": n in stored}

    def delete(self, name: Any, now: int) -> dict:
        """Delete a preset; the name is normalized as save stores it (clean_name: spaces collapsed and trimmed)."""
        n, stored = self.clean_name(name), self._stored()
        if n is None or n not in stored:
            return _refused("not_found", f"There is no preset named {name!r}.")
        del stored[n]
        self._write(stored, now)
        return {"ok": True}

    # -- launch --------------------------------------------------------------------------------------------------------
    def launchable(self, name: str) -> dict:
        """{ok, launch} for a preset whose venues are all still in their saved modes, else the refusal."""
        p = self.get(name)
        if p is None:
            return _refused("not_found", f"There is no preset named {name!r}.")
        now_modes = self._modes()
        for venue, mode in (p.get("modes") or {}).items():
            if now_modes.get(venue) != mode:
                return _refused("mode_changed", f"{market_spec(venue).label} is in {now_modes.get(venue)} now; "
                                                f"this preset was saved in {mode}. Launch from the PC.")
        launch = dict(p["launch"])
        if self._target is not None:
            try:
                market = market_of(launch)
            except LaunchError as e:
                return _refused(e.code, e.reason)
            target = self._target(launch.pop("name", None), market)
            launch.update(target)
        return {"ok": True, "launch": launch}
