# Judge rubrics (not in this stage)

Decided (user, 2026-10-31): the judge is a second vendor, a different model family from the Claude subject, to avoid
self-preference. Adding it needs four things first: the vendor choice, its API key in a secret file, the user's
approval of that vendor's official SDK package, and labelled data from the error analysis (evals/review/).

Code graders own every safety verdict. A judge is only for what code cannot decide, such as "is the stated reason
supported by the inputs?" or "did the message to the user describe the trade honestly?". The rules for a rubric:

- one failure mode per rubric, with a binary pass/fail verdict;
- a pass definition, a fail definition and labelled boundary examples;
- an "insufficient information" escape;
- JSON output with the critique before the verdict.

A judge must pass house rule 2, in order, before it gates anything:

1. It discriminates (no saturation).
2. Its verdict survives reordering.
3. The ranking does not flip with the choice of judge.
4. It is from another family than the subject.

Its validity is then measured against your labels: TPR and TNR of at least 0.90 on a held-out split. A judge change
is a production change: shadow, then canary, then authoritative.
