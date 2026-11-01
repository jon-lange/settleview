# Venue health: judge each thing by its own signal, not by "a message arrived"

Review fix plan 2026-10-31, P1 batch C (B3#7, B3#8, B3#10, B1#9). Four findings with one shape: a cheap signal that
usually travels with the real one was taken as the real one.

## 1. Perps book "fresh" because the ticker kept ticking (B3#8)

**Problem.** The Kalshi perps quote was served while its age (newest message of ANY kind) was under 2 s. Kalshi's
`ticker` channel sends one message a second per market even when nothing changes, so a frozen `orderbook_delta`
subscription still looked fresh, and protective prices and bands were computed from a stale touch. Same failure as
Polymarket's 2026-10-31 13:59 outage (books stopped, trades kept coming).

**Fix.** The quiet-book rule moved into `domain/book_freshness.py` and both books use it: freshness is the age of the
last BOOK update; a quiet book is served (30 s at most) only on an alive connection, with the trade subscription
confirmed since before the book's last update and no print after it. A trade sequence gap restarts trade coverage.

**How to avoid.** Never time a book by `last_msg_ms`. A heartbeat or ticker proves the socket, not the data.

## 2. A refused private subscription read as healthy (B3#7)

**Problem.** Polymarket answers a refused subscribe ASYNCHRONOUSLY with an error message carrying our request id; the
SDK emits it as `WebSocketError`, which `ws_error_is_fatal` rightly calls "not a dead socket". The private stream had
already set `_connected = True` after sending the subscribes, so a refused ORDER subscription left the feed "up"
while no fills arrived (slow polling, late take-profit fills).

**Fix.** An error naming a `pmpriv<n>` request ends the session (`PrivateSubscriptionRefused`), the feed reads down
with the reason, and it stays a problem until a later session has held every subscription for `UP_AFTER_MS`.

**How to avoid.** "Sent" is not "accepted". Any server error that names one of OUR request ids is about our state.

## 3. Retries in lockstep (B3#10)

**Problem.** Every loop had its own fixed schedule and none had jitter: after a blip, every pool socket and both Kalshi
streams reconnected at the same instants; a 429 was retried after 0.2 s whatever the venue said.

**Fix.** Full jitter (first a hand-rolled `adapters/retry.py`, the same day moved onto tenacity:
`adapters/backoff.py`) for reconnects and request retries, a 429's `Retry-After` as the floor
(capped at 10 s on order paths). It only decides how long to wait; whether a request may be re-sent stays with the
client (Kalshi: our loop only, same body and `client_order_id`; Polymarket creates: never).

**How to avoid.** New loops use tenacity (`AsyncRetrying` with `wait_venue` or `jittered`; a reconnect supervisor:
`reconnecting` + `start_over`), never a hand-rolled loop; tests that need exact waits use the `jitter_top` /
`jitter_bottom` fixtures, which replace tenacity's random waits.

## 4. The PIN lockout keyed on the Docker gateway (B1#9)

**Problem.** Behind Docker's port publishing every phone's TCP peer is the bridge gateway, so the per-IP lockout was
one bucket for the whole LAN: five wrong PINs from any device locked every phone out for 15 minutes.

**Fix.** A signed per-device cookie from `GET /login` joins the key (`<ip>#<device>`). Without it (scripts) the bare IP
is the key, and a POST never hands a cookie out, so dropping it never frees another device's bucket; the global cap
still bounds rotation. No header is trusted: uvicorn's `proxy_headers` is now off (it would have let a loopback peer
rewrite the client from `X-Forwarded-For`, which the docs already said was ignored).

**How to avoid.** A per-client limit behind NAT needs a per-client identity the server issued; a forwarded header is
only trustworthy from a proxy you configured. (Since P3 #1, 2026-11-01, that proxy exists: uvicorn's `proxy_headers` is on for the phone port from the
Tailscale sidecar's fixed address only, `BTCFLOW_FORWARDED_ALLOW_IPS`; solutions/2026-11-01-phones-https-tailscale-sidecar.md.)
