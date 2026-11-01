# One rule per figure: bracket net, money text, timestamps, signers

**Problem.** Review P2 "DRY": a bracket's net was computed in ~12 places (six with their own rounding), money was
formatted by a dozen copies (three with a hyphen for minus, two with the sign after the `$`), four ISO-time parsers,
four Kalshi signers that each re-implemented signing (only one pinned its host), seven update-then-insert settings
writes, and a few reconnect loops still doubling without jitter.

**What was not obvious.**
- *Float noise counted as a result.* Copies that summed the unrounded difference judged a win by `x > 0`:
  1.1 − 1.0 − 0.1 is 8.3e-17, a "win" on a $0 trade. The stored figures are 4-dp dollars, so rounding each net to
  $0.0001 first (`domain/brackets.net`) removes exactly the noise and nothing else.
- *Double rounding is the right one here.* The whole-cent sentences ("(+$0.56)") rounded the raw difference to the
  cent; they now round the 4-dp net. The two differ only when the 4-dp net is exactly on a half cent and float noise
  pushed the raw one across (about 1 in 200 random 4-dp nets); the new figure is the Record tab's 4-dp net rounded,
  so the sentence and the table can no longer disagree.
- *The browser parsed the server's text.* Play's `closedPnl` read the sign out of `trading.closed_sentence`
  (`/\(([+-])\$.../`). Switching the server to the real minus sign (U+2212) would have turned every loss into a
  green banner. Grep the web for regexes over server sentences before changing their wording.
- *`toLocaleString` and `toFixed` round differently.* Intl rounds the shortest decimal ("1.005" → 1.01), `toFixed` and
  Python's `f"{x:.2f}"` round the binary value (1.00499… → 1.00). The shared web formatter (`web/app.js`, `fmt.usd`)
  uses `toFixed` plus its own thousands separators so the browser shows what the server writes.
- *Polymarket times went through a float.* `int(datetime.timestamp() * 1000)` can land 1 ms early (the
  float for …:30.001 is just under it, and `int` cuts). `domain/timestamps.iso_to_ms` subtracts the epoch and
  floor-divides a `timedelta`: exact. The old copy also appended "+00:00" to whatever it got, so a time that carried its own offset was misread or dropped.
- *Pinning the host has to happen in both doors.* httpx calls `auth_flow(request)`; Kalshi's SDK calls
  `create_auth_headers(method, url)`. `KalshiSigner` checks scheme, host and port in both (`url_headers` for the
  SDK) before the allow-list, so a wrong base URL never receives KALSHI-ACCESS-* headers. The WebSocket handshakes
  sign `(method, path)` only; their URLs are constants, and `WsOnlyAuth` pins no REST host at all.

**Fix.** `domain/brackets.py` (net, totals, stop by side), `domain/fmt.py` + `web/app.js` `fmt.*` (money, cents,
side), `domain/timestamps.py`, `adapters/kalshi/auth.KalshiSigner` (Kalshi link, trading, margin, market-data WS),
`application/settings_store.py` (one upsert), `agent_sessions.market_of_strategy`, `adapters/retry.resend_while_busy`
and `Backoff` for the Telegram bot, the database writer's outage, startup reads and a refused Polymarket
subscription. Characterization tests (`tests/domain/test_bracket_net.py`, `test_fmt.py`, `test_timestamps.py`) keep
verbatim copies of the old code and prove which figures changed and why.

**Left as they are, on purpose.** Order-visibility polls (0.5 s, 1.0 s … after a create, to find an order a list does
not show yet) are not overload retries. The quotes recorder's book-silence wait (5 s doubling to 120 s, after a
connection that was silent for a minute), the migration lock wait and the process supervisor's restart delay are
single-process, minute-scale schedules with tests pinning them, in code another P2 branch owns.

**How to avoid.** A figure the user sees has one function. Before writing `proceeds - entry_cost`, `f"${x:.2f}"`,
`fromisoformat`, `KALSHI-ACCESS-SIGNATURE` or `repo.update(...)` then `repo.insert(...)`, import the one that exists.
