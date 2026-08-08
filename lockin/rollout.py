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
    contributions: dict[tuple[str, int], np.ndarray] = field(default_factory=dict)
    thresholds: dict[tuple[str, int], dict[int, float]] = field(default_factory=dict)
    misses: int = 0

    def contribution(
        self,
        sleeper_id: str,
        games: Sequence[Game],
        week: int,
        after_day: int,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """What this player goes on to count, given the week so far. ``(n_sims,)``.

        Games at or before ``after_day`` are gone: under the lock-in mechanic a
        score can only be banked before the player's next game tips, so passing
        on it forfeits it. What remains is the games after the cutoff, played
        out under the base policy.
        """
        key = (sleeper_id, after_day)
        cached = self.contributions.get(key)
        if cached is not None:
            return cached

        remaining = [g for g in games if g.day > after_day]
        if not remaining:
            # Nothing left to decide: an unlocked player counts his final game,
            # which has already happened and may well be a 0.0.
            last = games[-1]
            value = np.full(self.n_sims, last.score if last.played else 0.0)
        else:
            try:
                paths = self.source.project_path(
                    sleeper_id,
                    after_day + 1,
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
                value = apply_base_policy(paths, base_policy_thresholds(paths))
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
    after_day: int,
    cache: SimulationCache,
    rng: np.random.Generator,
) -> np.ndarray:
    """Simulated final totals for the opposing team. ``(n_sims,)``.

    The opponent is assumed to play the Phase 4 base policy. For games that have
    already happened the assumption is *resolved*, not sampled: their thresholds
    are known, so whether the policy would have banked is determined, and a
    banked player is a constant from then on. Only the genuinely unresolved part
    is simulated.

    In-season this is where §10's latent lock belief goes — a frozen
    ``players_points`` reveals a lock one game later, sharpened by the manager's
    fitted tendency. Retrospectively there is nothing to infer from, and §12
    makes the recorded lock field unreliable anyway, so a policy stands in for
    the belief.
    """
    total = np.zeros(cache.n_sims)
    for sleeper_id, games in lineup.items():
        past = [g for g in games if g.day <= after_day]
        outcome = replay(past, greedy_thresholds.get(sleeper_id, {})) if past else None
        if outcome is not None and outcome.locked_index is not None:
            total += outcome.counted  # already banked; a constant from here
        else:
            total += cache.contribution(sleeper_id, games, week, after_day, rng)
    return total


def replay_week(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    cache: SimulationCache,
    rng: np.random.Generator,
) -> WeekResult:
    """Walk one roster-week under the rollout policy and return what it scored."""
    locked: dict[str, float] = {}
    decisions = 0

    for day in decision_days(lineup):
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
                [cache.contribution(pid, lineup[pid], week, day, rng) for pid in unlocked]
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
    day: int,
    locked: dict[str, float],
    cache: SimulationCache,
    rng: np.random.Generator,
) -> dict[str, float]:
    """ "Lock him tonight if he clears X", for every starter playing on ``day``.

    The first-class output of §11 — what makes a missed check-in survivable.
    Per §7.2 these are computed assuming **no action on the intervening
    nights**, which is the pessimistic assumption and the only one worth
    printing: a threshold that quietly assumed you followed yesterday's advice
    is wrong exactly when you needed it.
    """
    opponent = opponent_totals(opponent_lineup, opponent_thresholds, week, day, cache, rng)
    unlocked = [pid for pid in lineup if pid not in locked]
    if not unlocked:
        return {}
    contributions = np.vstack(
        [cache.contribution(pid, lineup[pid], week, day, rng) for pid in unlocked]
    )
    out: dict[str, float] = {}
    for pid in unlocked:
        games = lineup[pid]
        if not any(g.day == day for g in games) or not any(g.day > day for g in games):
            continue  # not playing tonight, or nothing left to ride for
        out[pid] = lock_threshold(
            banked=sum(locked.values()),
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
    """One lock/pass call, exposed for the digest and for explanation."""
    opponent = opponent_totals(opponent_lineup, opponent_thresholds, week, day, cache, rng)
    unlocked = [pid for pid in lineup if pid not in locked]
    contributions = np.vstack(
        [cache.contribution(pid, lineup[pid], week, day, rng) for pid in unlocked]
    )
    return evaluate_lock(
        banked=sum(locked.values()),
        contributions=contributions,
        player=unlocked.index(sleeper_id),
        lock_value=score,
        opponent=opponent,
    )
