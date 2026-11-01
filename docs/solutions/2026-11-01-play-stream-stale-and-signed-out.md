# Play over a stream: what "live" means, and a stream that outlives its session

**Problem.** Play polled `/api/play/now` every 2 s with no in-flight guard (a slow answer could replace a newer one),
kept the last numbers on screen and the slide on when polls failed, and showed nothing when a phone's session ended:
every request just failed (review B6#1-2, #9).

**Causes worth knowing.**
- `EventSource` never tells the page the HTTP status. A stream refused with 401 looks exactly like a network blip
  (`onerror`, then the browser's own retries). The page sends one ordinary GET on `onerror` (`probe`) so the API
  client sees the 401 and shows the sign-in prompt.
- The access middleware checks a stream's session once, when it opens. Signing every phone out (or a PC browser
  unpaired) leaves an already-open stream flowing. The page closes Play's stream as soon as any request answers 401;
  a server-side cut-off of open streams on sign-out is not built (the same is true of the Perps and Agent streams).
- Mixing a delta stream with HTTP answers is the late-read class again
  (`2026-11-01-late-reads-overwrite-newer-ui-state.md`): a full HTTP view applied between two delta frames can leave a
  field the stream never re-sends. Rule used: while the stream is live it is the only source; HTTP reads apply only
  when it is not, newest request only and never older than the last `server_now_ms` shown; a reconnect or a
  `stale` frame makes the next frame whole (`full: true`).

**Fix.** `GET /api/play/stream` (adapters/http/push.py `TickStreams`): the view once a second, whole first, then
changed fields; a failed read is an explicit `event: stale`. The page is live only with a frame in the last 4 s;
otherwise the dock greys, the slide and Confirm are off, and one line says since when.

**How to avoid.** Liveness is a property of the transport, not of the last payload: track when the last good frame
arrived and gate every action on it. Never rely on an `EventSource` error to say why; probe with a request whose status
you can read.
