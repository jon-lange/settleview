# "Today" was three different days

**Problem.** Play's day limit and its today P&L used the UTC day (`now - now % DAY_MS`), the week was a rolling 7 x
24 h, and Record / the agents / the daily summary used "this machine's zone" (review B5#2, #5). In the container the
machine's zone is UTC unless `TZ` is set, so Record silently matched Play only by accident; with `TZ` set they
disagreed by the zone's offset.

**Causes worth knowing.**
- `datetime.fromtimestamp(ms).astimezone()` gives a *fixed* offset. Subtracting days from it and asking for midnight
  is an hour off across a daylight-saving change; the old code patched one path (`days_back`) but not the agents'
  `day_start`. A real `ZoneInfo` does wall-clock arithmetic correctly; going back by calendar date (`date() -
  timedelta(days=n)`, then midnight in the zone) is the safe form.
- A zone a half hour off UTC (or a 1 h window) means a trading window can start before local midnight: the window's
  committed dollars must be read from the earlier of the window start and the day start, not from the day's rows.
- Ask Settleview's record cached each closed trade's features on first read. A trade read before the Kalshi result
  recorder had written its result stayed "unknown" until a restart (B5#3): cache only final rows; re-read unfinished
  15-minute trades at most once a minute, for up to 6 h after the close.

**Fix.** `domain/days.py`: one zone (from `TZ` at startup, unset = UTC, unknown name = error), `day_start(now, tz,
days_back)` by calendar date. Every "today" takes the zone from bootstrap; the payloads name it (`day_tz`) and the
screens say when the browser is elsewhere.

**How to avoid.** Never compute a day with `% 86_400_000` or with a zone-less `astimezone()`. Ask `domain/days.py`.
