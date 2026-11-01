# Phones over HTTPS: the Tailscale sidecar and uvicorn's proxy headers (P3 #1)

**Problem.** Phones signed in with the PIN over plain HTTP on the LAN. The owner chose Tailscale (2026-11-01), with
everything in containers and nothing hand-rolled.

**What was built.** The standard container pattern: the official `tailscale/tailscale` image as a compose sidecar
(profile `tailscale`), `TS_SERVE_CONFIG` = `deploy/tailscale/serve.json` (HTTPS 443 → `http://app:8000`, Funnel off),
userspace networking, the auth key as a compose secret (`TS_AUTHKEY=file:/run/secrets/ts_authkey`). The app trusts
the proxy through uvicorn's own `proxy_headers` / `forwarded_allow_ips` (BTCFLOW_FORWARDED_ALLOW_IPS, main listener
only); the access middleware only reads the resulting scheme for `Secure` cookies and HSTS. A first draft parsed
`X-Forwarded-*` itself; the owner's rule "don't hand-roll what we can get off the shelf" replaced it with uvicorn's.

**Things the next agent would otherwise rediscover.**

1. **Tailscale Serve keeps the `Host`** and sets `X-Forwarded-Proto: https` (TLS only), `X-Forwarded-For: <the
   phone's tailnet IP>` and `X-Forwarded-Host` with `Header.Set` (a phone's own values are replaced):
   `ipn/ipnlocal/serve.go`, `addProxyForwardedHeaders` and `r.Out.Host = r.In.Host`, read in the v1.104.4 module. So
   the app's Host allow-list must hold the bare public name (no port: a browser omits :443), which is what
   `public_hosts()` adds for `BTCFLOW_PUBLIC_URL`; the same entry makes the HTTPS Origin allowed.
2. **uvicorn turns a malformed `forwarded_allow_ips` entry into a literal that never matches** (and `*` trusts
   everyone), silently. Settings validates every entry as an IP address or network and refuses `*` and `/0`.
3. **`forwarded_allow_ips` must be a fixed address nobody else can hold.** The sidecar sits at `10.89.250.2` on the
   internal network `tailscale-edge` (only app and sidecar), outside the network's dynamic `ip_range`
   (`10.89.250.4/30`), so Docker never hands that address to another container. Trusting the whole subnet would also
   trust the gateway (`.1`).
4. **Read-only root filesystem**: containerboot links its socket into `/var/run/tailscale`; without a tmpfs there it
   only warns, but `docker compose exec tailscale tailscale ...` then needs `--socket=/tmp/tailscaled.sock`.
5. **`TS_AUTH_ONCE=true`**: the auth key is used only while the state volume has no node key, so a one-off, one-day,
   tagged key is enough and can be revoked after the first start.
6. **`TS_LOCAL_ADDR_PORT=127.0.0.1:9002` + `TS_ENABLE_HEALTH_CHECK=true`** give `/healthz` (503 until the node is on
   the tailnet) for the compose healthcheck; the image has busybox `wget`.

**How it was checked without a tailnet (2026-11-01).**
- `deploy/tailscale/serve.json` decoded strictly into `tailscale.com/ipn.ServeConfig` (v1.104.4, `golang:1.27-alpine`,
  the way containerboot reads it: `${TS_CERT_DOMAIN}` replaced): HTTPS on 443 only, `/` → `http://app:8000`, Funnel off.
- The sidecar with the compose settings (read-only, `cap_drop: ALL`, userspace) and a fake key: it read the key file
  and Tailscale's control plane answered "invalid key: API key does not exist" (the `file:` path works), then it
  restarted (the policy). With no key and no network it waits in "Logged out" with `/healthz` 503.
- A throwaway stack: the real access middleware behind uvicorn configured as `make_servers` does, Caddy at
  `10.89.250.2` standing in for Serve (TLS, Host kept, XFP/XFF set), a client on a separate network: `Secure` session
  and device cookies, HSTS, the client = the caller's address (not the proxy's, not a forged XFF), `wss` with the public
  Origin accepted and a foreign Origin refused, five wrong PINs locking only that client; a container on the edge
  network at another address sending forged headers got no HSTS and no `Secure`.

**Only a real tailnet shows**: the login with a real key, the certificate for the name, the phone app's path.
