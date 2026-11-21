"""Who is in the rotation: a lineup regular who sits only on nights without a game.

A bench is two different groups of players. A manager who rotates daily benches
his best players on nights they have no game, so the morning poll's bench holds
the player who will be back in tomorrow. It also holds the players he benches
on nights they *do* play: the hurt, the demoted, and the end of the roster.
The projection cannot tell the two apart, and pricing both as options went
badly. In 2025-26, bench player-weeks scored about 21% below projection, while
starters scored about 9% above it (see the bench-aware CHANGELOG entry).

The manager's own lineups can tell them apart. For each night a player had a
game, the first poll after that slate shows whether he was in the lineup for
it. A player started on most of his game nights is a regular, and his bench
nights are scheduling, not judgement. Anyone else is left out.

A player who is locked stays in his slot for the rest of the week, so his later
game nights count as starts. That flatters only players who were good enough
to lock.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from lockin.clock import SLATE_COMPLETE_UTC_HOUR
from lockin.core.projections import SeasonPanel
from lockin.projections import date_of

ROTATION_SHARE = 0.6
"""Share of his game nights a player must have started to count as a regular."""
MIN_GAME_NIGHTS = 3
"""Fewer observed game nights than this is too little to call."""
EARLIEST_TIP_UTC_HOUR = 16
"""A poll after this, on the day after a slate, may already reflect the next
night's lineup. The earliest regular-season tips are about 17:00 UTC."""


@dataclass(frozen=True, slots=True)
class Usage:
    started: int
    game_nights: int

    @property
    def share(self) -> float:
        return self.started / self.game_nights if self.game_nights else 0.0

    @property
    def regular(self) -> bool:
        return self.game_nights >= MIN_GAME_NIGHTS and self.share >= ROTATION_SHARE


def lineup_usage(
    conn: sqlite3.Connection,
    panel: SeasonPanel,
    roster_id: int,
    *,
    through_day: int,
) -> dict[str, Usage]:
    """How often this roster started each of its players on his game nights.

    Only game nights on or before ``through_day`` count, and only polls taken
    before the next day's earliest tip, so nothing from after the decision is
    read. A night with no poll in its window is skipped, not counted as a
    benching. That matters in 2025-26, which averages about four polls a week.
    """
    cutoff = f"{date_of(through_day + 1)}T{EARLIEST_TIP_UTC_HOUR:02d}:00:00+00:00"
    polls: dict[int, dict[str, dict[str, bool]]] = defaultdict(lambda: defaultdict(dict))
    for row in conn.execute(
        "SELECT week, observed_at, sleeper_id, is_starter FROM weekly_matchups"
        " WHERE roster_id = ? AND observed_at < ? ORDER BY observed_at",
        (roster_id, cutoff),
    ):
        polls[row[0]][row[1]][row[2]] = bool(row[3])

    started: dict[str, int] = defaultdict(int)
    nights: dict[str, int] = defaultdict(int)
    for week, by_stamp in polls.items():
        stamps = sorted(by_stamp)
        players = {pid for poll in by_stamp.values() for pid in poll}
        for pid in players:
            hist = panel.histories.get(pid)
            if hist is None:
                continue
            for day in hist.day[(hist.week == week) & (hist.day <= through_day)]:
                after = f"{date_of(int(day) + 1)}T{SLATE_COMPLETE_UTC_HOUR:02d}:00:00+00:00"
                until = f"{date_of(int(day) + 1)}T{EARLIEST_TIP_UTC_HOUR:02d}:00:00+00:00"
                stamp = next((s for s in stamps if after <= s < until), None)
                if stamp is None or pid not in by_stamp[stamp]:
                    continue
                nights[pid] += 1
                started[pid] += by_stamp[stamp][pid]
    return {pid: Usage(started[pid], nights[pid]) for pid in nights}


def missed_last_game(panel: SeasonPanel, sleeper_id: str, through_day: int) -> bool:
    """Whether his most recent game on or before ``through_day`` was a DNP.

    A regular who sat out his last game is more likely hurt than rested. The
    designation feed catches some of these, and this catches the rest.
    """
    hist = panel.histories.get(sleeper_id)
    if hist is None:
        return False
    seen = np.nonzero(hist.day <= through_day)[0]
    return bool(len(seen)) and not bool(hist.played[seen[-1]])


def regulars(
    conn: sqlite3.Connection,
    panel: SeasonPanel,
    roster_id: int,
    candidates: list[str],
    *,
    through_day: int,
) -> list[str]:
    """The ``candidates`` who are rotation regulars and played their last game."""
    usage = lineup_usage(conn, panel, roster_id, through_day=through_day)
    return [
        pid
        for pid in candidates
        if pid in usage and usage[pid].regular and not missed_last_game(panel, pid, through_day)
    ]
