"""Persisted synthetic regressions for the five September 25 findings."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from live_fixture import (
    OPENING,
    ROSTER_POSITIONS,
    SCALED,
    SyntheticSeason,
    config_for,
    connect,
    ingest,
)
from test_runs import a_digest, call

from lockin import advice, calendar, clock, digest, shadow
from lockin.core.policy import Game
from lockin.core.projections import ProjectionParams
from lockin.core.winprob import evaluate_lock, lock_threshold
from lockin.ingest import sleeper as sleeper_ingest
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
        assert report.calls == original.calls[1:]
        assert report.p_win == original.p_win
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
    assert item.deadline_status(clock.aware_utc(target.expires_utc)) == "closed"


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
        assert (
            digest.stats_evidence(conn, 2, season.today.toordinal() - 1).problem is None
        ) == accepted
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
            assert (
                "complete" in digest.stats_evidence(conn, 2, season.today.toordinal() - 1).problem
            )


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


TIED = date(2026, 10, 28)
"""1003 scores 11.5 here and in his final game of week 2, on 10-30."""


def persisted_history(tmp_path, *, tie: bool = False):
    """Weeks 2-3 finalized, with an inferred, nothing-banked run every morning.

    Nobody locks, so nothing-banked is the right reading every morning. Left to
    the seed, 1003's 10-28 game ties his final one, and then the final score
    cannot say whether he rode or banked 10-28. Without ``tie`` he sits that
    game out, so every reading can be settled.
    """
    season = SyntheticSeason()
    if not tie:
        team = season.players["1003"].team
        game = next(f for f in season.games_for(team, 2) if f.date == TIED)
        season.dnp.add((game.sleeper_game_id, "1003"))
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


@pytest.fixture
def history(tmp_path):
    return persisted_history(tmp_path)


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


def test_one_checkable_starter_does_not_vouch_for_the_rest(history):
    """Review 2026-09-26, finding 2. With every roster-1 starter but 1001 missing
    his final score, each morning still had one check, so the weeks passed — and
    a wrong banked score for 1002 on every morning was skipped with his evidence."""
    season, cfg = history
    with connect(cfg) as conn:
        conn.execute(
            "UPDATE weekly_matchups SET counted_points=NULL"
            " WHERE roster_id=1 AND sleeper_id != '1001'"
        )
        for (run_id,) in conn.execute("SELECT run_id FROM digest_runs").fetchall():
            conn.execute("INSERT INTO digest_banked VALUES (?, '1002', 999)", (run_id,))
        report = shadow.build(conn, season.season, 1)
    assert not report.gate()[0], shadow.render(report)
    # 1006 never has two games behind him on a morning of either week — his
    # second in week 3 is its last night — so his readings need no final score.
    assert {u.sleeper_id for u in report.unverified} == {"1002", "1003", "1004", "1005"}
    assert {u.detail for u in report.unverified} == {"no final counted score"}
    # Before his second game no lock can show, whatever the final score says.
    assert any(m.sleeper_id == "1002" and m.as_of == "2026-10-26" for m in report.misses)
    assert "UNVERIFIED" in shadow.render(report)


def test_a_mix_of_checkable_and_unverifiable_starters_is_not_clean(history):
    season, cfg = history
    with connect(cfg) as conn:
        conn.execute(
            "UPDATE weekly_matchups SET counted_points=NULL WHERE roster_id=1 AND sleeper_id='1002'"
        )
        rerun = dict(conn.execute("SELECT * FROM digest_runs WHERE as_of='2026-10-30'").fetchone())
        rerun |= {"run_id": "rerun", "generated_at": rerun["generated_at"].replace("13:", "14:")}
        conn.execute(
            f"INSERT INTO digest_runs ({','.join(rerun)}) VALUES ({','.join('?' * len(rerun))})",
            list(rerun.values()),
        )
        report = shadow.build(conn, season.season, 1)
    assert not report.gate()[0] and not report.misses
    assert {u.sleeper_id for u in report.unverified} == {"1002"}
    keys = [(u.as_of, u.sleeper_id) for u in report.unverified]
    assert len(keys) == len(set(keys))  # once per morning, however many runs
    assert all(w.state_unverified and w.state_checked and not w.uncheckable for w in report.weeks)


def test_a_final_score_tied_with_an_earlier_game_is_unverified(tmp_path):
    """The seed's own tie: 1003 may have banked 10-28 or ridden to 10-30, and from
    the morning after his second game the two readings differ."""
    season, cfg = persisted_history(tmp_path, tie=True)
    with connect(cfg) as conn:
        report = shadow.build(conn, season.season, 1)
    assert report.gate() == (False, "week(s) 2 not clean")
    assert [(u.sleeper_id, u.as_of) for u in report.unverified] == [
        ("1003", day) for day in ("2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01")
    ]
    assert report.unverified[0].detail == "final score ties his last game with an earlier one"
    assert not report.misses and report.weeks[-1].clean


def test_a_final_poll_missing_starters_does_not_qualify_a_week(history):
    """Review 2026-09-26 follow-up, finding 1. The starters to check came from the
    final poll, so one it dropped left the check too, unless a run had banked him.
    Nothing is banked here: a final poll that kept only 1001 passed both weeks on
    one check a morning."""
    season, cfg = history
    with connect(cfg) as conn:
        conn.execute(
            "UPDATE weekly_matchups SET is_starter=0 WHERE roster_id=1 AND sleeper_id != '1001'"
        )
        conn.execute("UPDATE weekly_matchup_teams SET poll_complete=0 WHERE roster_id=1")
        report = shadow.build(conn, season.season, 1)
    assert report.gate() == (False, "week(s) 2, 3 not clean"), shadow.render(report)
    assert all(w.final_incomplete for w in report.weeks)
    assert not report.misses and not report.unverified
    assert "incomplete final poll" in shadow.render(report)


@pytest.mark.parametrize("flag", [0, None], ids=["incomplete", "legacy"])
def test_only_a_whole_final_poll_qualifies_its_week(history, flag):
    """Every starter is still there and checks out; the poll's own word that it
    is not whole, or its silence, is enough to keep the week from qualifying."""
    season, cfg = history
    with connect(cfg) as conn:
        conn.execute(
            "UPDATE weekly_matchup_teams SET poll_complete=? WHERE roster_id=1 AND week=3", (flag,)
        )
        report = shadow.build(conn, season.season, 1)
    assert report.gate() == (False, "week(s) 3 not clean"), shadow.render(report)
    week2, week3 = report.weeks
    assert week2.clean and not week2.final_incomplete
    assert week3.final_incomplete and week3.state_checked
    assert not week3.state_misses and not week3.state_unverified


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


def test_an_eliminated_team_with_an_empty_slot_has_no_matchup_not_missing_data(tmp_path):
    """In weeks 23-24 an eliminated team often leaves a slot empty ("0"). Its poll
    is whole, but was refused as incomplete, so every morning's digest abstained
    with "opponent data missing"."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=7))
    cfg = config_for(tmp_path, season)
    real = SyntheticSeason.matchups_payload

    def eliminated(self, week):
        payload = real(self, week)
        for team in payload:
            if team["roster_id"] == 1:
                team["matchup_id"] = None
                team["starters"][-1] = "0"
        return payload

    with patch.object(SyntheticSeason, "matchups_payload", eliminated):
        ingest(season, cfg, weeks=[1, 2])
    with connect(cfg) as conn:
        report = morning(conn, season)
    assert report.verified_no_matchup and not report.abstained
    assert report.note == "roster 1 has no matchup in week 2; nothing to decide"


def ingest_with_starters(tmp_path, roster_id, change):
    """The `live` fixture's season, with one roster's `starters` array rewritten."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=7))
    cfg = config_for(tmp_path, season)
    real = SyntheticSeason.matchups_payload

    def rewritten(self, week):
        payload = real(self, week)
        for team in payload:
            if team["roster_id"] == roster_id:
                team["starters"] = change(team["starters"])
        return payload

    with patch.object(SyntheticSeason, "matchups_payload", rewritten):
        ingest(season, cfg, weeks=[1, 2])
    return season, cfg


@pytest.mark.parametrize("roster_id", [1, 2], ids=["mine", "opponent"])
@pytest.mark.parametrize(
    "change", [lambda s: s[:-1], lambda s: [*s[:-1], s[0]]], ids=["missing", "duplicate"]
)
def test_a_poll_missing_a_starter_gives_no_live_advice(tmp_path, roster_id, change):
    """Review 2026-09-26, finding 1. Ingest marked the poll incomplete, but the
    lineup was read from it anyway: the missing starter left the simulation and
    the lock-state reading together, and the digest advised on five of six.

    The follow-up's finding 2: a poll that named its first starter again in
    place of the sixth was marked whole. Ingest keys the lineup by player, so
    the repeat filled one slot, and the digest advised on five of six again."""
    season, cfg = ingest_with_starters(tmp_path, roster_id, change)
    with connect(cfg) as conn:
        assert (
            conn.execute(
                "SELECT poll_complete FROM weekly_matchup_teams_latest"
                " WHERE week = 2 AND roster_id = ?",
                (roster_id,),
            ).fetchone()[0]
            == 0
        )
        report = morning(conn, season)
        assert report.abstained and not report.calls and not report.rules
        assert report.p_win is None
        assert f"roster {roster_id}" in report.note and "lineup" in report.note
        digest.persist(conn, report)
        assert "lineup" in advice.render(advice.latest_run(conn, 1))
        replay = digest.morning(
            conn, season.season, 1, season.today.isoformat(), n_sims=30, params=SCALED
        )
        assert replay.retrospective and not replay.abstained


@pytest.mark.parametrize("empty", [1, 2], ids=["one", "two"])
def test_an_empty_slot_in_a_matchup_is_a_whole_poll(tmp_path, empty):
    """An explicit empty slot ("0") accounts for the slot: advice goes ahead.
    Two of them are not a repeated starter."""
    season, cfg = ingest_with_starters(tmp_path, 1, lambda s: [*s[:-empty], *["0"] * empty])
    with connect(cfg) as conn:
        report = morning(conn, season)
    assert not report.abstained, report.note
    assert report.state_source == "inferred" and report.p_win is not None
    assert report.calls or report.rules


def test_a_poll_that_does_not_vouch_for_itself_gives_no_live_advice(live):
    """A poll written before completeness was recorded is not assumed whole."""
    season, cfg = live
    with connect(cfg) as conn:
        conn.execute("UPDATE weekly_matchup_teams SET poll_complete=NULL WHERE roster_id=2")
        report = morning(conn, season)
    assert report.abstained and "roster 2" in report.note


@pytest.mark.parametrize(
    ("change", "whole"),
    [
        (lambda t: t, True),
        (lambda t: t | {"starters": [*t["starters"][:-1], "0"]}, True),
        (lambda t: t | {"starters": [*t["starters"][:-2], "0", "0"]}, True),
        (lambda t: t | {"starters": t["starters"][:-1]}, False),
        (lambda t: t | {"starters": [*t["starters"][:-1], t["starters"][0]]}, False),
        (lambda t: t | {"starters": [*t["starters"][:-1], "9999"]}, False),
        (lambda t: t | {"starters": [*t["starters"][:-1], None]}, False),
        (lambda t: t | {"starters": None}, False),
    ],
    ids=[
        "full",
        "empty slot",
        "two empty slots",
        "slot missing",
        "duplicate starter",
        "not on roster",
        "null starter",
        "no lineup",
    ],
)
def test_a_whole_poll_names_every_slot(change, whole):
    team = SyntheticSeason().matchups_payload(1)[0]
    assert sleeper_ingest.poll_complete(change(team), ROSTER_POSITIONS) is whole


def test_shadow_exemptions_are_explicit_not_free_text(history):
    season, cfg = history
    with connect(cfg) as conn:
        conn.execute("UPDATE digest_runs SET state_source=NULL, note='no matchup this week'")
        assert not shadow.build(conn, season.season, 1).gate()[0]
        conn.execute("UPDATE digest_runs SET verified_no_matchup=1")
        report = shadow.build(conn, season.season, 1)
        assert report.gate()[0]
        assert sum(len(w.exemptions) for w in report.weeks) == 14


@pytest.mark.parametrize("shape", ["rules", "note"])
def test_a_replay_without_calls_is_still_labelled_historical(tmp_path, shape):
    """Review 2026-09-26, finding 3. The label lived in the calls section, so a
    replay with only standing rules — any Monday — read as tonight's orders."""
    tonight = date(2026, 10, 28).toordinal()
    if shape == "rules":
        report = a_digest(
            rules=[digest.StandingRule("p", "P", tonight, 30.5, 0.4, 0, 1)], retrospective=True
        )
    else:
        report = a_digest(note="week 2 has no countable games", retrospective=True)
    with session(tmp_path / "t.db") as conn:
        digest.persist(conn, report)
        page = advice.render(
            advice.latest_run(conn, 1),
            today=report.as_of,
            now=datetime(2026, 10, 28, 13, tzinfo=UTC),
        )
    assert 'data-warning="retrospective"' in page and "Historical replay" in page
    assert page.index('data-warning="retrospective"') < page.index("class=state")
    assert "Tonight" not in page and "Lock him" not in page
    assert "HISTORICAL REPLAY" in digest.render(report)
    assert "HISTORICAL REPLAY" in digest.render(report, compact=True)


def test_a_replayed_monday_is_labelled_historical_and_a_live_run_is_not(live):
    season, cfg = live
    with connect(cfg) as conn:
        replay = digest.morning(conn, season.season, 1, "2026-10-26", n_sims=30, params=SCALED)
        assert replay.retrospective and not replay.calls and replay.rules
        digest.persist(conn, replay)
        page = advice.render(advice.latest_run(conn, 1))
        assert "Historical replay" in page and "Tonight" not in page
        report = morning(conn, season)
        assert not report.abstained and report.rules
        now = datetime.combine(season.today, datetime.min.time(), UTC) + timedelta(hours=13)
        digest.persist(conn, report, now=now)
        page = advice.render(advice.latest_run(conn, 1), today=report.as_of, now=now)
    assert "Historical replay" not in page and "Tonight" in page


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
