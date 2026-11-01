# A late read overwrites newer state in the web app

**Problem.** The first CI run's e2e tests failed intermittently on Linux (never locally on Windows): the New agent
form's H4 tick "did not change its state"; the Telegram daily time saved as the old 22:00 instead of 21:30; the Assist
proposal card appeared and then vanished for longer than the 5 s expect timeout. A Docker repro on a 2-CPU Linux
container failed the New agent case 3 times in 4.

**Cause.** One pattern three times: a GET started before a change answers after it, and its stale answer is applied.
- New agent: `openNew` prefills from the newest run with an async read. "Until the user edits the form" was checked
  only when the read started, so a read that landed after the market pick and the H4 tick replaced them (here with the
  newest run's Polymarket market and rules).
- Telegram: the pairing poll reads `/api/telegram/status` every 3 s. A read sent before a change and answered after it
  set `tg` back to the old settings, the time input's `:value` bound to them, and the next change event posted the old
  time.
- Proposal card: reads overlap (mount, every proposal event, the 5 s poll). A read sent before a proposal was made
  that answered after a newer read dropped the new card until the next poll.

**Fix.** Late answers are dropped: the prefill read is ignored once the form is touched; Telegram status reads carry a
sequence number that every read and every change bumps (`tgSeq`); the proposal card applies a read only if it is newer
than the one shown.

**How to avoid.** Any state that a poll or a prefill writes and the user (or a newer read) also writes needs a
"newer wins" check at apply time, not at request time. Loopback servers on a fast PC hide these races; a slow, shared
runner shows them.
