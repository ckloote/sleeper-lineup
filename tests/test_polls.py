"""The latest lineup is one coherent poll (review 2026-09-23, finding 4).

`weekly_matchups_latest` used to choose the newest row *per player*. A player who
left the roster between two polls has no newer row, so his old one stayed
"latest" beside his replacement's, and the digest could simulate seven
starters. Separately, the ingest enumerated players from `players_points`
alone, so early in a week — when that map is sparse or empty — a valid lineup
was recorded as nobody.

Each test here is one of those failures, plus the two writers that had to
change to keep polls whole: `lockin repair`, and the migration that completes
the partial repair polls already in the 2025-26 file.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from live_fixture import OPENING, SyntheticSeason, config_for, connect, ingest

from lockin.store.db import _complete_partial_polls, apply_schema


def latest(conn: sqlite3.Connection, week: int, roster_id: int) -> dict[str, tuple]:
    return {
        r["sleeper_id"]: (r["counted_points"], r["is_starter"])
        for r in conn.execute(
            "SELECT * FROM weekly_matchups_latest WHERE week = ? AND roster_id = ?",
            (week, roster_id),
        )
    }


def test_a_dropped_starter_leaves_the_latest_lineup(tmp_path):
    """The review's reproduction: old in slot 0, then a poll with only new there."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=1))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1])
    old = season.rosters[1][0]

    new = season.drop_add(1, old)
    season.play_through(OPENING + timedelta(days=2))
    ingest(season, cfg, weeks=[1])

    with connect(cfg) as conn:
        now = latest(conn, 1, 1)
        history = conn.execute(
            "SELECT COUNT(*) FROM weekly_matchups WHERE sleeper_id = ?", (old,)
        ).fetchone()[0]
    assert old not in now
    assert new in now
    assert sum(starter for _, starter in now.values()) == 6
    assert history == 1, "the dropped player's poll is still history"


def test_a_lineup_with_no_scores_yet_is_still_a_lineup(tmp_path):
    """Opening morning: nobody has played, `players_points` is empty."""
    season = SyntheticSeason()
    cfg = config_for(tmp_path, season)
    assert all(not team["players_points"] for team in season.matchups_payload(1))

    ingest(season, cfg, weeks=[1], skip_nba=True)

    with connect(cfg) as conn:
        now = latest(conn, 1, 1)
    assert sum(starter for _, starter in now.values()) == 6
    assert len(now) == season.per_roster
    assert all(points is None for points, _ in now.values()), "no score is NULL, not 0.0"


def test_an_empty_roster_poll_is_an_empty_roster(tmp_path):
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=1))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1])

    season.rosters[2] = []
    ingest(season, cfg, weeks=[1])

    with connect(cfg) as conn:
        assert latest(conn, 1, 2) == {}
        assert latest(conn, 1, 1), "other rosters are unaffected"


def _poll(conn, stamp, week, roster_id, rows):
    conn.execute(
        "INSERT INTO weekly_matchup_teams (week, roster_id, matchup_id, observed_at)"
        " VALUES (?, ?, 1, ?)",
        (week, roster_id, stamp),
    )
    for sleeper_id, points, starter in rows:
        conn.execute(
            "INSERT INTO weekly_matchups (week, roster_id, matchup_id, sleeper_id,"
            " counted_points, is_starter, observed_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
            (week, roster_id, sleeper_id, points, starter, stamp),
        )


def test_the_migration_completes_old_partial_repairs_and_nothing_else(tmp_path):
    """Built the way the 2025-26 file was: a repair wrote one corrected starter.

    Two rosters. Roster 1 was repaired — its later observation holds only the
    corrected row, and must be completed from the poll before it. Roster 2 simply
    lost a player in a later live poll, and must *not* be completed, or the
    migration would resurrect him.
    """
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)

    ingest_at, repair_at, later = (
        "2026-09-01T10:30:00+00:00",
        "2026-09-20T14:54:41+00:00",
        "2026-09-21T10:30:00+00:00",
    )
    _poll(conn, ingest_at, 12, 1, [("a", 54.5, 1), ("b", 30.0, 1), ("bench", 9.0, 0)])
    _poll(conn, repair_at, 12, 1, [("a", 42.5, 1)])
    conn.execute(
        "INSERT INTO ingest_log VALUES ('repair', 'weekly_matchups:week=12', 1, ?, ?)",
        (repair_at, repair_at),
    )
    _poll(conn, ingest_at, 12, 2, [("c", 10.0, 1), ("gone", 5.0, 1)])
    _poll(conn, later, 12, 2, [("c", 10.0, 1)])

    assert _complete_partial_polls(conn) == 2
    assert latest(conn, 12, 1) == {"a": (42.5, 1), "b": (30.0, 1), "bench": (9.0, 0)}
    assert latest(conn, 12, 2) == {"c": (10.0, 1)}
    assert _complete_partial_polls(conn) == 0, "idempotent"


def test_the_migration_runs_once_and_is_recorded(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.db")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    apply_schema(conn)

    names = [r["name"] for r in conn.execute("SELECT name FROM schema_migrations")]
    assert "coherent-polls" in names
    assert len(names) == len(set(names)), "each migration is recorded exactly once"
