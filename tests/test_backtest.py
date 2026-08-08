"""The Phase 4 gate's machinery.

The season replay runs through `lockin backtest`. What is tested here is that
each gate would fail when it should — including the leakage guard, which is the
one that matters most: the honest result and the cheating result differ only in
magnitude, and the architecture doc's advice to "suspect leakage" if the gain
looks too large is only enforceable if something is actually measuring it.
"""

from __future__ import annotations

import numpy as np
import pytest

from lockin import backtest
from lockin import backtest as bt_mod
from lockin.backtest import GREEDY, LOCK_FIRST, NEVER_LOCK, ORACLE, BacktestResult, RosterWeek


def make_result(rows: list[dict]) -> BacktestResult:
    out = []
    for i, row in enumerate(rows):
        entry = RosterWeek(
            week=row.get("week", 20),
            roster_id=row.get("roster_id", i % 10 + 1),
            matchup_id=row.get("matchup_id"),
            starters=row.get("starters", 6),
        )
        entry.points = {k: float(v) for k, v in row["points"].items()}
        entry.zeroed = {k: int(v) for k, v in row.get("zeroed", {}).items()}
        entry.locked = {k: int(v) for k, v in row.get("locked", {}).items()}
        out.append(entry)
    return BacktestResult(rows=out)


def spread(mean_by_policy: dict[str, float], n=60, sd=25.0, jitter=3.0, seed=0) -> BacktestResult:
    """n roster-weeks with the given per-policy means.

    ``sd`` is week-to-week noise shared by every policy — the real thing is
    enormous and common, which is why the gate pairs. ``jitter`` is per-policy
    noise; set it to zero when a test needs the policy means to come out
    exactly, and leave it non-zero when a test exercises the paired t.
    """
    rng = np.random.default_rng(seed)
    shared = rng.normal(0, sd, n)
    rows = []
    for i in range(n):
        rows.append(
            {
                "points": {
                    k: v + shared[i] + (rng.normal(0, jitter) if jitter else 0.0)
                    for k, v in mean_by_policy.items()
                },
                "zeroed": {NEVER_LOCK: 1, LOCK_FIRST: 0, GREEDY: 0, ORACLE: 0},
                "locked": {NEVER_LOCK: 0, LOCK_FIRST: 6, GREEDY: 4},
            }
        )
    return make_result(rows)


BASE = {NEVER_LOCK: 190.0, LOCK_FIRST: 225.0, GREEDY: 270.0, ORACLE: 300.0}


# ------------------------------------------------------------------ the gate


def test_gate_passes_on_a_healthy_result():
    assert backtest.check_greedy_beats_never_lock(spread(BASE)).passed


def test_gate_fails_when_greedy_loses_to_never_lock():
    beaten = dict(BASE, **{GREEDY: 180.0})
    check = backtest.check_greedy_beats_never_lock(spread(beaten))
    assert not check.passed
    assert "against never-lock" in check.offenders[0]


def test_gate_fails_when_the_gain_is_indistinguishable_from_noise():
    """A tiny edge over a huge variance is not evidence of anything."""
    rng = np.random.default_rng(3)
    rows = [
        {
            "points": {
                NEVER_LOCK: 200 + rng.normal(0, 60),
                GREEDY: 200 + rng.normal(0, 60),
                LOCK_FIRST: 190.0,
                ORACLE: 300.0,
            }
        }
        for _ in range(40)
    ]
    check = backtest.check_greedy_beats_never_lock(make_result(rows))
    assert not check.passed


def test_gate_fails_when_greedy_cannot_beat_lock_first():
    check = backtest.check_greedy_beats_lock_first(spread(dict(BASE, **{GREEDY: 200.0})))
    assert not check.passed


# --------------------------------------------------------------- leakage guard


def test_foresight_guard_passes_a_policy_short_of_the_oracle():
    """Optimal stopping on iid draws captures ~75% of the headroom.

    (270 - 190) / (300 - 190) = 72.7%, which is where a real threshold policy
    lands and comfortably under the ceiling.
    """
    check = backtest.check_no_foresight(spread(BASE, jitter=0))
    assert check.passed
    assert "72.7%" in check.detail


def test_foresight_guard_catches_a_policy_that_matches_the_oracle():
    """The signature of a backtest that has read the future."""
    check = backtest.check_no_foresight(spread(dict(BASE, **{GREEDY: 300.0}), jitter=0))
    assert not check.passed
    assert "perfect foresight" in check.offenders[0]


def test_foresight_guard_catches_a_policy_that_merely_approaches_it():
    """95% of the headroom is not attainable without looking ahead."""
    check = backtest.check_no_foresight(spread(dict(BASE, **{GREEDY: 295.0}), jitter=0))
    assert not check.passed
    assert "suspect leakage" in check.offenders[0]
    assert "95.5%" in check.offenders[0]


# ----------------------------------------------------------- secondary checks


def test_zeroed_slot_check_fails_when_greedy_zeroes_more():
    result = spread(BASE)
    for row in result.rows:
        row.zeroed = {NEVER_LOCK: 0, LOCK_FIRST: 0, GREEDY: 2, ORACLE: 0}
    check = backtest.check_zeroed_slots(result)
    assert not check.passed


def test_lock_rate_check_catches_a_policy_that_always_locks():
    """Always-locking is lock-first under another name."""
    result = spread(BASE)
    for row in result.rows:
        row.locked = {NEVER_LOCK: 0, LOCK_FIRST: 6, GREEDY: 6}
    assert not backtest.check_lock_rate_is_selective(result).passed


def test_lock_rate_check_catches_a_policy_that_never_locks():
    result = spread(BASE)
    for row in result.rows:
        row.locked = {NEVER_LOCK: 0, LOCK_FIRST: 6, GREEDY: 0}
    assert not backtest.check_lock_rate_is_selective(result).passed


# -------------------------------------------------------------------- wins


def test_wins_flipped_counts_both_sides_of_a_matchup():
    rows = [
        {"week": 20, "roster_id": 1, "matchup_id": 1, "points": {GREEDY: 300.0, NEVER_LOCK: 200.0}},
        {"week": 20, "roster_id": 2, "matchup_id": 1, "points": {GREEDY: 150.0, NEVER_LOCK: 250.0}},
    ]
    wins, games = backtest.wins_flipped(make_result(rows), GREEDY)
    # roster 1 greedy 300 beats roster 2 never-lock 250; roster 2 greedy 150 loses to 200.
    assert (wins, games) == (1, 2)


def test_wins_flipped_ignores_rosters_with_no_matchup():
    """Weeks 23-24 exclude eliminated teams, and week 25 is unscored."""
    rows = [
        {
            "week": 25,
            "roster_id": 1,
            "matchup_id": None,
            "points": {GREEDY: 300.0, NEVER_LOCK: 1.0},
        },
        {
            "week": 25,
            "roster_id": 2,
            "matchup_id": None,
            "points": {GREEDY: 150.0, NEVER_LOCK: 1.0},
        },
    ]
    assert backtest.wins_flipped(make_result(rows), GREEDY) == (0, 0)


# ----------------------------------------------------------------- plumbing


def test_holdout_selects_the_contiguous_block():
    rows = [{"week": w, "points": dict(BASE)} for w in range(1, 26)]
    held = make_result(rows).holdout(18)
    assert [r.week for r in held.rows] == list(range(18, 26))


def test_points_accessor_skips_policies_a_row_lacks():
    rows = [{"points": {GREEDY: 10.0}}, {"points": {GREEDY: 20.0, NEVER_LOCK: 5.0}}]
    result = make_result(rows)
    assert result.points(GREEDY).tolist() == [10.0, 20.0]
    assert result.points(NEVER_LOCK).tolist() == [5.0]


def test_run_requires_something_in_the_holdout():
    result = make_result([{"week": 3, "points": dict(BASE)}])
    with pytest.raises(RuntimeError):
        backtest.run(None, "2025", holdout_from=18, result=result)


# ------------------------------------------------------------ Phase 5 gates

ROLLOUT = bt_mod.ROLLOUT
BASE5 = {NEVER_LOCK: 190.0, LOCK_FIRST: 225.0, GREEDY: 270.0, ROLLOUT: 268.0, ORACLE: 300.0}


def matchup_rows(results: list[tuple[float, float, float]], week=20) -> BacktestResult:
    """One matchup per entry; `results` is (my rollout, my greedy, opponent greedy).

    The opponent row deliberately carries no rollout score. `head_to_head`
    evaluates both sides of a matchup as "me", so giving the opponent one would
    silently add a second observation per matchup and halve the apparent effect.
    """
    rows = []
    for i, (roll, greedy, opp) in enumerate(results):
        rows.append(
            {
                "week": week,
                "roster_id": 2 * i + 1,
                "matchup_id": i + 1,
                "points": {ROLLOUT: roll, GREEDY: greedy, NEVER_LOCK: 100.0, ORACLE: 400.0},
            }
        )
        rows.append(
            {
                "week": week,
                "roster_id": 2 * i + 2,
                "matchup_id": i + 1,
                "points": {GREEDY: opp, NEVER_LOCK: 100.0, ORACLE: 400.0},
            }
        )
    return make_result(rows)


def test_head_to_head_pairs_each_policy_against_the_same_opponent():
    result = matchup_rows([(300.0, 200.0, 250.0)])
    pairs = backtest.head_to_head(result, ROLLOUT, GREEDY)
    # Team 1: rollout 300 > 250 (win), greedy 200 < 250 (loss).
    assert pairs[0].tolist() == [True, False]


def test_mcnemar_uses_only_the_discordant_pairs():
    """Team-weeks where both policies agree carry no information either way."""
    pairs = np.array([[True, False]] * 9 + [[False, True]] * 1 + [[True, True]] * 50)
    b, c, z = backtest.mcnemar(pairs)
    assert (b, c) == (9, 1)
    assert z == pytest.approx(8 / np.sqrt(10))


def test_mcnemar_is_empty_safe():
    assert backtest.mcnemar(np.zeros((0, 2), dtype=bool)) == (0, 0, 0.0)


def test_phase5_gate_fails_when_rollout_loses_on_wins():
    result = matchup_rows([(200.0, 300.0, 250.0)] * 10)
    check = backtest.check_rollout_beats_greedy_on_wins(result)
    assert not check.passed
    assert "rollout wins 0" in check.offenders[0]


def test_phase5_gate_fails_on_a_lead_too_small_to_resolve():
    """A one-week edge is not evidence. The paired test has to say so."""
    result = matchup_rows([(300.0, 200.0, 250.0)] + [(300.0, 300.0, 250.0)] * 9)
    check = backtest.check_rollout_beats_greedy_on_wins(result)
    assert not check.passed
    assert "inconclusive" in check.offenders[0]


def test_phase5_gate_passes_on_a_resolved_lead():
    result = matchup_rows([(300.0, 200.0, 250.0)] * 12 + [(200.0, 300.0, 250.0)] * 2)
    assert backtest.check_rollout_beats_greedy_on_wins(result).passed


def test_holdout_check_is_directional_and_says_so():
    """§7.1 predicted the holdout would be underpowered; the gate admits it."""
    result = matchup_rows([(300.0, 200.0, 250.0), (300.0, 300.0, 250.0)], week=20)
    check = backtest.check_rollout_holdout_direction(result, 18)
    assert check.passed
    assert "too few to resolve significance" in check.detail


def test_holdout_check_fails_if_rollout_actually_loses_there():
    result = matchup_rows([(200.0, 300.0, 250.0)] * 5, week=20)
    assert not backtest.check_rollout_holdout_direction(result, 18).passed


def test_points_sacrifice_is_allowed_but_bounded():
    """Giving up a little is the objective; giving up a lot is a broken opponent model."""
    assert backtest.check_rollout_trades_points_for_wins(spread(BASE5)).passed
    heavy = dict(BASE5, **{ROLLOUT: 230.0})
    check = backtest.check_rollout_trades_points_for_wins(spread(heavy))
    assert not check.passed
    assert "mispriced opponent" in check.offenders[0]
