# Wall-clock budgets in tests on shared CI runners

**Problem.** Two latency tests failed on the first GitHub Actions run and pass locally: the perps View "under 5 ms"
(5.69 ms, the mean of 50 calls) and perps prepare/approve "under 300 ms" (prepare 353 ms while every named step was
0 ms and approve took 10 ms).

**Cause.** A shared runner has one-off stalls (a neighbour, GC, the first write to a fresh SQLite file). A mean over a
loop lets one stall decide the result, and a single timed call that is also the process's first audit write and first
ticket measures a cold start, not the running app's warm path that the budget is about.

**Fix.** The View test times each call and asserts on the median (the pattern `tests/features/test_perf.py` already
used). The prepare/approve test does one untimed prepare first, then times a warm prepare and approve. The budgets
themselves are unchanged.

**How to avoid.** Time per call and take the median, warm the path once before timing it, and keep the budget at what
the user would notice. Do not loosen the budget to make CI pass.
