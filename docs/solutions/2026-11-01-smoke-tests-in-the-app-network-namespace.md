# Smoke-testing a running stack: in the app container's network namespace, with a read-only PC cookie

**Problem.** The deploy gate's smoke suite must load the PC's screens (Playwright) and the PC-only APIs of a running
stack, staging or production, without pairing a browser (a write), without publishing a port (an offline staging has
none: Docker publishes nothing on an `internal` network) and without contacting anything else.

**Cause.** Three app rules meet here. The PC listener answers only to `Host: localhost:<its own port>` (a published
`127.0.0.1:18001:8001` would not match), and only with a PC cookie. The cookie is `"<issued>.<nonce>.<hmac>"` under the
secret file `<data dir>/pc-secret`, so it needs no database row, but `PcCredentials` *creates* the secret file when it is
missing, which a read-only check must never do. And the dev image (Playwright's Chromium) and the app image differ.

**Fix.** scripts/smoke.py runs the dev image with `--network container:<app container>`: `localhost:<port>` is then the
app's own listener, from loopback, as on the PC. It mounts the app's data volume read-only and signs a cookie with
`adapters/pc_credentials.cookie_from_secret_file`, which only reads. The app listens inside the container on the same
numbers it is published on in staging (18000-18002), so a browser on the host works there too. The tests send GET/HEAD
only (the client refuses anything else before sending; the browser aborts it).

**How to avoid.** To test a container's own listeners without ports, share its network namespace instead of going
through the host; and give every "load a credential" helper a read-only variant before using it from a check.
