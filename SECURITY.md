# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (a private security advisory). Don't open a public issue, pull request or discussion for a
security problem.

Include what you found, how to reproduce it, and what an attacker could do with it. This is a personal project
maintained in spare time, so there is no fixed response time, but reports are taken seriously and you will be
credited in the advisory unless you ask not to be.

There are no versioned releases; fixes go to the default branch.

## Scope and design assumptions

Settleview is a **local, single-user app**. Read reports in light of how it is meant to run:

- **It runs on your own computer.** By default the server listens on `127.0.0.1` only.
- **Phone access is LAN or tailnet only.** With `BTCFLOW_API_HOST=0.0.0.0`, other devices are refused until a PIN
  is set on the PC, and then must sign in with it. The PIN is stored as a salted PBKDF2 hash; repeated wrong PINs
  lock a device out.
- **HTTPS only over Tailscale.** The app itself speaks plain HTTP. Phones get HTTPS from the opt-in Tailscale sidecar
  (compose profile `tailscale`, Tailscale Serve inside your tailnet, never Funnel); proxy headers are applied only
  from the sidecar's fixed address (`BTCFLOW_FORWARDED_ALLOW_IPS`). On the LAN port the PIN and session cookie travel
  in clear text. **Never expose it to the internet** (no port forwarding, no public reverse proxy, no Funnel).
- **Keys stay local.** Venue API keys are read from your `.env` and from key files on your disk. They are never
  written to the database, logged, or sent to the browser. Never commit `.env` or `*.pem` files.

In scope, for example:

- Bypassing the PIN or the local-only restriction on phone-access settings.
- Any way to place, change or cancel an order without the human approval step, or around the kill switch, limits or
  order gate.
- Leaking keys, secrets or the PIN (to logs, the database, API responses or the browser).
- Cross-site request forgery or script injection that a page on another site could use against a running instance.

Out of scope:

- Problems that only exist because the app was exposed to the internet or run behind a same-host reverse proxy
  against the guidance above.
- Clear-text traffic on the LAN port (a known, documented limitation; use the Tailscale sidecar).
- Vulnerabilities in Kalshi, Polymarket US or their SDKs. Report those to the vendor.
- Trading losses. The app is not financial advice and makes no promise of profit.

## Threat model: the trading audit

`trading_audit` records every approval, order, refusal, fill and limit change with who did it (`pc`, `phone:...`,
`agent:...`, `system`). STRIDE note for it (the rest of the threat model is planned with P3 item 7):

- **Tampering.** On Postgres the app's role may only `SELECT` and `INSERT` audit rows; the table, its trigger and the
  schema `app` belong to a no-login role (`deploy/postgres/audit.sql`), and a trigger refuses `UPDATE`, `DELETE` and
  `TRUNCATE` even for the owner and a superuser outside the documented maintenance transaction (docs/operations.md,
  "Postgres: the append-only audit"). A bug, an agent or a leaked app password therefore cannot rewrite or erase the
  record. SQLite installs refuse `UPDATE` and `DELETE` by trigger but have no roles: anyone who can write the file can
  change it.
- **Repudiation.** Rows cannot be removed by the app, and each carries its actor, so "the agent / the phone did not do
  that" can be checked against a record the app could not have edited afterwards.
- **Not covered.** A Postgres superuser (or root on the host) can still change anything; the maintenance transaction is
  logged but not prevented. Tamper evidence beyond the host comes from the nightly `pg_dump` today and, next (deferred
  with off-site backups), from copying those dumps to object storage with object lock (WORM). Statement-level database
  audit logging (`pgaudit`) would need an image the project does not build yet.
