"""Win probability, and the rollout policy that maximises it.

Pure. Takes simulated score samples and returns decisions.

**The objective is P(win), not points** (architecture doc §4). The two agree
early in the week, when the remaining variance is wide and the win-probability
curve is locally linear in margin. They come apart in the last day or two:
trailing badly, the correct policy takes variance and passes on safe scores;
leading comfortably, it banks everything. A points-maximising policy cannot do
either, because it never looks at the opponent.

**Rollout, not exact optimisation.** The true state is (unlocked slots, unlocked
players and their schedules, banked points, opponent belief, day) and backward
induction over it is intractable. Rollout policy improvement (§11) sidesteps
that: at each real decision, estimate V(lock) and V(pass) by simulating the rest
of the week under the cheap Phase 4 base policy, score win probability, and take
the higher. It is guaranteed in expectation to be no worse than the base policy,
which is the property that makes it worth preferring to something cleverer.

**Players are treated as independent within a week.** Measured rather than
assumed: teammates' standardised scores correlate −0.012 across 34,198
same-game pairs, and decomposing the variance of real team totals puts the
within-week component at 0.86 — independence is if anything mildly conservative.
The apparent excess is a *between-week* slate effect, and the simulator is given
each player's actual remaining fixtures, so it models that explicitly rather
than assuming it away. See implementation-plan.md §15.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from lockin.core.policy import continuation_value


def win_probability(mine: np.ndarray, theirs: np.ndarray) -> float:
    """P(my total beats theirs), counting a tie as half a win.

    Sleeper matchups can tie, and a policy that treated a tie as a loss would
    shade its decisions for no reason.
    """
    if len(mine) != len(theirs):
        raise ValueError(f"paired samples required, got {len(mine)} and {len(theirs)}")
    return float((mine > theirs).mean() + 0.5 * (mine == theirs).mean())


def base_policy_thresholds(paths: np.ndarray) -> list[float]:
    """Thresholds the base policy would apply to each game of a simulated week.

    ``thresholds[k]`` is what game *k* must beat: the expected value of riding
    on and stopping optimally from *k+1*. The last game gets ``+inf`` because
    banking it and riding it are the same outcome, so calling it a lock would
    only distort the lock-rate diagnostics.
    """
    horizon = paths.shape[1]
    out = [continuation_value(paths[:, k + 1 :]) for k in range(horizon - 1)]
    return [*out, float("inf")]


def apply_base_policy(paths: np.ndarray, thresholds: Sequence[float]) -> np.ndarray:
    """What each simulated week counts under the base policy. ``(n_paths,)``.

    Banks the first game beating its threshold, otherwise rides to the last —
    the same walk as ``lockin.core.policy.replay``, vectorised over paths.

    A DNP arrives as a 0.0 and cannot be banked by accident: thresholds are
    continuation values and so are never negative, and a score must *exceed*
    its threshold to fire.
    """
    if paths.shape[1] != len(thresholds):
        raise ValueError(f"{paths.shape[1]} games but {len(thresholds)} thresholds")
    counted = paths[:, -1].copy()
    done = np.zeros(len(paths), dtype=bool)
    for k, threshold in enumerate(thresholds):
        fires = ~done & (paths[:, k] > threshold)
        counted[fires] = paths[fires, k]
        done |= fires
    return counted


@dataclass(frozen=True, slots=True)
class RolloutDecision:
    lock: bool
    p_win_lock: float
    p_win_pass: float

    @property
    def edge(self) -> float:
        """Win probability gained by taking the recommended action."""
        return abs(self.p_win_lock - self.p_win_pass)


def evaluate_lock(
    banked: float,
    contributions: np.ndarray,
    player: int,
    lock_value: float,
    opponent: np.ndarray,
) -> RolloutDecision:
    """Should this player's score be banked now?

    ``contributions`` is ``(n_unlocked, n_sims)`` — what each still-unlocked
    starter goes on to count under the base policy, including the one deciding.
    ``opponent`` is ``(n_sims,)`` simulated opponent totals.

    Locking replaces this player's simulated future with the certain
    ``lock_value``. Everything else is held fixed across the two branches,
    including the random draws, so the comparison is paired and the Monte Carlo
    noise largely cancels — which matters, because the edge being measured is
    often under a percentage point.
    """
    others = contributions.sum(axis=0) - contributions[player]
    p_pass = win_probability(banked + others + contributions[player], opponent)
    p_lock = win_probability(banked + others + lock_value, opponent)
    return RolloutDecision(lock=p_lock > p_pass, p_win_lock=p_lock, p_win_pass=p_pass)


def lock_threshold(
    banked: float,
    contributions: np.ndarray,
    player: int,
    opponent: np.ndarray,
) -> float:
    """The score at which banking becomes correct — the standing rule.

    *"Lock Player X tonight if he clears 47."* This is a first-class output
    (§11), not a diagnostic: it is what makes a missed check-in survivable.

    The architecture doc proposes a binary search over hypothetical *S*. It is
    not needed. Writing ``D = opponent − banked − others``, the value of locking
    at *S* is P(S > D), which is the CDF of ``D`` evaluated at *S* and therefore
    increasing in *S*, while the value of passing does not depend on *S* at all.
    The crossing is then exactly the ``p_pass``-quantile of ``D`` — one sort
    instead of a search, and exact rather than converged to a tolerance.
    """
    others = contributions.sum(axis=0) - contributions[player]
    p_pass = win_probability(banked + others + contributions[player], opponent)
    deficit = opponent - banked - others
    if p_pass <= 0.0:
        return float(np.min(deficit))
    if p_pass >= 1.0:
        return float(np.max(deficit))
    return float(np.quantile(deficit, p_pass))
