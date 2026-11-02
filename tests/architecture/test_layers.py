"""KTD20: the hexagonal layer rules are enforced, not just documented."""

from pathlib import Path

from importlinter.cli import lint_imports

ROOT = Path(__file__).resolve().parents[2]


def test_layer_contracts_hold(monkeypatch):
    monkeypatch.chdir(ROOT)
    # no_cache: as the pre-commit hook (`lint-imports --no-cache`), nothing is written into the checkout.
    assert lint_imports(config_filename=str(ROOT / ".importlinter"), no_cache=True) == 0
