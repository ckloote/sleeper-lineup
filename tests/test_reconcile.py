"""Reconciliation checks, against synthetic databases.

Each test builds the minimum rows needed to make one check pass or fail, so a
failure names the specific gate that broke.
"""

from __future__ import annotations

import pytest

from lockin import reconcile
from lockin.store.db import session


def _box(conn, sleeper_id, week, game_id, played=1, date="2026-01-05"):
    conn.execute(
        "INSERT OR REPLACE INTO box_scores"
        " (sleeper_game_id, sleeper_id, season, season_type, fantasy_week, game_date,"
        "  played, raw_stats, ingested_at)"
        " VALUES (?, ?, '2025', 'regular', ?, ?, ?, '{}', 'now')",
        (game_id, sleeper_id, week, date, played),
    )


def _starter(conn, week, roster_id, sleeper_id):
    conn.execute(
        "INSERT OR REPLACE INTO weekly_matchups"
        " (week, roster_id, matchup_id, sleeper_id, counted_points, is_starter, observed_at)"
        " VALUES (?, ?, 1, ?, 0.0, 1, 'now')",
        (week, roster_id, sleeper_id),
    )


def _player(conn, sleeper_id, name="Someone"):
    conn.execute(
        "INSERT OR REPLACE INTO players (sleeper_id, full_name, positions, updated_at)"
        " VALUES (?, ?, '[]', 'now')",
        (sleeper_id, name),
    )


def test_weeks_present_fails_when_weeks_are_missing(tmp_path):
    with session(tmp_path / "t.db") as conn:
        _box(conn, "1", 12, "g1")
        check = reconcile.check_weeks_present(conn, "2025")
    assert not check.passed
    assert "1/25" in check.detail


def test_weeks_present_passes_with_all_25(tmp_path):
    with session(tmp_path / "t.db") as conn:
        for wk in range(1, 26):
            _box(conn, "1", wk, f"g{wk}")
        check = reconcile.check_weeks_present(conn, "2025")
    assert check.passed


def test_player_coverage_flags_a_lineup_player_missing_from_the_table(tmp_path):
    with session(tmp_path / "t.db") as conn:
        _starter(conn, 12, 1, "ghost")
        check = reconcile.check_player_coverage(conn)
    assert not check.passed
    assert "ghost" in check.offenders


def test_starter_coverage_flags_a_starter_with_no_games(tmp_path):
    with session(tmp_path / "t.db") as conn:
        _player(conn, "1", "Has Games")
        _player(conn, "2", "No Games")
        _starter(conn, 12, 1, "1")
        _starter(conn, 12, 1, "2")
        _box(conn, "1", 12, "g1")
        check = reconcile.check_starter_coverage(conn, "2025")
    assert not check.passed
    assert "1/2" in check.detail
    assert any("No Games" in o for o in check.offenders)


def test_game_links_ignores_postponed_and_exhibition_fixtures(tmp_path):
    """Neither can link by construction, so neither may count against the rate."""
    with session(tmp_path / "t.db") as conn:
        conn.execute(
            "INSERT INTO nba_schedule"
            " (nba_game_id, season, game_date, home_team, away_team)"
            " VALUES ('n1', '2025', '2026-01-05', 'CHI', 'DAL')"
        )
        conn.execute(
            "INSERT INTO game_links"
            " (sleeper_game_id, nba_game_id, game_date, team_a, team_b, occurred, is_exhibition)"
            " VALUES ('s1', 'n1', '2026-01-05', 'CHI', 'DAL', 1, 0)"
        )
        # postponed: occurred = 0, no NBA counterpart
        conn.execute(
            "INSERT INTO game_links"
            " (sleeper_game_id, game_date, team_a, team_b, occurred, is_exhibition)"
            " VALUES ('s2', '2026-01-08', 'CHI', 'MIA', 0, 0)"
        )
        # All-Star Game: occurred, but not a real fixture
        conn.execute(
            "INSERT INTO game_links"
            " (sleeper_game_id, game_date, team_a, team_b, occurred, is_exhibition)"
            " VALUES ('s3', '2026-02-15', 'STP', 'STR', 1, 1)"
        )
        check = reconcile.check_game_links(conn)
    assert check.passed
    assert "1/1" in check.detail


def test_game_links_fails_when_a_real_played_fixture_is_unlinked(tmp_path):
    with session(tmp_path / "t.db") as conn:
        conn.execute(
            "INSERT INTO game_links"
            " (sleeper_game_id, game_date, team_a, team_b, occurred, is_exhibition)"
            " VALUES ('s1', '2026-01-05', 'CHI', 'DAL', 1, 0)"
        )
        check = reconcile.check_game_links(conn)
    assert not check.passed


def test_postponement_check_flags_disagreement(tmp_path):
    """A fixture nobody played, that the NBA nonetheless has a game for."""
    with session(tmp_path / "t.db") as conn:
        conn.execute(
            "INSERT INTO nba_schedule (nba_game_id, season, game_date, home_team, away_team)"
            " VALUES ('n1', '2025', '2026-01-08', 'MIA', 'CHI')"
        )
        conn.execute(
            "INSERT INTO game_links"
            " (sleeper_game_id, nba_game_id, game_date, team_a, team_b, occurred)"
            " VALUES ('s1', 'n1', '2026-01-08', 'CHI', 'MIA', 0)"
        )
        check = reconcile.check_postponements(conn)
    assert not check.passed


@pytest.mark.parametrize("check_name", ["check_exhibitions", "check_tipoffs"])
def test_advisory_checks_never_fail(tmp_path, check_name):
    """Advisory checks report; they do not gate. Finding the ASG is correct."""
    with session(tmp_path / "t.db") as conn:
        fn = getattr(reconcile, check_name)
        check = fn(conn, "2025") if check_name == "check_tipoffs" else fn(conn)
    assert check.passed
