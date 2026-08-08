"""Lock inference, unit level.

`games` is always the already-filtered sequence: postponed fixtures and
exhibitions removed, unplayed real games kept. That filtering is the caller's
job (`lockin.locks._game_sequence`), and these tests assume it has happened.
"""

from __future__ import annotations

from lockin.core.locks import (
    Game,
    LockStatus,
    infer_lock,
    is_resolved,
    profile_manager,
)


def g(index: int, score: float | None) -> Game:
    """A played game with `score`, or an unplayed one when score is None."""
    return Game(index=index, played=score is not None, score=score or 0.0)


# --- degenerate weeks -------------------------------------------------------


def test_no_games_scheduled():
    inf = infer_lock(0.0, [])
    assert inf.status is LockStatus.NO_GAMES
    assert is_resolved(inf)


def test_single_game_is_not_a_decision():
    """With one game, lock and pass are outcome-equivalent (arch doc §15.3)."""
    inf = infer_lock(30.0, [g(0, 30.0)])
    assert inf.status is LockStatus.SINGLE_GAME
    assert inf.locked_early is None  # no tendency information
    assert is_resolved(inf)


def test_single_game_not_played():
    inf = infer_lock(0.0, [g(0, None)])
    assert inf.status is LockStatus.SINGLE_GAME


# --- riding -----------------------------------------------------------------


def test_rode_to_end_takes_the_final_game():
    inf = infer_lock(19.0, [g(0, 16.5), g(1, 21.5), g(2, 19.0)])
    assert inf.status is LockStatus.RODE_TO_END
    assert inf.matched_index == 2
    assert inf.locked_early is False


def test_rode_to_end_through_a_final_dnp_scores_zero():
    """The architecture doc's rule, and how a week gets thrown away.

    Real case: week 12, roster 7 — 61.0 in his only appearance, sat the team's
    final game, never locked, counted 0.0.
    """
    inf = infer_lock(0.0, [g(0, 61.0), g(1, None), g(2, None), g(3, None)])
    assert inf.status is LockStatus.RODE_TO_END
    assert inf.matched_index == 3
    assert inf.locked_early is False
    assert is_resolved(inf)


# --- locking ----------------------------------------------------------------


def test_locked_early_is_unambiguous_when_the_score_is_unique():
    inf = infer_lock(49.5, [g(0, 49.5), g(1, 52.0), g(2, 47.5)])
    assert inf.status is LockStatus.LOCKED_EARLY
    assert inf.matched_index == 0
    assert inf.locked_early is True
    assert is_resolved(inf)


def test_locking_on_the_last_game_is_indistinguishable_from_riding():
    """Identical outcome, so it reads as RODE_TO_END. Nothing is lost."""
    inf = infer_lock(46.0, [g(0, 30.0), g(1, 19.0), g(2, 46.0)])
    assert inf.status is LockStatus.RODE_TO_END


def test_a_dnp_mid_week_does_not_break_the_sequence():
    """Real case: Josh Hart, week 9 — locked the Cup final at index 0."""
    inf = infer_lock(30.0, [g(0, 30.0), g(1, None), g(2, 19.0), g(3, 46.0)])
    assert inf.status is LockStatus.LOCKED_EARLY
    assert inf.matched_index == 0


# --- ambiguity --------------------------------------------------------------


def test_two_games_with_the_same_score_are_flagged_not_guessed():
    """Real case: Karl-Anthony Towns, week 9 — 45.0 twice.

    He definitely locked (riding would have counted 3.0), but which game is
    unrecoverable. Guessing would corrupt the tendency profile.
    """
    inf = infer_lock(45.0, [g(0, 45.0), g(1, None), g(2, 45.0), g(3, 3.0)])
    assert inf.status is LockStatus.AMBIGUOUS
    assert inf.candidates == (0, 2)
    assert inf.matched_index is None
    assert inf.confidence == 0.5
    assert inf.locked_early is True  # locking is certain even if the game is not
    assert not is_resolved(inf)


def test_an_earlier_game_matching_the_ride_score_is_ambiguous():
    """Riding explains it, but so would locking early at the same value.

    Outcome is identical either way, so matched_index is still the last game —
    but locked_early must stay None rather than defaulting to False, or the
    tendency profile inherits a bias toward "rides".
    """
    inf = infer_lock(20.0, [g(0, 20.0), g(1, 35.0), g(2, 20.0)])
    assert inf.status is LockStatus.AMBIGUOUS
    assert inf.matched_index == 2
    assert inf.locked_early is None
    assert inf.confidence == 0.5


# --- the odd cases ----------------------------------------------------------


def test_counted_zero_while_the_final_game_scored():
    """The player held a slot at week's end without occupying one at tipoff."""
    inf = infer_lock(0.0, [g(0, 30.0), g(1, 25.0)])
    assert inf.status is LockStatus.NO_LOCKABLE_GAME
    assert inf.matched_index is None
    assert is_resolved(inf)


def test_a_counted_score_matching_nothing_is_unresolved():
    """Never a legitimate outcome — indicates a bug or a data problem."""
    inf = infer_lock(99.0, [g(0, 30.0), g(1, 25.0)])
    assert inf.status is LockStatus.UNRESOLVED
    assert inf.confidence == 0.0
    assert not is_resolved(inf)


def test_a_genuine_zero_scoring_game_is_a_lock_not_a_gap():
    """0.0 can be a real game score, not only an absence."""
    inf = infer_lock(0.0, [g(0, 0.0), g(1, 25.0)])
    assert inf.status is LockStatus.LOCKED_EARLY
    assert inf.matched_index == 0


def test_negative_scores_are_matched_like_any_other():
    inf = infer_lock(-2.5, [g(0, -2.5), g(1, 30.0)])
    assert inf.status is LockStatus.LOCKED_EARLY


# --- manager profiling ------------------------------------------------------


def test_profile_counts_only_weeks_with_a_real_choice():
    """Single-game and no-game weeks carry no tendency information."""
    infs = [
        infer_lock(50.0, [g(0, 50.0), g(1, 20.0)]),  # locked early
        infer_lock(20.0, [g(0, 50.0), g(1, 20.0)]),  # rode
        infer_lock(30.0, [g(0, 30.0)]),  # single game — excluded
        infer_lock(0.0, []),  # no games — excluded
    ]
    p = profile_manager(7, infs)
    assert p.decisions == 2
    assert (p.locked_early, p.rode_to_end) == (1, 1)
    assert p.lock_rate == 0.5


def test_profile_excludes_ambiguous_ride_cases_from_tendency():
    """locked_early is None there, so it must not be counted as riding."""
    infs = [infer_lock(20.0, [g(0, 20.0), g(1, 35.0), g(2, 20.0)])]
    p = profile_manager(1, infs)
    assert p.decisions == 0
    assert p.lock_rate == 0.0


def test_mean_lock_position_separates_bankers_from_riders():
    banker = profile_manager(
        1, [infer_lock(50.0, [g(0, 50.0), g(1, 20.0), g(2, 30.0)]) for _ in range(5)]
    )
    rider = profile_manager(
        2, [infer_lock(30.0, [g(0, 50.0), g(1, 20.0), g(2, 30.0)]) for _ in range(5)]
    )
    assert banker.mean_lock_position == 0.0
    assert rider.mean_lock_position == 1.0
    assert banker.lock_rate > rider.lock_rate


def test_profile_with_no_decisions_does_not_divide_by_zero():
    p = profile_manager(3, [])
    assert p.decisions == 0
    assert p.lock_rate == 0.0
    assert p.mean_lock_position is None
