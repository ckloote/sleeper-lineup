"""Replaying a roster-week under the rollout policy.

The database-facing half of Phase 5. ``lockin.core.winprob`` decides a single
lock given samples; this assembles the samples, walks the week in order, and
keeps the state the decisions depend on.

Why a week cannot be replayed player by player, as Phase 4 did: with a points
objective each starter's stopping problem is separable, because points add. With
a **win-probability** objective they are not. Whether to bank a 40 depends on
what the rest of the team has already banked and on what the opponent is likely
to finish with, so the week has to be walked as one chronological process with
shared state.

Decisions are taken **once a day**, at the end of the day, over every starter who
played that day and still has games left. That is not a simplification of the
mechanic — it is the product. The tool exists to be read once a day, and a policy
that assumed intra-day check-ins would be measuring something nobody will do.

**Two dates, not one.** The backtest only ever asks about the end of a day that
has finished, where "what is known" and "what is past banking" are the same
instant. Phase 6's digest is read in the *morning*, and there they come apart:
tonight is unobserved but still bankable, and §7.2 requires the nights between
now and a forward threshold be assumed idle. Every simulation entry point
therefore takes ``known_through`` (observed through here) and ``act_from`` (first
night a lock may be taken) rather than a single ``after_day``. Where they
coincide the behaviour is exactly the Phase 5 behaviour, which is pinned by
``tests/test_digest.py::test_backtest_cutoffs_are_unchanged``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from lockin.core.policy import Game, replay
from lockin.core.projections import EWMAProjectionSource, InsufficientHistory
from lockin.core.winprob import (
    RolloutDecision,
    apply_base_policy,
    base_policy_thresholds,
    evaluate_lock,
    lock_threshold,
)


@dataclass(slots=True)
class SimulationCache:
    """Path draws and base-policy thresholds, keyed by (player, cutoff).

    A decision day asks about a dozen players at the same cutoff, and every
    starter deciding that day asks about the same dozen. Without this the same
    simulation is run six times over.
    """

    source: EWMAProjectionSource
    n_sims: int
    dnp_scale: dict[int, float] = field(default_factory=dict)
    """Per-week hazard correction for started players, fit on prior weeks only.
    See ``lockin.backtest.starter_dnp_scale`` for why it is needed."""
    contributions: dict[tuple[str, int, int], np.ndarray] = field(default_factory=dict)
    thresholds: dict[tuple[str, int], dict[int, float]] = field(default_factory=dict)
    misses: int = 0

    def contribution(
        self,
        sleeper_id: str,
        games: Sequence[Game],
        week: int,
        rng: np.random.Generator,
        *,
        known_through: int,
        act_from: int | None = None,
    ) -> np.ndarray:
        """What this player goes on to count from here. ``(n_sims,)``.

        Two dates, and keeping them apart is the point of the signature:

        ``known_through``
            The last day whose games are **observed**. Everything after it is
            simulated, conditioned on the history up to it.
        ``act_from``
            The first night a lock may be taken. Games before it are ridden
            through: they cannot be banked, and under the one-game-counts rule
            an unbanked game is forfeited. Defaults to ``known_through + 1`` —
            act at the first opportunity, which is the Phase 4-5 base policy.

        In the backtest the two always coincide, because decisions are taken at
        the end of a day: the day being decided is both the last day observed
        and the last day forfeited, and passing ``after_day`` for both was
        therefore correct. A **morning** digest breaks that. Tonight's games
        have not tipped, so ``known_through`` is yesterday, while the night a
        standing rule applies to may be tonight or later — and §7.2 requires
        the nights in between be assumed idle. Collapsing the two would read a
        score out of a game that has not been played.
        """
        act_from = known_through + 1 if act_from is None else act_from
        key = (sleeper_id, known_through, act_from)
        cached = self.contributions.get(key)
        if cached is not None:
            return cached

        remaining = [g for g in games if g.day > known_through]
        if not remaining:
            # Nothing left to decide: an unlocked player counts his final game,
            # which has already happened and may well be a 0.0.
            last = games[-1]
            value = np.full(self.n_sims, last.score if last.played else 0.0)
        else:
            try:
                paths = self.source.project_path(
                    sleeper_id,
                    known_through + 1,
                    [g.day for g in remaining],
                    [week] * len(remaining),
                    rng=rng,
                    n_paths=self.n_sims,
                    dnp_scale=self.dnp_scale.get(week, 1.0),
                )
            except InsufficientHistory:
                # No basis to project him; assume he rides, which is what
                # happens by default anyway.
                last = games[-1]
                value = np.full(self.n_sims, last.score if last.played else 0.0)
            else:
                # An idle night is a threshold no score can clear. Masking them
                # leaves the rest of the base policy untouched: each surviving
                # threshold is a continuation value over strictly later columns,
                # which the mask does not reach.
                thresholds = [
                    float("inf") if game.day < act_from else threshold
                    for game, threshold in zip(
                        remaining, base_policy_thresholds(paths), strict=True
                    )
                ]
                value = apply_base_policy(paths, thresholds)
                self.misses += 1
        self.contributions[key] = value
        return value


@dataclass(slots=True)
class WeekResult:
    total: float
    counted: dict[str, float]
    locked: dict[str, float]
    decisions: int = 0
    """Lock/pass calls actually faced — the denominator for a lock rate."""


def decision_days(lineup: dict[str, list[Game]]) -> list[int]:
    return sorted({g.day for games in lineup.values() for g in games if g.played})


def opponent_totals(
    lineup: dict[str, list[Game]],
    greedy_thresholds: dict[str, dict[int, float]],
    week: int,
    known_through: int,
    cache: SimulationCache,
    rng: np.random.Generator,
) -> np.ndarray:
    """Simulated final totals for the opposing team. ``(n_sims,)``.

    The opponent is assumed to play the Phase 4 base policy. For games that have
    already happened the assumption is *resolved*, not sampled: their thresholds
    are known, so whether the policy would have banked is determined, and a
    banked player is a constant from then on. Only the genuinely unresolved part
    is simulated.

    §7.2's idle-nights assumption is deliberately **not** applied here. It is a
    statement about the nights *you* might miss, and imposing it on the opponent
    would lower his simulated total — pessimism about yourself and optimism
    about him, which is not conservatism, just a thumb on the scale.

    In-season this is where §10's latent lock belief goes — a frozen
    ``players_points`` reveals a lock one game later, sharpened by the manager's
    fitted tendency. Retrospectively there is nothing to infer from, and §12
    makes the recorded lock field unreliable anyway, so a policy stands in for
    the belief.
    """
    total = np.zeros(cache.n_sims)
    for sleeper_id, games in lineup.items():
        past = [g for g in games if g.day <= known_through]
        outcome = replay(past, greedy_thresholds.get(sleeper_id, {})) if past else None
        if outcome is not None and outcome.locked_index is not None:
            total += outcome.counted  # already banked; a constant from here
        else:
            total += cache.contribution(sleeper_id, games, week, rng, known_through=known_through)
    return total


def walk_locks(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    cache: SimulationCache,
    rng: np.random.Generator,
    *,
    through_day: int | None = None,
) -> tuple[dict[str, float], int]:
    """Take every lock the rollout policy would take, day by day.

    Returns the banked scores and how many calls were faced. ``through_day``
    stops the walk after that day, which is how the digest reconstructs the
    state a week is *currently* in without also asserting how it ends. Reading
    the whole week and then discarding the tail would be the same computation
    only if nothing downstream saw the discarded part, and that is exactly the
    kind of thing that stops being true after one edit.
    """
    locked: dict[str, float] = {}
    decisions = 0

    for day in decision_days(lineup):
        if through_day is not None and day > through_day:
            break
        # Highest score first: the strongest lock candidate is judged against a
        # state nothing else has disturbed yet. Any fixed order is defensible —
        # rollout is a heuristic — but an arbitrary one would be harder to read.
        todays = sorted(
            (
                (next(g.score for g in games if g.day == day and g.played), pid)
                for pid, games in lineup.items()
                if pid not in locked
                and any(g.day == day and g.played for g in games)
                and any(g.day > day for g in games)
            ),
            reverse=True,
        )
        if not todays:
            continue

        opponent = opponent_totals(opponent_lineup, opponent_thresholds, week, day, cache, rng)

        for score, sleeper_id in todays:
            unlocked = [pid for pid in lineup if pid not in locked]
            contributions = np.vstack(
                [
                    cache.contribution(pid, lineup[pid], week, rng, known_through=day)
                    for pid in unlocked
                ]
            )
            decision = evaluate_lock(
                banked=sum(locked.values()),
                contributions=contributions,
                player=unlocked.index(sleeper_id),
                lock_value=score,
                opponent=opponent,
            )
            decisions += 1
            if decision.lock:
                locked[sleeper_id] = score

    return locked, decisions


def replay_week(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    cache: SimulationCache,
    rng: np.random.Generator,
) -> WeekResult:
    """Walk one roster-week under the rollout policy and return what it scored."""
    locked, decisions = walk_locks(lineup, opponent_lineup, opponent_thresholds, week, cache, rng)
    counted = dict(locked)
    for sleeper_id, games in lineup.items():
        if sleeper_id not in counted:
            last = games[-1]
            counted[sleeper_id] = last.score if last.played else 0.0
    return WeekResult(
        total=float(sum(counted.values())),
        counted=counted,
        locked=dict(locked),
        decisions=decisions,
    )


def standing_thresholds(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    night: int,
    locked: dict[str, float],
    cache: SimulationCache,
    rng: np.random.Generator,
    *,
    known_through: int,
) -> dict[str, float]:
    """ "Lock him if he clears X on ``night``", for every starter playing then.

    The first-class output of §11 — what makes a missed check-in survivable —
    and the reason :meth:`SimulationCache.contribution` carries two dates.

    ``known_through`` is the last day whose games are observed. For a digest
    read in the morning it is *yesterday*, so tonight is simulated like any
    other future night. Passing ``night`` for both, as the end-of-day backtest
    can, would resolve tonight's games out of scores that do not exist yet.

    Per §7.2, nights strictly between ``known_through`` and ``night`` are
    assumed **idle**: a threshold for Thursday that quietly assumed you acted on
    Tuesday's is wrong exactly when you needed it. From ``night`` onwards the
    base policy resumes, because a night you are reading a threshold for is a
    night you are checking in.

    The asymmetry that makes this correct: every other starter is priced with
    ``act_from=night``, since his game that night is still live and bankable.
    The player being asked about is priced with ``act_from=night + 1``, because
    the threshold *is* the question of whether to bank that game — the pass
    branch has to be his continuation from the following day, not a value that
    silently re-banks the score under test.
    """
    opponent = opponent_totals(
        opponent_lineup, opponent_thresholds, week, known_through, cache, rng
    )
    unlocked = [pid for pid in lineup if pid not in locked]
    if not unlocked:
        return {}
    banked = sum(locked.values())
    # Teammates: their `night` games are live, so the base policy may bank them.
    teammate = {
        pid: cache.contribution(
            pid, lineup[pid], week, rng, known_through=known_through, act_from=night
        )
        for pid in unlocked
    }
    out: dict[str, float] = {}
    for pid in unlocked:
        games = lineup[pid]
        if not any(g.day == night for g in games) or not any(g.day > night for g in games):
            continue  # not playing that night, or nothing left to ride for
        contributions = np.vstack(
            [
                cache.contribution(
                    other,
                    lineup[other],
                    week,
                    rng,
                    known_through=known_through,
                    act_from=night + 1,
                )
                if other == pid
                else teammate[other]
                for other in unlocked
            ]
        )
        out[pid] = lock_threshold(
            banked=banked,
            contributions=contributions,
            player=unlocked.index(pid),
            opponent=opponent,
        )
    return out


def decision_for(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    day: int,
    sleeper_id: str,
    score: float,
    locked: dict[str, float],
    cache: SimulationCache,
    rng: np.random.Generator,
) -> RolloutDecision:
    """One lock/pass call, exposed for the digest and for explanation.

    ``day`` is the night the game was played, and the call is taken after it, so
    here the two dates genuinely do coincide: everything through ``day`` is
    observed, and everything through ``day`` is past banking. This is the
    end-of-day case :func:`standing_thresholds` had to generalise, not a
    simplification of it.
    """
    opponent = opponent_totals(opponent_lineup, opponent_thresholds, week, day, cache, rng)
    unlocked = [pid for pid in lineup if pid not in locked]
    contributions = np.vstack(
        [cache.contribution(pid, lineup[pid], week, rng, known_through=day) for pid in unlocked]
    )
    return evaluate_lock(
        banked=sum(locked.values()),
        contributions=contributions,
        player=unlocked.index(sleeper_id),
        lock_value=score,
        opponent=opponent,
    )
