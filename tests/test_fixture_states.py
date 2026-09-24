"""A game nobody has played yet is not a postponed game (review finding 1).

The occurrence rule was "no stat lines, so it did not happen". On a finished
season that is right; on a season in progress it is also true of every game
still to come, and it removed all 681 of them from the digest. These tests
replay that unfinished season — the real 2025-26 one, with everything from
2026-01-08 on un-played — and require the digest built from it to be the digest
built from the finished season. That is the check the review asked for: not
blanking a completed season after ingest, but ingesting one that has not
finished.
"""

from __future__ import annotations

import sqlite3

import pytest

from lockin import digest as digest_mod
from lockin.ingest.sleeper import classify_fixtures, fixture_state
from lockin.store.db import apply_schema

AS_OF = "2026-01-08"
BANKED = {"1000": 46.0, "1787": 47.5}


# ------------------------------------------------------------- the truth table


@pytest.mark.parametrize(
    ("evidence", "state"),
    [
        (dict(played=True, linked=True, nba_status=3, game_date="2026-01-07"), "final"),
        (dict(played=True, linked=True, nba_status=2, game_date="2026-01-08"), "in_progress"),
        (dict(played=True, linked=False, nba_status=None, game_date="2026-01-07"), "final"),
        (dict(played=False, linked=True, nba_status=1, game_date="2026-01-09"), "scheduled"),
        (dict(played=False, linked=True, nba_status=1, game_date="2026-01-08"), "scheduled"),
        (dict(played=False, linked=True, nba_status=2, game_date="2026-01-08"), "in_progress"),
        (dict(played=False, linked=True, nba_status=3, game_date="2026-01-07"), "unknown"),
        (dict(played=False, linked=True, nba_status=1, game_date="2026-01-07"), "unknown"),
        (dict(played=False, linked=True, nba_status=None, game_date="2026-01-07"), "unknown"),
        (dict(played=False, linked=False, nba_status=None, game_date="2026-01-07"), "postponed"),
        (dict(played=False, linked=False, nba_status=None, game_date="2026-01-09"), "postponed"),
    ],
)
def test_each_combination_of_evidence(evidence, state):
    assert fixture_state(today="2026-01-08", schedule_loaded=True, **evidence) == state


def test_without_a_schedule_only_a_past_date_can_say_postponed():
    kw = dict(played=False, linked=False, nba_status=None, today="2026-01-08")
    assert fixture_state(game_date="2026-01-07", schedule_loaded=False, **kw) == "postponed"
    assert fixture_state(game_date="2026-01-09", schedule_loaded=False, **kw) == "scheduled"


# -------------------------------------------------- 2025-26, stopped midway


def _copy(season_db, dst):
    src = sqlite3.connect(f"file:{season_db}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.row_factory = sqlite3.Row
    apply_schema(out)
    return out


@pytest.fixture(scope="module")
def unfinished(season_db, tmp_path_factory):
    """The recorded season as it stood on the morning of 2026-01-08."""
    conn = _copy(season_db, tmp_path_factory.mktemp("unfinished") / "season.db")
    conn.execute(
        "UPDATE box_scores SET played = 0, seconds_played = NULL, raw_stats = '{}'"
        " WHERE season = '2025' AND game_date >= ?",
        (AS_OF,),
    )
    conn.execute("UPDATE nba_schedule SET status = 3 WHERE game_date < ?", (AS_OF,))
    conn.execute("UPDATE nba_schedule SET status = 1 WHERE game_date >= ?", (AS_OF,))
    classify_fixtures(conn, AS_OF)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def finished(season_db, tmp_path_factory):
    conn = _copy(season_db, tmp_path_factory.mktemp("finished") / "season.db")
    yield conn
    conn.close()


def test_games_still_to_come_are_scheduled_not_postponed(unfinished):
    """The review's reproduction. Only fixtures the NBA really moved are postponed."""
    future = dict(
        unfinished.execute(
            "SELECT state, COUNT(*) FROM game_links WHERE game_date >= ? GROUP BY state",
            (AS_OF,),
        ).fetchall()
    )
    moved = unfinished.execute(
        "SELECT COUNT(*) FROM game_links WHERE game_date >= ? AND nba_game_id IS NULL"
        "   AND COALESCE(is_exhibition, 0) = 0",
        (AS_OF,),
    ).fetchone()[0]
    assert future.get("scheduled", 0) > 600
    assert future.get("postponed", 0) == moved, "only the NBA's own moves are postponements"
    assert "final" not in future


def test_the_digest_cannot_tell_an_unfinished_season_from_a_finished_one(unfinished, finished):
    """Everything after the cutoff is absent in one database and present in the
    other. A digest that differs has read the future — or lost it."""
    got = digest_mod.build(
        digest_mod.load_context(unfinished, "2025"),
        4,
        AS_OF,
        n_sims=200,
        n_paths=200,
        locked=dict(BANKED),
    )
    want = digest_mod.build(
        digest_mod.load_context(finished, "2025"),
        4,
        AS_OF,
        n_sims=200,
        n_paths=200,
        locked=dict(BANKED),
    )

    assert got.rules, "the unfinished season must still have nights ahead"
    assert digest_mod.render(got) == digest_mod.render(want)
