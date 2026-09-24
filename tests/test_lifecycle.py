"""The season, one morning at a time (review 2026-09-23, recommended step 3).

    fresh season -> first tip -> first morning -> DNP -> roster change
    -> partial ingest -> week rollover -> postponement

Each morning runs what the cron runs — `lockin ingest --weeks current` and then
the digest, live — against a synthetic season that has not finished, and the
assertions are on the notification text a phone would show, not on exit codes.
A 50% P(win) on opening day satisfied the old checklist; these ask what the
page actually says.

The league is small (four rosters), so the cold-start threshold is scaled to
it: `min_pool_rows` counts league-wide rows, and a quarter of the league
reaches the real 400 a month late. The threshold itself is gated separately, in
`tests/test_cold_start.py`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest import mock

import pytest
from live_fixture import OPENING, FakeSleeperClient, SyntheticSeason, config_for, connect, ingest

from lockin import digest as digest_mod
from lockin.core.projections import ProjectionParams


class Rehearsal:
    def __init__(self, tmp_path) -> None:
        self.season = SyntheticSeason()
        self.cfg = config_for(tmp_path, self.season)
        self.params = ProjectionParams(min_pool_rows=60)
        self.log: list[str] = []

    def morning(self, day: date, *, ingest_fails_on: int | None = None, locked=None):
        """Last night final; the 06:30 ingest; the 09:00 digest. Returns (digest, push)."""
        self.season.play_through(day - timedelta(days=1))
        if ingest_fails_on is None:
            self.log += ingest(self.season, self.cfg)
        else:
            real = FakeSleeperClient.week_stats

            def failing(client, season, week, season_type="regular"):
                if week == ingest_fails_on:
                    raise RuntimeError("Sleeper is down")
                return real(client, season, week, season_type)

            with (
                mock.patch.object(FakeSleeperClient, "week_stats", failing),
                pytest.raises(RuntimeError),
            ):
                ingest(self.season, self.cfg)

        now = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=13)
        with connect(self.cfg) as conn:
            report = digest_mod.morning(
                conn,
                self.season.season,
                1,
                day.isoformat(),
                n_sims=100,
                locked=locked,
                live=True,
                now=now,
                params=self.params,
            )
            digest_mod.persist(conn, report)
        push = digest_mod.render(report, compact=True)
        assert all(len(line) <= digest_mod.WIDTH for line in push.splitlines()), push
        return report, push

    def starter_playing(self, week: int, *, on: date, then: bool = True) -> str:
        """A roster-1 starter with a game on ``on`` and, if asked, one after it that week."""
        for pid in self.season.starters(week, 1):
            games = [f.date for f in self.season.games_for(self.season.players[pid].team, week)]
            if on in games and (not then or any(d > on for d in games)):
                return pid
        pytest.skip(f"no roster-1 starter plays on {on} in this synthetic season")


def test_a_season_from_opening_morning_to_its_first_postponement(tmp_path):
    r = Rehearsal(tmp_path)
    tue, thu = OPENING, OPENING + timedelta(days=2)

    # -- fresh season, before the first tip ---------------------------------
    report, push = r.morning(tue)
    assert report.abstained and "insufficient history" in push
    assert "P(win)" not in push

    # -- two nights in: games exist, but not enough to calibrate on ----------
    report, push = r.morning(thu)
    assert report.abstained and "not calibrated until 60" in report.note

    # -- the first morning with advice (Wednesday of week 2) -----------------
    wed = OPENING + timedelta(days=8)
    report, push = r.morning(wed)
    assert not report.abstained, report.note
    assert report.week == 2 and report.state_source == "inferred"
    assert "P(win)" in push
    assert "TONIGHT" in push or "TIP" in push

    # -- a DNP last night: nothing to bank, so no call -----------------------
    thursday = wed + timedelta(days=1)
    sat = r.starter_playing(2, on=wed)
    game = next(f for f in r.season.games_for(r.season.players[sat].team, 2) if f.date == wed)
    r.season.dnp.add((game.sleeper_game_id, sat))
    report, _ = r.morning(thursday)
    assert not report.abstained, report.note
    assert sat not in {c.sleeper_id for c in report.calls}

    # -- a roster change between polls ---------------------------------------
    friday = thursday + timedelta(days=1)
    gone = r.season.starters(2, 1)[1]
    arrived = r.season.drop_add(1, gone)
    report, push = r.morning(friday)
    assert not report.abstained, report.note
    assert arrived in report.names and gone not in report.names

    # -- the ingest dies part-way: no advice on half the data ----------------
    saturday = friday + timedelta(days=1)
    report, push = r.morning(saturday, ingest_fails_on=2)
    assert report.abstained
    assert "ingest" in report.note or "not final" in report.note

    # -- the week rolls over (Monday of week 3) ------------------------------
    monday = OPENING + timedelta(days=13)
    report, push = r.morning(monday)
    assert report.week == 3 and "wk 3" in push
    assert not any("WARNING" in line for line in r.log[-25:]), r.log[-25:]

    # -- a postponement: the NBA moves a game out of the week ----------------
    wednesday = monday + timedelta(days=2)
    victim = r.starter_playing(3, on=wednesday + timedelta(days=1), then=False)
    moved = next(
        f
        for f in r.season.games_for(r.season.players[victim].team, 3)
        if f.date == wednesday + timedelta(days=1)
    )
    r.season.postpone(moved.sleeper_game_id, moved.date + timedelta(days=30))
    report, push = r.morning(wednesday)
    night = (wednesday + timedelta(days=1)).toordinal()
    assert not report.abstained, report.note
    assert not any(x.sleeper_id == victim and x.night == night for x in report.rules)
    with connect(r.cfg) as conn:
        ctx = digest_mod.load_context(conn, "2026", params=r.params)
        games = digest_mod.lineup_as_of(ctx, [victim], 3, wednesday.toordinal() - 1, live=True)
    assert night not in {g.day for g in games.get(victim, [])}
