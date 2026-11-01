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
- **Phone access is LAN-only.** With `BTCFLOW_API_HOST=0.0.0.0`, other devices are refused until a PIN is set on the
  PC, and then must sign in with it. The PIN is stored as a salted PBKDF2 hash; repeated wrong PINs lock an address
  out.
- **There is no HTTPS.** The PIN and session cookie travel in clear text on your network. Use it only on a home LAN
  or over a VPN. **Never expose it to the internet** (no port forwarding, no public reverse proxy). A reverse proxy
  on the same machine makes every request look local and bypasses the PIN.
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
- Clear-text traffic on the LAN (a known, documented limitation).
- Vulnerabilities in Kalshi, Polymarket US or their SDKs. Report those to the vendor.
- Trading losses. The app is not financial advice and makes no promise of profit.
