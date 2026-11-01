# The eval export's redaction leaked balances until its own scan was made independent

Built with stage 1 of the agent evals (`evals/redact.py`, `evals/export.py`; tests in
`tests/evals/test_export_redaction.py`). Linked from evals/README.md, "The export and its redaction".

**Problem.** The first redaction replaced the number after "cash" / "balance" / "equity". On the real snapshot
(931 wakes) agents wrote account figures in other shapes, and some of them got through:

- lists: "cash $41.27 and $90.10", where only the first number was caught;
- the amount before the word: "(Kalshi $41.27, Polymarket $88.40 cash)";
- a bare number in a separate parenthesis: "Kalshi (cash X) and Polymarket (87.15)".

Over-redaction also happened: "polymarket_15m" became "polymarket_<balance>m", and Polymarket market slugs were
masked as secrets.

**Cause.** One regex was checked against itself: the scan used the same pattern as the redaction, so it could only
confirm what the redaction already did. Rules also overlapped. The high-entropy secret rule masked 32-hex bracket
ids, so turning off the id rule leaked nothing, and that rule could not be shown to work.

**Fix.**
- The balance rule is applied to a fixed point. It is followed by a sentence-fragment rule: in a sentence (ending
  at `.`, `;`, `!`, `?` or a newline, but not a decimal point) that names a balance word or holds a `<balance>`
  placeholder, every money amount is redacted.
- The number must not be glued to a word, so `polymarket_15m` is safe.
- The high-entropy rule needs one unbroken 20+ character run and skips pure hex. Pure hex belongs to the id rule.
- Each rule now has exactly one job. The test plants one value per rule and switches each rule off in turn, and the
  scan must then fail the export: a mutation check per redaction. The scan also has rules the redaction does not
  (dropped keys present, raw session ids and their 8-character prefixes), and it found the leaks above by reading
  the real export by eye after it passed.

**How to avoid.** Never let a sanitizer verify itself (house rule 7). Plant values, switch off each redaction rule
in turn, and require the check to fail. Also read a sample of the real output by eye: the leaks above were found
there, after the scan passed.
