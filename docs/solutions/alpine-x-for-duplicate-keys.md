# Alpine x-for: "Cannot read properties of undefined (reading 'after')"

**Problem.** The Agents tab's fleet audit (All agents' activity) threw `Cannot read properties of undefined (reading
'after')` as a page error the moment rows of two agents were on screen; the list then stopped updating.

**Cause.** Two items of one `x-for` had the same `:key`. The list puts a run divider wherever the run changes; in the
audit, rows of different agents interleave (newest first by row id), so one run's divider appeared more than once
under the key `'r' + session_id`. Alpine 3 looks up the previous item's element by key while it inserts the next one;
a duplicate key leaves that lookup `undefined` and `undefined.after(...)` throws. The message names neither the
key nor the list.

**Fix.** Key each divider by the run and the first row of its stretch (`'r' + session_id + ':' + row.id`), so every
key in the list is unique (`web/views/agent.js`, `jrItems`).

**How to avoid.** Any list that can show the same thing twice (dividers, headers, a repeated row) needs a key made
from its position as well as its identity. When this error appears, look for a repeated `:key` in the nearest
`x-for` first.
