"""Manager decision quality.

The season-wide ranking is `lockin managers`. These pin the scoring rules it
rests on — above all that a *correct* variance-taking decision is scored as
correct, which is the entire reason this metric exists alongside points capture.
"""

from __future__ import annotations

import numpy as np
import pytest

from lockin.core.policy import Game
from lockin.managers import Decision, ManagerReport, _upside


def decision(**kw) -> Decision:
    base = dict(
        week=20,
        roster_id=1,
        sleeper_id="p",
        score=45.0,
        chose_lock=True,
        p_win_lock=0.6,
        p_win_pass=0.4,
        greedy_locks=True,
    )
    return Decision(**{**base, **kw})


def week(*specs: tuple[bool, float]) -> list[Game]:
    return [Game(index=i, day=739000 + 2 * i, played=p, score=s) for i, (p, s) in enumerate(specs)]


# ------------------------------------------------------------------- regret


def test_taking_the_better_side_costs_nothing():
    assert decision(chose_lock=True, p_win_lock=0.6, p_win_pass=0.4).regret == pytest.approx(0.0)
    assert decision(chose_lock=False, p_win_lock=0.3, p_win_pass=0.5).regret == pytest.approx(0.0)


def test_taking_the_worse_side_costs_the_difference():
    d = decision(chose_lock=True, p_win_lock=0.30, p_win_pass=0.55)
    assert d.regret == pytest.approx(0.25)
    assert not d.right


def test_passing_while_far_behind_is_scored_as_correct():
    """The objection this whole metric exists to answer.

    Down badly, banking a safe score still loses. Declining it and riding the
    variance is the right play — and a points metric would call it a blunder.
    """
    d = decision(chose_lock=False, p_win_lock=0.02, p_win_pass=0.18, greedy_locks=True)
    assert d.right
    assert d.regret == pytest.approx(0.0)
    assert d.divergent, "points says bank, win probability says ride"


def test_banking_while_comfortably_ahead_is_scored_as_correct():
    d = decision(chose_lock=True, p_win_lock=0.95, p_win_pass=0.80, greedy_locks=False)
    assert d.right
    assert d.divergent


def test_divergence_is_about_the_two_objectives_not_the_manager():
    """A manager's own choice must not affect whether a decision is high leverage."""
    for chose in (True, False):
        assert decision(
            chose_lock=chose, p_win_lock=0.7, p_win_pass=0.3, greedy_locks=False
        ).divergent
        assert not decision(
            chose_lock=chose, p_win_lock=0.7, p_win_pass=0.3, greedy_locks=True
        ).divergent


# ------------------------------------------------------------ points capture


def test_upside_ignores_weeks_with_no_choice():
    assert _upside(week((True, 40.0)), 40.0) is None


def test_upside_ignores_weeks_where_riding_was_already_best():
    """The last game was his best; there was nothing to get wrong."""
    assert _upside(week((True, 10.0), (True, 50.0)), 50.0) is None


def test_upside_measures_capture_between_floor_and_ceiling():
    games = week((True, 50.0), (True, 10.0))  # ride gives 10, best is 50
    assert _upside(games, 50.0) == (40.0, 40.0)  # banked the best: full capture
    assert _upside(games, 10.0) == (0.0, 40.0)  # rode: none
    assert _upside(games, 30.0) == (20.0, 40.0)


def test_upside_floor_is_zero_when_the_week_ends_in_a_dnp():
    games = week((True, 60.0), (False, 0.0))
    assert _upside(games, 60.0) == (60.0, 60.0)


# ----------------------------------------------------------------- report


def test_ranking_is_by_regret_ascending():
    report = ManagerReport(
        decisions=[
            decision(roster_id=1, chose_lock=True, p_win_lock=0.1, p_win_pass=0.9),
            decision(roster_id=2, chose_lock=True, p_win_lock=0.9, p_win_pass=0.1),
        ]
    )
    from lockin.managers import Scorecard

    report.scorecards = [
        Scorecard(1, 1, 0.8, 0.0, 0, 0.0, 0.5, 1, 0),
        Scorecard(2, 1, 0.0, 1.0, 0, 0.0, 0.5, 1, 0),
    ]
    assert [s.roster_id for s in report.ranked()] == [2, 1]


def test_regret_by_agreement_splits_the_two_populations():
    report = ManagerReport(
        decisions=[
            # Concordant: both objectives say pass; the manager banked anyway.
            decision(chose_lock=True, p_win_lock=0.3, p_win_pass=0.5, greedy_locks=False),
            # Divergent: points says bank, win probability says pass.
            decision(chose_lock=True, p_win_lock=0.4, p_win_pass=0.5, greedy_locks=True),
        ]
    )
    agree, differ = report.regret_by_agreement()
    assert agree == pytest.approx(0.2)
    assert differ == pytest.approx(0.1)
    assert report.divergence_rate() == pytest.approx(0.5)


def test_empty_report_does_not_divide_by_zero():
    empty = ManagerReport()
    assert empty.divergence_rate() == 0.0
    assert empty.regret_by_agreement() == (0.0, 0.0)


def test_bootstrap_band_brackets_the_mean():
    from lockin.managers import bootstrap_regret

    rng = np.random.default_rng(0)
    ds = [
        decision(roster_id=7, chose_lock=True, p_win_lock=0.5 - x, p_win_pass=0.5)
        for x in rng.uniform(0, 0.2, 300)
    ]
    report = ManagerReport(decisions=ds)
    lo, hi = bootstrap_regret(report, 7)
    mean = float(np.mean([d.regret for d in ds]))
    assert lo < mean < hi


def test_bootstrap_is_empty_safe():
    from lockin.managers import bootstrap_regret

    assert bootstrap_regret(ManagerReport(), 3) == (0.0, 0.0)
