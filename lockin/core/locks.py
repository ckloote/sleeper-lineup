"""Recovering lock decisions from counted scores.

Pure. Given what a player actually counted for a week and what each of his games
was worth, work out which game counted — and therefore whether his manager
locked early or rode to the end.

This is the highest-leverage plumbing in the project. It recovers every lock
decision by every manager across the season, which supplies both the human
baseline the backtest is measured against and the per-manager tendency profile
that sharpens live win-probability estimates (architecture doc §10).

The mechanic, from architecture doc §3.1:

- Locking fixes a player's score after a game completes, before his next tips.
- If never locked, his FINAL game of the week counts — including 0.0 if he does
  not play it.

So the counted value identifies the game. Half-point scoring granularity makes
collisions rare; where they happen the case is flagged, never guessed.

Three fixture facts feed in from Phase 0, and all three change the answer:

- Postponed fixtures are excluded. They never happened, so they are not the
  "final game" and do not zero anybody.
- The All-Star Game is excluded. It carries real stats but does not count, and
  it falls at the end of a week.
- An unplayed REAL game is not excluded. It is a genuine 0.0 for an unlocked
  starter, which is exactly how a week gets thrown away.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Scores are in half-points, so anything closer than this is the same number.
TOLERANCE = 0.005


class LockStatus(StrEnum):
    LOCKED_EARLY = "locked_early"
    """Counted a game before the last: an unambiguous, deliberate lock."""

    RODE_TO_END = "rode_to_end"
    """Counted the final game's outcome. Either never locked, or locked on the
    last game — outcome-identical and indistinguishable, and the distinction
    does not affect the score."""

    SINGLE_GAME = "single_game"
    """Exactly one game scheduled. Lock and pass are outcome-equivalent, so
    there was no decision to make (architecture doc §15.3)."""

    NO_GAMES = "no_games"
    """No games scheduled. The slot scores 0.0 whoever fills it."""

    NO_LOCKABLE_GAME = "no_lockable_game"
    """Counted 0.0 while the final game was played for a nonzero score — the
    player was not in a starting slot when his games tipped."""

    AMBIGUOUS = "ambiguous"
    """Several games share the counted value. Flagged rather than guessed."""

    UNRESOLVED = "unresolved"
    """The counted value matches no game. Indicates a bug or a data problem,
    never a legitimate outcome."""


@dataclass(frozen=True, slots=True)
class Game:
    """One scheduled, non-excluded game in a player's week, in date order."""

    index: int
    """0-based ordinal within this player's filtered, date-ordered week."""
    played: bool
    score: float
    """Fantasy points. 0.0 when not played — which is what the slot counts."""


@dataclass(frozen=True, slots=True)
class LockInference:
    status: LockStatus
    n_games: int
    matched_index: int | None
    """Which game's score counted. None when undetermined."""
    candidates: tuple[int, ...]
    confidence: float
    locked_early: bool | None
    """True if the manager demonstrably locked before the week ended. None when
    undetermined. Used for tendency profiling, so an ambiguous case must not
    silently read as False."""


def _ride_score(games: list[Game]) -> float:
    """What the player counts having never locked: his final game, 0.0 if unplayed."""
    last = games[-1]
    return last.score if last.played else 0.0


def infer_lock(counted: float, games: list[Game], *, tolerance: float = TOLERANCE) -> LockInference:
    """Recover which game produced `counted`.

    `games` must already exclude postponed fixtures and exhibitions, and must be
    in date order.
    """
    n = len(games)
    if n == 0:
        return LockInference(LockStatus.NO_GAMES, 0, None, (), 1.0, None)

    candidates = tuple(g.index for g in games if g.played and abs(g.score - counted) <= tolerance)
    rides = abs(counted - _ride_score(games)) <= tolerance
    last_index = games[-1].index

    if n == 1:
        # Nothing to decide: the one game counts whether or not it was locked.
        return LockInference(LockStatus.SINGLE_GAME, n, games[0].index, candidates, 1.0, None)

    if rides:
        # Riding explains it. An earlier game with the identical score would too,
        # and the score is the same either way — but "locked early" vs "rode" is
        # exactly what the tendency profile measures, so do not assume.
        earlier = tuple(i for i in candidates if i != last_index)
        if earlier:
            return LockInference(
                LockStatus.AMBIGUOUS,
                n,
                last_index,
                candidates,
                1.0 / (len(earlier) + 1),
                None,
            )
        return LockInference(LockStatus.RODE_TO_END, n, last_index, candidates, 1.0, False)

    if not candidates:
        # Counted 0.0 but the week did not end in a zero: the player held a slot
        # at week's end without having occupied one when his games tipped.
        if abs(counted) <= tolerance:
            return LockInference(LockStatus.NO_LOCKABLE_GAME, n, None, (), 1.0, None)
        return LockInference(LockStatus.UNRESOLVED, n, None, (), 0.0, None)

    if len(candidates) == 1:
        return LockInference(LockStatus.LOCKED_EARLY, n, candidates[0], candidates, 1.0, True)

    # Several games share the value. The manager definitely locked (riding does
    # not explain it), but not which game.
    return LockInference(LockStatus.AMBIGUOUS, n, None, candidates, 1.0 / len(candidates), True)


RESOLVED_STATUSES = frozenset(
    {
        LockStatus.LOCKED_EARLY,
        LockStatus.RODE_TO_END,
        LockStatus.SINGLE_GAME,
        LockStatus.NO_GAMES,
        LockStatus.NO_LOCKABLE_GAME,
    }
)


def is_resolved(inference: LockInference) -> bool:
    """Did we determine what happened, at full confidence?"""
    return inference.status in RESOLVED_STATUSES and inference.confidence >= 1.0


@dataclass(frozen=True, slots=True)
class ManagerProfile:
    """How a manager plays the stopping problem.

    The architecture doc wants "locks early and safe vs. rides to Sunday" as a
    per-manager trait, because it sharpens the opponent belief state in-season.
    """

    roster_id: int
    decisions: int
    """Player-weeks where a real choice existed: two or more games."""
    locked_early: int
    rode_to_end: int
    mean_lock_position: float | None
    """Average position in the week of the game that counted, 0.0 (first game)
    to 1.0 (last). Low means banks early."""

    @property
    def lock_rate(self) -> float:
        return self.locked_early / self.decisions if self.decisions else 0.0


def profile_manager(roster_id: int, inferences: list[LockInference]) -> ManagerProfile:
    """Summarise one manager's stopping behaviour.

    Only player-weeks with a genuine choice count. Single-game and no-game weeks
    carry no information about tendency and would dilute it toward zero.
    """
    decisions = [inf for inf in inferences if inf.n_games >= 2 and inf.locked_early is not None]
    early = sum(1 for inf in decisions if inf.locked_early)
    rode = sum(1 for inf in decisions if not inf.locked_early)

    positioned = [
        inf.matched_index / (inf.n_games - 1)
        for inf in decisions
        if inf.matched_index is not None and inf.n_games >= 2
    ]
    mean_pos = sum(positioned) / len(positioned) if positioned else None

    return ManagerProfile(
        roster_id=roster_id,
        decisions=len(decisions),
        locked_early=early,
        rode_to_end=rode,
        mean_lock_position=mean_pos,
    )
