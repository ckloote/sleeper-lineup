"""The synthetic season behaves like the upstreams it stands in for.

Everything the live-state tests assert rests on `tests/live_fixture.py` being a
faithful enough Sleeper and NBA. These checks are what "faithful enough" means:
the real ingest accepts its payloads, the scoring gate reproduces its counted
values, and building it twice gives the same season.
"""

from __future__ import annotations

from datetime import timedelta

from live_fixture import OPENING, SyntheticSeason, config_for, connect, ingest, week_of

from lockin import verify as verify_mod


def test_week_one_holds_opening_night_and_weeks_run_monday_to_sunday():
    assert week_of(OPENING) == 1
    assert week_of(OPENING + timedelta(days=5)) == 1  # Sunday 25 Oct
    assert week_of(OPENING + timedelta(days=6)) == 2  # Monday 26 Oct


def test_the_real_ingest_accepts_an_unfinished_season(tmp_path):
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=8))
    cfg = config_for(tmp_path, season)

    out = ingest(season, cfg, weeks=[1, 2])

    assert any("schedule" in line and "not yet played" in line for line in out)
    with connect(cfg) as conn:
        played = conn.execute("SELECT COUNT(*) FROM box_scores WHERE played = 1").fetchone()[0]
        unplayed = conn.execute(
            "SELECT COUNT(*) FROM nba_schedule WHERE game_date > ?",
            ((OPENING + timedelta(days=8)).isoformat(),),
        ).fetchone()[0]
        polls = conn.execute("SELECT COUNT(*) FROM weekly_matchup_teams").fetchone()[0]
    assert played > 0
    assert unplayed > 0, "the schedule must hold games that have not been played"
    assert polls == 2 * season.n_rosters


def test_the_scoring_gate_reproduces_the_fixtures_counted_values(tmp_path):
    """If this fails, the fixture's counted values and its box scores disagree,
    and every lock inference built on them would be testing noise."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=12))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1, 2])

    with connect(cfg) as conn:
        checks = verify_mod.run(conn, season.season)
    failed = [c for c in checks if not c.passed]
    assert not failed, [(c.name, c.detail, c.offenders[:3]) for c in failed]


def test_a_season_built_twice_is_identical():
    a, b = SyntheticSeason(seed=3), SyntheticSeason(seed=3)
    for s in (a, b):
        s.play_through(OPENING + timedelta(days=9))
    assert a.stat_rows(2) == b.stat_rows(2)
    assert a.matchups_payload(2) == b.matchups_payload(2)
    assert a.schedule_payload() == b.schedule_payload()
