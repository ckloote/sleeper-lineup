"""Stopping policies and slot assignment.

The season-wide comparison lives in `lockin backtest`. These tests pin the
mechanics it rests on: that the backward induction is the recursion it claims to
be, that the week walk honours the lock-in rules, and that the assignment is
actually optimal rather than merely plausible.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from lockin.core.eligibility import NoValidLineup, assign_slots
from lockin.core.policy import (
    Game,
    continuation_value,
    lock_first_thresholds,
    replay,
)

SLOTS = ["PG", "G", "F", "C", "UTIL", "UTIL"]


def week(*specs: tuple[bool, float]) -> list[Game]:
    """Build a week from (played, score) pairs, one game every other day."""
    return [
        Game(index=i, day=739000 + 2 * i, played=played, score=score)
        for i, (played, score) in enumerate(specs)
    ]


# ------------------------------------------------------------ backward induction


def test_continuation_value_matches_the_recursion_by_hand():
    """V[last] = E[S_last]; V[k] = E[max(S_k, V[k+1])]."""
    paths = np.array([[10.0, 20.0], [30.0, 5.0]])
    # V[1] = (20 + 5)/2 = 12.5 ; V[0] = (max(10,12.5) + max(30,12.5))/2 = 21.25
    assert continuation_value(paths) == pytest.approx(21.25)


def test_continuation_value_of_a_single_game_is_its_mean():
    """The last game counts whether or not you bank it, so there is no choice."""
    paths = np.array([[10.0], [30.0], [20.0]])
    assert continuation_value(paths) == pytest.approx(20.0)


def test_continuation_value_with_nothing_left_never_blocks_a_lock():
    assert continuation_value(np.zeros((5, 0))) == -math.inf


def test_continuation_value_is_between_the_last_game_and_the_best_game():
    """The two bounds that make it a stopping value rather than either extreme.

    It must beat taking the final game blind, and it cannot reach the best game,
    which needs foresight. This is the invariant the Phase 4 leakage guard leans
    on.
    """
    rng = np.random.default_rng(3)
    paths = rng.gamma(2.0, 15.0, size=(4000, 4))
    value = continuation_value(paths)
    assert paths[:, -1].mean() < value < paths.max(axis=1).mean()


def test_continuation_value_rejects_a_one_dimensional_sample():
    with pytest.raises(ValueError):
        continuation_value(np.zeros(10))


# -------------------------------------------------------------------- replay


def test_never_lock_takes_the_final_game():
    games = week((True, 50.0), (True, 10.0))
    assert replay(games, None).counted == 10.0


def test_never_lock_counts_zero_when_the_final_game_is_a_dnp():
    """The disaster the engine exists to prevent: a big week thrown away."""
    games = week((True, 61.0), (True, 47.5), (False, 0.0))
    outcome = replay(games, None)
    assert outcome.counted == 0.0
    assert outcome.zeroed
    assert outcome.locked_index is None


def test_lock_first_takes_the_first_played_game():
    games = week((True, 12.0), (True, 60.0))
    outcome = replay(games, lock_first_thresholds(games))
    assert outcome.counted == 12.0
    assert outcome.locked_index == 0


def test_lock_first_skips_a_dnp_because_there_is_nothing_to_bank():
    games = week((False, 0.0), (True, 33.0), (True, 5.0))
    assert replay(games, lock_first_thresholds(games)).counted == 33.0


def test_a_threshold_only_fires_when_the_score_clears_it():
    games = week((True, 40.0), (True, 12.0))
    assert replay(games, {0: 50.0}).counted == 12.0  # did not clear; rode on
    assert replay(games, {0: 30.0}).counted == 40.0  # cleared; banked


def test_a_week_of_nothing_but_dnps_counts_zero():
    assert replay(week((False, 0.0), (False, 0.0)), None).counted == 0.0


def test_an_empty_week_counts_zero():
    assert replay([], None).counted == 0.0


def test_thresholds_on_unplayed_games_are_ignored():
    """You cannot bank a game he did not play, whatever the threshold says."""
    games = week((False, 0.0), (True, 20.0))
    assert replay(games, {0: -math.inf, 1: -math.inf}).locked_index == 1


# ---------------------------------------------------------------- assignment


def test_assignment_beats_the_greedy_trap():
    """Greedy fills UTIL with the best player left and strands the only centre.

    C accepts only C/PF. Taking the 50-value centre for UTIL would leave C
    unfillable or force a much worse body into it.
    """
    positions = {
        "guard_a": ["PG"],
        "guard_b": ["SG"],
        "wing": ["SF"],
        "centre": ["C"],
        "big": ["PF"],
        "flex": ["PG", "SG"],
    }
    values = {"guard_a": 10, "guard_b": 20, "wing": 30, "centre": 50, "big": 25, "flex": 40}
    out = assign_slots(SLOTS, list(positions), positions, values)
    assert out["C"] == "centre"
    assert set(out.values()) == set(positions)


def test_assignment_maximises_total_value():
    rng = np.random.default_rng(11)
    pool = {f"p{i}": [rng.choice(["PG", "SG", "SF", "PF", "C"])] for i in range(9)}
    values = {p: float(rng.uniform(0, 60)) for p in pool}
    out = assign_slots(SLOTS, list(pool), pool, values)
    got = sum(values[p] for p in out.values())

    # Brute force over every legal lineup of six from nine.
    import itertools

    from lockin.core.eligibility import eligible

    best = 0.0
    for chosen in itertools.permutations(pool, len(SLOTS)):
        if all(eligible(s, pool[p], sleeper_id=p) for s, p in zip(SLOTS, chosen, strict=True)):
            best = max(best, sum(values[p] for p in chosen))
    assert got == pytest.approx(best)


def test_duplicate_slots_keep_separate_identities():
    """Two UTILs must both be filled; a name-keyed dict would drop one."""
    positions = {f"p{i}": ["PG", "SG", "SF", "PF", "C"] for i in range(6)}
    out = assign_slots(SLOTS, list(positions), positions, dict.fromkeys(positions, 1.0))
    assert set(out) == {"PG", "G", "F", "C", "UTIL#1", "UTIL#2"}
    assert len(set(out.values())) == 6


def test_locked_players_keep_their_slot():
    """Under the lock-in rule a banked player may not be moved (§7.6)."""
    positions = {f"p{i}": ["PG", "SG", "SF", "PF", "C"] for i in range(6)}
    values = {p: float(i) for i, p in enumerate(positions)}
    out = assign_slots(SLOTS, list(positions), positions, values, locked={"C": "p0"})
    assert out["C"] == "p0"
    assert sorted(out.values()) == sorted(positions)


def test_assignment_refuses_when_no_legal_lineup_exists():
    positions = {f"g{i}": ["PG"] for i in range(6)}  # nobody can fill C or F
    with pytest.raises(NoValidLineup):
        assign_slots(SLOTS, list(positions), positions, dict.fromkeys(positions, 1.0))


def test_assignment_refuses_when_too_few_players():
    positions = {"a": ["PG"], "b": ["C"]}
    with pytest.raises(NoValidLineup):
        assign_slots(SLOTS, list(positions), positions, dict.fromkeys(positions, 1.0))


def test_assignment_honours_the_observed_extra_slots():
    """Amen Thompson is listed PG/SG but Sleeper let him start at F."""
    positions = {
        "2574": ["PG", "SG"],
        "a": ["PG"],
        "b": ["SG"],
        "c": ["C"],
        "d": ["PF"],
        "e": ["SF"],
    }
    values = {"2574": 100.0, "a": 1, "b": 1, "c": 1, "d": 1, "e": 1}
    out = assign_slots(SLOTS, list(positions), positions, values)
    assert set(out.values()) == set(positions)
