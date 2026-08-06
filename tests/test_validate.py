"""Shape validation must raise, not warn. See lockin/ingest/validate.py."""

from __future__ import annotations

import pytest

from lockin.ingest.validate import (
    SchemaDriftError,
    check_shot_consistency,
    validate_league,
    validate_matchups,
    validate_stat_rows,
)


def test_validate_league_accepts_a_well_formed_payload():
    league = {
        "league_id": "1",
        "season": "2025",
        "scoring_settings": {"pts": 1.0},
        "roster_positions": ["PG", "G", "F", "C", "UTIL", "UTIL"],
        "settings": {},
        "total_rosters": 10,
    }
    assert validate_league(league) is league


@pytest.mark.parametrize("missing", ["scoring_settings", "roster_positions", "total_rosters"])
def test_validate_league_raises_on_missing_key(missing):
    league = {
        "league_id": "1",
        "season": "2025",
        "scoring_settings": {"pts": 1.0},
        "roster_positions": ["PG"],
        "settings": {},
        "total_rosters": 10,
    }
    del league[missing]
    with pytest.raises(SchemaDriftError, match=missing):
        validate_league(league)


def test_validate_league_raises_on_empty_roster_positions():
    with pytest.raises(SchemaDriftError, match="roster_positions"):
        validate_league(
            {
                "league_id": "1",
                "season": "2025",
                "scoring_settings": {"pts": 1.0},
                "roster_positions": [],
                "settings": {},
                "total_rosters": 10,
            }
        )


def test_validate_matchups_requires_starters_and_points():
    good = [
        {
            "roster_id": 1,
            "matchup_id": 2,
            "players": [],
            "players_points": {},
            "starters": [],
            "starters_points": [],
            "points": 0.0,
        }
    ]
    assert validate_matchups(good, 12) == good
    with pytest.raises(SchemaDriftError, match="starters_points"):
        validate_matchups([{k: v for k, v in good[0].items() if k != "starters_points"}], 12)


def test_validate_matchups_tolerates_null_matchup_id():
    """Weeks 23-25 legitimately carry a null matchup_id for teams not playing."""
    rows = [
        {
            "roster_id": 8,
            "matchup_id": None,
            "players": [],
            "players_points": {},
            "starters": [],
            "starters_points": [],
            "points": 0.0,
        }
    ]
    assert validate_matchups(rows, 23) == rows


def test_validate_stat_rows_requires_a_stats_object():
    good = [
        {
            "player_id": "1970",
            "game_id": "g1",
            "date": "2026-01-05",
            "week": 12,
            "season": "2025",
            "season_type": "regular",
            "stats": {},
        }
    ]
    assert validate_stat_rows(good, 12) == good
    bad = [{**good[0], "stats": []}]
    with pytest.raises(SchemaDriftError, match="stats"):
        validate_stat_rows(bad, 12)


def test_validate_raises_on_wrong_top_level_type():
    with pytest.raises(SchemaDriftError, match="expected array"):
        validate_stat_rows({"not": "a list"}, 12)


# --- shot consistency -------------------------------------------------------


def test_shot_consistency_accepts_agreeing_values():
    check_shot_consistency({"fga": 12, "fgm": 9, "fgmi": 3, "fta": 1, "ftm": 1}, "ctx")


def test_shot_consistency_treats_absent_as_zero():
    """Sleeper omits zero-valued keys, so a perfect line has no fgmi at all."""
    check_shot_consistency({"fga": 5, "fgm": 5}, "ctx")


def test_shot_consistency_raises_when_made_and_missed_disagree():
    with pytest.raises(SchemaDriftError, match="fgmi"):
        check_shot_consistency({"fga": 12, "fgm": 9, "fgmi": 99}, "ctx")


def test_shot_consistency_ignores_categories_with_no_attempts():
    """No attempts means the category is absent, not inconsistent."""
    check_shot_consistency({"fga": 3, "fgm": 3, "tpm": 0}, "ctx")
