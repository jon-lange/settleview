# Alpine's CSP build (3.14) evaluates property paths only

Found while removing `'unsafe-eval'` from the Content-Security-Policy (P2, review B6#6), with `@alpinejs/csp` 3.14.9.

**Problem.** The standard Alpine build compiles every directive (`x-show="a && !b"`, `@click="pick(x)"`) with
`new AsyncFunction(...)`, which a CSP without `'unsafe-eval'` blocks. The CSP build of the same version does not
compile anything, and that is stricter than its docs suggest.

**What the 3.14 CSP build accepts.** Its evaluator splits the expression on `.` and walks the scope: only a dotted
property path (`open`, `pickerLabel`, `$store.app.view`, `it.name`). No operators, literals, calls with arguments or
assignments. A path that names a function is called with the component as `this` (an event handler's with the event as
its first argument: a method with an optional parameter gets the Event). A path whose value is `undefined` logs
"Alpine is unable to interpret ...", and one through `null` throws. `x-model` cannot assign (it builds
`path = __placeholder`), so it needs a getter returning `{get, set}` (`BTC.model`). `x-data=""` is looked up as the
path `{}` and warns too. A store method referenced by path (`$store.agent.stale`) runs with the component, not the
store, as `this`.

**Fix.** Every view registers its component with `Alpine.data`, and every directive names a getter or method on it
(about 1,300 expressions moved into the view scripts). `tests/api/test_alpine_csp.py` fails on any directive that is
not a path (and on an `x-model` not ending in `Model`), and the e2e guard fails on any `Alpine` console warning, so an
inline expression or a path to nothing cannot come back unnoticed.

**How to avoid.** Read the CSP build's `evaluator.js` for the version you vendor before planning the conversion; the
3.15 CSP build has a real expression parser and would have needed far fewer changes, but it is a different Alpine
version.
