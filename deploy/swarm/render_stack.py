"""Render the Docker Swarm stack file from compose.yaml + deploy/swarm/compose.swarm.yaml, and refuse one that would
publish a port meant for this PC only (docs/operations.md, "Docker Swarm").

    python deploy/swarm/render_stack.py [--profile observability] [--out settleview-stack.json]
    docker stack deploy -c settleview-stack.json settleview

`docker stack deploy` cannot read compose.yaml itself (it rejects `depends_on` conditions and numeric CPU limits) and,
worse, drops the host IP of a publish: "127.0.0.1:8001:8001" would publish the PC listener on every node. This script
asks `docker compose config` (no interpolation: `docker stack deploy` fills ${...} from the deploying shell) for the
merged file, turns it into what Swarm accepts, and exits 1 without writing anything when any service would publish
8001 (the PC listener), 8002 (the agent listener) or 5080 (OpenObserve). JSON is YAML, so the output is a stack file.
CI runs it and then `docker stack config` on the result. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OVERRIDE = Path("deploy") / "swarm" / "compose.swarm.yaml"
# Ports compose publishes on the host's 127.0.0.1 only: on Swarm they would be published on every node.
PC_ONLY_PORTS = {8001: "the PC listener", 8002: "the agent listener", 5080: "OpenObserve"}


def merged(profiles: list[str]) -> dict:
    cmd = ["docker", "compose", "-f", "compose.yaml", "-f", str(OVERRIDE)]
    for p in profiles:
        cmd += ["--profile", p]
    cmd += ["config", "--no-interpolate", "--format", "json"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)  # noqa: S603 - docker compose with fixed arguments
    if out.returncode != 0:
        raise SystemExit(f"docker compose config failed:\n{out.stderr.strip()}")
    return json.loads(out.stdout)


def _port_numbers(p) -> set[int]:
    if isinstance(p, dict):
        vals = (p.get("published"), p.get("target"))
    else:  # "host_ip:published:target[/proto]" or "published:target"
        vals = str(p).split("/")[0].split(":")[-2:]
    got = set()
    for v in vals:
        try:
            got.add(int(str(v).split("-")[0]))
        except (TypeError, ValueError):
            pass
    return got


def violations(doc: dict) -> list[str]:
    """Every publish of a PC-only port, as "service: port (what)"."""
    bad = []
    for name, svc in (doc.get("services") or {}).items():
        for p in svc.get("ports") or ():
            for n in sorted(_port_numbers(p) & set(PC_ONLY_PORTS)):
                bad.append(f"{name}: {n} ({PC_ONLY_PORTS[n]})")
    return bad


def to_stack(doc: dict, profiles: list[str] = ()) -> dict:
    """What `docker stack deploy` accepts: only the services of the chosen profiles (Swarm ignores `profiles`), no
    depends_on / build / profiles, CPU limits as strings, the ports in the long form without a host IP (Swarm ignores
    it), no project name."""
    out = {k: v for k, v in doc.items() if k != "name"}
    out["services"] = {n: s for n, s in (doc.get("services") or {}).items()
                       if not s.get("profiles") or set(s["profiles"]) & set(profiles)}
    for svc in out["services"].values():
        for key in ("depends_on", "build", "profiles", "pull_policy"):
            svc.pop(key, None)
        for part in ("limits", "reservations"):
            res = ((svc.get("deploy") or {}).get("resources") or {}).get(part) or {}
            if "cpus" in res:
                res["cpus"] = str(res["cpus"])
        ports = []
        for p in svc.get("ports") or ():
            if isinstance(p, dict):
                p = {k: v for k, v in p.items() if k in ("target", "published", "protocol", "mode")}
                if "published" in p:
                    p["published"] = int(p["published"])
            ports.append(p)
        if ports:
            svc["ports"] = ports
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", action="append", default=[], help="a compose profile to include (observability)")
    ap.add_argument("--out", type=Path, default=Path("settleview-stack.json"))
    a = ap.parse_args(argv)
    stack = to_stack(merged(a.profile), a.profile)
    bad = violations(stack)
    if bad:
        print("REFUSED: these publishes would be open on every Swarm node: " + "; ".join(bad), file=sys.stderr)
        return 1
    a.out.write_text(json.dumps(stack, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {a.out} (services: {', '.join(stack.get('services', {}))}); no PC-only port is published")
    return 0


if __name__ == "__main__":
    sys.exit(main())
