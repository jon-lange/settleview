"""Are the research recorders writing? (docs/plans/2026-10-31-0830-ops-portable-runtime-plan.md P4; the 31-hour silent
outage in docs/solutions/2026-10-31-pm-recorder-died-silently.md.)

The app reads each recorder's newest row through the `RecorderData` port (read-only) and:
- calls a recorder **silent** when its newest row is older than its limit: the quotes recorder after 2 minutes (it
  writes every second while it runs), the settlement recorder after 10 minutes (it writes one row per closed window,
  every 5 minutes at best, so 2 minutes would be a false alarm every window);
- tells each change once (`changes`): silent, back (with how long it was silent), and a missing research database
  (once per app run, plainly: "not found", never treated as fresh or retried as an alert). A read error (a locked
  file) changes nothing and is logged once;
- tells, when the quotes recorder is silent while prints are still being recorded (the newest `pm-trades` row within
  the same limit), that the venue is sending trades but no quotes (2026-10-31 13:59-14:45 CDT: a venue-side book
  outage; the recorder keeps its sockets and reconnects, docs/solutions/2026-10-31-pm-quiet-book-vs-dead-book-feed.md)
  instead of only "silent";
- gives yesterday's coverage (UTC day): 5-minute windows with at least one quote out of 288, the longest run of
  windows without one, and how many settled (`coverage_line`).

`status` (cached for CACHE_MS) is what /api/health shows under `recorders`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from btc_signal_engine.application.ports import RecorderData

log = logging.getLogger(__name__)

LIMITS_MS = {"pm-quotes": 120_000, "pm-settle": 600_000}
LABELS = {"pm-quotes": "Quotes recorder", "pm-settle": "Settlement recorder"}
WHAT = {"pm-quotes": "Polymarket quotes", "pm-settle": "Polymarket settlements"}
CACHE_MS = 5_000
TRADES = "pm-trades"  # RecorderData.newest_ms's extra key: the newest recorded print
TRADES_ONLY = "trades_only"  # a silent quotes recorder whose prints are still fresh: the venue sends no quotes
DAY_MS = 86_400_000
SPAN_5M_MS = 300_000


@dataclass(frozen=True)
class Change:
    kind: str  # "silent" | "back" | "missing"
    recorder: str | None  # None for "missing" (the whole database)
    age_ms: int | None = None  # silent: how old the newest row is; back: how long it was silent
    note: str | None = None  # silent: TRADES_ONLY when prints are still being recorded (the venue sends no quotes)


def minutes(ms: int | None) -> str:
    if ms is None:
        return "never"
    m = ms / 60_000
    if m < 120:
        return f"{max(1, round(m))} min"
    return f"{m / 60:.0f} h" if m < 48 * 60 else f"{m / 1440:.0f} days"


class RecorderWatch:
    def __init__(self, data: RecorderData, *, limits_ms: dict[str, int] | None = None,
                 db_label: str = "polymarket_us.db") -> None:
        self.data = data
        self.limits = dict(limits_ms or LIMITS_MS)
        self.db_label = db_label
        self._silent_since: dict[str, int | None] = {}  # recorder -> newest row time when it went silent
        self._missing_told = False
        self._error_logged = False
        self._cache: tuple[int, dict] | None = None

    def _read(self) -> dict[str, int | None] | None | str:
        """The newest rows, None when the database does not exist, "error" when it could not be read."""
        try:
            out = self.data.newest_ms()
        except FileNotFoundError:
            return None
        except Exception:
            if not self._error_logged:
                log.warning("recorder freshness: the research database could not be read", exc_info=True)
                self._error_logged = True
            return "error"
        self._error_logged = False
        return out

    def _note(self, name: str, newest: dict, now: int) -> str | None:
        """TRADES_ONLY when `name` is the quotes recorder and prints are still fresh (within its limit)."""
        if name != "pm-quotes":
            return None
        t = newest.get(TRADES)
        return TRADES_ONLY if t is not None and now - t <= self.limits[name] else None

    def status(self, now: int) -> dict:
        """{available, reason?, recorders: {name: {newest_ms, age_s, limit_s, state: ok | silent | empty, reason?}}}
        (a recorder's `reason`: "the venue is sending trades but no quotes" when that is why it is silent)."""
        if self._cache is not None and now - self._cache[0] < CACHE_MS:
            return self._cache[1]
        newest = self._read()
        if newest is None:
            out = {"available": False, "reason": f"{self.db_label} not found", "recorders": {}}
        elif newest == "error":
            out = {"available": False, "reason": f"{self.db_label} could not be read", "recorders": {}}
        else:
            recs = {}
            for name, limit in self.limits.items():
                ts = newest.get(name)
                age = None if ts is None else now - ts
                state = "empty" if ts is None else ("silent" if age > limit else "ok")
                recs[name] = {"newest_ms": ts, "age_s": None if age is None else round(age / 1000),
                              "limit_s": limit // 1000, "state": state}
                if state != "ok" and self._note(name, newest, now) == TRADES_ONLY:
                    recs[name]["reason"] = "the venue is sending trades but no quotes"
            out = {"available": True, "recorders": recs}
        self._cache = (now, out)
        return out

    def changes(self, now: int) -> list[Change]:
        """What changed since the last call (the first call reports what is wrong now)."""
        newest = self._read()
        if newest == "error":
            return []
        if newest is None:
            if self._missing_told:
                return []
            self._missing_told = True
            return [Change("missing", None)]
        out = []
        for name, limit in self.limits.items():
            ts = newest.get(name)
            silent = ts is None or now - ts > limit
            if silent and name not in self._silent_since:
                self._silent_since[name] = ts
                out.append(Change("silent", name, None if ts is None else now - ts, self._note(name, newest, now)))
            elif not silent and name in self._silent_since:
                since = self._silent_since.pop(name)
                out.append(Change("back", name, None if since is None else ts - since))
        return out

    def coverage(self, now: int) -> dict | None:
        """Yesterday's (UTC) 5-minute coverage: {windows, recorded, settled, longest_gap_min, day}; None without a
        readable database."""
        end = now - now % DAY_MS
        return self._coverage(end - DAY_MS, end)

    def rolling_coverage(self, now: int) -> dict | None:
        """The same over the last 24 h of complete 5-minute windows (the settleview.recorder.coverage and
        .longest_gap gauges the OpenObserve alerts read); None without a readable database."""
        end = now - now % SPAN_5M_MS
        return self._coverage(end - DAY_MS, end)

    def _coverage(self, start: int, end: int) -> dict | None:
        try:
            c = self.data.coverage(start, end, SPAN_5M_MS)
        except FileNotFoundError:
            return None
        except Exception:
            log.warning("recorder coverage: the research database could not be read", exc_info=True)
            return None
        run = best = 0
        prev = None
        for st in c["missing_starts"]:
            run = run + 1 if prev is not None and st - prev == SPAN_5M_MS else 1
            best = max(best, run)
            prev = st
        return {"windows": c["windows"], "recorded": c["recorded"], "settled": c["settled"],
                "longest_gap_min": best * SPAN_5M_MS // 60_000, "day_start": start}


def change_text(c: Change, db_label: str = "polymarket_us.db") -> tuple[str, str]:
    """(head, detail) of one change, plain text (the notifier adds its emphasis and escaping)."""
    if c.kind == "missing":
        return "No recorder data", f"{db_label} not found in the research folder; the recorders are not running here"
    label, what = LABELS.get(c.recorder, c.recorder), WHAT.get(c.recorder, "rows")
    if c.kind == "silent":
        detail = f"no {what} for {minutes(c.age_ms)}" if c.age_ms is not None else f"no {what} recorded yet"
        if c.note == TRADES_ONLY:
            detail += ("; trades are still arriving, so Polymarket is sending trades but no quotes (a venue-side book "
                       "outage; the recorder keeps reconnecting)")
        return f"{label} silent", detail
    return f"{label} back", (f"writing again after {minutes(c.age_ms)}" if c.age_ms is not None
                             else "writing again")


def coverage_text(cov: dict) -> str:
    gap = "no gap" if not cov["longest_gap_min"] else f"longest gap {cov['longest_gap_min']} min"
    return (f"Data yesterday (UTC): 5m windows {cov['recorded']}/{cov['windows']} recorded · {gap} · "
            f"{cov['settled']}/{cov['windows']} settled")

