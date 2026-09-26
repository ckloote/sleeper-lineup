"""`lockin shadow`: the digest's calls against what was done (review sequence step 4).

A synthetic week of live mornings, the cron's sequence each time, with a user
who takes the first LOCK call and ignores the rest. Once the league has scored
the week, the report must read those actions back out of the final scores —
and must notice when a morning's BANKED list was wrong, which is the check
day-one.md step 7 used to ask for by hand.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime, timedelta

import pytest
from live_fixture import OPENING, SyntheticSeason, config_for, connect, ingest

from lockin import digest as digest_mod
from lockin import shadow
from lockin.core.projections import ProjectionParams

TUESDAY = OPENING + timedelta(days=7)  # week 2; the scaled cold-start gate is open
WEEK = 2


def nine_am(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=13)


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
        )
        digest_mod.persist(conn, report, now=nine_am(day))
    return report


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    """Week 2, every morning from Tuesday to the Monday that finalizes it."""
    season = SyntheticSeason()
    cfg = config_for(tmp_path_factory.mktemp("shadow"), season)
    params = ProjectionParams(min_pool_rows=60)
    taken: tuple[str, int] | None = None
    ignored: list[tuple[str, int]] = []
    for offset in range(7):
        report = run_morning(season, cfg, TUESDAY + timedelta(days=offset), params)
        for call in report.calls:
            if not call.lock:
                continue
            if taken is None:
                taken = (call.sleeper_id, call.day)
                season.locks[(WEEK, 1, call.sleeper_id)] = date.fromordinal(call.day)
            else:
                ignored.append((call.sleeper_id, call.day))
    assert taken is not None and ignored, "the synthetic week must produce LOCK calls"
    return season, cfg, taken, ignored


@pytest.fixture
def db(played, tmp_path):
    """A copy the test may change."""
    season, cfg, taken, ignored = played
    copy = config_for(tmp_path, season)
    shutil.copy(cfg.db_path, copy.db_path)
    return season, copy, taken, ignored


def report_for(season, cfg) -> shadow.ShadowReport:
    with connect(cfg) as conn:
        return shadow.build(conn, season.season, 1)


def test_the_call_taken_and_the_calls_ignored_are_read_back(played):
    season, cfg, taken, ignored = played
    report = report_for(season, cfg)
    verdicts = {(c.sleeper_id, c.for_day): c for c in report.calls if c.action == "LOCK"}

    assert verdicts[taken].verdict == shadow.FOLLOWED
    for key in ignored:
        if key[0] == taken[0] and key[1] > taken[1]:
            assert verdicts[key].verdict == shadow.MOOT  # already banked
        elif key in verdicts and verdicts[key].verdict != shadow.UNKNOWN:
            assert verdicts[key].verdict == shadow.OVERRIDDEN, verdicts[key]


def test_a_correct_state_reading_passes_and_the_week_is_clean(played):
    season, cfg, taken, _ = played
    report = report_for(season, cfg)
    week = next(w for w in report.weeks if w.week == WEEK)

    assert week.runs == 6 and week.state_checked > 0  # Tuesday to Sunday
    assert week.state_misses == 0, report.misses
    assert week.same_input_flips == 0
    assert week.partial and week.mornings_missing == ["roster 1 2026-10-26"]
    assert not week.clean
    assert report.gate() == (False, f"1 finalized week(s) of tracking; need {shadow.GATE_WEEKS}")
    text = shadow.render(report)
    assert "week  2  NOT CLEAN" in text and "GATE  not yet" in text


def test_a_wrong_banked_list_is_caught(db):
    season, cfg, taken, _ = db
    with connect(cfg) as conn:
        # Drop the banked lock from the last run of the week that had it.
        run_id = conn.execute(
            "SELECT b.run_id FROM digest_banked b JOIN digest_runs d ON d.run_id = b.run_id"
            " WHERE b.sleeper_id = ? AND d.week = ? ORDER BY d.generated_at DESC LIMIT 1",
            (taken[0], WEEK),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM digest_banked WHERE run_id = ? AND sleeper_id = ?", (run_id, taken[0])
        )
    report = report_for(season, cfg)

    assert [m.sleeper_id for m in report.misses] == [taken[0]]
    assert report.misses[0].detail.startswith("missed")
    assert not next(w for w in report.weeks if w.week == WEEK).clean


def test_a_rerun_on_the_same_inputs_that_changes_a_call_is_a_bug(db):
    season, cfg, _, _ = db
    with connect(cfg) as conn:
        run = conn.execute(
            "SELECT d.* FROM digest_runs d JOIN recommendations r ON r.run_id = d.run_id"
            " WHERE d.week = ? AND r.action = 'LOCK' ORDER BY d.generated_at LIMIT 1",
            (WEEK,),
        ).fetchone()
        rerun = dict(run)
        rerun["run_id"] = "rerun"
        rerun["generated_at"] = (
            datetime.fromisoformat(run["generated_at"]) + timedelta(minutes=5)
        ).isoformat()
        conn.execute(
            f"INSERT INTO digest_runs ({', '.join(rerun)}) VALUES ({', '.join('?' * len(rerun))})",
            list(rerun.values()),
        )
        for rec in conn.execute(
            "SELECT * FROM recommendations WHERE run_id = ?", (run["run_id"],)
        ).fetchall():
            row = dict(rec, run_id="rerun", generated_at=rerun["generated_at"])
            row["action"] = {"LOCK": "PASS", "PASS": "LOCK"}.get(row["action"], row["action"])
            conn.execute(
                f"INSERT INTO recommendations ({', '.join(row)})"
                f" VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
    report = report_for(season, cfg)
    week = next(w for w in report.weeks if w.week == WEEK)

    assert week.same_input_flips > 0 and not week.clean
    assert all(f.same_inputs for f in report.flips if f.after.endswith(f"({run['as_of']})"))


def test_a_replay_run_afterwards_advised_nobody(db):
    season, cfg, _, _ = db
    before = next(w for w in report_for(season, cfg).weeks if w.week == WEEK).runs
    with connect(cfg) as conn:
        replay = digest_mod.morning(
            conn,
            season.season,
            1,
            TUESDAY.isoformat(),
            n_sims=100,
            params=ProjectionParams(min_pool_rows=60),
        )
        digest_mod.persist(conn, replay, now=nine_am(TUESDAY + timedelta(days=9)))

    assert next(w for w in report_for(season, cfg).weeks if w.week == WEEK).runs == before


def test_a_missed_morning_is_reported(db):
    season, cfg, _, _ = db
    with connect(cfg) as conn:
        conn.execute(
            "DELETE FROM digest_runs WHERE as_of = ?", ((TUESDAY + timedelta(days=2)).isoformat(),)
        )
    week = next(w for w in report_for(season, cfg).weeks if w.week == WEEK)

    assert week.mornings_missing == [
        "roster 1 2026-10-26",
        f"roster 1 {(TUESDAY + timedelta(days=2)).isoformat()}",
    ]
    assert not week.clean


# ------------------------------------------------------------ the pure parts

D = 739_900


def truth(days, counted=40.0, game_days=(D, D + 2, D + 4)) -> shadow.Truth:
    return shadow.Truth(None if days is None else frozenset(days), counted, game_days)


@pytest.mark.parametrize(
    "action, day, days, expected",
    [
        ("LOCK", D + 2, {D + 2}, shadow.FOLLOWED),
        ("LOCK", D + 2, set(), shadow.OVERRIDDEN),  # rode
        ("LOCK", D + 2, {D + 4}, shadow.OVERRIDDEN),  # banked a later game instead
        ("LOCK", D + 2, {D}, shadow.MOOT),  # already banked
        ("LOCK", D + 2, {D, D + 4}, shadow.UNKNOWN),  # tied either side of the call
        ("LOCK", D + 2, None, shadow.UNKNOWN),
        ("PASS", D + 2, set(), shadow.FOLLOWED),
        ("PASS", D + 2, {D + 4}, shadow.FOLLOWED),
        ("PASS", D + 2, {D + 2}, shadow.OVERRIDDEN),
        ("PASS", D + 2, {D + 2, D + 4}, shadow.UNKNOWN),
    ],
)
def test_each_verdict(action, day, days, expected):
    assert shadow.verdict(action, day, truth(days))[0] == expected


def test_a_lock_shows_only_once_he_has_played_again():
    locked_on_first = truth({D})
    assert shadow.expected_banked(locked_on_first, D) is None  # last night: still open
    assert shadow.expected_banked(locked_on_first, D + 2) == 40.0
    assert shadow.expected_banked(truth(set()), D + 4) is None
    assert shadow.expected_banked(truth(None), D + 4) is False
    assert shadow.expected_banked(truth({D, D + 4}), D + 2) is False  # a tie across the morning


def summary(week: int, *, clean: bool = True) -> shadow.WeekSummary:
    return shadow.WeekSummary(week=week, runs=7, state_checked=30, state_misses=0 if clean else 1)


def test_the_gate_needs_two_consecutive_clean_weeks():
    gate = shadow.ShadowReport
    assert gate(weeks=[summary(2), summary(3)]).gate()[0]
    assert not gate(weeks=[summary(2), summary(3, clean=False)]).gate()[0]
    assert not gate(weeks=[summary(2), shadow.WeekSummary(week=3), summary(4)]).gate()[0]
    assert gate(weeks=[summary(2, clean=False), summary(3), summary(4)]).gate()[0]
