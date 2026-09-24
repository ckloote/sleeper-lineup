"""Which fantasy week a date belongs to, without asking the box scores.

`resolve_week` used to answer by finding the first box-score row on or after the
date. That works on a finished season and on nothing else: live, the rows for
games not yet played may not exist (§7.5), and a week whose games are all in the
future then has no answer at all (review finding 1).

The answer does not need them. Every one of 2025-26's 25 weeks ran Monday to
Sunday, week 1 being the week that holds opening night — including the
All-Star break, which is the Monday-to-Wednesday gap at the start of week 18,
not a week of its own. So the calendar is arithmetic on one date, opening night,
which the NBA schedule publishes months ahead.

It is a rule inferred from one season, and a league could change its week
structure. Two guards: every ingest compares it with Sleeper's own `leg` for
today (`disagreement`), and `tests/test_calendar.py` pins it against every row
of the recorded season.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from lockin.config import ALL_STAT_WEEKS

REGULAR_SEASON_PREFIX = "002"


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def opening_night(conn: sqlite3.Connection, season: str) -> date | None:
    """The season's first regular-season game date, from the NBA schedule."""
    row = conn.execute(
        "SELECT MIN(game_date) FROM nba_schedule WHERE season = ? AND nba_game_id LIKE ?",
        (season, REGULAR_SEASON_PREFIX + "%"),
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def week_of(day: date, opening: date) -> int:
    """The fantasy week holding ``day``. Before opening night that is week 1."""
    return max((day - monday_of(opening)).days // 7 + 1, 1)


def week_bounds(week: int, opening: date) -> tuple[date, date]:
    """Monday and Sunday of a fantasy week."""
    start = monday_of(opening) + timedelta(weeks=week - 1)
    return start, start + timedelta(days=6)


def resolve_week(conn: sqlite3.Connection, season: str, as_of: str) -> int | None:
    """Which fantasy week contains this date. None once the season is over.

    A date between two weeks' games — the All-Star break, or a preseason
    morning — belongs to the week it falls in by the calendar, which is the
    week about to be played: nothing is decided during the break, and the
    useful digest on its Tuesday is the one that previews Thursday.

    A database with no NBA schedule (ingested with `--skip-nba`) falls back to
    the box scores, which is the old behaviour and right only for past dates.
    """
    opening = opening_night(conn, season)
    if opening is None:
        row = conn.execute(
            "SELECT fantasy_week FROM box_scores WHERE season = ? AND game_date >= ?"
            " ORDER BY game_date LIMIT 1",
            (season, as_of),
        ).fetchone()
        return int(row[0]) if row is not None else None
    week = week_of(date.fromisoformat(as_of), opening)
    return week if week in ALL_STAT_WEEKS else None


def disagreement(conn: sqlite3.Connection, season: str, today: str) -> str | None:
    """Does the calendar disagree with Sleeper's own `leg` for today?

    Sleeper's `settings.leg` is the week it is playing. On a Monday morning the
    two may legitimately differ by one — Sleeper rolls the week over at a time
    this project has not observed — so that case is tolerated. Anything else
    means the Monday-to-Sunday rule does not hold for this league, and every
    week the digest resolves would be wrong.
    """
    opening = opening_night(conn, season)
    row = conn.execute(
        "SELECT payload_json FROM league_settings WHERE season = ?", (season,)
    ).fetchone()
    if opening is None or row is None:
        return None
    settings = json.loads(row[0]).get("settings") or {}
    leg, status = settings.get("leg"), json.loads(row[0]).get("status")
    if not isinstance(leg, int) or status == "complete":
        return None
    day = date.fromisoformat(today)
    expected = week_of(day, opening)
    if leg == expected or (day.weekday() == 0 and leg == expected - 1):
        return None
    return (
        f"Sleeper says week {leg} is being played; the calendar (Monday-to-Sunday weeks"
        f" from opening night {opening}) says {today} is in week {expected}"
    )
