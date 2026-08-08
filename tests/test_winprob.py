"""Win probability, the rollout decision, and the standing threshold.

The season replay is `lockin backtest`. These pin the properties that make the
rollout worth trusting: that it reduces to the points policy when the opponent
is irrelevant, that it takes variance when behind and banks when ahead, and that
the published threshold is the score at which its own recommendation flips.
"""

from __future__ import annotations

import numpy as np
import pytest

from lockin.core.policy import continuation_value
from lockin.core.winprob import (
    apply_base_policy,
    base_policy_thresholds,
    evaluate_lock,
    lock_threshold,
    win_probability,
)


def constant(value: float, n: int = 4000) -> np.ndarray:
    return np.full(n, float(value))


# ------------------------------------------------------------ win probability


def test_win_probability_counts_a_tie_as_half():
    assert win_probability(constant(10.0), constant(10.0)) == pytest.approx(0.5)
    assert win_probability(constant(11.0), constant(10.0)) == pytest.approx(1.0)
    assert win_probability(constant(9.0), constant(10.0)) == pytest.approx(0.0)


def test_win_probability_requires_paired_samples():
    with pytest.raises(ValueError):
        win_probability(np.zeros(10), np.zeros(11))


# ------------------------------------------------------------- base policy


def test_apply_base_policy_matches_the_stopping_value_it_is_built_from():
    """The vectorised walk must realise the value the recursion promises."""
    rng = np.random.default_rng(1)
    paths = rng.gamma(2.0, 15.0, size=(20000, 3))
    counted = apply_base_policy(paths, base_policy_thresholds(paths))
    assert counted.mean() == pytest.approx(continuation_value(paths), rel=0.02)


def test_apply_base_policy_banks_the_first_game_over_its_threshold():
    paths = np.array([[10.0, 50.0, 5.0], [60.0, 1.0, 2.0]])
    got = apply_base_policy(paths, [20.0, 20.0, float("inf")])
    assert got.tolist() == [50.0, 60.0]


def test_apply_base_policy_rides_when_nothing_clears():
    paths = np.array([[10.0, 12.0, 7.0]])
    assert apply_base_policy(paths, [99.0, 99.0, float("inf")]).tolist() == [7.0]


def test_apply_base_policy_never_banks_a_dnp():
    """A 0.0 must not be lockable — thresholds are never negative."""
    paths = np.array([[0.0, 0.0, 30.0], [0.0, 40.0, 0.0]])
    thresholds = base_policy_thresholds(paths)
    assert all(t >= 0 for t in thresholds[:-1])
    assert apply_base_policy(paths, thresholds).tolist() == [30.0, 40.0]


def test_apply_base_policy_rejects_mismatched_thresholds():
    with pytest.raises(ValueError):
        apply_base_policy(np.zeros((3, 2)), [1.0])


# ------------------------------------------------------------- the decision


def test_locks_when_the_certain_score_beats_the_gamble():
    """Ahead and safe: bank it."""
    decision = evaluate_lock(
        banked=200.0,
        contributions=np.vstack([constant(20.0)]),
        player=0,
        lock_value=60.0,
        opponent=constant(240.0),
    )
    assert decision.lock
    assert decision.p_win_lock == pytest.approx(1.0)
    assert decision.p_win_pass == pytest.approx(0.0)


def test_passes_when_the_certain_score_is_not_enough():
    """Behind by more than the sure thing covers: take the variance.

    This is the decision a points-maximising policy cannot make. Banking 60
    guarantees a loss; riding a coin flip between 10 and 90 wins half the time.
    """
    rng = np.random.default_rng(2)
    gamble = np.where(rng.random(8000) < 0.5, 10.0, 90.0)
    decision = evaluate_lock(
        banked=200.0,
        contributions=np.vstack([gamble]),
        player=0,
        lock_value=60.0,
        opponent=constant(270.0, 8000),
    )
    assert not decision.lock
    assert decision.p_win_lock == pytest.approx(0.0)
    assert decision.p_win_pass == pytest.approx(0.5, abs=0.03)


def test_the_same_score_is_banked_or_ridden_depending_on_the_opponent():
    """The whole point of a win-probability objective.

    Identical player, identical distribution, opposite calls — because one
    opponent is beatable by banking and the other is not.
    """
    rng = np.random.default_rng(3)
    gamble = np.where(rng.random(8000) < 0.5, 10.0, 90.0)
    args = dict(banked=200.0, contributions=np.vstack([gamble]), player=0, lock_value=60.0)
    assert evaluate_lock(**args, opponent=constant(240.0, 8000)).lock
    assert not evaluate_lock(**args, opponent=constant(270.0, 8000)).lock


def test_other_players_contributions_shift_the_call():
    """A teammate still to play changes whether banking is right."""
    rng = np.random.default_rng(4)
    mate = rng.normal(40.0, 30.0, 8000)
    lonely = evaluate_lock(
        banked=180.0,
        contributions=np.vstack([constant(20.0, 8000)]),
        player=0,
        lock_value=55.0,
        opponent=constant(250.0, 8000),
    )
    with_mate = evaluate_lock(
        banked=180.0,
        contributions=np.vstack([constant(20.0, 8000), mate]),
        player=0,
        lock_value=55.0,
        opponent=constant(250.0, 8000),
    )
    assert lonely.p_win_lock != with_mate.p_win_lock


# --------------------------------------------------------------- threshold


def test_threshold_is_the_score_where_the_recommendation_flips():
    """The published rule must agree with the engine that publishes it."""
    rng = np.random.default_rng(5)
    contributions = np.vstack([rng.normal(35.0, 20.0, 6000), rng.normal(30.0, 18.0, 6000)])
    opponent = rng.normal(250.0, 40.0, 6000)
    args = dict(banked=190.0, contributions=contributions, player=0, opponent=opponent)

    crossing = lock_threshold(**args)
    assert not evaluate_lock(**args, lock_value=crossing - 3.0).lock
    assert evaluate_lock(**args, lock_value=crossing + 3.0).lock


def test_threshold_matches_a_brute_force_scan():
    """The closed form must agree with the search the doc proposed."""
    rng = np.random.default_rng(6)
    contributions = np.vstack([rng.normal(35.0, 20.0, 4000)])
    opponent = rng.normal(250.0, 40.0, 4000)
    args = dict(banked=200.0, contributions=contributions, player=0, opponent=opponent)

    grid = np.arange(-20.0, 160.0, 0.5)
    scanned = next(s for s in grid if evaluate_lock(**args, lock_value=float(s)).lock)
    assert lock_threshold(**args) == pytest.approx(scanned, abs=1.0)


def test_threshold_rises_against_a_stronger_opponent():
    """A tougher matchup demands more before banking is worth it."""
    rng = np.random.default_rng(7)
    contributions = np.vstack([rng.normal(35.0, 20.0, 6000)])
    weak = lock_threshold(190.0, contributions, 0, rng.normal(220.0, 35.0, 6000))
    strong = lock_threshold(190.0, contributions, 0, rng.normal(280.0, 35.0, 6000))
    assert strong > weak


def test_threshold_is_finite_when_the_matchup_is_already_decided():
    """Hopeless and won are both real states; neither may produce a NaN."""
    contributions = np.vstack([constant(20.0)])
    hopeless = lock_threshold(0.0, contributions, 0, constant(9999.0))
    won = lock_threshold(9999.0, contributions, 0, constant(0.0))
    assert np.isfinite(hopeless) and np.isfinite(won)
