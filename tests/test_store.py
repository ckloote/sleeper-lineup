"""Schema application and additive migration."""

from __future__ import annotations

import sqlite3

import pytest

from lockin.store.db import apply_schema, connect, session


def test_schema_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    conn = connect(db)
    apply_schema(conn)
    apply_schema(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"box_scores", "weekly_matchups", "game_links", "nba_schedule"} <= tables


def test_session_reports_the_real_error_when_no_transaction_is_open(tmp_path):
    """An unguarded ROLLBACK would raise OperationalError and hide this."""
    db = tmp_path / "t.db"
    with pytest.raises(RuntimeError, match="the real problem"):
        with session(db) as conn:
            conn.executescript("CREATE TABLE scratch (x INTEGER)")  # implicitly commits
            raise RuntimeError("the real problem")


def test_matchup_id_is_nullable(tmp_path):
    """Weeks 23-25 have teams with no matchup; a NOT NULL here would abort ingest."""
    db = tmp_path / "t.db"
    with session(db) as conn:
        conn.execute(
            "INSERT INTO weekly_matchups"
            " (week, roster_id, matchup_id, sleeper_id, counted_points, is_starter, observed_at)"
            " VALUES (23, 8, NULL, '1970', 0.0, 1, 'now')"
        )
        conn.execute(
            "INSERT INTO weekly_matchup_teams (week, roster_id, matchup_id, points, observed_at)"
            " VALUES (23, 8, NULL, 0.0, 'now')"
        )
        assert conn.execute("SELECT COUNT(*) c FROM weekly_matchups").fetchone()["c"] == 1


def test_added_columns_are_applied_to_a_preexisting_table(tmp_path):
    """A database created before `occurred`/`is_exhibition` existed must gain them.

    CREATE TABLE IF NOT EXISTS will not add a column to a table that already
    exists, so without the migration an older database silently lacks the
    columns and every query referencing them fails.
    """
    db = tmp_path / "t.db"
    conn = connect(db)
    conn.execute(
        "CREATE TABLE game_links ("
        " sleeper_game_id TEXT PRIMARY KEY, nba_game_id TEXT,"
        " game_date TEXT NOT NULL, team_a TEXT NOT NULL, team_b TEXT NOT NULL)"
    )
    conn.close()

    with session(db) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(game_links)")}
    assert {"occurred", "is_exhibition"} <= cols


def test_session_rolls_back_on_error(tmp_path):
    db = tmp_path / "t.db"
    try:
        with session(db) as conn:
            conn.execute(
                "INSERT INTO players (sleeper_id, full_name, positions, updated_at)"
                " VALUES ('x', 'X', '[]', 'now')"
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with session(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"] == 0


def test_foreign_keys_are_enforced(tmp_path):
    db = tmp_path / "t.db"
    with session(db) as conn:
        try:
            conn.execute(
                "INSERT INTO game_links (sleeper_game_id, nba_game_id, game_date, team_a, team_b)"
                " VALUES ('s1', 'nonexistent', '2026-01-05', 'CHI', 'MIA')"
            )
        except sqlite3.IntegrityError:
            return
        raise AssertionError("expected a foreign key violation")


def test_latest_view_does_not_double_count_repeated_observations(tmp_path):
    """weekly_matchups is append-only, so two ingests mean two rows per
    player-week. A reader that sums the base table returns twice the team's
    score — which is exactly what happened after the second full ingest.
    """
    db = tmp_path / "t.db"
    with session(db) as conn:
        for stamp, pts in (
            ("2026-08-05T00:00:00+00:00", 50.0),
            ("2026-08-07T00:00:00+00:00", 61.0),
        ):
            conn.execute(
                "INSERT INTO weekly_matchups"
                " (week, roster_id, matchup_id, sleeper_id, counted_points,"
                "  is_starter, observed_at)"
                " VALUES (12, 7, 4, '1747', ?, 1, ?)",
                (pts, stamp),
            )

        base = conn.execute(
            "SELECT COUNT(*) c, SUM(counted_points) s FROM weekly_matchups"
        ).fetchone()
        assert (base["c"], base["s"]) == (2, 111.0), "history must be retained"

        latest = conn.execute(
            "SELECT COUNT(*) c, SUM(counted_points) s FROM weekly_matchups_latest"
        ).fetchone()
        assert (latest["c"], latest["s"]) == (1, 61.0), "readers must see only the newest"


def test_latest_view_keeps_rosters_and_weeks_separate(tmp_path):
    db = tmp_path / "t.db"
    with session(db) as conn:
        rows = [
            (12, 7, "a", 10.0, "2026-08-05T00:00:00+00:00"),
            (12, 7, "a", 20.0, "2026-08-07T00:00:00+00:00"),
            (12, 8, "a", 30.0, "2026-08-05T00:00:00+00:00"),
            (13, 7, "a", 40.0, "2026-08-05T00:00:00+00:00"),
        ]
        for wk, rid, pid, pts, stamp in rows:
            conn.execute(
                "INSERT INTO weekly_matchups"
                " (week, roster_id, matchup_id, sleeper_id, counted_points,"
                "  is_starter, observed_at)"
                " VALUES (?, ?, 1, ?, ?, 1, ?)",
                (wk, rid, pid, pts, stamp),
            )
        got = {
            (r["week"], r["roster_id"]): r["counted_points"]
            for r in conn.execute("SELECT * FROM weekly_matchups_latest")
        }
        assert got == {(12, 7): 20.0, (12, 8): 30.0, (13, 7): 40.0}
