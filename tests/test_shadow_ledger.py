"""The shadow report on a week whose banked state came from the lock ledger.

The truth there is the manager's answers, not the final poll, which Sleeper
keeps rewriting. A LOCK call nobody answered blocks the week.
"""

from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta

import pytest
from live_fixture import SyntheticSeason, config_for, connect, ingest
from test_shadow import TUESDAY, WEEK, nine_am

from lockin import digest as digest_mod
from lockin import ledger, shadow
from lockin.core.projections import ProjectionParams


def run_morning(season, cfg, day: date, params) -> digest_mod.Digest:
    season.play_through(day - timedelta(days=1))
    ingest(season, cfg)
    with connect(cfg) as conn:
        report = digest_mod.morning(
            conn,
            season.season,
            1,
            day.isoformat(),
            n_sims=100,
            live=True,
            now=nine_am(day),
            params=params,
            ledger=True,
        )
        digest_mod.persist(conn, report, now=nine_am(day))
    return report


def answer(cfg, pid, day, decision, at):
    with connect(cfg) as conn:
        ledger.record(
            conn,
            week=WEEK,
            roster_id=1,
            sleeper_id=pid,
            game_day=day,
            decision=decision,
            source="page",
            recorded_at=at.isoformat(),
        )


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    """Week 2 with the ledger on: the first LOCK is taken and recorded, every
    other LOCK call is answered Passed."""
    season = SyntheticSeason()
    cfg = config_for(tmp_path_factory.mktemp("shadow-ledger"), season)
    params = ProjectionParams(min_pool_rows=60)
    taken: tuple[str, int] | None = None
    passed: list[tuple[str, int]] = []
    for offset in range(7):
        day = TUESDAY + timedelta(days=offset)
        report = run_morning(season, cfg, day, params)
        assert report.abstained or report.state_source == "ledger"
        later = nine_am(day) + timedelta(hours=2)
        for call in report.calls:
            if not call.lock or (call.sleeper_id, call.day) in [taken, *passed]:
                continue
            if taken is None:
                taken = (call.sleeper_id, call.day)
                season.locks[(WEEK, 1, call.sleeper_id)] = date.fromordinal(call.day)
                answer(cfg, call.sleeper_id, call.day, "LOCK", later)
            else:
                passed.append((call.sleeper_id, call.day))
                answer(cfg, call.sleeper_id, call.day, "PASS", later)
    assert taken is not None and passed, "the synthetic week must produce LOCK calls"
    return season, cfg, taken, passed


def report_for(season, cfg) -> shadow.ShadowReport:
    with connect(cfg) as conn:
        return shadow.build(conn, season.season, 1)


def test_a_ledger_week_is_judged_by_its_answers(played):
    season, cfg, taken, passed = played
    report = report_for(season, cfg)
    week = next(w for w in report.weeks if w.week == WEEK)
    assert week.ledger
    assert week.state_checked > 0 and week.state_misses == 0, report.misses
    assert not week.failed_inference and not week.uncheckable and not week.final_incomplete
    assert week.calls[shadow.UNANSWERED] == 0
    verdicts = {(c.sleeper_id, c.for_day): c.verdict for c in report.calls if c.action == "LOCK"}
    assert verdicts[taken] == shadow.FOLLOWED
    for key in passed:
        expected = shadow.MOOT if key[0] == taken[0] and key[1] > taken[1] else shadow.OVERRIDDEN
        assert verdicts[key] == expected, key
    assert "banked state from the lock ledger" in shadow.render(report)


def test_an_unanswered_lock_call_blocks_the_week(played, tmp_path):
    season, cfg, _, passed = played
    copy = config_for(tmp_path, season)
    shutil.copy(cfg.db_path, copy.db_path)
    pid, day = passed[0]
    with connect(copy) as conn:
        conn.execute("DELETE FROM lock_ledger WHERE sleeper_id = ? AND game_day = ?", (pid, day))
    report = report_for(season, copy)
    week = next(w for w in report.weeks if w.week == WEEK)
    assert week.calls[shadow.UNANSWERED] >= 1 and not week.clean
    assert "UNANSWERED" in shadow.render(report)


@pytest.mark.parametrize(
    ("action", "answers", "expected"),
    [
        pytest.param("LOCK", {("p", 5): "LOCK"}, shadow.FOLLOWED, id="locked as told"),
        pytest.param("LOCK", {("p", 5): "PASS"}, shadow.OVERRIDDEN, id="passed a lock"),
        pytest.param("PASS", {("p", 5): "LOCK"}, shadow.OVERRIDDEN, id="locked a pass"),
        pytest.param("PASS", {}, shadow.FOLLOWED, id="a pass needs no answer"),
        pytest.param("LOCK", {}, shadow.UNANSWERED, id="a lock does"),
        pytest.param("LOCK", {("p", 3): "LOCK"}, shadow.MOOT, id="banked earlier"),
        pytest.param("LOCK", {("p", 7): "LOCK"}, shadow.OVERRIDDEN, id="banked later"),
    ],
)
def test_each_ledger_verdict(action, answers, expected):
    recorded = {
        key: ledger.Answer(key[0], key[1], decision, "2026-11-01T00:00:00+00:00")
        for key, decision in answers.items()
    }
    assert shadow.ledger_verdict(action, 5, "p", recorded)[0] == expected


def copy_of(played, tmp_path):
    season, cfg, taken, passed = played
    copy = config_for(tmp_path, season)
    shutil.copy(cfg.db_path, copy.db_path)
    return season, copy, taken


def deadline_and_runs(conn, pid, day):
    """The call's printed deadline, and the week's ledger runs, oldest first."""
    tip = conn.execute(
        "SELECT MIN(expires_utc) FROM recommendations WHERE sleeper_id = ? AND for_day = ?",
        (pid, day),
    ).fetchone()[0]
    runs = conn.execute(
        "SELECT run_id, generated_at FROM digest_runs WHERE week = ? AND state_source = 'ledger'"
        " AND NOT abstained ORDER BY generated_at",
        (WEEK,),
    ).fetchall()
    return datetime.fromisoformat(tip.replace("Z", "+00:00")), [
        (r[0], datetime.fromisoformat(r[1])) for r in runs
    ]


def test_a_lock_whose_window_closed_before_a_digest_is_a_miss_however_soon_tapped(played, tmp_path):
    """His next tip passed before the run, so the lock was made before it: tapping
    a minute after the run does not excuse a BANKED list without him."""
    season, copy, (pid, day) = copy_of(played, tmp_path)
    with connect(copy) as conn:
        tip, runs = deadline_and_runs(conn, pid, day)
        run_id, ran_at = next((r, at) for r, at in runs if at > tip)
        conn.execute("DELETE FROM digest_banked WHERE run_id = ? AND sleeper_id = ?", (run_id, pid))
        conn.execute(
            "UPDATE lock_ledger SET recorded_at = ? WHERE sleeper_id = ? AND game_day = ?",
            ((ran_at + timedelta(minutes=1)).isoformat(), pid, day),
        )
    report = report_for(season, copy)
    [miss] = [m for m in report.misses if m.sleeper_id == pid]
    assert "before his next tip, recorded after this digest" in miss.detail
    assert not next(w for w in report.weeks if w.week == WEEK).clean
    verdict = next(c for c in report.calls if (c.sleeper_id, c.for_day) == (pid, day)).verdict
    assert verdict == shadow.FOLLOWED, "the final answer alone would have passed it"


def test_a_lock_tapped_while_its_window_was_open_faults_no_earlier_morning(played, tmp_path):
    """Played Monday, next tips Friday, locked and tapped Thursday: Tuesday's and
    Wednesday's digests were right to price him unlocked."""
    season, copy, (pid, day) = copy_of(played, tmp_path)
    with connect(copy) as conn:
        tip, runs = deadline_and_runs(conn, pid, day)
        tapped = tip - timedelta(minutes=1)
        for run_id, at in runs:
            if at < tapped:
                conn.execute(
                    "DELETE FROM digest_banked WHERE run_id = ? AND sleeper_id = ?", (run_id, pid)
                )
        conn.execute(
            "UPDATE lock_ledger SET recorded_at = ? WHERE sleeper_id = ? AND game_day = ?",
            (tapped.isoformat(), pid, day),
        )
    report = report_for(season, copy)
    assert not [m for m in report.misses if m.sleeper_id == pid]


def test_a_banked_score_that_is_not_the_locked_games_is_a_miss(played, tmp_path):
    """Corrected from an 18-point game to a 43-point one: a morning that banked 18
    ran on the wrong record, though the player is banked either way."""
    season, copy, (pid, day) = copy_of(played, tmp_path)
    with connect(copy) as conn:
        run_id, have = conn.execute(
            "SELECT run_id, score FROM digest_banked WHERE sleeper_id = ? LIMIT 1", (pid,)
        ).fetchone()
        conn.execute(
            "UPDATE digest_banked SET score = ? WHERE run_id = ? AND sleeper_id = ?",
            (have - 25.0, run_id, pid),
        )
    report = report_for(season, copy)
    [miss] = [m for m in report.misses if m.sleeper_id == pid]
    assert miss.detail.startswith(f"banked {have - 25.0:.1f}, but the lock was {have:.1f}")
    assert not next(w for w in report.weeks if w.week == WEEK).clean


def test_a_lock_recorded_before_the_next_digest_is_not_a_miss(played):
    season, cfg, (pid, _), _ = played
    report = report_for(season, cfg)
    assert not [m for m in report.misses if m.sleeper_id == pid]


def test_a_lock_undone_after_mornings_banked_it_is_a_miss(played, tmp_path):
    season, copy, (pid, day) = copy_of(played, tmp_path)
    answer(copy, pid, day, "UNDO", nine_am(TUESDAY + timedelta(days=7)))
    report = report_for(season, copy)
    undone = [m for m in report.misses if m.sleeper_id == pid]
    assert undone and all("final answer is not a lock" in m.detail for m in undone)


def incomplete_final_poll(conn) -> None:
    stamp = conn.execute(
        "SELECT MAX(observed_at) FROM weekly_matchup_teams WHERE week = ? AND roster_id = 1",
        (WEEK,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE weekly_matchup_teams SET poll_complete = 0"
        " WHERE week = ? AND roster_id = 1 AND observed_at = ?",
        (WEEK, stamp),
    )


def test_poll_read_runs_still_need_a_whole_final_poll(played, tmp_path):
    """A week that switched to the ledger midweek: its earlier runs read the poll."""
    season, copy, _ = copy_of(played, tmp_path)
    with connect(copy) as conn:
        incomplete_final_poll(conn)
        first = conn.execute(
            "SELECT run_id FROM digest_runs WHERE week = ? AND NOT abstained"
            " ORDER BY generated_at LIMIT 1",
            (WEEK,),
        ).fetchone()[0]
        conn.execute("UPDATE digest_runs SET state_source = 'inferred' WHERE run_id = ?", (first,))
    week = next(w for w in report_for(season, copy).weeks if w.week == WEEK)
    assert week.final_incomplete and not week.clean


def test_an_all_ledger_week_does_not_need_the_final_poll(played, tmp_path):
    season, copy, _ = copy_of(played, tmp_path)
    with connect(copy) as conn:
        incomplete_final_poll(conn)
    week = next(w for w in report_for(season, copy).weeks if w.week == WEEK)
    assert not week.final_incomplete


def test_three_mornings_inside_a_long_window_are_not_faulted(monkeypatch):
    """Played Monday, next tips Friday 00:00 UTC, locked and tapped Thursday
    evening: Tuesday's, Wednesday's and Thursday's digests all ran while the
    decision was open. The old next-run rule faulted Tuesday and Wednesday."""
    import sqlite3

    from lockin.store.db import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    monday = date(2026, 10, 26)
    for i, as_of in enumerate(("2026-10-27", "2026-10-28", "2026-10-29")):
        run = f"r{i}"
        conn.execute(
            "INSERT INTO digest_runs (run_id, generated_at, roster_id, as_of, week, state_source,"
            " abstained, state_supplied) VALUES (?, ?, 1, ?, ?, 'ledger', 0, 0)",
            (run, f"{as_of}T13:00:00+00:00", as_of, WEEK),
        )
        conn.execute(
            "INSERT INTO recommendations (generated_at, week, roster_id, sleeper_id, action,"
            " for_day, run_id, expires_utc) VALUES (?, ?, 1, 'p', 'LOCK', ?, ?, ?)",
            (f"{as_of}T13:00:00+00:00", WEEK, monday.toordinal(), run, "2026-10-31T00:00:00Z"),
        )
    answers = {
        ("p", monday.toordinal()): ledger.Answer(
            "p", monday.toordinal(), "LOCK", "2026-10-30T23:00:00+00:00"
        )
    }
    runs = conn.execute("SELECT * FROM digest_runs ORDER BY generated_at").fetchall()
    monkeypatch.setattr(shadow, "_game_scores", lambda *a: {})
    report, summary = shadow.ShadowReport(), shadow.WeekSummary(week=WEEK)
    shadow._ledger_state(conn, "2026", report, summary, runs, answers)
    assert report.misses == [] and summary.state_checked == 3


def test_a_banked_lock_with_no_final_box_score_is_unverified(played, monkeypatch):
    """With nothing to check the banked score against, it is not taken on trust."""
    season, cfg, (pid, day), _ = played
    monkeypatch.setattr(shadow, "_game_scores", lambda *a: {})
    report = report_for(season, cfg)
    week = next(w for w in report.weeks if w.week == WEEK)
    mine = [u for u in report.unverified if u.sleeper_id == pid]
    assert mine and all(u.detail.startswith("no box score for the locked game") for u in mine)
    assert week.state_unverified >= len(mine) and not week.clean


def ledger_runs(conn, mornings):
    """Ledger runs reading the ledger at 13:00 UTC and saved at 13:05, each calling
    player p's Monday game with a deadline."""
    monday = date(2026, 10, 26)
    for i, (as_of, expires) in enumerate(mornings):
        conn.execute(
            "INSERT INTO digest_runs (run_id, generated_at, ledger_read_at, roster_id, as_of,"
            " week, state_source, abstained, state_supplied)"
            " VALUES (?, ?, ?, 1, ?, ?, 'ledger', 0, 0)",
            (f"r{i}", f"{as_of}T13:05:00+00:00", f"{as_of}T13:00:00+00:00", as_of, WEEK),
        )
        conn.execute(
            "INSERT INTO recommendations (generated_at, week, roster_id, sleeper_id, action,"
            " for_day, run_id, expires_utc) VALUES (?, ?, 1, 'p', 'LOCK', ?, ?, ?)",
            (f"{as_of}T13:05:00+00:00", WEEK, monday.toordinal(), f"r{i}", expires),
        )
    return monday.toordinal(), conn.execute(
        "SELECT * FROM digest_runs ORDER BY generated_at"
    ).fetchall()


def test_a_postponed_tip_judges_each_morning_by_the_deadline_it_knew(monkeypatch):
    """Thursday's tip moved to Friday evening, and Friday's digest printed it. A lock
    made Friday afternoon was still open when Friday's digest ran."""
    import sqlite3

    from lockin.store.db import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    day, runs = ledger_runs(
        conn,
        [
            ("2026-10-29", "2026-10-30T00:00:00+00:00"),  # Thursday night's tip
            ("2026-10-30", "2026-10-31T00:00:00+00:00"),  # postponed to Friday night
        ],
    )
    answers = {("p", day): ledger.Answer("p", day, "LOCK", "2026-10-30T20:00:00+00:00")}
    monkeypatch.setattr(shadow, "_game_scores", lambda *a: {})
    report, summary = shadow.ShadowReport(), shadow.WeekSummary(week=WEEK)
    shadow._ledger_state(conn, "2026", report, summary, runs, answers)
    assert report.misses == []


def test_the_deadline_in_effect_is_the_latest_published_by_then():
    from datetime import UTC
    from datetime import datetime as dt

    thu, fri = dt(2026, 10, 30, tzinfo=UTC), dt(2026, 10, 31, tzinfo=UTC)
    published = [(dt(2026, 10, 29, 13, tzinfo=UTC), thu), (dt(2026, 10, 30, 13, tzinfo=UTC), fri)]
    assert shadow.deadline_in_effect(published, dt(2026, 10, 29, 14, tzinfo=UTC)) == thu
    assert shadow.deadline_in_effect(published, dt(2026, 10, 30, 13, tzinfo=UTC)) == fri
    assert shadow.deadline_in_effect(published, dt(2026, 10, 28, tzinfo=UTC)) == thu
    assert shadow.deadline_in_effect([], dt(2026, 10, 28, tzinfo=UTC)) is None


def test_a_tap_made_while_the_digest_computed_does_not_fault_it(monkeypatch):
    """Read the ledger at 13:00, saved at 13:05, tap at 13:02 with the tip hours off."""
    import sqlite3

    from lockin.store.db import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    day, runs = ledger_runs(conn, [("2026-10-29", "2026-10-30T00:00:00+00:00")])
    answers = {("p", day): ledger.Answer("p", day, "LOCK", "2026-10-29T13:02:00+00:00")}
    monkeypatch.setattr(shadow, "_game_scores", lambda *a: {})
    report, summary = shadow.ShadowReport(), shadow.WeekSummary(week=WEEK)
    shadow._ledger_state(conn, "2026", report, summary, runs, answers)
    assert report.misses == []


def test_a_ledger_miss_names_the_ledger_as_its_evidence(monkeypatch):
    """Judged by the recorded answer and the box score, not the final poll."""
    import sqlite3

    from lockin.store.db import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    day, runs = ledger_runs(conn, [("2026-10-29", "2026-10-27T00:00:00+00:00")])
    answers = {("p", day): ledger.Answer("p", day, "LOCK", "2026-10-27T01:00:00+00:00")}
    monkeypatch.setattr(shadow, "_game_scores", lambda *a: {})
    report, summary = shadow.ShadowReport(), shadow.WeekSummary(week=WEEK)
    shadow._ledger_state(conn, "2026", report, summary, runs, answers)
    [miss] = report.misses
    assert miss.evidence == shadow.LEDGER_EVIDENCE
    summary.runs = 1
    report.weeks.append(summary)
    text = shadow.render(report)
    assert "LEDGER STATE" in text and "disagreed with the final poll" not in text
