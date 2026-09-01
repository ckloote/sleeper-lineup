"""`lockin observe` — watch upstream without disturbing what it observes.

Sleeper keeps rewriting the completed 2025-26 season (implementation-plan.md
§12), and there is no historical endpoint, so an unobserved change is gone. The
three observations that existed before this command were all accidents of other
work.

The load-bearing property is what it does *not* do. Refreshing the archive with
a full `lockin ingest` would refetch a completed season into the very file that
preserves it — the thing deployment.md step 3 forbids — overwriting `box_scores`
and moving `weekly_matchups` on. So this opens no database at all.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from lockin import cli
from lockin.store import snapshots
from lockin.store.db import session


def payload(**player_points):
    """One roster, six starters, in Sleeper's matchup shape."""
    ids = list(player_points) or ["1000"]
    return [
        {
            "roster_id": 1,
            "matchup_id": 1,
            "starters": ids,
            "starters_points": [player_points[i] for i in ids],
            "players": ids,
            "players_points": dict(player_points),
            "points": sum(player_points.values()),
            "custom_points": None,
        }
    ]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCKIN_SNAPSHOTS", "snapshots")
    monkeypatch.setenv("LOCKIN_SEASON", "2025")
    monkeypatch.setenv("LOCKIN_DB", "data/season.db")
    with session(tmp_path / "data" / "season.db", create=True) as conn:
        conn.execute("SELECT 1")
    return tmp_path


def fake_client(monkeypatch, payloads):
    """Serve a canned payload per week, and count the calls."""
    calls = []

    class Fake:
        def matchups(self, league_id, week):
            calls.append(week)
            return payloads[week]

    monkeypatch.setattr(cli.sleeper_ingest, "SleeperClient", lambda *a, **k: Fake())
    return calls


def test_it_never_opens_the_database(project, monkeypatch):
    """The whole reason this is not `lockin ingest --weeks ...`."""
    db = project / "data" / "season.db"
    before = db.read_bytes()
    fake_client(monkeypatch, {12: payload(**{"1697": 42.5})})

    opened = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: (opened.append(a[0]), real_connect(*a, **k))[1]
    )

    result = CliRunner().invoke(cli.main, ["observe", "--weeks", "12"])

    assert result.exit_code == 0, result.output
    assert opened == [], f"observe opened a database: {opened}"
    assert db.read_bytes() == before


def test_a_first_observation_is_written(project, monkeypatch):
    fake_client(monkeypatch, {12: payload(**{"1697": 42.5})})

    result = CliRunner().invoke(cli.main, ["observe", "--weeks", "12"])

    assert "first observation" in result.output
    assert len(snapshots.list_snapshots(project / "snapshots", "matchups", "2025", 12)) == 1


def test_an_unchanged_week_writes_nothing(project, monkeypatch):
    """Dedup is what makes a weekly cron free on a stable season."""
    same = payload(**{"1697": 42.5})
    fake_client(monkeypatch, {12: same})
    runner = CliRunner()

    runner.invoke(cli.main, ["observe", "--weeks", "12"])
    result = runner.invoke(cli.main, ["observe", "--weeks", "12"])

    assert "unchanged" in result.output
    assert len(snapshots.list_snapshots(project / "snapshots", "matchups", "2025", 12)) == 1


def test_a_changed_week_is_recorded_with_the_values_that_moved(project, monkeypatch):
    """The §12 mutation, in miniature: same player, a different game's score."""
    payloads = {12: payload(**{"1697": 42.5})}
    fake_client(monkeypatch, payloads)
    runner = CliRunner()
    runner.invoke(cli.main, ["observe", "--weeks", "12"])

    payloads[12] = payload(**{"1697": 29.0})
    result = runner.invoke(cli.main, ["observe", "--weeks", "12"])

    assert "CHANGED" in result.output
    assert "42.5 -> 29.0" in result.output
    paths = snapshots.list_snapshots(project / "snapshots", "matchups", "2025", 12)
    assert len(paths) == 2, "the earlier observation must survive, never be overwritten"
    assert json.loads(paths[0].read_text())[0]["players_points"]["1697"] == 42.5


def test_it_sweeps_every_week_by_default(project, monkeypatch):
    calls = fake_client(monkeypatch, {w: payload(**{"1697": 1.0}) for w in range(1, 26)})

    CliRunner().invoke(cli.main, ["observe"])

    assert calls == list(range(1, 26))


def test_current_is_refused_with_a_reason(project, monkeypatch):
    """`current` is for ingesting a live week; observe watches weeks already played."""
    fake_client(monkeypatch, {})

    result = CliRunner().invoke(cli.main, ["observe", "--weeks", "current"])

    assert result.exit_code != 0
    assert "name them" in result.output


def test_two_observations_in_one_second_do_not_collide(project, monkeypatch):
    """`stamp` resolves to the second; the earlier payload is irreplaceable (§12).

    Found by this suite: `save` wrote `{stamp}.json` unconditionally, so a rerun
    inside the same second silently overwrote the observation it was meant to
    preserve — the one thing snapshots exist to prevent.
    """
    payloads = {12: payload(**{"1697": 42.5})}
    fake_client(monkeypatch, payloads)
    monkeypatch.setattr(cli.sleeper_ingest, "snapshot_stamp", lambda: "20260901T020425Z")
    runner = CliRunner()
    runner.invoke(cli.main, ["observe", "--weeks", "12"])

    payloads[12] = payload(**{"1697": 29.0})
    runner.invoke(cli.main, ["observe", "--weeks", "12"])

    paths = snapshots.list_snapshots(project / "snapshots", "matchups", "2025", 12)
    assert len(paths) == 2
    first, second = (json.loads(p.read_text())[0]["players_points"]["1697"] for p in paths)
    assert (first, second) == (42.5, 29.0), "list_snapshots must stay in time order"
