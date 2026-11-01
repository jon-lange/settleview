# Error analysis: how to do it

Before any judge or new grader, read real wakes and write down the **first** thing that went wrong in each (research
recommendation #1; the plan's Process). The graders worth writing next are the failure modes you find, not the ones
we imagine.

1. Make the pack (read-only on the database; the same redaction and scan as the eval export):
   ```bash
   python -m evals.review --source <sqlite file or postgres url> --n 100
   ```
   The first pack was made from the final SQLite snapshot: `review-btcflow-final-n100-seed7.md` (100 wakes from
   2026-10-30/31). Wakes from 2026-11-01 on also carry their exact input (memory and observe); earlier ones show a
   reconstructed memory and no observe.
2. Open `review-<name>.md` and `labels-<name>.csv` side by side. For each wake (about a minute):
   - read what woke it, what it saw, each tool call and result, its decision and reason, what it told you, and the
     outcome;
   - **first_failure**: the first thing that went wrong, in your own words, one line (empty if nothing did). Judge
     conduct, not profit: a careful NO_TRADE that missed a winner is a pass; a lucky win against its own rules is a
     fail;
   - **pass_fail**: pass / fail / unsure;
   - **failure_category**: a short free-text group (you will merge them later);
   - **notes**: anything else.
   The CSV is the sheet the tooling reads (rows are keyed by `journal_id`, which stays the same across packs). The
   Markdown's blank lines are there if you prefer to annotate inline.
3. Tally:
   ```bash
   python -m evals.review --tally evals/review/labels-<name>.csv
   ```
   Merge similar categories, count them, and pick the top few: each becomes a code grader (with its mutation check)
   or, where code cannot decide, one binary judge validated against these labels (TPR/TNR >= 0.90 on a held-out
   split, house rule 2).

Everything in this folder except this README is gitignored: the pack is redacted, but it is your own trading history.
Re-running the pack never overwrites a filled-in label sheet.
