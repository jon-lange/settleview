"""Read seed ruleset TOML files from disk (stdlib tomllib)."""

from __future__ import annotations

import tomllib
from pathlib import Path


def read_seed_files(directory: Path) -> list[dict]:
    seeds = []
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as fh:
            seeds.append(tomllib.load(fh))
    return seeds
