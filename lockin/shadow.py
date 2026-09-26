"""`lockin shadow`: what the digest said, against what was then done.

The 2026-09-23 review's fourth step: run live in shadow before trusting the
notifications unattended — capture the recommendations and the actions actually
taken, and judge the engine prospectively. The recommendations are already
captured (`digest_runs`, `recommendations`, `digest_banked`). The actions are
recoverable too, once a week is final: the counted scores say which game each
starter banked, which is Phase 2's lock inference (`lockin.core.locks`).

Four questions, for each week the league has finished scoring:

1. **Calls against actions.** Was each LOCK/PASS call followed, overridden, or
   moot because he had already banked an earlier game? Some cannot be told:
   two games with the same score, or a counted value nothing explains.
2. **The state reading.** Each run's BANKED list came from that morning's poll
   (`lockin.state`). Once the week is final, the truth is known: did the run
   bank exactly the locks that were knowable by then? This is day-one.md step
   7's daily comparison, done by the code.
3. **Calibration.** Each morning's P(win) against the result. Reported, not
   gated: a week gives about seven mornings, and a Brier score on seven
   forecasts says very little.
4. **Stability.** A call that changed between two runs. After a new night of
   games that is expected. On the same inputs it is a bug, because a digest is
   seeded and deterministic.

Only runs made on the morning they describe count. A replay of a past date,
run afterwards, advised nobody. And only your roster's: the cron advises that
one, and a digest run by hand for another — a look at an opponent — is not a
morning anything owed.

Writes nothing.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from lockin import calendar, clock
from lockin.core.locks import Game, LockStatus, infer_lock
from lockin.core.scoring import score_recorded
from lockin.locks import game_sequence
from lockin.managers import last_scored_week
from lockin.projections import date_of, day_index
from lockin.verify import scoring_settings

FOLLOWED, OVERRIDDEN, MOOT, UNKNOWN = "followed", "overridden", "moot", "unknown"

GATE_WEEKS = 2
"""Consecutive clean weeks before the daily cross-check can stop (day-one.md step 7)."""

NO_EARLY_LOCK = {LockStatus.RODE_TO_END, LockStatus.SINGLE_GAME, LockStatus.NO_GAMES}
"""Final readings that mean nothing was banked before his last game."""


@dataclass(frozen=True, slots=True)
class Truth:
    """What a starter banked, as the final counted score shows it."""

    days: frozenset[int] | None
    """Days of the games he may have banked early: one when it is clear, several
    when their scores tie, none when he rode. None when it cannot be told."""
    counted: float
    game_days: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CallOutcome:
    week: int
    roster_id: int
    sleeper_id: str
    action: str
    for_day: int
    as_of: str
    verdict: str
    detail: str


@dataclass(frozen=True, slots=True)
class StateMiss:
    week: int
    roster_id: int
    sleeper_id: str
    as_of: str
    run_banked: float | None
    expected: float | None
    detail: str


@dataclass(frozen=True, slots=True)
class Forecast:
    week: int
    roster_id: int
    as_of: str
    p_win: float
    outcome: float
    """1 won, 0 lost, 0.5 tied."""


@dataclass(frozen=True, slots=True)
class Flip:
    week: int
    roster_id: int
    sleeper_id: str
    for_day: int
    before: str
    after: str
    same_inputs: bool
    detail: str


@dataclass(slots=True)
class WeekSummary:
    week: int
    runs: int = 0
    mornings_missing: list[str] = field(default_factory=list)
    failed_inference: list[str] = field(default_factory=list)
    supplied_only: list[str] = field(default_factory=list)
    uncheckable: list[str] = field(default_factory=list)
    exemptions: list[str] = field(default_factory=list)
    partial: bool = False
    calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    state_checked: int = 0
    state_misses: int = 0
    flips: int = 0
    same_input_flips: int = 0

    @property
    def clean(self) -> bool:
        return (
            self.runs > 0
            and (self.state_checked > 0 or bool(self.exemptions))
            and not self.partial
            and not self.failed_inference
            and not self.supplied_only
            and not self.uncheckable
            and self.state_misses == 0
            and self.same_input_flips == 0
            and not self.mornings_missing
        )


@dataclass(slots=True)
class ShadowReport:
    finalized: int = 0
    """The last week the league has finished scoring: weeks 1 to this are final."""
    weeks: list[WeekSummary] = field(default_factory=list)
    """From the first week with a live run to `finalized`, unbroken — a week with
    no run is in it. Empty when no finalized week has one."""
    calls: list[CallOutcome] = field(default_factory=list)
    misses: list[StateMiss] = field(default_factory=list)
    forecasts: list[Forecast] = field(default_factory=list)
    flips: list[Flip] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)

    def brier(self) -> float | None:
        if not self.forecasts:
            return None
        return sum((f.p_win - f.outcome) ** 2 for f in self.forecasts) / len(self.forecasts)

    def gate(self) -> tuple[bool, str]:
        """Passed once the latest ``GATE_WEEKS`` weeks of tracking are all clean.

        `weeks` is unbroken, so they are consecutive by construction, and a week
        with no run is among them rather than skipped.
        """
        recent = self.weeks[-GATE_WEEKS:]
        if len(recent) < GATE_WEEKS:
            return False, f"{len(self.weeks)} finalized week(s) of tracking; need {GATE_WEEKS}"
        dirty = [w.week for w in recent if not w.clean]
        if dirty:
            return False, f"week(s) {', '.join(map(str, dirty))} not clean"
        return True, f"weeks {recent[0].week}-{recent[-1].week} clean"


# ------------------------------------------------------------------ the truth


def truths(
    conn: sqlite3.Connection, season: str, weeks: list[int], rosters: set[int]
) -> dict[tuple[int, int, str], Truth]:
    """(week, roster, player) -> what he banked, for every starter of ``rosters``."""
    scoring = scoring_settings(conn)
    seq = game_sequence(conn, season)
    out: dict[tuple[int, int, str], Truth] = {}
    marks = ",".join("?" * len(weeks))
    for row in conn.execute(
        f"SELECT week, roster_id, sleeper_id, counted_points FROM weekly_matchups_latest"
        f" WHERE is_starter = 1 AND week IN ({marks})",
        weeks,
    ):
        if row["roster_id"] not in rosters:
            continue
        raw = seq.get((row["sleeper_id"], row["week"]), [])
        games = [
            Game(
                index=i,
                played=bool(g["played"]),
                score=score_recorded(json.loads(g["raw_stats"]), scoring) if g["played"] else 0.0,
            )
            for i, g in enumerate(raw)
        ]
        counted = row["counted_points"] if row["counted_points"] is not None else 0.0
        inference = infer_lock(counted, games)
        game_days = tuple(day_index(g["game_date"]) for g in raw)
        if row["counted_points"] is None:
            days = None  # missing final evidence is not a counted zero
        elif inference.status is LockStatus.LOCKED_EARLY:
            days = frozenset({game_days[inference.matched_index]})
        elif inference.status is LockStatus.AMBIGUOUS and inference.locked_early:
            days = frozenset(game_days[i] for i in inference.candidates)
        elif inference.status in NO_EARLY_LOCK:
            days = frozenset()
        else:
            days = None  # unresolved, benched, or a tie with his final game
        out[(row["week"], row["roster_id"], row["sleeper_id"])] = Truth(days, counted, game_days)
    return out


def verdict(action: str, day: int, truth: Truth | None) -> tuple[str, str]:
    """Whether a LOCK/PASS call on the game of ``day`` was followed."""
    if truth is None or truth.days is None:
        return UNKNOWN, "what he banked cannot be read from the final score"
    days = truth.days
    if days and max(days) < day:
        return MOOT, f"he had already banked {date_of(max(days))}"
    if len(days) > 1 and min(days) <= day:
        return UNKNOWN, "tied with another game he may have banked instead"
    locked_it = day in days
    if action == "LOCK":
        if locked_it:
            return FOLLOWED, "locked"
        if days:
            return OVERRIDDEN, f"banked {date_of(min(days))} instead"
        return OVERRIDDEN, "rode instead"
    if locked_it:
        return OVERRIDDEN, "locked it anyway"
    return FOLLOWED, "passed"


def expected_banked(truth: Truth, known_through: int) -> float | None | bool:
    """What a poll read on the morning after ``known_through`` should show as banked.

    A lock shows once he has played again: before that, the counted value is
    last night's score whether or not he locked it. Returns the banked score,
    None for nothing banked, or False when the truth cannot settle it.
    """
    if truth.days is None:
        return False
    if not truth.days:
        return None
    later = [d for d in truth.game_days if d <= known_through]
    if any(d > max(truth.days) for d in later):
        return truth.counted
    if not any(d > min(truth.days) for d in later):
        return None
    return False  # tied games either side of the morning: could be either


# ------------------------------------------------------------------ the report


def _live_runs(conn: sqlite3.Connection, weeks: list[int], roster_id: int) -> list[sqlite3.Row]:
    """The roster's runs made on the morning they describe, in finalized weeks, oldest first."""
    marks = ",".join("?" * len(weeks))
    zone = clock.zone()
    rows = conn.execute(
        f"SELECT * FROM digest_runs WHERE run_id IS NOT NULL AND week IN ({marks})"
        " AND roster_id = ? ORDER BY generated_at",
        [*weeks, roster_id],
    ).fetchall()
    return [
        r
        for r in rows
        if not ("retrospective" in r.keys() and r["retrospective"])
        and datetime.fromisoformat(r["generated_at"]).astimezone(zone).date().isoformat()
        == r["as_of"]
    ]


def _final_points(conn: sqlite3.Connection, week: int, roster_id: int) -> float | None:
    row = conn.execute(
        "SELECT points FROM weekly_matchup_teams WHERE week = ? AND roster_id = ?"
        " ORDER BY observed_at DESC LIMIT 1",
        (week, roster_id),
    ).fetchone()
    return row["points"] if row else None


def build(conn: sqlite3.Connection, season: str, roster_id: int) -> ShadowReport:
    last = last_scored_week(conn) if _has_league(conn) else 0
    report = ShadowReport(finalized=last)
    weeks = list(range(1, last + 1))
    if not weeks:
        return report
    runs = _live_runs(conn, weeks, roster_id)
    by_week: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in runs:
        by_week[r["week"]].append(r)
    truth = truths(conn, season, weeks, {roster_id})
    opening = calendar.opening_night(conn, season)
    first_live = runs[0]["as_of"] if runs else None
    first_week = min(by_week) if by_week else last + 1

    for week in range(first_week, last + 1):
        summary = WeekSummary(week=week, runs=len(by_week[week]))
        report.weeks.append(summary)
        _mornings(summary, by_week[week], week, opening, roster_id, first_live, truth)
        _calls(conn, report, summary, by_week[week], truth)
        _state(conn, report, summary, by_week[week], truth)
        _forecasts(conn, report, by_week[week])

    pids = {c.sleeper_id for c in report.calls} | {m.sleeper_id for m in report.misses}
    pids |= {f.sleeper_id for f in report.flips}
    if pids:
        marks = ",".join("?" * len(pids))
        report.names = {
            r[0]: r[1]
            for r in conn.execute(
                f"SELECT sleeper_id, full_name FROM players WHERE sleeper_id IN ({marks})",
                sorted(pids),
            )
            if r[1]
        }
    return report


def _has_league(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM league_settings)").fetchone()[0] == 1


def _mornings(
    summary: WeekSummary,
    runs: list[sqlite3.Row],
    week: int,
    opening: date | None,
    roster_id: int,
    first_live: str | None,
    truth: dict[tuple[int, int, str], Truth],
) -> None:
    """Require automatic, checkable inference each calendar morning.

    Monday is owed a run, not an inference. Nothing is banked before a week's
    first game, so there is no state to read, and its digest may abstain for want
    of the week itself: Sleeper need not have rolled over by the 06:30 ingest,
    which then fetches only the week just gone.
    """
    if opening is None or first_live is None:
        summary.partial = True
        return
    monday, sunday = calendar.week_bounds(week, opening)
    start = date.fromisoformat(first_live)
    if start > sunday:
        return
    if start > monday:
        summary.partial = True
    day = monday
    while day <= sunday:
        label = f"roster {roster_id} {day.isoformat()}"
        morning = [r for r in runs if r["as_of"] == day.isoformat()]
        inferred = [r for r in morning if r["state_source"] == "inferred" and not r["abstained"]]
        checkable = any(
            expected_banked(t, day.toordinal() - 1) is not False
            for (w, rid, _), t in truth.items()
            if w == week and rid == roster_id
        )
        if inferred and checkable:
            pass
        elif any(
            "verified_no_matchup" in r.keys() and r["verified_no_matchup"] and not r["abstained"]
            for r in morning
        ):
            summary.exemptions.append(label)
        elif not morning:
            summary.mornings_missing.append(label)
        elif day == monday:
            pass
        elif inferred:
            summary.uncheckable.append(label)
        elif any(r["state_source"] == "supplied" and not r["abstained"] for r in morning):
            summary.supplied_only.append(label)
        else:
            summary.failed_inference.append(label)
        day += timedelta(days=1)


def _calls(
    conn: sqlite3.Connection,
    report: ShadowReport,
    summary: WeekSummary,
    runs: list[sqlite3.Row],
    truth: dict[tuple[int, int, str], Truth],
) -> None:
    """Each call's last word before his window closed, and whether it was taken."""
    run_of = {r["run_id"]: r for r in runs}
    marks = ",".join("?" * len(run_of))
    rows = conn.execute(
        f"SELECT run_id, sleeper_id, action, for_day FROM recommendations"
        f" WHERE run_id IN ({marks}) AND action IN ('LOCK', 'PASS') ORDER BY generated_at",
        list(run_of),
    ).fetchall()
    history: dict[tuple[int, str, int], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        history[(run_of[row["run_id"]]["roster_id"], row["sleeper_id"], row["for_day"])].append(row)
    for (roster_id, pid, day), seen in history.items():
        final = seen[-1]
        run = run_of[final["run_id"]]
        outcome, detail = verdict(final["action"], day, truth.get((summary.week, roster_id, pid)))
        summary.calls[outcome] += 1
        report.calls.append(
            CallOutcome(
                summary.week, roster_id, pid, final["action"], day, run["as_of"], outcome, detail
            )
        )
        for a, b in zip(seen, seen[1:], strict=False):
            if a["action"] == b["action"]:
                continue
            ra, rb = run_of[a["run_id"]], run_of[b["run_id"]]
            same = (ra["as_of"], ra["ingest_run_id"], ra["poll_observed_at"]) == (
                rb["as_of"],
                rb["ingest_run_id"],
                rb["poll_observed_at"],
            )
            summary.flips += 1
            summary.same_input_flips += int(same)
            report.flips.append(
                Flip(
                    summary.week,
                    roster_id,
                    pid,
                    day,
                    f"{a['action']} ({ra['as_of']})",
                    f"{b['action']} ({rb['as_of']})",
                    same,
                    "same ingest and poll: a bug"
                    if same
                    else "new data in between"
                    if ra["as_of"] == rb["as_of"]
                    else "a night of games in between",
                )
            )


def _state(
    conn: sqlite3.Connection,
    report: ShadowReport,
    summary: WeekSummary,
    runs: list[sqlite3.Row],
    truth: dict[tuple[int, int, str], Truth],
) -> None:
    """Each poll-read BANKED list against the locks knowable that morning."""
    for run in runs:
        if run["state_source"] != "inferred":
            continue  # supplied by hand, or no poll: nothing of the reading's to check
        known_through = day_index(run["as_of"]) - 1
        banked = {
            r["sleeper_id"]: r["score"]
            for r in conn.execute(
                "SELECT sleeper_id, score FROM digest_banked WHERE run_id = ?", (run["run_id"],)
            )
        }
        starters = {pid for (w, rid, pid) in truth if w == summary.week and rid == run["roster_id"]}
        for pid in sorted(starters | set(banked)):
            t = truth.get((summary.week, run["roster_id"], pid))
            expected = expected_banked(t, known_through) if t else None
            if expected is False:
                continue
            summary.state_checked += 1
            have = banked.get(pid)
            if have is None and expected is None:
                continue
            if have is not None and expected is not None and abs(have - expected) < 0.005:
                continue
            if t is None:
                detail = "banked, but not a starter when the week ended"
            elif have is None:
                detail = f"missed: he had banked {expected:.1f}"
            elif expected is None:
                detail = f"banked {have:.1f}, but nothing was banked yet"
            else:
                detail = f"banked {have:.1f}, but the lock was {expected:.1f}"
            summary.state_misses += 1
            report.misses.append(
                StateMiss(summary.week, run["roster_id"], pid, run["as_of"], have, expected, detail)
            )


def _forecasts(conn: sqlite3.Connection, report: ShadowReport, runs: list[sqlite3.Row]) -> None:
    """The last advising run of each morning, against the week's result."""
    last: dict[tuple[int, str], sqlite3.Row] = {}
    for run in runs:
        if run["p_win"] is not None and run["opponent_roster_id"] is not None:
            last[(run["roster_id"], run["as_of"])] = run
    for run in last.values():
        mine = _final_points(conn, run["week"], run["roster_id"])
        theirs = _final_points(conn, run["week"], run["opponent_roster_id"])
        if mine is None or theirs is None:
            continue
        outcome = 1.0 if mine > theirs else 0.0 if mine < theirs else 0.5
        report.forecasts.append(
            Forecast(run["week"], run["roster_id"], run["as_of"], run["p_win"], outcome)
        )


# ------------------------------------------------------------------ rendering


BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))


def render(report: ShadowReport) -> str:
    if not any(w.runs for w in report.weeks):
        scored = f"weeks 1-{report.finalized}" if report.finalized else "no week"
        return (
            f"SHADOW  {scored} finalized, and no live digest runs in them.\n"
            "Nothing to compare yet: runs count once the league has scored their week."
        )
    name = report.names.get
    out = ["SHADOW  what the digest said, against what was done", ""]
    for w in report.weeks:
        calls = ", ".join(f"{w.calls[k]} {k}" for k in (FOLLOWED, OVERRIDDEN, MOOT, UNKNOWN))
        out.append(
            f"week {w.week:>2}  {'clean' if w.clean else 'NOT CLEAN'}  {w.runs} run(s);"
            f" calls: {calls}; state: {w.state_misses} miss(es) in {w.state_checked} checks;"
            f" flips: {w.flips} ({w.same_input_flips} on the same inputs)"
        )
        if w.partial:
            out.append("          partial first week: a full week is required")
        for label, entries in (
            ("failed inference", w.failed_inference),
            ("supplied-only", w.supplied_only),
            ("uncheckable evidence", w.uncheckable),
            ("verified exemption", w.exemptions),
        ):
            for entry in entries:
                out.append(f"          {label}: {entry}")
        for missing in w.mornings_missing:
            out.append(f"          no run: {missing}")

    if report.misses:
        out += ["", "STATE READING — each is a morning the BANKED list was wrong"]
        out += [
            f"  wk {m.week} {m.as_of}  {name(m.sleeper_id) or m.sleeper_id}: {m.detail}"
            for m in report.misses
        ]
    overridden = [c for c in report.calls if c.verdict in (OVERRIDDEN, MOOT)]
    if overridden:
        out += ["", "CALLS NOT TAKEN — your decision, or a stale call"]
        out += [
            f"  wk {c.week} {c.action:<4} {name(c.sleeper_id) or c.sleeper_id}"
            f" on {date_of(c.for_day)}: {c.verdict}, {c.detail}"
            for c in overridden
        ]
    if report.flips:
        out += ["", "FLIPS — a call that changed between runs"]
        out += [
            f"  wk {f.week} {name(f.sleeper_id) or f.sleeper_id} on {date_of(f.for_day)}:"
            f" {f.before} -> {f.after}, {f.detail}"
            for f in report.flips
        ]

    out += ["", f"CALIBRATION  {len(report.forecasts)} morning(s) — reported, not gated"]
    if report.forecasts:
        out.append(f"  Brier {report.brier():.3f}  (0.25 is a coin flip)")
        for lo, hi in BINS:
            inside = [f for f in report.forecasts if lo <= f.p_win < hi]
            if inside:
                won = sum(f.outcome for f in inside) / len(inside)
                mean = sum(f.p_win for f in inside) / len(inside)
                out.append(
                    f"  P(win) {lo:.0%}-{min(hi, 1):.0%}: {len(inside):>3} morning(s),"
                    f" said {mean:.0%}, won {won:.0%}"
                )

    passed, why = report.gate()
    out += ["", f"GATE  {'passed' if passed else 'not yet'} — {why}"]
    if passed:
        out.append("  The daily --locked cross-check can stop (day-one.md step 7).")
    return "\n".join(out)
