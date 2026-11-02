DRAFT rubric (wiring only; shadow, never gates). Replace it from the error analysis (evals/review/): one failure
mode, labelled boundary examples, validated against the user's labels before it counts (rubrics/README.md).

Failure mode: the decision's stated reason is not supported by what the agent saw.

You are shown one wake of a trading agent: the message it received (memory, events, observe), its tool calls with
their results, and its final message. Judge only this: is every factual claim in the agent's recorded thesis and
final message supported by the inputs and tool results shown? A claim about a price, a rule, a position or a limit
that the inputs do not show is unsupported. Do not judge whether the trade was wise or profitable.

PASS: every stated fact can be found in the inputs or tool results.
FAIL: at least one stated fact is absent from, or contradicts, the inputs or tool results.
If the transcript does not show enough to decide, answer FAIL and say what is missing.

Write your critique first, then end with exactly one line: `GRADE: C` (pass) or `GRADE: I` (fail).
