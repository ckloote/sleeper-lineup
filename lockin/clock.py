"""What day it is, for a project whose days are NBA days.

One question, asked in three places — the digest's as-of date, `explain`'s, and
how old `advice` thinks a run is — and until 2026-08-15 they did not all answer
it the same way. The CLI used `date.today()`, local to the machine; `advice` used
`datetime.now(UTC).date()`. On a Pi in US Eastern those disagree every evening
after 7pm, so a digest generated that morning would be labelled a day old the
moment you opened the page after dinner.

**The right basis is the schedule's timezone, not the machine's.** NBA game dates
are US Eastern: a game tipping at ``2025-10-22T02:00:00Z`` carries ``game_date``
``2025-10-21``, because 02:00 UTC is 22:00 the previous evening in New York. 77%
of the season's games tip on a different *UTC* date than the one they are filed
under, so "today" in UTC is the wrong question and "today" where the server
happens to sit is only accidentally right.

Hence ``LOCKIN_TZ``, defaulting to ``America/New_York`` — and it means "the
timezone the schedule is dated in", not "where you are". A Pi that moves to
Europe keeps working; a Pi whose system clock is misconfigured is no longer able
to shift the digest by a day.

**A timing constraint falls out of this and belongs with it.** The digest treats
every game dated ``today - 1`` as complete. The last games of a night tip around
04:00 UTC and finish by roughly 06:30 UTC, so it must not run before then. At
9am US Eastern — 13:00 or 14:00 UTC depending on daylight saving — there is seven
hours of headroom. :func:`too_early_for` states the rule so a cron scheduled
somewhere unexpected fails loudly rather than silently reading a half-played
slate as finished.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/New_York"
"""The timezone NBA game dates are filed under. Not a user preference."""

SLATE_COMPLETE_UTC_HOUR = 7
"""The hour by which a night's games have certainly finished, in UTC.

Latest tipoffs are around 04:00 UTC and run about two and a half hours. Seven is
that plus margin, and it is compared against rather than assumed: see
:func:`too_early_for`.
"""


def zone(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or os.environ.get("LOCKIN_TZ", DEFAULT_TZ))


def now(tz: str | None = None) -> datetime:
    return datetime.now(zone(tz))


def today(tz: str | None = None) -> date:
    """The current NBA date — the one `game_date` would use right now."""
    return now(tz).date()


def today_iso(tz: str | None = None) -> str:
    return today(tz).isoformat()


def too_early_for(as_of: str, moment: datetime | None = None) -> bool:
    """Would a digest for ``as_of`` be reading an unfinished slate?

    True when the previous night's games may still be in progress. A digest run
    then would treat partial box scores as final — the same class of error as a
    leak, arriving from the other direction.

    Returns False for any past date, which is the backtest and as-of case: those
    slates finished months ago whatever the clock says.
    """
    moment = moment or datetime.now(UTC)
    cutoff = datetime.combine(date.fromisoformat(as_of), time(SLATE_COMPLETE_UTC_HOUR), tzinfo=UTC)
    return moment.astimezone(UTC) < cutoff
