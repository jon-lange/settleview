"""deploy/swarm/render_stack.py: Swarm drops a publish's 127.0.0.1, so the stack file must never publish the PC
listener (8001), the agent listener (8002) or OpenObserve (5080) (B4#1). The pure parts; CI also renders the real file
and runs `docker stack config` on it."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("render_stack", ROOT / "deploy" / "swarm" / "render_stack.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def compose_like() -> dict:
    """What `docker compose config --format json` gives for compose.yaml alone (the shapes that matter)."""
    return {"name": "settleview", "services": {
        "app": {"ports": [{"mode": "ingress", "host_ip": "0.0.0.0", "target": 8000, "published": "8000"},
                          {"mode": "ingress", "host_ip": "127.0.0.1", "target": 8001, "published": "8001"},
                          {"mode": "ingress", "host_ip": "127.0.0.1", "target": 8002, "published": "8002"}],
                "depends_on": {"postgres": {"condition": "service_healthy"}}, "build": {"context": "."},
                "deploy": {"resources": {"limits": {"cpus": 2.0, "memory": "2147483648"}}}},
        "openobserve": {"profiles": ["observability"], "ports": ["127.0.0.1:5080:5080"]},
        "postgres": {}}}


def test_compose_yaml_as_it_is_would_publish_every_pc_only_port_on_swarm():
    assert rs.violations(rs.to_stack(compose_like(), ["observability"])) == [
        "app: 8001 (the PC listener)", "app: 8002 (the agent listener)", "openobserve: 5080 (OpenObserve)"]
    assert rs.violations({"services": {"x": {"ports": ["8001:8001"]}}}) == ["x: 8001 (the PC listener)"]
    assert rs.violations({"services": {"x": {"ports": ["9000:8002/tcp"]}}}) == ["x: 8002 (the agent listener)"]


def test_the_stack_drops_what_swarm_rejects_and_keeps_only_the_chosen_profiles():
    doc = compose_like()
    doc["services"]["app"]["ports"] = doc["services"]["app"]["ports"][:1]
    st = rs.to_stack(doc)
    assert "name" not in st and set(st["services"]) == {"app", "postgres"}  # openobserve: profile not chosen
    app = st["services"]["app"]
    assert "depends_on" not in app and "build" not in app
    assert app["deploy"]["resources"]["limits"]["cpus"] == "2.0"
    assert app["ports"] == [{"mode": "ingress", "target": 8000, "published": 8000}]  # no host_ip: Swarm ignores it
    assert rs.violations(st) == []
    assert "openobserve" in rs.to_stack(compose_like(), ["observability"])["services"]


def test_the_swarm_override_publishes_only_the_phone_port():
    text = (ROOT / "deploy" / "swarm" / "compose.swarm.yaml").read_text(encoding="utf-8")
    assert "ports: !override" in text and '- "8000:8000"' in text and "ports: !reset []" in text
    assert "8001:" not in text and "8002:" not in text and "5080:" not in text
