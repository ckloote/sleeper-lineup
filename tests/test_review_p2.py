"""Persisted synthetic regressions for the five September 25 findings."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from live_fixture import OPENING, SyntheticSeason, config_for, connect, ingest
from test_runs import a_digest, call

from lockin import advice, calendar, digest, shadow
from lockin.core.policy import Game
from lockin.core.projections import ProjectionParams
from lockin.core.winprob import evaluate_lock, lock_threshold
from lockin.store import runs
from lockin.store.db import apply_schema, session


@pytest.fixture
def live(tmp_path):
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=7))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1, 2])
    return season, cfg


def morning(conn, season, **kwargs):
    return digest.morning(
        conn,
        season.season,
        1,
        season.today.isoformat(),
        live=True,
        now=datetime.combine(season.today, datetime.min.time(), UTC) + timedelta(hours=13),
        params=ProjectionParams(min_pool_rows=60),
        n_sims=30,
        **kwargs,
    )


def test_calendar_guard_precedes_context_and_persists(live):
    season, cfg = live
    with connect(cfg) as conn:
        row = conn.execute("SELECT payload_json FROM league_settings").fetchone()
        payload = json.loads(row[0])
        payload["settings"]["leg"] = 4
        conn.execute("UPDATE league_settings SET payload_json=?", (json.dumps(payload),))
        with patch("lockin.digest.load_context", side_effect=AssertionError("too late")):
            report = morning(conn, season)
        assert report.abstained and not report.calls and not report.rules
        assert "week 4" in report.note and "week 2" in report.note
        assert season.today.isoformat() in report.note
        digest.persist(conn, report)
        assert "week 4" in advice.render(advice.latest_run(conn, 1))
        assert "week 4" in digest.render(report)
        replay = digest.morning(conn, season.season, 1, season.today.isoformat(), n_sims=20)
        assert replay.note != report.note
        payload["settings"]["leg"] = 1
        conn.execute("UPDATE league_settings SET payload_json=?", (json.dumps(payload),))
        assert calendar.disagreement(conn, season.season, "2026-10-26") is None


@pytest.mark.parametrize("stamp", [None, "broken", "2026-10-29T00:00:00"])
def test_unknown_deadline_suppresses_only_affected_calls(live, stamp):
    season, cfg = live
    with connect(cfg) as conn:
        original = morning(conn, season)
        assert len(original.calls) > 1
        target = original.calls[0]
        real = digest.week_slate

        def slate(*args, **kwargs):
            result = real(*args, **kwargs)
            for key in result.tipoff:
                if key[0] == target.sleeper_id:
                    result.tipoff[key] = stamp
            return result

        with patch("lockin.digest.week_slate", side_effect=slate):
            report = morning(conn, season)
        assert target.sleeper_id not in {c.sleeper_id for c in report.calls}
        assert len(report.calls) == len(original.calls) - 1
        assert report.p_win == original.p_win or report.p_win is not None
        digest.persist(conn, report)
        saved = advice.latest_run(conn, 1)
        assert any(w[1] == "unknown deadline" for w in saved.warnings)
        legacy = replace(call("old"), expires_utc=stamp)
        digest.persist(conn, a_digest(calls=[legacy]))
        page = advice.render(advice.latest_run(conn, 1))
        assert "unknown deadline" in page and "Lock now" not in page


def test_exact_tipoff_is_closed(live):
    season, cfg = live
    with connect(cfg) as conn:
        original = morning(conn, season)
        target = original.calls[0]
        report = digest.morning(
            conn,
            season.season,
            1,
            season.today.isoformat(),
            live=True,
            now=datetime.fromisoformat(target.expires_utc.replace("Z", "+00:00")),
            params=ProjectionParams(min_pool_rows=60),
            n_sims=30,
        )
        assert target.sleeper_id not in {c.sleeper_id for c in report.calls}
    item = advice.Item("p", "P", "LOCK", 1, 11.5, 0.6, 0.5, "", target.expires_utc)
    assert item.deadline_status(digest.valid_deadline(target.expires_utc)) == "closed"


@pytest.mark.parametrize(
    "start,end,accepted",
    [
        ("06:00", "06:30", False),
        ("06:59", "07:01", False),
        ("07:00", "07:01", True),
        ("08:00", "09:00", True),
    ],
)
def test_fetch_request_boundary_and_selected_provenance(live, start, end, accepted):
    season, cfg = live
    with connect(cfg) as conn:
        run = runs.latest_covering(conn, 2)
        day = season.today.isoformat()
        started, finished = (f"{day}T{x}:00+00:00" for x in (start, end))
        conn.execute("UPDATE ingest_runs SET started_at=?", (f"{day}T01:00:00+00:00",))
        conn.execute(
            "UPDATE ingest_stats_fetches SET started_at=?, finished_at=? WHERE week=2",
            (started, finished),
        )
        assert (digest.stale_ingest(conn, 2, season.today.toordinal() - 1) is None) == accepted
        report = morning(conn, season, locked={})
        assert report.abstained != accepted
        if accepted:
            assert report.ingest_run_id == run["run_id"]
            runs.start(conn, [2], day)
            digest.persist(conn, report)
            saved = conn.execute("SELECT * FROM digest_runs").fetchone()
            assert saved["ingest_run_id"] == run["run_id"]
            assert saved["stats_fetch_started_at"] == started
            assert advice.latest_run(conn, 1).stats_fetch_started_at == started
            assert "complete" in digest.stale_ingest(conn, 2, season.today.toordinal() - 1)


def test_missing_evidence_and_unrelated_refresh_cannot_certify(live):
    season, cfg = live
    with connect(cfg) as conn:
        conn.execute("DELETE FROM ingest_stats_fetches")
        conn.execute("UPDATE ingest_log SET finished_at='2099-01-01T00:00:00+00:00'")
        assert "run ingest again" in morning(conn, season, locked={}).note
        conn.execute("DROP TABLE ingest_stats_fetches")
        assert "run ingest again" in morning(conn, season).note
        apply_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM ingest_stats_fetches").fetchone()[0] == 0


def test_fetch_evidence_survives_checkpoint_and_matches_each_week(live):
    _, cfg = live
    with connect(cfg) as conn:
        rows = conn.execute("SELECT * FROM ingest_stats_fetches ORDER BY week").fetchall()
        assert [r["week"] for r in rows] == [1, 2]
        assert all(r["started_at"] <= r["finished_at"] for r in rows)


def test_a_week_named_twice_is_fetched_once(tmp_path):
    """`--weeks 1-2,2` crashed on the second week-2 evidence row, after a
    checkpoint had committed the run as running — which then blocked every live
    digest until a clean re-ingest."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=7))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1, 2, 2])
    with connect(cfg) as conn:
        run = conn.execute("SELECT status, weeks FROM ingest_runs").fetchone()
        assert (run["status"], json.loads(run["weeks"])) == ("complete", [1, 2])
        fetched = conn.execute("SELECT week FROM ingest_stats_fetches ORDER BY week").fetchall()
        assert [r["week"] for r in fetched] == [1, 2]


def test_half_point_probability_and_rendering(tmp_path):
    args = dict(
        banked=0.0,
        contributions=np.array([[11.0, 11.0]]),
        player=0,
        opponent=np.array([11.0, 11.0]),
    )
    assert lock_threshold(**args) == 11.5
    assert evaluate_lock(**args, lock_value=11.5).lock
    ctx = SimpleNamespace(
        source=SimpleNamespace(project_path=lambda *a, **k: np.full((20, 1), 11.5)),
        dnp_scale={},
    )
    assert (
        digest.clearing_chance(
            ctx, "p", [Game(0, 10, False, 0)], 2, 9, 10, 11.5, np.random.default_rng(0)
        )
        == 1
    )
    report = a_digest(
        calls=[replace(call("p"), break_even=11.5)],
        rules=[digest.StandingRule("p", "P", date(2026, 10, 28).toordinal(), 11.5, 1.0, 0, 1)],
    )
    assert "score 11.5 or more" in digest.render(report)
    with session(tmp_path / "t.db") as conn:
        digest.persist(conn, report)
        assert "score 11.5 or more" in advice.render(advice.latest_run(conn, 1))
        for row in conn.execute("SELECT threshold, rationale FROM recommendations"):
            assert row["threshold"] == 11.5 and "11.5 or more" in row["rationale"]


@pytest.fixture
def history(tmp_path):
    season = SyntheticSeason()
    season.play_through(date(2026, 11, 8))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1, 2, 3])
    with connect(cfg) as conn:
        for offset in range(14):
            day = date(2026, 10, 26) + timedelta(days=offset)
            report = a_digest(
                as_of=day.isoformat(),
                week=2 + offset // 7,
                known_through=day.toordinal() - 1,
                banked={},
            )
            digest.persist(
                conn,
                report,
                now=datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=13),
            )
    return season, cfg


def test_daily_inference_passes_without_lock_calls(history):
    season, cfg = history
    with connect(cfg) as conn:
        report = shadow.build(conn, season.season, 1)
        assert report.gate()[0], shadow.render(report)


def test_twelve_abstentions_fail_and_reruns_recover(history):
    season, cfg = history
    with connect(cfg) as conn:
        originals = conn.execute("SELECT * FROM digest_runs").fetchall()
        conn.execute(
            "UPDATE digest_runs SET abstained=1, state_source=NULL"
            " WHERE as_of NOT IN ('2026-10-26', '2026-11-02')"
        )
        report = shadow.build(conn, season.season, 1)
        assert not report.gate()[0]
        assert sum(len(w.failed_inference) for w in report.weeks) == 12
        for r in originals:
            if r["as_of"] in ("2026-10-26", "2026-11-02"):
                continue
            row = dict(
                r,
                run_id="rerun" + r["run_id"],
                generated_at=r["generated_at"].replace("13:00", "14:00"),
            )
            conn.execute(
                f"INSERT INTO digest_runs ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                list(row.values()),
            )
        assert shadow.build(conn, season.season, 1).gate()[0]
        conn.execute("INSERT INTO digest_banked VALUES (?, '1001', 999)", (originals[0]["run_id"],))
        assert not shadow.build(conn, season.season, 1).gate()[0]


@pytest.mark.parametrize("kind", ["supplied", "uncheckable", "missing_week", "partial"])
def test_shadow_coverage_failures(history, kind):
    season, cfg = history
    with connect(cfg) as conn:
        if kind == "supplied":
            conn.execute("UPDATE digest_runs SET state_source='supplied' WHERE as_of='2026-10-28'")
        elif kind == "uncheckable":
            conn.execute("UPDATE weekly_matchups SET counted_points=99999")
        elif kind == "missing_week":
            conn.execute("DELETE FROM digest_runs WHERE week=3")
        else:
            conn.execute("DELETE FROM digest_runs WHERE as_of='2026-10-26'")
        report = shadow.build(conn, season.season, 1)
        assert not report.gate()[0], shadow.render(report)
        if kind == "missing_week":
            assert len(report.weeks[-1].mornings_missing) == 7


def test_a_digest_run_by_hand_for_another_roster_is_not_owed_daily(history):
    """One look at the opponent (`lockin digest --roster 2`) made roster 2 owe a
    run every morning for the rest of the season, so the gate could never pass."""
    season, cfg = history
    with connect(cfg) as conn:
        digest.persist(
            conn,
            a_digest(roster_id=2, as_of="2026-10-26", banked={}),
            now=datetime(2026, 10, 26, 14, tzinfo=UTC),
        )
        report = shadow.build(conn, season.season, 1)
        assert report.gate()[0], shadow.render(report)
        assert "roster 2" not in shadow.render(report)


def test_monday_is_owed_a_run_not_an_inference(history):
    """Before Sleeper rolls its week over, the 06:30 ingest fetches only the week
    just gone, and Monday's digest abstains for want of the new one. Nothing is
    banked before a week's first game, so there was no state to read anyway."""
    season, cfg = history
    with connect(cfg) as conn:
        for monday, week in (("2026-10-26", 2), ("2026-11-02", 3)):
            conn.execute(
                "UPDATE digest_runs SET abstained=1, state_source=NULL, note=? WHERE as_of=?",
                (f"no complete newest ingest covering week {week}.", monday),
            )
        report = shadow.build(conn, season.season, 1)
        assert report.gate()[0], shadow.render(report)
        conn.execute("DELETE FROM digest_runs WHERE as_of='2026-11-02'")
        report = shadow.build(conn, season.season, 1)
        assert report.weeks[-1].mornings_missing == ["roster 1 2026-11-02"]
        assert not report.gate()[0]


def test_finalized_weeks_are_named_before_any_live_run(history):
    """Tracking starts at the first live run, so with none there are no weeks to
    report — which the header used to read as none having been scored."""
    season, cfg = history
    with connect(cfg) as conn:
        conn.execute("DELETE FROM digest_runs")
        report = shadow.build(conn, season.season, 1)
    assert report.finalized == 3 and not report.weeks
    assert shadow.render(report).startswith("SHADOW  weeks 1-3 finalized, and no live digest")
    assert report.gate() == (False, f"0 finalized week(s) of tracking; need {shadow.GATE_WEEKS}")


@pytest.mark.parametrize(
    "kind", ["verified", "missing_opponent", "missing_starters", "stale", "incomplete"]
)
def test_only_verified_complete_no_matchup_poll_is_exempt(live, kind):
    season, cfg = live
    with connect(cfg) as conn:
        if kind == "missing_opponent":
            conn.execute("DELETE FROM weekly_matchups WHERE roster_id=2")
        else:
            conn.execute("UPDATE weekly_matchups SET matchup_id=NULL WHERE roster_id=1")
            conn.execute("UPDATE weekly_matchup_teams SET matchup_id=NULL WHERE roster_id=1")
        if kind == "missing_starters":
            conn.execute("DELETE FROM weekly_matchups WHERE roster_id=1 AND is_starter=1")
        if kind == "stale":
            conn.execute("UPDATE weekly_matchup_teams SET observed_at='2026-10-20T10:00:00+00:00'")
        if kind == "incomplete":
            conn.execute("UPDATE weekly_matchup_teams SET poll_complete=0")
        report = morning(conn, season)
        assert bool(report.verified_no_matchup) == (kind == "verified")
        digest.persist(conn, report)
        assert bool(conn.execute("SELECT verified_no_matchup FROM digest_runs").fetchone()[0]) == (
            kind == "verified"
        )


def test_shadow_exemptions_are_explicit_not_free_text(history):
    season, cfg = history
    with connect(cfg) as conn:
        conn.execute("UPDATE digest_runs SET state_source=NULL, note='no matchup this week'")
        assert not shadow.build(conn, season.season, 1).gate()[0]
        conn.execute("UPDATE digest_runs SET verified_no_matchup=1")
        report = shadow.build(conn, season.season, 1)
        assert report.gate()[0]
        assert sum(len(w.exemptions) for w in report.weeks) == 14


def test_retrospective_call_never_becomes_lock_now(tmp_path):
    with session(tmp_path / "t.db") as conn:
        report = a_digest(calls=[call("p")], retrospective=True)
        digest.persist(conn, report)
        page = advice.render(
            advice.latest_run(conn, 1),
            today=report.as_of,
            now=datetime(2026, 10, 28, 13, tzinfo=UTC),
        )
        assert "Historical replay" in page and "Lock now" not in page
        assert "HISTORICAL REPLAY" in digest.render(report)


def test_readonly_advice_tolerates_pre_evidence_schema(tmp_path):
    from lockin.store.db import connect_readonly

    path = tmp_path / "legacy.db"
    with session(path) as conn:
        digest.persist(conn, a_digest(calls=[replace(call("p"), expires_utc=None)]))
        for column in (
            "stats_fetch_started_at",
            "stats_fetch_finished_at",
            "verified_no_matchup",
            "retrospective",
        ):
            conn.execute(f"ALTER TABLE digest_runs DROP COLUMN {column}")
        conn.execute("DROP TABLE ingest_stats_fetches")
    conn = connect_readonly(path)
    try:
        page = advice.render(advice.latest_run(conn, 1))
        assert "unknown deadline" in page and "Lock now" not in page
    finally:
        conn.close()
