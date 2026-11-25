"""The lock ledger: the manager's answers are the banked state.

Sleeper's matchup poll cannot show a lock (`lockin.ledger`), so with the ledger
on the digest reads what the manager recorded on the advice page, and nothing
from the poll.

The week is `test_lock_state.Week`: roster 1's A-E and roster 2's F, on
schedules built for the test, the morning of Wednesday 10-28. E locked Monday
in the simulated Sleeper, which the poll path reads; the ledger path must not.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime

import pytest
from live_fixture import connect
from test_lock_state import MON, TUE, WED, Week, said_on

from lockin import digest as digest_mod
from lockin import ledger
from lockin.core.policy import Game
from lockin.core.projections import ProjectionParams
from lockin.store.db import apply_schema

RUN = "2026-10-28T13:00:00+00:00"
BEFORE_RUN = "2026-10-28T02:00:00+00:00"
AFTER_RUN = "2026-10-28T14:00:00+00:00"


@pytest.fixture
def week(tmp_path):
    return Week().ingest(tmp_path)


def answer(week, pid, day, decision, *, at=BEFORE_RUN, roster=1, wk=2):
    with connect(week.cfg) as conn:
        ledger.record(
            conn,
            week=wk,
            roster_id=roster,
            sleeper_id=pid,
            game_day=day.toordinal(),
            decision=decision,
            source="cli",
            recorded_at=at,
        )


def ledger_digest(week, **kwargs):
    return week.digest(ledger=True, **kwargs)


# ------------------------------------------------------------------ the table


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c)
    yield c
    c.close()


def put(conn, pid, day, decision, at):
    ledger.record(
        conn,
        week=5,
        roster_id=4,
        sleeper_id=pid,
        game_day=day,
        decision=decision,
        source="page",
        recorded_at=at,
    )


def test_the_newest_answer_wins_and_undo_withdraws_it(conn):
    put(conn, "kat", 10, "LOCK", "2026-11-17T03:00:00+00:00")
    put(conn, "kat", 10, "PASS", "2026-11-17T03:05:00+00:00")
    put(conn, "allen", 9, "LOCK", "2026-11-17T03:06:00+00:00")
    put(conn, "allen", 9, "UNDO", "2026-11-17T03:07:00+00:00")
    got = ledger.answers(conn, 5, 4)
    assert {k: a.decision for k, a in got.items()} == {("kat", 10): "PASS"}


def test_an_answer_after_the_moment_described_is_not_read(conn):
    put(conn, "kat", 10, "LOCK", "2026-11-17T03:00:00+00:00")
    put(conn, "kat", 10, "PASS", "2026-11-17T20:00:00+00:00")
    got = ledger.answers(conn, 5, 4, before="2026-11-17T13:00:00+00:00")
    assert got[("kat", 10)].decision == "LOCK"
    assert ledger.answers(conn, 5, 4, before="2026-11-17T02:00:00+00:00") == {}
    assert not ledger.in_use(conn, 4, before="2026-11-17T02:00:00+00:00")
    assert ledger.in_use(conn, 4)


def test_a_bad_decision_or_source_is_refused(conn):
    with pytest.raises(ValueError):
        put(conn, "kat", 10, "MAYBE", "2026-11-17T03:00:00+00:00")
    with pytest.raises(ValueError):
        ledger.record(
            conn, week=5, roster_id=4, sleeper_id="k", game_day=1, decision="LOCK", source="web"
        )


def test_the_banked_score_is_the_games_not_one_typed_in(conn):
    games = {"kat": [Game(0, 10, True, 57.5), Game(1, 12, True, 50.5)]}
    put(conn, "kat", 10, "LOCK", "2026-11-17T03:00:00+00:00")
    got = ledger.state(conn, 5, 4, games, known_through=12, before=None)
    assert got.banked == {"kat": 57.5} and got.problems == []
    # A lock on a game that has not been played yet is not banked yet.
    assert ledger.state(conn, 5, 4, games, known_through=9, before=None).banked == {}


def test_a_lock_the_digest_cannot_use_is_reported_not_banked(conn):
    games = {"kat": [Game(0, 10, True, 57.5)]}
    put(conn, "stranger", 10, "LOCK", "2026-11-17T03:00:00+00:00")
    put(conn, "kat", 11, "LOCK", "2026-11-17T03:00:00+00:00")
    got = ledger.state(conn, 5, 4, games, known_through=12, before=None)
    assert got.banked == {}
    assert sorted(pid for pid, _ in got.problems) == ["kat", "stranger"]


def test_a_dnp_lock_banks_zero(conn):
    games = {"kat": [Game(0, 10, False, 0.0), Game(1, 12, True, 40.0)]}
    put(conn, "kat", 10, "LOCK", "2026-11-17T03:00:00+00:00")
    assert ledger.state(conn, 5, 4, games, known_through=12, before=None).banked == {"kat": 0.0}


def test_no_table_reads_as_no_answers():
    c = sqlite3.connect(":memory:")
    assert ledger.answers(c, 5, 4) == {} and not ledger.in_use(c, 4)


# ------------------------------------------------------------------ the digest


def test_with_the_ledger_on_the_poll_is_not_read(week):
    """E's Monday lock is in the simulated poll. Nobody recorded it, so it is not banked."""
    report = ledger_digest(week)
    assert report.state_source == "ledger"
    assert report.banked == {}
    assert report.poll_observed_at is None


def test_a_recorded_lock_is_banked_at_its_box_score(week):
    answer(week, week.a, MON, "LOCK")
    report = ledger_digest(week)
    assert report.banked == {week.a: week.score(week.a, 0)}
    assert week.a not in {c.sleeper_id for c in report.calls}


def test_a_lock_recorded_after_the_run_is_not_read(week):
    answer(week, week.a, MON, "LOCK", at=AFTER_RUN)
    assert ledger_digest(week).banked == {}


def test_an_unanswered_earlier_lock_call_is_marked_and_not_banked(week):
    said_on(week)
    report = ledger_digest(week)
    calls = {c.sleeper_id: c for c in report.calls}
    assert calls[week.a].said_lock_on == TUE.isoformat()
    assert week.a not in report.banked
    full, compact = digest_mod.render(report), digest_mod.render(report, compact=True)
    for text in (full, compact):
        assert "Tue said LOCK; not recorded" in text, text
        assert "ignore if you locked" not in text
        assert all(len(line) <= digest_mod.WIDTH for line in text.splitlines())
    assert "No answer is recorded for an earlier LOCK" in " ".join(full.split())


def test_a_passed_earlier_call_is_just_tonights_call(week):
    said_on(week)
    answer(week, week.a, MON, "PASS")
    report = ledger_digest(week)
    calls = {c.sleeper_id: c for c in report.calls}
    assert calls[week.a].said_lock_on is None


def test_an_unusable_record_is_a_warning(week):
    answer(week, week.f, MON, "LOCK")  # the opponent's player, on my ledger
    report = ledger_digest(week)
    assert report.banked == {}
    assert [(w.sleeper_id, w.kind) for w in report.warnings if w.kind == "lock record"] == [
        (week.f, "lock record")
    ]


def test_what_you_supply_still_wins_over_the_ledger(week):
    answer(week, week.a, MON, "LOCK")
    report = ledger_digest(week, locked={})
    assert report.state_source == "supplied" and report.banked == {}


def test_a_replay_before_the_ledger_began_keeps_the_poll(week):
    conn = connect(week.cfg)
    ctx = digest_mod.load_context(conn, "2026", params=ProjectionParams(min_pool_rows=20))
    report = digest_mod.build(
        ctx, 1, WED.isoformat(), n_sims=100, n_paths=100, live=False, ledger=True
    )
    assert report.state_source == "inferred"


def test_off_by_default_in_build(week):
    assert week.digest().state_source == "inferred"


# ------------------------------------------------------------------ open calls


def persisted(week, report, *, at=RUN):
    with connect(week.cfg) as conn:
        digest_mod.persist(conn, report, now=datetime.fromisoformat(at))
        return conn.execute(
            "SELECT run_id FROM digest_runs ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()[0]


def test_only_a_call_of_this_weeks_live_digest_can_be_answered(week):
    report = ledger_digest(week)
    run_id = persisted(week, report)
    call = report.calls[0]
    with connect(week.cfg) as conn:
        assert ledger.open_call(conn, 1, run_id, call.sleeper_id, call.day) == 2
        with pytest.raises(ledger.NotACall):
            ledger.open_call(conn, 1, run_id, call.sleeper_id, call.day + 1)
        with pytest.raises(ledger.NotACall):
            ledger.open_call(conn, 2, run_id, call.sleeper_id, call.day)
        rule = report.rules[0]
        with pytest.raises(ledger.NotACall):
            ledger.open_call(conn, 1, run_id, rule.sleeper_id, rule.night)


def test_a_replay_or_a_finished_week_cannot_be_answered(week):
    report = ledger_digest(week)
    run_id = persisted(week, replace(report, retrospective=True))
    call = report.calls[0]
    with connect(week.cfg) as conn, pytest.raises(ledger.NotACall):
        ledger.open_call(conn, 1, run_id, call.sleeper_id, call.day)

    live_id = persisted(week, report, at=AFTER_RUN)
    persisted(week, replace(report, week=3, calls=[], rules=[]), at="2026-11-02T13:00:00+00:00")
    with connect(week.cfg) as conn, pytest.raises(ledger.NotACall):
        ledger.open_call(conn, 1, live_id, call.sleeper_id, call.day)


def test_the_page_says_an_unanswered_repeat_is_unrecorded_not_unseen(week):
    from datetime import UTC

    from lockin import advice

    said_on(week)
    report = ledger_digest(week)
    persisted(week, report)
    with connect(week.cfg) as conn:
        run = advice.latest_run(conn, 1)
    page = advice.render(
        run, today=WED.isoformat(), now=datetime(2026, 10, 28, 14, tzinfo=UTC), interactive=True
    )
    assert "not recorded; tap Locked if you took it" in page
    assert "counted as unlocked until recorded" in page
    assert "shows in the poll" not in page and "ignore if you locked" not in page
    assert "your answers on this page" in page
