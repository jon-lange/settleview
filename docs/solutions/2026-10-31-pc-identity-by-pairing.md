# "The PC" was a network position: pair the browser instead

**Problem.** The review (docs/plans/2026-10-31-1800-review-fix-plan.md, P0 #1) found that real-money rights (resume
the kill switch, switch to production, raise limits with the constant phrase, post the presence heartbeat, then
approve an agent's proposal) went to whoever reached the right socket:
- every request on the PC listener (`BTCFLOW_PC_PORT`, 8001) was the PC. Compose binds it on `0.0.0.0` inside the
  container (the `127.0.0.1` publish needs that), and every service shares the default network, so a sibling container
  (OpenObserve, a recorder) could call `http://app:8001` with `Host: localhost:8001` and be the PC;
- loopback was the PC, so any process inside the container (the agent CLI child shares its network namespace) or on a
  native host was the PC as long as it did not send `X-Agent-Token`. The header was the only thing between an external
  agent and the human;
- `docker stack deploy` drops the host IP of a publish: `127.0.0.1:8001:8001` (and OpenObserve's 5080) would be open
  on every Swarm node.

**Cause.** Identity was inferred from where a request came from. In a container that carries almost no information:
every client arrives from a bridge address, a listener bound for a port publish is reachable from the whole compose
network, and the host IP of a publish is a Docker Desktop / Compose feature Swarm ignores.

**Fix.** A credential, with network position kept only as an extra condition:
- `python -m btc_signal_engine.ops pair-pc` writes the SHA-256 of a one-time code (5 minutes, one browser) to the
  app's data folder and prints a link; `POST /pair` on the PC listener redeems it and sets an HttpOnly,
  SameSite=Strict cookie, an HMAC under `<data dir>/pc-secret`. The PC = the PC listener AND that cookie. The secret
  is a file, not a database row, because the recorders share the app's database role and the research role reads the
  app schema; only the app's container mounts the data folder. Rotation (Sign out PC browsers, `ops signout-pc`)
  replaces the file; the app re-reads it at most every 5 s.
- The PC listener stays: it is the only place pairing is allowed and the only place the cookie counts, so a phone
  (or a stolen cookie on the phone port) can never become the PC, and its fixed Host list keeps DNS rebinding out.
- External agents get their own listener (`BTCFLOW_AGENT_PORT`, actor `agent-api`), which serves only the tool API;
  every tool call needs a live session's token (reads too, since the port is reachable from the compose network).
  People-only routes check for a person's actor (`access.human`), and `from_pc` fails closed without an actor.
- `/api/health` is open (the healthcheck runs on loopback) but answers `{"status": "up"}` only without a session.
- Swarm: `deploy/swarm/render_stack.py` merges `deploy/swarm/compose.swarm.yaml` over compose.yaml and refuses a
  stack that publishes 8001, 8002 or 5080. `docker stack deploy` cannot read compose.yaml directly anyway (it rejects
  `depends_on` conditions and numeric `cpus`), and Compose's `!reset` / `!override` tags only work in
  `docker compose config`, so the script renders through it (`--no-interpolate`, so the deploying shell fills
  `${...}` and no `.env` value lands in the file). CI runs it and `docker stack config` on the output.

**How to avoid.** Never grant rights from a client address, a listening port or a missing header alone in anything
that runs in a container: ask what else can reach that socket (sibling services, processes in the same namespace,
other Swarm nodes). Tie a privilege to something the person holds (a cookie from a pairing code, a PIN session, a
session token) and use the network position only to narrow where that credential is accepted. When a route test
needs a privileged actor, set it explicitly (`tests/pc.py`): a default that grants it hides exactly this bug.
