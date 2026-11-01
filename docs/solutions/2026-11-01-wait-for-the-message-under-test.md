# Wait for the message under test, not an earlier sign of it

**Problem.** On the Postgres CI job, `test_a_book_older_than_two_seconds_with_a_print_after_it_is_not_served`
(tests/kalshi/test_margin_stream.py) got a perps quote at +3.4 s where the book-freshness rule
(`domain/book_freshness.py`) should have refused it as "book feed broken". The SQLite job passed the same test.

**Cause.** The test, not the rule. The fake socket holds the whole captured session (57 messages, ending on a trade
after the last delta). The stream reads one message per event-loop turn (`asyncio.wait` on the reader, then
`handle`). The test waited for "a quote with a last price", which is true from message 12 (the first ticker), then
moved the injected clock and checked at once, with no await in between. On a slow runner the test's 5 ms poll woke
while the stream was still mid-capture, before any trade had been read. With no print after the book, the rule
correctly served a quiet book. The quote in the failure fits this: `ticker_age_ms` was 3400, so the newest ticker read
was at NOW. The Postgres store has nothing to do with it; that job's runner was just slower.

A reader that waits 2 ms per message (a stand-in for a loaded runner) reproduced it every time: the wait ended with 0
of 4 trades read and the quote served. Busy-looping every CPU in the dev container did not reproduce it in 40 runs,
which is why it looked like it only happened on CI.

**Live behaviour was right.** The rule judges what the stream has read, by message number and the injected clock. A
real-time stream cannot see a print it has not read yet, and once the print is read the quote is refused after the 2 s
grace. Nothing that gates real-money perps orders changed.

**Fix.** `read_whole_capture(rig)` waits until all of the capture's trades have been emitted (its last message is a
trade, which the helper asserts). Both tests that replay the whole capture use it. The first replay test had the same
race: it waited for the last delta's seq and then asserted all 4 trades.

**How to avoid.** When a fake feed is queued up front, wait on the effect of its LAST message (or the specific
message the assertion depends on), never on a condition an earlier message already satisfies. Move a fake clock only
after that.
