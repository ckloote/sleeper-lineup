"""The shadow report on a week whose banked state came from the lock ledger.

The truth there is the manager's answers, not the final poll, which Sleeper
keeps rewriting. A LOCK call nobody answered blocks the week.
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta

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
    assert week.state_checked == 0 and week.state_misses == 0
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
