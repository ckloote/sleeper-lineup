"""The scoring function, unit level.

The season-wide proof that this agrees with reality lives in
`lockin verify` and `tests/test_season_invariants.py`. These tests pin the
behaviour at the boundaries, where the real data happens to be thin.
"""

from __future__ import annotations

import pytest

from lockin.core.scoring import (
    StatLine,
    UnscorableStat,
    break_even_rate,
    count_doubles,
    derive_bonuses,
    line_from_stats,
    score_line,
    score_recorded,
    shot_values,
)

# The league's actual settings. test_season_invariants asserts these match live.
SCORING = {
    "pts": 1.0,
    "ast": 1.0,
    "oreb": 1.5,
    "dreb": 1.0,
    "reb": 0.0,
    "stl": 2.0,
    "blk": 2.0,
    "to": -1.0,
    "fgm": 0.5,
    "fgmi": -1.0,
    "ftm": 1.0,
    "ftmi": -1.0,
    "tpm": 2.0,
    "tpa": 0.0,
    "tpmi": 0.0,
    "dd": 10.0,
    "td": 20.0,
    "tf": -3.0,
    "ff": -2.0,
    "bonus_pt_40p": 0.0,
    "bonus_pt_50p": 0.0,
}


# --- derived quantities -----------------------------------------------------


def test_total_rebounds_are_the_sum_of_offensive_and_defensive():
    assert StatLine(oreb=7, dreb=5).reb == 12


def test_missed_shots_derive_from_attempts_and_makes():
    line = StatLine(fga=16, fgm=6, fta=4, ftm=3, tpa=1, tpm=0)
    assert (line.fgmi, line.ftmi, line.tpmi) == (10, 1, 1)


# --- double-double / triple-double boundary ---------------------------------


def test_nine_is_not_double_figures():
    assert count_doubles(StatLine(pts=9, ast=9, oreb=5, dreb=4)) == 0


def test_exactly_ten_counts():
    assert count_doubles(StatLine(pts=10, ast=10)) == 2


def test_rebound_doubles_use_the_total_not_either_half():
    """6 offensive + 4 defensive is a double-digit rebound total."""
    assert count_doubles(StatLine(pts=20, oreb=6, dreb=4)) == 2
    assert derive_bonuses(StatLine(pts=20, oreb=6, dreb=4)) == (1, 0)


def test_steals_and_blocks_count_toward_doubles():
    assert count_doubles(StatLine(pts=10, stl=10, blk=10)) == 3


def test_single_category_is_no_bonus():
    assert derive_bonuses(StatLine(pts=40)) == (0, 0)


def test_double_double():
    assert derive_bonuses(StatLine(pts=21, oreb=7, dreb=5)) == (1, 0)


def test_triple_double_stacks_by_default():
    """Verified behaviour: a TD pays dd + td = 30, not 20."""
    assert derive_bonuses(StatLine(pts=38, oreb=2, dreb=8, ast=10)) == (1, 1)


def test_triple_double_can_be_configured_to_supersede():
    assert derive_bonuses(StatLine(pts=38, dreb=10, ast=10), td_stacks_dd=False) == (0, 1)


def test_quadruple_double_still_reads_as_a_triple_double():
    """There is no 'qd' setting, so four categories pays the same as three."""
    assert derive_bonuses(StatLine(pts=10, dreb=10, ast=10, blk=10)) == (1, 1)


# --- scoring ----------------------------------------------------------------


def test_score_line_reproduces_a_known_game():
    """Jusuf Nurkić, 2026-01-05 vs POR. Recorded players_points was 61.0.

    21 pts + 5 ast + 7*1.5 oreb + 5 dreb + 2*2 stl - 1 tov
    + 9*0.5 fgm - 3 fgmi + 1 ftm + 2*2 tpm + 10 dd = 61.0
    """
    line = StatLine(
        pts=21,
        ast=5,
        oreb=7,
        dreb=5,
        stl=2,
        blk=0,
        tov=1,
        fgm=9,
        fga=12,
        ftm=1,
        fta=1,
        tpm=2,
        tpa=3,
    )
    assert score_line(line, SCORING) == 61.0


def test_score_line_reproduces_a_known_triple_double():
    """Luka Dončić, 2026-01-07: 38/10/10 with 3 steals, recorded at 88.5."""
    stats = {
        "pts": 38.0,
        "reb": 10.0,
        "ast": 10.0,
        "stl": 3.0,
        "dreb": 9.0,
        "oreb": 1.0,
        "fgm": 12.0,
        "fga": 25.0,
        "fgmi": 13.0,
        "ftm": 8.0,
        "fta": 9.0,
        "ftmi": 1.0,
        "tpm": 6.0,
        "tpa": 13.0,
        "tpmi": 7.0,
        "to": 4.0,
        "dd": 1.0,
        "td": 1.0,
    }
    assert score_recorded(stats, SCORING) == score_line(line_from_stats(stats), SCORING)


def test_a_scoreless_line_can_go_negative():
    """Bricking shots with nothing else is worse than not playing."""
    assert score_line(StatLine(fga=4, fgm=0), SCORING) == -4.0


def test_an_empty_line_scores_zero():
    assert score_line(StatLine(), SCORING) == 0.0


def test_recorded_ignores_unweighted_keys():
    assert score_recorded({"pts": 10.0, "pf": 6.0, "plus_minus": -20.0}, SCORING) == 10.0


def test_recorded_treats_absent_keys_as_zero():
    """Sleeper omits zero-valued stats entirely."""
    assert score_recorded({"pts": 10.0}, SCORING) == 10.0


def test_line_from_stats_ignores_precomputed_bonuses():
    """Otherwise comparing the two paths would be a tautology."""
    stats = {"pts": 10.0, "ast": 10.0, "dd": 0.0, "td": 0.0}
    assert score_line(line_from_stats(stats), SCORING) == 30.0  # 10 + 10 + dd


def test_unrepresentable_weighted_stat_raises():
    """Silently skipping it would under-score every simulated game."""
    with pytest.raises(UnscorableStat, match="bonus_pt_40p"):
        score_line(StatLine(pts=45), {**SCORING, "bonus_pt_40p": 5.0})


# --- per-attempt economics (architecture doc §2) ----------------------------


def test_shot_values_match_the_documented_table():
    v = shot_values(SCORING)
    assert v["three"] == (5.5, -1.0)
    assert v["two"] == (2.5, -1.0)
    assert v["free_throw"] == (2.0, -1.0)


@pytest.mark.parametrize(
    ("shot", "expected"),
    [("three", 0.1538), ("two", 0.2857), ("free_throw", 0.3333)],
)
def test_break_even_rates_match_the_documented_table(shot, expected):
    made, missed = shot_values(SCORING)[shot]
    assert break_even_rate(made, missed) == pytest.approx(expected, abs=0.0001)


def test_three_pointers_are_the_most_valuable_attempt():
    """The reason this format overweights three-point volume."""
    rates = {s: break_even_rate(*v) for s, v in shot_values(SCORING).items()}
    assert rates["three"] < rates["two"] < rates["free_throw"]


def test_break_even_is_undefined_when_making_and_missing_pay_the_same():
    with pytest.raises(ValueError, match="no break-even"):
        break_even_rate(1.0, 1.0)
