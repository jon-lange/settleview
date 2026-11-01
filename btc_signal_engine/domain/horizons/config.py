"""Prediction horizons are configuration, not code (R6)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class Horizon:
    seconds: int
    name: str

    @property
    def ms(self) -> int:
        return self.seconds * 1000


def parse_horizons(raw: dict[str, int]) -> tuple[Horizon, ...]:
    """Validate a name->seconds mapping. Horizons must be positive whole minutes."""
    if not raw:
        raise ValueError("at least one prediction horizon is required")
    horizons = []
    for name, seconds in raw.items():
        if not isinstance(seconds, int) or seconds <= 0 or seconds % 60:
            raise ValueError(f"horizon {name!r}={seconds!r} must be a positive multiple of 60 seconds")
        horizons.append(Horizon(seconds=seconds, name=name))
    if len({h.seconds for h in horizons}) != len(horizons):
        raise ValueError("horizon durations must be unique")
    return tuple(sorted(horizons))
