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
        day=739000,
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
        Scorecard(1, 1, 0.9, 0.05, 0.8, 0.0, 0, 0.0, 0.5, 1, 0),
        Scorecard(2, 1, 0.0, 0.05, 0.0, 1.0, 0, 0.0, 0.5, 1, 0),
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


# ------------------------------------------------------------- persistence


def test_a_player_can_face_several_decisions_in_one_week():
    """The identity of a decision includes the night it was made.

    A four-game week gives a player up to three lock/pass calls. Keying the
    stored table on (week, roster, player) alone silently collapsed them — 726
    of the season's real decisions would have been lost, and the UNIQUE
    constraint is what caught it.
    """
    import sqlite3

    from lockin.managers import persist
    from lockin.store.db import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)

    report = ManagerReport(
        decisions=[
            decision(week=5, roster_id=2, sleeper_id="x", day=739000),
            decision(week=5, roster_id=2, sleeper_id="x", day=739002),
            decision(week=5, roster_id=2, sleeper_id="x", day=739004),
        ]
    )
    n_decisions, _ = persist(conn, report)
    assert n_decisions == 3
    assert conn.execute("SELECT COUNT(*) FROM manager_decisions").fetchone()[0] == 3


def test_persist_replaces_rather_than_accumulating():
    """These are derived; two models' answers side by side would be worse than one."""
    import sqlite3

    from lockin.managers import Scorecard, persist
    from lockin.store.db import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)

    report = ManagerReport(
        decisions=[decision(roster_id=1, day=739000)],
        scorecards=[Scorecard(1, 1, 0.2, 0.05, 0.01, 1.0, 0, 0.0, 0.5, 1, 0)],
    )
    persist(conn, report)
    persist(conn, report)
    assert conn.execute("SELECT COUNT(*) FROM manager_decisions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM manager_scorecards").fetchone()[0] == 1


def test_stored_scorecard_carries_the_uncertainty_band():
    """A dashboard that renders the ranking without it would imply an order
    the data does not support."""
    import sqlite3

    from lockin.managers import Scorecard, persist
    from lockin.store.db import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)

    rng = np.random.default_rng(2)
    ds = [
        decision(roster_id=4, day=739000 + i, chose_lock=True, p_win_lock=0.5 - x, p_win_pass=0.5)
        for i, x in enumerate(rng.uniform(0, 0.2, 200))
    ]
    persist(
        conn,
        ManagerReport(
            decisions=ds,
            scorecards=[
                Scorecard(
                    roster_id=4,
                    decisions=len(ds),
                    squandered_share=0.2,
                    mean_stake=0.05,
                    mean_regret=float(np.mean([d.regret for d in ds])),
                    right_rate=0.0,
                    divergent=0,
                    divergent_right_rate=0.0,
                    upside_share=0.5,
                    upside_decisions=10,
                    rode_to_zero=0,
                )
            ],
        ),
    )
    row = conn.execute("SELECT * FROM manager_scorecards").fetchone()
    assert row["regret_lo"] < row["mean_regret"] < row["regret_hi"]


# ----------------------------------------------------- circumstance vs skill


def test_stake_is_the_gap_between_the_two_options():
    """With two choices, regret is either zero or exactly the stake."""
    d = decision(chose_lock=True, p_win_lock=0.30, p_win_pass=0.55)
    assert d.stake == pytest.approx(0.25)
    assert d.regret == pytest.approx(d.stake)
    assert decision(chose_lock=False, p_win_lock=0.30, p_win_pass=0.55).regret == 0.0


def test_a_blown_out_manager_does_not_look_skilled_on_the_share():
    """The confound the share exists to remove.

    Two managers, both wrong exactly half the time. One spends the season in
    hopeless matchups where nothing is at stake, so raw regret flatters him.
    Normalising by the stakes scores them identically, which is correct — they
    decided equally badly.
    """
    blown_out = [
        decision(
            roster_id=1, day=739000 + i, chose_lock=(i % 2 == 0), p_win_lock=0.03, p_win_pass=0.05
        )
        for i in range(100)
    ]
    competitive = [
        decision(
            roster_id=2, day=739000 + i, chose_lock=(i % 2 == 0), p_win_lock=0.40, p_win_pass=0.60
        )
        for i in range(100)
    ]

    def mean(ds):
        return float(np.mean([d.regret for d in ds]))

    def share(ds):
        return sum(d.regret for d in ds) / sum(d.stake for d in ds)

    assert mean(blown_out) < mean(competitive) / 5, "raw regret rewards being blown out"
    assert share(blown_out) == pytest.approx(share(competitive)), "the share does not"


def test_competitive_flags_only_live_matchups():
    assert decision(p_win_lock=0.50, p_win_pass=0.45).competitive
    assert not decision(p_win_lock=0.05, p_win_pass=0.03).competitive
    assert not decision(p_win_lock=0.95, p_win_pass=0.97).competitive


def test_ranking_sorts_on_the_share_not_raw_regret():
    """A manager with low raw regret purely from low stakes must not rank first."""
    from lockin.managers import Scorecard

    report = ManagerReport(
        scorecards=[
            # tiny regret, but threw away most of what little was at stake
            Scorecard(1, 100, 0.40, 0.005, 0.002, 0.5, 0, 0.0, 0.5, 10, 0),
            # larger regret, but on much bigger stakes
            Scorecard(2, 100, 0.10, 0.100, 0.010, 0.8, 0, 0.0, 0.5, 10, 0),
        ]
    )
    assert [s.roster_id for s in report.ranked()] == [2, 1]
