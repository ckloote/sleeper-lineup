"""Stopping policies: when to bank a score and when to ride.

Pure. Takes a week's games and a set of thresholds, returns what the week
counted. No simulation happens here — the thresholds arrive precomputed — so a
policy can be replayed and diffed without a projection model in the loop.

The mechanic, from architecture doc §3.1 and §7.6:

- After a player's game finishes you may lock that score, irreversibly, and only
  before his next game tips.
- You may only lock a game he **played**. There is nothing to bank otherwise.
- If you never lock, his **final game of the week** counts — including 0.0 if he
  does not play it.
- He must stay in a starting slot throughout. Benching forfeits the score.

Every policy in the backtest is the same walk over the week under different
thresholds, which is deliberate: it makes the comparison a comparison of
thresholds rather than of three separately-written pieces of logic that might
differ for uninteresting reasons.

    never lock        no threshold ever clears      ride to the end
    lock first        every threshold is -inf       bank the first played game
    greedy threshold  E[value of continuing]        bank when tonight beats it
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Game:
    """One scheduled, countable game in a player's week, in date order.

    Postponed fixtures and exhibitions must already be filtered out: a game that
    did not happen is not the "final game", and treating the All-Star Game as
    one would bank the whole league before the break.
    """

    index: int
    day: int
    played: bool
    score: float
    """Fantasy points. 0.0 when not played, which is what an unlocked slot counts."""


@dataclass(frozen=True, slots=True)
class WeekOutcome:
    counted: float
    locked_index: int | None
    """Which game was banked. None means the week rode to the end."""

    @property
    def zeroed(self) -> bool:
        """Did this slot end up counting nothing?

        The failure the engine exists to prevent — architecture doc §12's
        roster 7 started a player who finished 0.0 and lost the week by 58.
        """
        return self.counted == 0.0


NEVER = None
"""Threshold map meaning 'no threshold ever clears' — ride to the end."""


def replay(games: Sequence[Game], thresholds: Mapping[int, float] | None) -> WeekOutcome:
    """Walk a player's week and return what it counted.

    Locks the first played game whose score strictly exceeds its threshold. A
    threshold of ``-inf`` therefore banks the first played game, and ``None``
    for the whole map never banks at all.
    """
    if not games:
        return WeekOutcome(0.0, None)

    if thresholds is not None:
        for game in games:
            if not game.played:
                continue
            threshold = thresholds.get(game.index)
            if threshold is not None and game.score > threshold:
                return WeekOutcome(game.score, game.index)

    last = games[-1]
    return WeekOutcome(last.score if last.played else 0.0, None)


def continuation_value(paths: np.ndarray) -> float:
    """What the rest of the week is worth if you pass now, played optimally.

    ``paths`` is ``(n_paths, n_remaining)`` — joint draws of the remaining
    games, which must come from a path simulator rather than from independent
    marginals (implementation-plan.md §13).

    Backward induction on the finite horizon:

        V[last] = E[S_last]                    the last game counts regardless
        V[k]    = E[max(S_k, V[k+1])]          bank it or carry on

    The value returned is ``V[0]``, and it is exactly the threshold the current
    game has to beat. Note it is a *scalar*: the resulting policy compares
    tonight's realised score against an unconditional expectation, which is what
    makes this the base heuristic rather than the optimal policy. Conditioning
    the continuation on the state is what Phase 5's rollout adds.
    """
    if paths.ndim != 2:
        raise ValueError(f"expected (n_paths, n_remaining), got shape {paths.shape}")
    if paths.shape[1] == 0:
        # Nothing left to ride. Banking is weakly better than anything, so the
        # threshold is one no score can fail.
        return -math.inf

    value = float(paths[:, -1].mean())
    for k in range(paths.shape[1] - 2, -1, -1):
        value = float(np.maximum(paths[:, k], value).mean())
    return value


def lock_first_thresholds(games: Sequence[Game]) -> dict[int, float]:
    """Bank the first game he plays, whatever it was worth."""
    return {game.index: -math.inf for game in games}


def optimal_stop_value(paths: np.ndarray) -> float:
    """Expected points from playing a whole week optimally under this policy.

    Used to price a week before it starts — the same backward induction as
    :func:`continuation_value`, which is the value of *arriving* at game 0.
    """
    return continuation_value(paths)
