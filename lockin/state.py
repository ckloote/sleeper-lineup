"""What has already been banked — observed, not predicted (review finding 2).

With no `--locked`, the digest used to reconstruct the week by replaying its own
policy through yesterday: it assumed last night's recommended locks had been
taken, added them to the banked total, and then left those players out of the
calls it was about to deliver. The actions the digest existed to announce were
consumed by the digest itself.

The matchup poll says what actually happened, one game late. A player's
counted value freezes when he is locked, so once he has played again the poll
shows whether an earlier game was banked: counted equals an earlier game's score
and not the latest one's. That is a **closed window** — decided, and knowable.
Last night's game is the **open window**: counted equals its score whether or
not it was locked, and the poll cannot tell yet. Those are exactly the calls
the digest makes, so nothing is lost by not knowing them — and a user who
already locked one simply ignores that line.

`core.locks.infer_lock` does the reading. It was written for completed weeks
and is applied here to the week so far, where "his final game" means his latest
one.

This is the architecture doc's §10 reading, unverified against a live week
until the season starts: day-one.md's week-1 shadow check compares it with the
locks actually made before the cron is trusted without `--locked`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from lockin.clock import SLATE_COMPLETE_UTC_HOUR
from lockin.core.locks import Game as LockGame
from lockin.core.locks import LockStatus, infer_lock
from lockin.core.policy import Game
from lockin.projections import date_of

SOURCE_SUPPLIED = "supplied"
"""Given on the command line: `--locked`."""
SOURCE_INFERRED = "inferred"
"""Read from a matchup poll taken after last night's games finished."""
SOURCE_ASSUMED = "assumed"
"""Reconstructed from this engine's own policy — replays of past dates only."""
SOURCE_STAND_IN = "stand-in"
"""The opponent, with no usable poll: the greedy base policy stands in."""

UNDECIDED = {LockStatus.RODE_TO_END, LockStatus.SINGLE_GAME, LockStatus.NO_LOCKABLE_GAME}
"""Readings that mean nothing is banked yet: he rode (or locked the game still
in its window, which is the same thing for now), had one game, or was not in a
slot when his games tipped."""

BANKED, OPEN, AMBIGUOUS, UNRESOLVED = "banked", "open", "ambiguous", "unresolved"


def reading(counted: float, seen: list[Game]) -> str:
    """What one starter's poll value says about his week so far.

    ``banked``      he locked an earlier game — which one may be unclear when
                    two scored the same, but the banked value is not
    ``open``        nothing banked; his latest game's window may still be open
    ``ambiguous``   his value matches his latest game *and* an earlier one:
                    locked earlier, or riding, and the poll cannot say which
                    until he plays again. Treated as open, with a warning
    ``unresolved``  no reading of his games explains the value
    """
    inference = infer_lock(
        counted, [LockGame(index=i, played=g.played, score=g.score) for i, g in enumerate(seen)]
    )
    if inference.status is LockStatus.LOCKED_EARLY:
        return BANKED
    if inference.status is LockStatus.AMBIGUOUS:
        return BANKED if inference.locked_early else AMBIGUOUS
    if inference.status in UNDECIDED:
        return OPEN
    return UNRESOLVED


@dataclass(slots=True)
class LockState:
    banked: dict[str, float] = field(default_factory=dict)
    source: str = SOURCE_STAND_IN
    unresolved: list[str] = field(default_factory=list)
    """Starters whose poll value no reading of the games explains."""
    ambiguous: list[str] = field(default_factory=list)
    """Starters who may have locked an earlier game or may be riding: their
    value matches both. Treated as riding; the digest says so."""
    poll_observed_at: str | None = None
    reason: str | None = None
    """Why the poll could not be used, when it could not."""

    @property
    def usable(self) -> bool:
        return self.source in (SOURCE_SUPPLIED, SOURCE_INFERRED) and not self.unresolved


def slate_final_at(known_through: int) -> str:
    """The UTC moment by which the games of ``known_through`` are certainly over."""
    return f"{date_of(known_through + 1)}T{SLATE_COMPLETE_UTC_HOUR:02d}:00:00+00:00"


def latest_poll(
    conn: sqlite3.Connection, week: int, roster_id: int, *, after: str, before: str
) -> tuple[str, dict[str, float | None]] | None:
    """The newest whole poll of a roster-week taken in [after, before), and its starters."""
    stamp = conn.execute(
        "SELECT MAX(observed_at) FROM weekly_matchup_teams"
        " WHERE week = ? AND roster_id = ? AND observed_at >= ? AND observed_at < ?",
        (week, roster_id, after, before),
    ).fetchone()[0]
    if stamp is None:
        return None
    points = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT sleeper_id, counted_points FROM weekly_matchups"
            " WHERE week = ? AND roster_id = ? AND observed_at = ? AND is_starter = 1",
            (week, roster_id, stamp),
        )
    }
    return stamp, points


def infer_state(
    conn: sqlite3.Connection,
    week: int,
    roster_id: int,
    lineup: dict[str, list[Game]],
    known_through: int,
    *,
    before: str,
) -> LockState:
    """Every closed-window lock on this roster, read from its poll.

    ``before`` is the moment the digest runs: a poll taken after it would be
    reading the future, which is also what keeps a replay of a past date from
    using polls gathered after the season.
    """
    poll = latest_poll(conn, week, roster_id, after=slate_final_at(known_through), before=before)
    if poll is None:
        return LockState(
            source="unavailable",
            reason=f"no poll of roster {roster_id} since last night's games finished",
        )
    stamp, points = poll
    state = LockState(source=SOURCE_INFERRED, poll_observed_at=stamp)
    for pid, games in lineup.items():
        seen = [g for g in games if g.day <= known_through]
        if not seen:
            continue  # his week has not started; nothing can be banked
        counted = points.get(pid)
        if counted is None:
            # `players_points` can be sparse (review finding 4). Silence about a
            # player who has only sat is consistent — a DNP cannot be banked —
            # but silence about one who has played is not explained by anything.
            if any(g.played for g in seen):
                state.unresolved.append(pid)
            continue
        verdict = reading(counted, seen)
        if verdict == BANKED:
            state.banked[pid] = counted
        elif verdict == AMBIGUOUS:
            state.ambiguous.append(pid)
        elif verdict == UNRESOLVED:
            state.unresolved.append(pid)
    return state
