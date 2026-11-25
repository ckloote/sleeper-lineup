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
OPEN = {"season": "2026", "today": "2026-10-28"}
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
    assert sorted(pid for pid, _, _ in got.problems) == ["kat", "stranger"]
    assert not any(counted for _, _, counted in got.problems)


def test_two_locks_bank_the_later_game_and_say_so(conn):
    from lockin.projections import day_index

    first, second = day_index("2026-11-17"), day_index("2026-11-19")
    games = {"kat": [Game(0, first, True, 57.5), Game(1, second, True, 50.5)]}
    put(conn, "kat", first, "LOCK", "2026-11-17T03:00:00+00:00")
    put(conn, "kat", second, "LOCK", "2026-11-19T03:00:00+00:00")
    got = ledger.state(conn, 5, 4, games, known_through=second, before=None)
    assert got.banked == {"kat": 50.5}
    [(pid, detail, counted)] = got.problems
    assert pid == "kat" and counted
    assert detail == "recorded as locked on 2026-11-17 and 2026-11-19; counting 2026-11-19 (50.5)"


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
        assert ledger.open_call(conn, 1, run_id, call.sleeper_id, call.day, **OPEN) == 2
        with pytest.raises(ledger.NotACall):
            ledger.open_call(conn, 1, run_id, call.sleeper_id, call.day + 1, **OPEN)
        with pytest.raises(ledger.NotACall):
            ledger.open_call(conn, 2, run_id, call.sleeper_id, call.day, **OPEN)
        rule = report.rules[0]
        with pytest.raises(ledger.NotACall):
            ledger.open_call(conn, 1, run_id, rule.sleeper_id, rule.night, **OPEN)


def test_a_replay_or_a_finished_week_cannot_be_answered(week):
    report = ledger_digest(week)
    run_id = persisted(week, replace(report, retrospective=True))
    call = report.calls[0]
    with connect(week.cfg) as conn, pytest.raises(ledger.NotACall):
        ledger.open_call(conn, 1, run_id, call.sleeper_id, call.day, **OPEN)

    live_id = persisted(week, report, at=AFTER_RUN)
    persisted(week, replace(report, week=3, calls=[], rules=[]), at="2026-11-02T13:00:00+00:00")
    with connect(week.cfg) as conn, pytest.raises(ledger.NotACall):
        ledger.open_call(conn, 1, live_id, call.sleeper_id, call.day, **OPEN)


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


def test_the_digest_says_which_of_two_locks_it_counted(week):
    answer(week, week.e, MON, "LOCK")
    answer(week, week.e, TUE, "LOCK", at="2026-10-28T02:30:00+00:00")
    report = ledger_digest(week)
    assert report.banked == {week.e: 0.0}  # Tuesday was a DNP
    [warn] = [w for w in report.warnings if w.kind == "lock record"]
    assert f"counting {TUE.isoformat()} (0)" in warn.detail
    assert "as banked. Undo the wrong one" in warn.detail
    assert "not counted" not in warn.detail


def test_a_replay_reads_the_ledger_from_the_first_morning_a_live_run_did(week):
    """The live run advised on an empty ledger; its replay must not read the poll."""
    persisted(week, ledger_digest(week))
    conn = connect(week.cfg)
    ctx = digest_mod.load_context(conn, "2026", params=ProjectionParams(min_pool_rows=20))
    report = digest_mod.build(
        ctx, 1, WED.isoformat(), n_sims=100, n_paths=100, live=False, ledger=True
    )
    assert report.state_source == "ledger" and report.banked == {}
    earlier = digest_mod.build(
        ctx, 1, TUE.isoformat(), n_sims=100, n_paths=100, live=False, ledger=True
    )
    assert earlier.state_source != "ledger"


def test_a_call_cannot_be_answered_once_its_week_is_over_by_the_calendar(week):
    """Sunday's run stays the latest after the rollover until a digest succeeds."""
    report = ledger_digest(week)
    run_id = persisted(week, report)
    call = report.calls[0]
    args = (1, run_id, call.sleeper_id, call.day)
    with connect(week.cfg) as conn:
        assert ledger.open_call(conn, *args, season="2026", today="2026-11-01") == 2  # Sunday
        with pytest.raises(ledger.NotACall, match="week is over"):
            ledger.open_call(conn, *args, season="2026", today="2026-11-02")
        with pytest.raises(ledger.NotACall, match="calendar is unknown"):
            ledger.open_call(conn, *args, season=None, today="2026-10-28")


def test_a_replay_follows_the_source_its_own_morning_saved(week):
    """Tuesday read the ledger; Wednesday ran on the poll (LOCKIN_LEDGER=0)."""
    report = ledger_digest(week)
    persisted(week, replace(report, as_of=TUE.isoformat()), at="2026-10-27T13:00:00+00:00")
    wednesday = persisted(week, report)
    with connect(week.cfg) as conn:
        conn.execute(
            "UPDATE digest_runs SET state_source = 'inferred' WHERE run_id = ?", (wednesday,)
        )
        assert ledger.replays_ledger(conn, 1, TUE.isoformat(), before=RUN)
        assert not ledger.replays_ledger(conn, 1, WED.isoformat(), before=RUN)
        # A morning with no saved live run falls back to the first ledger morning.
        assert ledger.replays_ledger(conn, 1, "2026-10-29", before=RUN)
    conn = connect(week.cfg)
    ctx = digest_mod.load_context(conn, "2026", params=ProjectionParams(min_pool_rows=20))
    replay = digest_mod.build(
        ctx, 1, WED.isoformat(), n_sims=100, n_paths=100, live=False, ledger=True
    )
    assert replay.state_source == "inferred"


def test_the_scheduled_run_is_nine_local_in_either_season(monkeypatch):
    from lockin import clock

    monkeypatch.setenv("LOCKIN_TZ", "America/New_York")
    assert clock.scheduled_run("2026-10-28") == "2026-10-28T13:00:00+00:00"  # EDT
    assert clock.scheduled_run("2026-11-24") == "2026-11-24T14:00:00+00:00"  # EST


def test_a_replay_reads_the_answers_its_live_run_saw(week):
    """A later run that morning saw a tap the fixed 13:00 UTC cutoff would drop."""
    answer(week, week.a, MON, "LOCK", at="2026-10-28T13:30:00+00:00")
    late = ledger_digest(week, now="2026-10-28T13:45:00+00:00")
    persisted(week, late, at="2026-10-28T13:46:00+00:00")
    conn = connect(week.cfg)
    ctx = digest_mod.load_context(conn, "2026", params=ProjectionParams(min_pool_rows=20))
    replay = digest_mod.build(
        ctx, 1, WED.isoformat(), n_sims=100, n_paths=100, live=False, ledger=True
    )
    assert replay.banked == {week.a: week.score(week.a, 0)}


def test_a_tap_made_while_the_live_digest_computed_is_not_counted_as_seen(week):
    """The run read the ledger at 13:00 and was saved at 13:05; the tap was at 13:02."""
    report = ledger_digest(week)
    assert report.ledger_read_at == RUN
    run_id = persisted(week, report, at="2026-10-28T13:05:00+00:00")
    answer(week, week.a, MON, "LOCK", at="2026-10-28T13:02:00+00:00")
    with connect(week.cfg) as conn:
        saved = conn.execute(
            "SELECT ledger_read_at FROM digest_runs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        assert saved == RUN
        assert ledger.replay_moment(conn, 1, WED.isoformat()) == RUN
    conn = connect(week.cfg)
    ctx = digest_mod.load_context(conn, "2026", params=ProjectionParams(min_pool_rows=20))
    replay = digest_mod.build(
        ctx, 1, WED.isoformat(), n_sims=100, n_paths=100, live=False, ledger=True
    )
    assert replay.banked == {}, "the live advice never saw it"


def test_an_existing_database_gains_the_ledger_cutoff_column():
    import sqlite3

    from lockin.store.db import apply_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE digest_runs (generated_at TEXT, roster_id INTEGER, as_of TEXT)")
    apply_schema(conn)
    assert "ledger_read_at" in {r["name"] for r in conn.execute("PRAGMA table_info(digest_runs)")}
