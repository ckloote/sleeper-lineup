"""Observed state, open windows, and deadlines (review findings 2, 6 and 7).

Three things the morning digest got wrong about time, each reproduced here on a
synthetic season whose schedule is built to expose it:

- **Finding 2.** With no `--locked` it replayed its own advice through
  yesterday, so last night's recommended locks were counted as banked and left
  out of the calls meant to announce them. State now comes from the poll.
- **Finding 6.** It chose one "last night" for the whole team. A player who
  played Monday and next plays Thursday can still bank Monday on Wednesday,
  whatever his teammates did on Tuesday.
- **Finding 7.** A call stays actionable only until that player's next tip,
  and a run before last night's slate is final must not read it as final.

The week built below is week 2 (Mon 26 Oct to Sun 1 Nov). The digest is read
on Wednesday morning, so Tuesday is the last night observed.
"""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

import pytest
from live_fixture import OPENING, TEAMS, Fixture, SyntheticSeason, config_for, connect, ingest

from lockin import digest as digest_mod
from lockin.core.policy import Game
from lockin.core.projections import ProjectionParams
from lockin.state import AMBIGUOUS, BANKED, OPEN, UNRESOLVED, infer_state, reading

MON, TUE, WED, THU, FRI = (date(2026, 10, 26) + timedelta(days=i) for i in range(5))


PLANNED = {"ATL", "BOS", "CHI", "DAL", "DEN", "DET"}
"""The teams whose weeks the tests control; never borrowed as someone's opponent."""


def fixture_week(
    season: SyntheticSeason, team: str, days: list[date], ids: itertools.count
) -> list[Fixture]:
    """Give ``team`` exactly these games in week 2, against opponents free that day."""
    end = MON + timedelta(days=6)
    season.fixtures = [
        f for f in season.fixtures if not (team in f.teams() and MON <= f.date <= end)
    ]
    out = []
    for day in days:
        busy = {t for f in season.fixtures if f.date == day for t in f.teams()}
        opponent = next(t for t in TEAMS if t not in PLANNED | busy)
        n = next(ids)
        game = Fixture(f"00229{n:05d}", f"88{n:08d}", day, team, opponent)
        season.fixtures.append(game)
        out.append(game)
    return out


class Week:
    """Roster 1's starters A-E and roster 2's F, on schedules built for the test."""

    def __init__(self) -> None:
        self.season = s = SyntheticSeason(n_rosters=2, per_roster=8)
        self.ids = itertools.count(1)  # per week, so every instance is the same season
        mine, theirs = s.rosters[1], s.rosters[2]
        self.a, self.b, self.c, self.d, self.e = mine[:5]
        self.f = theirs[0]
        plan = {
            self.a: ("ATL", [MON, THU]),  # played Monday; open until Thursday
            self.b: ("BOS", [TUE, FRI]),  # played Tuesday; open until Friday
            self.c: ("CHI", [MON, TUE, THU]),  # Monday, a DNP Tuesday, then Thursday
            self.d: ("DAL", [TUE, WED]),  # played Tuesday; closes at tonight's tip
            self.e: ("DEN", [MON, TUE, THU]),  # locked Monday, sat Tuesday
            self.f: ("DET", [MON, TUE, THU]),  # the opponent's own Monday lock
        }
        assert {team for team, _ in plan.values()} == PLANNED
        # Clear every planned team's week first, so no later team's fixtures
        # can be matched against one already built.
        for team, _ in plan.values():
            fixture_week(s, team, [], self.ids)
        self.games = {}
        for pid, (team, days) in plan.items():
            s.players[pid].team = team
            s.players[pid].dnp_rate = 0.0
            self.games[pid] = fixture_week(s, team, days, self.ids)
        s.dnp.add((self.games[self.c][1].sleeper_game_id, self.c))
        s.dnp.add((self.games[self.e][1].sleeper_game_id, self.e))
        s.dnp.add((self.games[self.f][1].sleeper_game_id, self.f))
        s.locks[(2, 1, self.e)] = MON
        s.locks[(2, 2, self.f)] = MON
        s.play_through(TUE)

    def ingest(self, tmp_path, **kwargs):
        self.cfg = config_for(tmp_path, self.season)
        ingest(self.season, self.cfg, weeks=[1, 2], **kwargs)
        return self

    def digest(self, *, now: str = "2026-10-28T13:00:00+00:00", **kwargs):
        conn = connect(self.cfg)
        ctx = digest_mod.load_context(conn, "2026", params=ProjectionParams(min_pool_rows=20))
        return digest_mod.build(
            ctx,
            1,
            WED.isoformat(),
            n_sims=100,
            n_paths=100,
            live=True,
            now=datetime.fromisoformat(now),
            **kwargs,
        )

    def score(self, pid: str, n: int) -> float:
        return self.season.score(self.games[pid][n], pid)


@pytest.fixture
def week(tmp_path):
    return Week().ingest(tmp_path)


# ------------------------------------------------------------- finding 6


def test_each_player_is_called_on_his_own_open_window(week):
    """Monday's game is still bankable on Wednesday; the team's Tuesday is irrelevant."""
    report = week.digest()

    calls = {c.sleeper_id: c for c in report.calls}
    assert not report.abstained, report.note
    assert date.fromordinal(calls[week.a].day) == MON
    assert calls[week.a].score == week.score(week.a, 0)
    assert date.fromordinal(calls[week.b].day) == TUE
    assert week.d in calls


def test_a_dnp_between_two_games_closes_the_earlier_window(week):
    """Tuesday's DNP tipped after Monday's game: Monday cannot be banked, and a 0.0
    is nothing to bank."""
    report = week.digest()
    assert week.c not in {c.sleeper_id for c in report.calls}


def test_each_call_carries_the_tip_that_closes_it(week):
    report = week.digest()
    calls = {c.sleeper_id: c for c in report.calls}
    assert calls[week.a].expires_utc == week.games[week.a][1].tipoff_utc  # Thursday
    assert calls[week.d].expires_utc == week.games[week.d][1].tipoff_utc  # tonight


# ------------------------------------------------------------- finding 7


def test_a_call_whose_tip_has_passed_is_gone(week):
    """Re-run after tonight's tip: D's window shut, A's and B's are still open."""
    report = week.digest(now="2026-10-28T23:45:00+00:00")
    called = {c.sleeper_id for c in report.calls}
    assert week.d not in called
    assert {week.a, week.b} <= called


def test_a_run_before_last_nights_slate_is_final_waits(week):
    report = week.digest(now="2026-10-28T03:00:00+00:00")
    assert report.abstained
    assert "still be in progress" in report.note


def test_before_seven_utc_it_says_to_wait_not_to_ingest_again(tmp_path):
    """Its ingest ran before 07:00 too, so the stats request predates the slate's
    end. "Run ingest again" would fail the same way until 07:00."""
    w = Week().ingest(tmp_path, at="2026-10-28T05:30:00")

    report = w.digest(now="2026-10-28T06:00:00+00:00")

    assert report.abstained
    assert "Re-run after 07:00 UTC" in report.note


def test_a_game_the_nba_has_finished_but_sleeper_has_not_scored_waits(tmp_path):
    w = Week()
    blank = w.games[w.b][0]  # Tuesday, final on the NBA side
    real = w.season.stats
    w.season.stats = lambda f, sid: {} if f is blank else real(f, sid)
    w.ingest(tmp_path)

    report = w.digest()

    assert report.abstained
    assert "last night is not final yet" in report.note


# ------------------------------------------------------------- finding 2


def test_last_nights_calls_are_delivered_not_assumed_taken(week):
    """No `--locked`: the poll says what is banked, and last night is a call."""
    report = week.digest()
    assert report.state_source == "inferred"
    assert week.b not in report.banked and week.d not in report.banked
    assert {week.b, week.d} <= {c.sleeper_id for c in report.calls}


def test_a_closed_window_lock_is_read_from_the_poll(week):
    """E locked Monday and sat Tuesday: his counted value froze on Monday's game."""
    report = week.digest()
    assert report.banked == {week.e: week.score(week.e, 0)}
    assert week.e not in {c.sleeper_id for c in report.calls}


def test_the_opponents_locks_are_read_too(week):
    report = week.digest()
    assert report.opponent_state == "inferred"
    with connect(week.cfg) as conn:
        ctx = digest_mod.load_context(conn, "2026", params=ProjectionParams(min_pool_rows=20))
        theirs = digest_mod.lineup_as_of(ctx, ctx.lineup_ids(2, 2), 2, WED.toordinal() - 1)
        state = infer_state(conn, 2, 2, theirs, WED.toordinal() - 1, before="2026-10-28T13:00Z")
    assert state.banked == {week.f: week.score(week.f, 0)}


def test_a_poll_nothing_explains_is_refused(week):
    with connect(week.cfg) as conn:
        conn.execute(
            "UPDATE weekly_matchups SET counted_points = 999.5"
            " WHERE sleeper_id = ? AND week = 2"
            "   AND observed_at = (SELECT MAX(observed_at) FROM weekly_matchups WHERE week = 2)",
            (week.a,),
        )
    report = week.digest()
    assert report.abstained
    assert "lock state unclear" in report.note


def test_an_ingest_from_before_last_nights_games_is_not_fresh(tmp_path):
    """Finding 9, live: the newest complete ingest must post-date the slate it reads."""
    w = Week().ingest(tmp_path, at="2026-10-27T20:00:00")  # Tuesday evening, before the games

    report = w.digest()

    assert report.abstained
    assert "started before last night's games finished" in report.note


def test_no_poll_since_last_night_means_no_live_advice(tmp_path):
    """A fresh ingest whose matchups poll lacks this roster: state unknown, not guessed."""
    w = Week()
    w.cfg = config_for(tmp_path, w.season)
    ingest(w.season, w.cfg, weeks=[1, 2], at="2026-10-27T20:00:00")
    ingest(w.season, w.cfg, weeks=[1, 2])
    with connect(w.cfg) as conn:
        latest = conn.execute(
            "SELECT MAX(observed_at) FROM weekly_matchup_teams WHERE week = 2 AND roster_id = 1"
        ).fetchone()[0]
        for table in ("weekly_matchups", "weekly_matchup_teams"):
            conn.execute(
                f"DELETE FROM {table} WHERE week = 2 AND roster_id = 1 AND observed_at = ?",
                (latest,),
            )

    report = w.digest()

    assert report.abstained
    assert "lock state unknown" in report.note and "--locked" in report.note


def test_what_you_supply_still_wins(week):
    report = week.digest(locked={week.a: week.score(week.a, 0)})
    assert report.state_source == "supplied"
    assert report.banked == {week.a: week.score(week.a, 0)}
    assert week.a not in {c.sleeper_id for c in report.calls}


def test_the_rendered_calls_name_their_deadline(week):
    text = digest_mod.render(week.digest())
    assert "BEFORE WED 7:30PM TIP" in text, text
    assert all(len(line) <= digest_mod.WIDTH for line in text.splitlines())


def test_week_uses_real_team_codes():
    assert {"ATL", "BOS", "CHI", "DAL", "DEN", "DET"} <= set(TEAMS)
    assert OPENING < MON
    assert datetime(2026, 10, 28, tzinfo=UTC).weekday() == 2


# --------------------------------------------------- reading one poll value


def games(*scores):
    """Played games on consecutive days, scores as given (None: a DNP)."""
    return [
        Game(index=i, day=100 + i, played=x is not None, score=x or 0.0)
        for i, x in enumerate(scores)
    ]


@pytest.mark.parametrize(
    ("counted", "seen", "verdict"),
    [
        (30.0, games(30.0, 45.0), BANKED),  # froze on Monday
        (45.0, games(30.0, 45.0), OPEN),  # riding, or locked last night
        (0.0, games(30.0, None), OPEN),  # sat his latest: counts 0.0 so far
        (30.0, games(30.0, 30.0), AMBIGUOUS),  # locked Monday, or riding Tuesday's 30
        (30.0, games(30.0, 30.0, 12.0), BANKED),  # one of the two 30s: banked either way
        (99.5, games(30.0, 45.0), UNRESOLVED),  # nothing he played explains it
        (7.0, [], UNRESOLVED),  # no game, yet a score
    ],
)
def test_each_reading_of_a_poll_value(counted, seen, verdict):
    assert reading(counted, seen) == verdict


def test_a_tie_between_two_games_warns_rather_than_blocking_advice(tmp_path):
    """A 41 on Monday and a 41 on Tuesday: locked Monday, or riding? Say so; keep going."""
    w = Week()
    pid = w.season.rosters[1][5]
    assert not any("WAS" in f.teams() for gs in w.games.values() for f in gs)
    mon, tue, _ = fixture_week(w.season, "WAS", [MON, TUE, THU], w.ids)
    w.season.players[pid].team, w.season.players[pid].dnp_rate = "WAS", 0.0
    real = w.season.stats
    w.season.stats = lambda f, sid: real(mon, sid) if (sid == pid and f is tue) else real(f, sid)
    w.season.play_through(TUE)
    w.ingest(tmp_path)

    report = w.digest()

    assert not report.abstained, report.note
    assert pid in {c.sleeper_id for c in report.calls}
    assert any(x.sleeper_id == pid and x.kind == "lock state ambiguous" for x in report.warnings)


def test_a_poll_silent_about_a_player_who_has_only_sat_is_not_a_mystery(tmp_path):
    """Sparse `players_points`: C's only games so far are Monday and a DNP. Drop him
    from the poll and nothing is unexplained — a DNP cannot be banked. Drop A, who
    played, and it is."""
    w = Week()
    w.season.dnp.add((w.games[w.c][0].sleeper_game_id, w.c))  # Monday too: all DNPs
    w.ingest(tmp_path)

    def silence(pid):
        with connect(w.cfg) as conn:
            conn.execute(
                "UPDATE weekly_matchups SET counted_points = NULL WHERE sleeper_id = ?"
                "   AND week = 2 AND observed_at ="
                "   (SELECT MAX(observed_at) FROM weekly_matchups WHERE week = 2)",
                (pid,),
            )

    silence(w.c)
    assert not w.digest().abstained
    silence(w.a)
    report = w.digest()
    assert report.abstained and "lock state unclear" in report.note
