"""The day-one checklist's own gates can pass (review finding 11).

day-one.md step 4 ingests week 1 and requires `reconcile` and `verify` to pass.
`reconcile` demanded all 25 weeks and read "no fixtures played yet" as a 0% link
rate, so on the one morning the checklist is for, the gate it prescribes could
not pass. These run the documented sequence against a fresh, unfinished season
and require every gate to pass for the right reason, not merely to exit zero.
"""

from __future__ import annotations

from datetime import timedelta

from live_fixture import OPENING, SyntheticSeason, config_for, connect, ingest

from lockin import reconcile, verify


def gates(cfg, season):
    with connect(cfg) as conn:
        return reconcile.run(conn, season.season, cfg.snapshot_root) + verify.run(
            conn, season.season
        )


def failed(checks):
    return [(c.name, c.detail, c.offenders[:3]) for c in checks if not c.passed]


def test_opening_morning_passes_before_a_single_game(tmp_path):
    season = SyntheticSeason()
    cfg = config_for(tmp_path, season)

    out = ingest(season, cfg, weeks=[1])

    checks = gates(cfg, season)
    assert not failed(checks), failed(checks)
    by_name = {c.name: c for c in checks}
    assert by_name["played fixtures link to NBA schedule (>=99%)"].detail == (
        "no fixtures played yet"
    )
    assert "0/0" in by_name["every completed week ingested"].detail
    assert by_name["every week so far of matchups ingested"].detail == "1/1 weeks present"
    assert not any("WARNING" in line for line in out), out


def test_the_first_morning_after_games_passes_too(tmp_path):
    season = SyntheticSeason()
    season.play_through(OPENING)
    cfg = config_for(tmp_path, season)

    ingest(season, cfg, weeks=[1])

    checks = gates(cfg, season)
    assert not failed(checks), failed(checks)
    links = next(c for c in checks if c.name.startswith("played fixtures link"))
    assert links.detail.startswith("2/2 linked"), "opening night's two games"


def test_a_missed_week_still_fails_once_the_season_is_under_way(tmp_path):
    """Live gates are narrower, not absent: week 2 skipped is still caught in week 3."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=14))  # Tuesday of week 3
    cfg = config_for(tmp_path, season)

    ingest(season, cfg, weeks=[1, 3])

    names = [name for name, _, _ in failed(gates(cfg, season))]
    assert "every completed week ingested" in names
    assert "every week so far of matchups ingested" in names


def test_the_calendar_is_checked_against_sleepers_own_week(tmp_path):
    """If Sleeper's weeks are not Monday-to-Sunday, every resolved week is wrong."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=8))  # Wednesday of week 2
    season.settings = {"leg": 3}

    out = ingest(season, config_for(tmp_path, season), weeks=[2])

    assert any("WARNING" in line and "says week 3" in line for line in out), out


def test_a_monday_before_sleeper_rolls_the_week_over_is_not_an_alarm(tmp_path):
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=12))  # Sunday; today is Monday of week 3
    season.settings = {"leg": 2}

    out = ingest(season, config_for(tmp_path, season), weeks=[2])

    assert not any("WARNING" in line for line in out), out
