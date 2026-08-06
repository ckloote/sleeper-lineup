"""Invariants of the ingested 2025-26 season.

These encode facts established by inspecting the real data, so that a future
change to ingest — or a change upstream — cannot quietly undo them. Each is
something the decision engine will depend on.

Skipped when the database has not been built. Build it with `lockin ingest`.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from lockin.config import Config

cfg = Config.from_env()
pytestmark = pytest.mark.skipif(
    not cfg.db_path.exists(), reason=f"no database at {cfg.db_path}; run `lockin ingest`"
)


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture(scope="module")
def scoring(conn):
    row = conn.execute("SELECT payload_json FROM league_settings LIMIT 1").fetchone()
    return json.loads(row["payload_json"])["scoring_settings"]


_KEY = {
    "pts": "pts",
    "ast": "ast",
    "oreb": "oreb",
    "dreb": "dreb",
    "reb": "reb",
    "stl": "stl",
    "blk": "blk",
    "to": "tov",
    "fgm": "fgm",
    "fgmi": "fgmi",
    "ftm": "ftm",
    "ftmi": "ftmi",
    "tpm": "tpm",
    "tpa": "tpa",
    "tpmi": "tpmi",
    "tf": "tech",
    "ff": "flagrant",
    "dd": "dd",
    "td": "td",
}


def score(row, scoring) -> float:
    return round(sum(w * (row[_KEY[k]] or 0) for k, w in scoring.items() if w and k in _KEY), 2)


def games_for(conn, sleeper_id, week, real_only=True):
    q = """
        SELECT b.* FROM box_scores b
          JOIN game_links g ON g.sleeper_game_id = b.sleeper_game_id
         WHERE b.sleeper_id = ? AND b.fantasy_week = ?
    """
    if real_only:
        q += " AND COALESCE(g.is_exhibition, 0) = 0"
    return conn.execute(q + " ORDER BY b.game_date", (sleeper_id, week)).fetchall()


# --- scoring ---------------------------------------------------------------


def test_scoring_settings_match_the_documented_league(scoring):
    expected = {
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
    }
    for k, v in expected.items():
        assert scoring.get(k, 0.0) == v, f"{k}: expected {v}, got {scoring.get(k)}"


def test_total_rebounds_score_zero_but_are_still_stored(conn, scoring):
    """reb pays 0.0, yet double-double detection needs it."""
    assert scoring.get("reb", 0.0) == 0.0
    row = conn.execute("SELECT reb FROM box_scores WHERE played = 1 AND reb > 0 LIMIT 1").fetchone()
    assert row is not None and row["reb"] > 0


def test_triple_doubles_stack_with_the_double_double_bonus(conn, scoring):
    """A triple-double pays dd + td = 30, not 20.

    Resolves open question #1 in the architecture doc. Every TD game in the
    season also carries dd=1; if td superseded dd, the counted scores that match
    a TD game would be 10 lower.
    """
    tds = conn.execute("SELECT * FROM box_scores WHERE td > 0 AND played = 1").fetchall()
    assert tds, "expected at least one triple-double in the season"
    assert all(g["dd"] == 1 for g in tds), "every TD game should also carry dd=1"

    matched = 0
    for g in tds:
        counted = conn.execute(
            "SELECT counted_points FROM weekly_matchups"
            " WHERE week = ? AND sleeper_id = ? AND counted_points IS NOT NULL LIMIT 1",
            (g["fantasy_week"], g["sleeper_id"]),
        ).fetchone()
        if counted and abs(counted["counted_points"] - score(g, scoring)) < 0.001:
            matched += 1
    assert matched > 0, "no counted score matched a stacked triple-double"


# --- fixture semantics -----------------------------------------------------


def test_the_all_star_game_is_flagged_as_an_exhibition(conn):
    row = conn.execute(
        "SELECT * FROM game_links WHERE game_date = '2026-02-15' AND is_exhibition = 1"
    ).fetchone()
    assert row is not None
    assert {row["team_a"], row["team_b"]} == {"STP", "STR"}


def test_all_star_participants_did_not_count_their_all_star_line(conn, scoring):
    """The ASG is published with real stats but does not score.

    It falls at the END of fantasy week 17, so treating it as a real game makes
    it every All-Star's final game — and an unlocked starter would bank the
    exhibition score. No rostered participant did.
    """
    asg = conn.execute(
        "SELECT sleeper_game_id FROM game_links WHERE is_exhibition = 1 LIMIT 1"
    ).fetchone()["sleeper_game_id"]
    participants = [
        r["sleeper_id"]
        for r in conn.execute(
            "SELECT sleeper_id FROM box_scores WHERE sleeper_game_id = ? AND played = 1", (asg,)
        )
    ]
    assert len(participants) >= 10

    checked = 0
    for pid in participants:
        m = conn.execute(
            "SELECT counted_points FROM weekly_matchups WHERE week = 17 AND sleeper_id = ?", (pid,)
        ).fetchone()
        if not m or m["counted_points"] is None:
            continue
        asg_row = conn.execute(
            "SELECT * FROM box_scores WHERE sleeper_game_id = ? AND sleeper_id = ?", (asg, pid)
        ).fetchone()
        asg_score = score(asg_row, scoring)
        real = [g for g in games_for(conn, pid, 17) if g["played"]]
        real_scores = {score(g, scoring) for g in real}
        # If the ASG counted, someone's counted value would be the ASG score and
        # nothing else. Allow coincidental ties with a real game.
        if abs(m["counted_points"] - asg_score) < 0.001 and asg_score not in real_scores:
            raise AssertionError(f"player {pid} appears to have counted the All-Star game")
        checked += 1
    assert checked >= 10


def test_the_nba_cup_final_counts_and_is_not_an_exhibition(conn):
    """Unlike the ASG, the Cup championship is a real, lockable game.

    LeagueGameFinder excludes it (it is not a regular-season game), so it is
    backfilled from the scoreboard. Two managers locked on it in week 9, which
    is only possible for a scoring game.
    """
    row = conn.execute(
        "SELECT * FROM game_links WHERE game_date = '2025-12-16' AND team_a = 'NYK'"
    ).fetchone()
    assert row is not None, "Cup final fixture missing"
    assert row["is_exhibition"] == 0
    assert row["occurred"] == 1
    assert row["nba_game_id"] is not None, "Cup final should be linked to the NBA schedule"


def test_team_aggregate_rows_are_flagged_and_absent_from_the_player_table(conn):
    """Sleeper mixes team totals into the stat feed alongside players.

    "TEAM_OKC" posts 125 pts / 38 reb / 29 ast, which a naive double-double
    derivation reads as a triple-double every single night. They never appear
    in a lineup, so nothing scores them — but they must be excluded from any
    validation that compares derived bonuses against Sleeper's flags.
    """
    n_team = conn.execute("SELECT COUNT(*) c FROM box_scores WHERE is_team_row = 1").fetchone()["c"]
    assert n_team > 0, "expected team-aggregate rows in the feed"

    # The flag is set by "not in players"; assert that agrees with the prefix,
    # so a real player missing from the table cannot be silently reclassified.
    strays = conn.execute(
        "SELECT DISTINCT sleeper_id FROM box_scores"
        " WHERE is_team_row = 1 AND sleeper_id NOT LIKE 'TEAM_%'"
    ).fetchall()
    assert not strays, f"unrecognised team rows: {[r['sleeper_id'] for r in strays]}"

    missed = conn.execute(
        "SELECT DISTINCT sleeper_id FROM box_scores"
        " WHERE is_team_row = 0 AND sleeper_id LIKE 'TEAM_%'"
    ).fetchall()
    assert not missed, "a TEAM_ row was classified as a player"

    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM weekly_matchups WHERE sleeper_id LIKE 'TEAM_%'"
        ).fetchone()["c"]
        == 0
    )


def test_postponed_fixtures_are_recorded_as_not_occurred(conn):
    """A postponed fixture must not read as a DNP.

    An unplayed REAL game scores 0.0 for an unlocked starter; a postponed
    fixture is excluded. Conflating them mis-scores the end of a week.
    """
    postponed = conn.execute("SELECT * FROM game_links WHERE occurred = 0").fetchall()
    assert postponed, "expected at least one postponed fixture in 2025-26"
    for p in postponed:
        played = conn.execute(
            "SELECT COUNT(*) c FROM box_scores WHERE sleeper_game_id = ? AND played = 1",
            (p["sleeper_game_id"],),
        ).fetchone()["c"]
        assert played == 0


def test_an_unlocked_starter_whose_final_real_game_was_a_dnp_scores_zero(conn):
    """The architecture doc's rule, confirmed against a real instance.

    Week 12, roster 7: a starter scored 61.0 in his only appearance, sat out the
    team's final game of the week, was never locked, and counted 0.0.
    """
    row = conn.execute(
        "SELECT * FROM weekly_matchups"
        " WHERE week = 12 AND roster_id = 7 AND is_starter = 1 AND counted_points = 0.0"
    ).fetchone()
    assert row is not None, "expected the zeroed starter in week 12 roster 7"

    games = games_for(conn, row["sleeper_id"], 12)
    assert games[-1]["played"] == 0, "his final scheduled game should be a DNP"
    assert any(g["played"] for g in games), "he should have played earlier in the week"


# --- coverage ---------------------------------------------------------------


def test_all_25_fantasy_weeks_are_present(conn):
    weeks = {
        r["fantasy_week"] for r in conn.execute("SELECT DISTINCT fantasy_week FROM box_scores")
    }
    assert weeks == set(range(1, 26))


def test_every_played_row_has_internally_consistent_shooting(conn):
    """fgmi must equal fga - fgm across the whole season, not just at ingest."""
    bad = conn.execute(
        "SELECT COUNT(*) c FROM box_scores"
        " WHERE played = 1 AND fga IS NOT NULL"
        "   AND COALESCE(fgmi, 0) != fga - COALESCE(fgm, 0)"
    ).fetchone()["c"]
    assert bad == 0


def test_starters_points_sum_to_the_team_total(conn):
    """`points` sums the six starter slots only — bench players do not count.

    This is why lock inference reads starters/starters_points rather than
    players_points, which is populated for bench players too.
    """
    mismatches = []
    for t in conn.execute(
        "SELECT week, roster_id, points FROM weekly_matchup_teams WHERE points IS NOT NULL"
    ):
        s = conn.execute(
            "SELECT COALESCE(SUM(counted_points), 0) s FROM weekly_matchups"
            " WHERE week = ? AND roster_id = ? AND is_starter = 1",
            (t["week"], t["roster_id"]),
        ).fetchone()["s"]
        if abs(s - t["points"]) > 0.001:
            mismatches.append((t["week"], t["roster_id"], t["points"], s))
    assert not mismatches, f"starter sums differ from team points: {mismatches[:5]}"
