"""Ruleset versions and their per-horizon roles (U6, KTD10, KTD17).

Roles per horizon: champion (headline call), challenger (shadow candidate), active
(also decides, shown for comparison), previous (last champion, for rollback), retired.
"""

from __future__ import annotations

from btc_signal_engine.application.ports import Repository
from btc_signal_engine.domain.strategies.ruleset import build_spec, spec_hash

DECIDING_ROLES = ("champion", "challenger", "active")


class RulesetService:
    def __init__(self, repo: Repository, max_active_per_horizon: int = 5) -> None:
        self._repo = repo
        self._max_active = max_active_per_horizon

    def register(self, spec: dict, origin: str, now: int, parent_hash: str | None = None) -> dict:
        """Store a spec as a version (idempotent by hash). Returns the stored row."""
        h = spec_hash(spec)
        existing = self._repo.select("rulesets", {"hash": h})
        if existing:
            return existing[0]
        versions = [r["version"] for r in self._repo.select("rulesets", {"name": spec["name"]})]
        row = {"name": spec["name"], "version": max(versions, default=0) + 1, "hash": h, "spec": spec,
               "origin": origin, "parent_hash": parent_hash, "created_ts": now}
        self._repo.insert("rulesets", [row])
        return self._repo.select("rulesets", {"hash": h})[0]

    def load_seeds(self, seeds: list[dict], now: int) -> list[dict]:
        rows = []
        for seed in seeds:
            spec = build_spec(seed["name"], seed["signals"], seed["horizons"], seed.get("combine", "first_match"),
                              seed.get("params"))
            row = self.register(spec, "seed", now)
            rows.append(row)
            same_name = {r["hash"] for r in self._repo.select("rulesets", {"name": spec["name"]})} - {row["hash"]}
            for h in spec["horizons"]:
                if self._repo.select("ruleset_roles", {"horizon": h, "ruleset_hash": row["hash"]}):
                    continue
                roles = self.roles(h)
                # A new code version of a seed replaces its older non-champion versions; a champion is
                # never displaced by seeding (it must lose through the promotion gates).
                if any(roles.get(old) == "champion" for old in same_name):
                    continue
                for old in same_name:
                    if roles.get(old) in ("active", "challenger"):
                        self.set_role(h, old, "retired", now, f"superseded by seed version {row['version']}")
                has_champion = any(r == "champion" for r in self.roles(h).values())
                role = "champion" if seed.get("champion") and not has_champion else "active"
                try:
                    self.set_role(h, row["hash"], role, now, "seed")
                except ValueError:  # deciding cap reached: keep the version registered but inactive
                    pass
        return rows

    def get(self, ruleset_hash: str) -> dict:
        rows = self._repo.select("rulesets", {"hash": ruleset_hash})
        if not rows:
            raise KeyError(ruleset_hash)
        return rows[0]

    def roles(self, horizon: int) -> dict[str, str]:
        return {r["ruleset_hash"]: r["role"] for r in self._repo.select("ruleset_roles", {"horizon": horizon})}

    def deciding(self, horizon: int) -> list[dict]:
        """Rulesets that make live decisions on this horizon, champion first."""
        order = {r: i for i, r in enumerate(DECIDING_ROLES)}
        rows = [r for r in self._repo.select("ruleset_roles", {"horizon": horizon}) if r["role"] in DECIDING_ROLES]
        rows.sort(key=lambda r: (order[r["role"]], r["since_ts"]))
        return [{**self.get(r["ruleset_hash"]), "role": r["role"]} for r in rows]

    def champion(self, horizon: int) -> str | None:
        rows = self._repo.select("ruleset_roles", {"horizon": horizon, "role": "champion"})
        return rows[0]["ruleset_hash"] if rows else None

    def set_role(self, horizon: int, ruleset_hash: str, role: str, now: int, note: str = "") -> None:
        if role in ("active", "challenger") and self._roles_count(horizon, DECIDING_ROLES) >= self._max_active \
                and self.roles(horizon).get(ruleset_hash) not in DECIDING_ROLES:
            raise ValueError(f"horizon {horizon} already has {self._max_active} deciding rulesets")
        if role == "champion":
            current = self.champion(horizon)
            if current and current != ruleset_hash:
                self._put(horizon, current, "previous", now, f"replaced by {ruleset_hash}")
            for r in self._repo.select("ruleset_roles", {"horizon": horizon, "role": "previous"}):
                if r["ruleset_hash"] not in (current, ruleset_hash):
                    self._put(horizon, r["ruleset_hash"], "active", now, "older champion")
        self._put(horizon, ruleset_hash, role, now, note)

    def _put(self, horizon: int, ruleset_hash: str, role: str, now: int, note: str) -> None:
        self._repo.insert("ruleset_roles", [{"horizon": horizon, "ruleset_hash": ruleset_hash, "role": role,
                                             "since_ts": now, "note": note}], ignore_conflicts=False)

    def _roles_count(self, horizon: int, roles: tuple[str, ...]) -> int:
        return sum(1 for r in self._repo.select("ruleset_roles", {"horizon": horizon}) if r["role"] in roles)
