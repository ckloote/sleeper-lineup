"""Append-only runs with provenance, and freshness that means it (findings 9, 12).

Two records were weaker than their comments claimed. `recommendations` was
called append-only, but runs were stamped to the second and written with INSERT
OR REPLACE, so two runs for one roster inside a second merged into one page.
And "when the ingest last finished" was the newest line of a log that records
sub-steps, so a players refresh could certify stale stats as fresh.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from live_fixture import OPENING, FakeSleeperClient, SyntheticSeason, config_for, connect, ingest

from lockin import advice
from lockin import digest as digest_mod
from lockin.digest import Digest, LockCall, StandingRule, Warning
from lockin.store import runs
from lockin.store.db import session


def a_digest(**overrides) -> Digest:
    values = dict(
        as_of="2026-10-28",
        week=2,
        roster_id=1,
        opponent_roster_id=2,
        known_through=datetime(2026, 10, 27).toordinal(),
        banked={"1005": 36.0},
        p_win=0.61,
        my_total=260.0,
        opponent_total=250.0,
        margin={"q10": -30.0, "q50": 10.0, "q90": 50.0},
        state_source="inferred",
        opponent_state="stand-in",
        poll_observed_at="2026-10-28T10:30:00+00:00",
        n_sims=400,
        seed=7,
        model="lockin 0.1.0; projection params abc",
    )
    values.update(overrides)
    return Digest(**values)


def call(pid: str, lock: bool = True) -> LockCall:
    return LockCall(
        sleeper_id=pid,
        name=f"Player {pid}",
        day=datetime(2026, 10, 27).toordinal(),
        score=41.0,
        lock=lock,
        p_win_lock=0.6,
        p_win_pass=0.5,
        break_even=38.0,
        expires_utc="2026-10-29T23:30:00Z",
    )


# ------------------------------------------------------------- finding 12


def test_two_runs_in_quick_succession_never_mix(tmp_path):
    """The review's scenario: a re-run within the second. Each page is one run's."""
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, a_digest(calls=[call("1001"), call("1002")]))
        digest_mod.persist(conn, a_digest(calls=[call("1003")]))
        ids = [r[0] for r in conn.execute("SELECT run_id FROM digest_runs")]
        run = advice.latest_run(conn, 1)

    assert len(set(ids)) == 2
    assert [i.sleeper_id for i in run.calls] == ["1003"]


def test_an_exact_collision_is_an_error_not_a_merge(tmp_path):
    frozen = datetime(2026, 10, 28, 13, 0, 0, 123456, tzinfo=UTC)
    with session(tmp_path / "t.db") as conn, mock.patch("lockin.digest.datetime") as clock:
        clock.now.return_value = frozen
        digest_mod.persist(conn, a_digest(calls=[call("1001")]))
        with pytest.raises(sqlite3.IntegrityError):
            digest_mod.persist(conn, a_digest(calls=[call("1001")]))


def test_a_run_records_what_it_was_computed_from(tmp_path):
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, a_digest(calls=[call("1001")]))
        row = conn.execute("SELECT * FROM digest_runs").fetchone()
        banked = conn.execute("SELECT sleeper_id, score FROM digest_banked").fetchall()
        rec = conn.execute("SELECT run_id, expires_utc FROM recommendations").fetchone()

    assert row["state_source"] == "inferred" and row["opponent_state"] == "stand-in"
    assert row["poll_observed_at"] == "2026-10-28T10:30:00+00:00"
    assert (row["n_sims"], row["seed"]) == (400, 7)
    assert row["model"].startswith("lockin ")
    assert [tuple(b) for b in banked] == [("1005", 36.0)]
    assert rec["run_id"] == row["run_id"] and rec["expires_utc"] == "2026-10-29T23:30:00Z"


def test_the_warnings_the_notification_carried_reach_the_page(tmp_path):
    warning = Warning("1004", "Player 1004", "final-game DNP risk", "unlocked, 40% DNP", "40%")
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, a_digest(warnings=[warning]))
        run = advice.latest_run(conn, 1)

    page = advice.render(run, today="2026-10-28", now=datetime(2026, 10, 28, 14, tzinfo=UTC))
    assert run.warnings == (("1004", "final-game DNP risk", "unlocked, 40% DNP"),)
    assert "final-game DNP risk" in page and "unlocked, 40% DNP" in page


def test_the_page_shows_the_chance_the_notification_printed(tmp_path):
    """A threshold alone does not say whether it can be met. The notification
    printed the chance beside it; the page used to drop it."""
    night = datetime(2026, 10, 28).toordinal()
    rules = [
        StandingRule("1001", "Player 1001", night, 44.0, 0.23, 0, 2),
        StandingRule("1002", "Player 1002", night, 51.0, float("nan"), 0, 1),
    ]
    report = a_digest(rules=rules)
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, report)
        stored = conn.execute(
            "SELECT sleeper_id, p_clear, games_after FROM recommendations ORDER BY sleeper_id"
        ).fetchall()
        run = advice.latest_run(conn, 1)

    page = advice.render(run, today="2026-10-28", now=datetime(2026, 10, 28, 14, tzinfo=UTC))
    assert [tuple(r) for r in stored] == [("1001", 0.23, 2), ("1002", None, 1)]
    assert "23%" in digest_mod.render(report) and "<td class=num>23%</td>" in page
    assert "<td class=num></td>" in page, "an unknown chance is blank, not 0%"


def test_a_repeated_warning_is_stored_once_not_fatal(tmp_path):
    warning = Warning("", "schedule", "no schedule", "no NBA schedule ingested", "no NBA schedule")
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, a_digest(calls=[call("1001")], warnings=[warning, warning]))
        stored = conn.execute("SELECT sleeper_id, kind FROM digest_warnings").fetchall()
        run = advice.latest_run(conn, 1)

    assert [tuple(r) for r in stored] == [("", "no schedule")]
    assert [i.sleeper_id for i in run.calls] == ["1001"], "the run was saved, calls and all"
    assert run.warnings[0][0] == "this run"


def test_a_digest_with_no_schedule_says_so_once_and_is_saved(tmp_path):
    """Both teams' slates used to report the missing schedule as a player warning
    with an empty id — two rows for one key, and the whole run rolled back."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=8))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1, 2], skip_nba=True)
    # Without a schedule only a past morning resolves to a week: one whose games
    # are all in the box scores.
    replay = (OPENING + timedelta(days=4)).isoformat()

    with connect(cfg) as conn:
        report = digest_mod.morning(conn, season.season, 1, replay, n_sims=50)
        digest_mod.persist(conn, report)
        stored = conn.execute("SELECT sleeper_id, kind FROM digest_warnings").fetchall()
        today = digest_mod.morning(conn, season.season, 1, season.today.isoformat(), n_sims=50)

    notices = [w for w in report.warnings if w.kind == "no schedule"]
    assert report.week == 1 and len(notices) == 1
    assert ("", "no schedule") in [tuple(r) for r in stored]
    assert "season is over" not in today.note and "no NBA schedule" in today.note


def test_a_call_past_its_tip_is_shown_closed(tmp_path):
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, a_digest(calls=[call("1001")]))
        run = advice.latest_run(conn, 1)

    before = advice.render(run, today="2026-10-28", now=datetime(2026, 10, 28, 14, tzinfo=UTC))
    after = advice.render(run, today="2026-10-28", now=datetime(2026, 10, 30, 1, tzinfo=UTC))
    assert "Lock now" in before and "by Thu 7:30pm tip" in before
    assert "Lock now" not in after and "closed at Thu 7:30pm tip" in after


# -------------------------------------------------------------- finding 9


def test_a_run_that_failed_part_way_does_not_vouch_for_the_data(tmp_path):
    """The review's reproduction, end to end: yesterday's complete ingest, then
    today's that refreshed players and died in the stats, after a commit."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=8))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1, 2], at="2026-10-28T10:30:00")

    season.play_through(OPENING + timedelta(days=9))
    real = FakeSleeperClient.week_stats

    def fail_on_week_two(self, s, week, season_type="regular"):
        if week == 2:
            raise RuntimeError("Sleeper is down")
        return real(self, s, week, season_type)

    with (
        mock.patch.object(FakeSleeperClient, "week_stats", fail_on_week_two),
        pytest.raises(RuntimeError, match="Sleeper is down"),
    ):
        ingest(season, cfg, weeks=[1, 2], at="2026-10-29T10:30:00")

    with connect(cfg) as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM ingest_runs ORDER BY run_id")]
        newest_log = conn.execute("SELECT MAX(finished_at) FROM ingest_log").fetchone()[0]
        fresh = digest_mod.last_ingest_at(conn, 2)
    assert statuses == ["complete", "running"], "the failed run committed week 1, and says so"
    assert newest_log > "2026-10-29"  # the old signal: fooled by today's partial run
    assert fresh.startswith("2026-10-28T10:30"), "the complete run is yesterday's"


def test_a_database_no_recording_ingest_has_touched_uses_the_old_log(tmp_path):
    with session(tmp_path / "t.db") as conn:
        conn.execute(
            "INSERT INTO ingest_log VALUES ('sleeper', 'players', 1, ?, ?)",
            ("2026-10-28T10:30:00+00:00", "2026-10-28T10:31:00+00:00"),
        )
        assert not runs.any_recorded(conn)
        assert digest_mod.last_ingest_at(conn) == "2026-10-28T10:31:00+00:00"


def test_a_run_that_skipped_the_nba_does_not_vouch_for_a_live_digest(tmp_path):
    """--skip-nba finishes every step it attempts, and so used to count as
    complete. Without the NBA's statuses nothing it wrote says last night is final."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=8))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1, 2])
    season.play_through(OPENING + timedelta(days=9))
    ingest(season, cfg, weeks=[1, 2], skip_nba=True)

    today = season.today.isoformat()
    now = datetime.combine(season.today, datetime.min.time(), UTC) + timedelta(hours=13)
    with connect(cfg) as conn:
        newest, vouching = runs.latest_complete(conn, 2), runs.latest_complete(conn, 2, live=True)
        report = digest_mod.morning(conn, season.season, 1, today, live=True, now=now)

    assert runs.skipped(newest) == {"nba"} and vouching["run_id"] < newest["run_id"]
    assert report.abstained and "--skip-nba" in report.note


def test_the_page_says_how_old_each_input_was(tmp_path):
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=8))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1, 2])

    with connect(cfg) as conn:
        digest_mod.persist(conn, a_digest(week=2))
        row = conn.execute("SELECT schedule_at, status_at FROM digest_runs").fetchone()
        run = advice.latest_run(conn, 1)

    page = advice.render(run, today="2026-10-28", now=datetime(2026, 10, 28, 14, tzinfo=UTC))
    assert row["schedule_at"] and row["status_at"]
    assert "Inputs: box scores" in page and "designations" in page
    assert "not recorded" not in page, page[page.index("Inputs:") :][:300]
