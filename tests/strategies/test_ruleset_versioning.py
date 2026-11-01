import pytest

from btc_signal_engine.application.rulesets import RulesetService
from btc_signal_engine.domain.strategies import ruleset as rs
from btc_signal_engine.domain.strategies.ruleset import build_spec, clone_spec, spec_hash


def test_param_change_changes_hash_and_key_order_does_not():
    a = build_spec("m", ["momentum"], [60, 300])
    b = clone_spec(a, {"momentum": {"300": {"min_buy_share": 0.62}}})
    assert spec_hash(a) != spec_hash(b)
    reordered = dict(reversed(list(a.items())))
    assert spec_hash(reordered) == spec_hash(a)


def test_rule_version_bump_changes_hash(monkeypatch):
    a = build_spec("m", ["momentum"], [60])
    before = spec_hash(a)
    monkeypatch.setattr(rs.SIGNAL_RULES["momentum"].__class__, "RULE_VERSION", "999")
    assert spec_hash(a) != before


def test_clone_rejects_unknown_param_and_out_of_range():
    a = build_spec("m", ["momentum"], [60])
    with pytest.raises(ValueError, match="unknown parameter"):
        clone_spec(a, {"momentum": {"60": {"nope": 1}}})
    with pytest.raises(ValueError, match="between"):
        clone_spec(a, {"momentum": {"60": {"min_buy_share": 1.5}}})


def test_seeds_load_with_one_champion(store):
    svc = RulesetService(store)
    seeds = [{"name": "momentum", "signals": ["momentum"], "horizons": [60, 300], "champion": True},
             {"name": "breakout", "signals": ["breakout"], "horizons": [60, 300]}]
    rows = svc.load_seeds(seeds, now=1)
    svc.load_seeds(seeds, now=2)  # idempotent
    assert len(store.select("rulesets")) == 2
    assert svc.champion(60) == rows[0]["hash"]
    assert [r["role"] for r in svc.deciding(60)] == ["champion", "active"]


def test_new_seed_versions_replace_old_non_champions_and_never_the_champion(store):
    svc = RulesetService(store)
    seeds = [{"name": "momentum", "signals": ["momentum"], "horizons": [60], "champion": True},
             {"name": "breakout", "signals": ["breakout"], "horizons": [60]}]
    old = svc.load_seeds(seeds, now=1)
    # simulate a code change: same seed names, different specs (new hashes)
    seeds2 = [{**seeds[0], "params": {"momentum": {"60": {"min_buy_share": 0.61}}}},
              {**seeds[1], "params": {"breakout": {"60": {"volume_mult": 1.6}}}}]
    new = svc.load_seeds(seeds2, now=2)
    roles = svc.roles(60)
    assert roles[old[0]["hash"]] == "champion" and new[0]["hash"] not in roles  # champion kept, no duplicate
    assert roles[old[1]["hash"]] == "retired" and roles[new[1]["hash"]] == "active"


def test_promoting_keeps_previous_champion_for_rollback(store):
    svc = RulesetService(store)
    a = svc.register(build_spec("m", ["momentum"], [60]), "seed", 1)
    b = svc.register(build_spec("m", ["momentum"], [60], params={"momentum": {"60": {"min_buy_share": 0.7}}}), "clone", 2)
    svc.set_role(60, a["hash"], "champion", 1)
    svc.set_role(60, b["hash"], "champion", 5)
    assert svc.champion(60) == b["hash"] and svc.roles(60)[a["hash"]] == "previous"


def test_new_params_change_the_hash():
    m = build_spec("m", ["momentum"], [60])
    assert spec_hash(clone_spec(m, {"momentum": {"60": {"min_conditions": 3}}})) != spec_hash(m)
    with pytest.raises(ValueError, match="one of"):
        clone_spec(m, {"momentum": {"60": {"min_conditions": 1}}})
    e = build_spec("e", ["evidence"], [60])
    for override in ({"threshold": 2.0}, {"book_weight": 0.5}, {"kc_weight": 2.0}):
        assert spec_hash(clone_spec(e, {"evidence": {"60": override}})) != spec_hash(e)


def test_evidence_seed_is_valid_and_not_champion():
    import tomllib
    from pathlib import Path

    seed = tomllib.loads((Path(__file__).resolve().parents[2] / "rulesets" / "evidence_v1.toml").read_text("utf-8"))
    assert seed["signals"] == ["evidence"] and seed["horizons"] == [60, 300, 900]
    assert not seed.get("champion", False)
    build_spec(seed["name"], seed["signals"], seed["horizons"], seed.get("combine", "first_match"), seed.get("params"))


def test_existing_seed_champion_flags_unchanged():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "rulesets"
    flags = {p.stem: tomllib.loads(p.read_text("utf-8")).get("champion", False) for p in root.glob("*.toml")}
    assert [k for k, v in flags.items() if v] == ["momentum_v1"]
