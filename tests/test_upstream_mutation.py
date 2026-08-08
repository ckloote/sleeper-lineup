"""Sleeper mutates completed-season results.

Discovered 2026-08-07 (implementation-plan.md §12). Between 2026-08-05 and
2026-08-07 the recorded results of the finished 2025-26 season changed: 38% of
week-12 starter values, and every team total.

The box scores did NOT change. Only the selection of which game counts moved,
which means the change is in lock state rather than in the underlying stats.

`tests/golden/*_2026-08-05.json` are raw API responses captured on 2026-08-05.
They are the only surviving record of the original values for any week, and the
architecture doc's independently-written week-12 anecdotes corroborate them.

These tests run offline against the committed snapshots. They do not call the
API, so they cannot flake, and they will keep passing after the next mutation —
which is the point: they pin what was true, not what is currently served.
"""

from __future__ import annotations

import json
import pathlib

import pytest

GOLDEN = pathlib.Path(__file__).resolve().parent / "golden"
MATCHUPS = GOLDEN / "matchups_week12_observed_2026-08-05.json"
STATS = GOLDEN / "stats_week12_observed_2026-08-05.json"


@pytest.fixture(scope="module")
def snapshot() -> dict[int, dict]:
    rows = json.loads(MATCHUPS.read_text())
    return {t["roster_id"]: t for t in rows}


def test_the_snapshot_is_committed():
    """If this file is ever lost, the original season is unrecoverable."""
    assert MATCHUPS.exists() and STATS.exists()


def test_snapshot_matches_the_architecture_docs_first_anecdote(snapshot):
    """ "Week 12 ... one of five matchups finished 289.5 to 287.5."

    Written before this project began, from live data. Today's API returns
    291.5 / 287.0 for the same matchup.
    """
    assert snapshot[4]["points"] == 289.5
    assert snapshot[5]["points"] == 287.5
    assert snapshot[4]["matchup_id"] == snapshot[5]["matchup_id"]


def test_snapshot_matches_the_architecture_docs_second_anecdote(snapshot):
    """ "roster 7 started a player who finished 0.0 and lost by 58."

    Today's API returns 289.0 vs 310.5 — a 21.5 margin, and no zeroed starter.
    """
    assert snapshot[7]["points"] == 221.5
    assert snapshot[8]["points"] == 279.5
    assert snapshot[8]["points"] - snapshot[7]["points"] == 58.0
    assert 0.0 in snapshot[7]["starters_points"]


def test_snapshot_totals_equal_the_sum_of_their_starters(snapshot):
    """The snapshot is internally consistent, so it is a real observation
    rather than a partial or corrupted read."""
    for roster_id, team in snapshot.items():
        assert abs(sum(team["starters_points"]) - team["points"]) < 0.005, roster_id


def test_all_ten_rosters_captured(snapshot):
    assert sorted(snapshot) == list(range(1, 11))
    assert all(len(t["starters"]) == 6 for t in snapshot.values())


def test_box_scores_in_the_snapshot_are_a_full_week():
    """Box scores were byte-identical across the mutation, so this file is a
    control: it shows the stats did not move while the counted values did."""
    rows = json.loads(STATS.read_text())
    assert len(rows) == 2069
    assert all(r.get("game_id") for r in rows)
    assert {r["week"] for r in rows} == {12}
