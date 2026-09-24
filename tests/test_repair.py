"""`lockin repair` — recover the original locks from the archive.

Sleeper rewrites completed seasons, but a corrupted starter value reverts to the
stored one at the next rewrite (implementation-plan.md §12, "The locks are
intact"). With three or more observations the original is recoverable by
counting, which is what this command does.

Two properties carry the design and each has a test that would fail loudly if it
broke. It **appends** — a repair that updated in place would destroy the very
history that proves the repair was right. And it touches **starters only** — a
bench player's `players_points` can hold a stale value from a game played while
started, so the archive is not authoritative for it.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lockin import cli, repair
from lockin.store import snapshots
from lockin.store.db import now_iso, session

STARTERS = ["1697", "1809", "2133"]


def payload(roster_points, *, matchup_id=1):
    """Sleeper's matchup shape: {roster_id: {player: points}}."""
    return [
        {
            "roster_id": roster_id,
            "matchup_id": matchup_id,
            "starters": STARTERS,
            "starters_points": [points[p] for p in STARTERS],
            "players": [*STARTERS, "9999"],
            "players_points": {**points, "9999": 0.0},
            "points": sum(points[p] for p in STARTERS),
            "custom_points": None,
        }
        for roster_id, points in roster_points.items()
    ]


def archive(root, season, week, payloads, *, final_from: int | None = 0):
    """Snapshot each payload a day apart.

    ``final_from`` is the index of the first one taken after Sleeper finished
    scoring the week — the marker `lockin repair` reads evidence from. None
    leaves the week open.
    """
    for i, p in enumerate(payloads):
        stamp = f"2026090{i + 1}T000000Z"
        if final_from is not None and i == final_from:
            snapshots.mark_finalized(root, snapshots.MATCHUPS, season, week, stamp=stamp)
        snapshots.save(root, snapshots.MATCHUPS, season, week, p, stamp=stamp)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCKIN_SNAPSHOTS", "snapshots")
    monkeypatch.setenv("LOCKIN_SEASON", "2025")
    monkeypatch.setenv("LOCKIN_DB", "data/season.db")
    with session(tmp_path / "data" / "season.db", create=True) as conn:
        conn.execute("SELECT 1")
    return tmp_path


def seed_db(path, week, roster_points, *, observed_at="2026-08-08T00:00:00+00:00"):
    """One ingest's worth of rows, as `ingest_matchups` would have written them."""
    with session(path / "data" / "season.db") as conn:
        for roster_id, points in roster_points.items():
            conn.execute(
                "INSERT OR REPLACE INTO weekly_matchup_teams"
                " (week, roster_id, matchup_id, points, custom_points, observed_at)"
                " VALUES (?, ?, 1, ?, NULL, ?)",
                (week, roster_id, sum(points[p] for p in STARTERS), observed_at),
            )
            for player, value in {**points, "9999": 0.0}.items():
                starter = player in STARTERS
                conn.execute(
                    "INSERT OR REPLACE INTO weekly_matchups"
                    " (week, roster_id, matchup_id, sleeper_id, counted_points, is_starter,"
                    "  slot_index, slot, observed_at)"
                    " VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
                    (
                        week,
                        roster_id,
                        player,
                        value,
                        1 if starter else 0,
                        STARTERS.index(player) if starter else None,
                        "UTIL" if starter else None,
                        observed_at,
                    ),
                )


# --- the vote ------------------------------------------------------------


def test_the_majority_wins(project):
    """Three reads say 42.5, one says 54.5. The outlier is the corruption."""
    root = project / "snapshots"
    archive(
        root,
        "2025",
        12,
        [
            payload({1: {"1697": 42.5, "1809": 30.0, "2133": 20.0}}),
            payload({1: {"1697": 54.5, "1809": 30.0, "2133": 20.0}}),
            payload({1: {"1697": 42.5, "1809": 30.0, "2133": 20.0}}),
            payload({1: {"1697": 42.5, "1809": 31.0, "2133": 20.0}}),
        ],
    )
    verdicts, skipped = repair.consensus(root, "2025", [12])

    assert skipped == []
    verdict = verdicts[12][(1, "1697")]
    assert verdict.value == 42.5
    assert (verdict.votes, verdict.observations) == (3, 4)
    assert not verdict.tied


def test_a_tie_breaks_to_the_earliest_observation(project):
    """57 of the 58 tied slots in the 2025 archive have the original among them."""
    root = project / "snapshots"
    archive(
        root,
        "2025",
        12,
        [
            payload({1: {"1697": 42.5, "1809": 30.0, "2133": 20.0}}),
            payload({1: {"1697": 54.5, "1809": 30.0, "2133": 20.0}}),
            payload({1: {"1697": 54.5, "1809": 31.0, "2133": 20.0}}),
            payload({1: {"1697": 42.5, "1809": 30.0, "2133": 20.0}}),
        ],
    )
    verdict = repair.consensus(root, "2025", [12])[0][12][(1, "1697")]

    assert verdict.value == 42.5, "earliest should win a tie"
    assert verdict.tied


def test_a_thin_week_abstains(project):
    """Week 25 has one snapshot. A majority of one is not a majority."""
    root = project / "snapshots"
    archive(root, "2025", 25, [payload({1: {"1697": 42.5, "1809": 30.0, "2133": 20.0}})])

    verdicts, skipped = repair.consensus(root, "2025", [25])

    assert verdicts == {}
    assert skipped == [25]


def test_only_readings_taken_after_the_week_was_final_are_evidence(project):
    """The review's reproduction: [0, 0, 0, 50] must not recover a confident 0.

    Three early polls of a live week read 0.0 because the player had not played
    yet. They are not corruption, and once the week is scored they must not
    outvote the final value.
    """
    root = project / "snapshots"
    early = {"1697": 0.0, "1809": 30.0, "2133": 20.0}
    final = {"1697": 50.0, "1809": 30.0, "2133": 20.0}
    # Distinct payloads, so none dedupe: three live readings, three final ones.
    live = [payload({1: {**early, "1809": 30.0 + i}}) for i in range(3)]
    done = [payload({1: {**final, "2133": 20.0 + i}}) for i in range(3)]
    archive(root, "2025", 12, [*live, *done], final_from=3)

    verdicts, skipped = repair.consensus(root, "2025", [12])

    assert skipped == []
    verdict = verdicts[12][(1, "1697")]
    assert verdict.value == 50.0
    assert (verdict.votes, verdict.observations) == (3, 3), "only final readings vote"


def test_an_open_week_is_refused_not_merely_thin(project):
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    archive(
        root, "2025", 12, [payload({1: {**good, "1809": v}}) for v in (30, 31, 32)], final_from=None
    )

    verdicts, skipped = repair.consensus(root, "2025", [12])

    assert verdicts == {}
    assert skipped == [], "an open week is not short of evidence; it has none yet"
    assert repair.open_weeks(root, "2025", [12]) == [12]


def test_a_plurality_is_reported_as_one(project):
    root = project / "snapshots"
    values = (42.5, 54.5, 42.5, 30.0, 31.0)
    archive(
        root, "2025", 12, [payload({1: {"1697": v, "1809": 30.0, "2133": 20.0}}) for v in values]
    )

    verdict = repair.consensus(root, "2025", [12])[0][12][(1, "1697")]

    assert (verdict.value, verdict.votes, verdict.observations) == (42.5, 2, 5)
    assert verdict.strength == "plurality"


def test_the_first_sighting_of_a_final_week_is_never_moved(tmp_path):
    assert snapshots.mark_finalized(tmp_path, "matchups", "2026", 3, stamp="20261110T103000Z")
    assert not snapshots.mark_finalized(tmp_path, "matchups", "2026", 3, stamp="20261111T103000Z")
    assert snapshots.finalized_at(tmp_path, "matchups", "2026", 3) == "20261110T103000Z"


# --- the plan ------------------------------------------------------------


def test_it_finds_the_database_disagreeing_with_the_archive(project):
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    bad = {**good, "1697": 54.5}
    archive(root, "2025", 12, [payload({1: good}), payload({1: bad}), payload({1: good})])
    seed_db(project, 12, {1: bad})  # the ingest landed on a corrupted day

    with session(project / "data" / "season.db") as conn:
        repairs, _ = repair.plan(conn, root, "2025", [12])

    assert len(repairs) == 1
    assert (repairs[0].db_value, repairs[0].consensus.value) == (54.5, 42.5)
    assert repairs[0].delta == -12.0


def test_an_agreeing_database_needs_no_repair(project):
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    archive(
        root,
        "2025",
        12,
        [payload({1: good}), payload({1: {**good, "1697": 54.5}}), payload({1: good})],
    )
    seed_db(project, 12, {1: good})

    with session(project / "data" / "season.db") as conn:
        repairs, _ = repair.plan(conn, root, "2025", [12])

    assert repairs == []


# --- the write -----------------------------------------------------------


def test_it_appends_and_never_overwrites(project):
    """The corrupted row is the evidence. Losing it would be the same bug again."""
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    bad = {**good, "1697": 54.5}
    archive(root, "2025", 12, [payload({1: good}), payload({1: bad}), payload({1: good})])
    seed_db(project, 12, {1: bad})

    with session(project / "data" / "season.db") as conn:
        repairs, _ = repair.plan(conn, root, "2025", [12])
        repair.apply(conn, root, "2025", repairs, observed_at=now_iso())
        rows = conn.execute(
            "SELECT counted_points FROM weekly_matchups"
            " WHERE week = 12 AND roster_id = 1 AND sleeper_id = '1697'"
            " ORDER BY observed_at"
        ).fetchall()
        latest = conn.execute(
            "SELECT counted_points FROM weekly_matchups_latest"
            " WHERE week = 12 AND roster_id = 1 AND sleeper_id = '1697'"
        ).fetchone()

    assert [r["counted_points"] for r in rows] == [54.5, 42.5], "both observations must survive"
    assert latest["counted_points"] == 42.5, "readers must see the repaired value"


def test_the_team_total_is_recomputed_from_the_repaired_starters(project):
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    bad = {**good, "1697": 54.5}
    archive(root, "2025", 12, [payload({1: good}), payload({1: bad}), payload({1: good})])
    seed_db(project, 12, {1: bad})

    with session(project / "data" / "season.db") as conn:
        repairs, _ = repair.plan(conn, root, "2025", [12])
        repair.apply(conn, root, "2025", repairs, observed_at=now_iso())
        total = conn.execute(
            "SELECT points, matchup_id FROM weekly_matchup_teams"
            " WHERE week = 12 AND roster_id = 1 ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()

    assert total["points"] == 92.5, "points must stay the sum of its six slots"
    assert total["matchup_id"] == 1, "the pairing is carried forward, not invented"


def test_the_bench_is_left_alone(project):
    """`players_points` for a bench player can be stale; the archive cannot fix it."""
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    bad = {**good, "1697": 54.5}
    archive(root, "2025", 12, [payload({1: good}), payload({1: bad}), payload({1: good})])
    seed_db(project, 12, {1: bad})

    with session(project / "data" / "season.db") as conn:
        repairs, _ = repair.plan(conn, root, "2025", [12])
        repair.apply(conn, root, "2025", repairs, observed_at=now_iso())
        bench = conn.execute(
            "SELECT counted_points, is_starter FROM weekly_matchups_latest"
            " WHERE sleeper_id = '9999'"
        ).fetchall()

    assert repairs, "the corrupted starter should have been planned"
    assert all(r.consensus.sleeper_id != "9999" for r in repairs), (
        "the archive must never propose a bench value"
    )
    # Still on the roster — a repair writes the whole poll — and untouched.
    assert [(r["counted_points"], r["is_starter"]) for r in bench] == [(0.0, 0)]


def test_the_write_is_logged_as_a_repair(project):
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    bad = {**good, "1697": 54.5}
    archive(root, "2025", 12, [payload({1: good}), payload({1: bad}), payload({1: good})])
    seed_db(project, 12, {1: bad})

    with session(project / "data" / "season.db") as conn:
        repairs, _ = repair.plan(conn, root, "2025", [12])
        repair.apply(conn, root, "2025", repairs, observed_at=now_iso())
        logged = conn.execute("SELECT source, target, rows FROM ingest_log").fetchall()

    assert [(r["source"], r["target"], r["rows"]) for r in logged] == [
        ("repair", "weekly_matchups:week=12", 1)
    ]


# --- the statistics ------------------------------------------------------


def test_stats_show_the_reversion_asymmetry(project):
    """The finding the whole command rests on: wrong values come back.

    A regeneration on each read cannot prefer one value over another based on
    where it came from, so it would make these two rates equal.
    """
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    archive(
        root,
        "2025",
        12,
        [
            payload({1: good}),
            payload({1: {**good, "1697": 54.5}}),
            payload({1: good}),
            payload({1: {**good, "1697": 12.5}}),
            payload({1: good}),
        ],
    )
    st = repair.stats(root, "2025", [12])

    assert st.return_to == st.return_to_total, "every excursion returned"
    assert st.return_to_total == 2
    assert st.excursions[1] == 2, "both excursions lasted one rewrite"
    assert st.earliest_agrees == st.earliest_total


def test_stats_count_a_flipped_matchup(project):
    """12.8% of real matchup-observations report the wrong winner."""
    root = project / "snapshots"
    a = {"1697": 42.5, "1809": 30.0, "2133": 20.0}  # 92.5
    b = {"1697": 40.0, "1809": 30.0, "2133": 20.0}  # 90.0, loses
    archive(
        root,
        "2025",
        12,
        [
            payload({1: a, 2: b}),
            payload({1: {**a, "1697": 20.0}, 2: b}),  # 70.0 — roster 1 now loses
            payload({1: a, 2: b}),
        ],
    )
    st = repair.stats(root, "2025", [12])

    assert (st.matchups_wrong, st.matchups_total) == (1, 3)


# --- the command ---------------------------------------------------------


def test_the_dry_run_writes_nothing(project):
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    bad = {**good, "1697": 54.5}
    archive(root, "2025", 12, [payload({1: good}), payload({1: bad}), payload({1: good})])
    seed_db(project, 12, {1: bad})
    before = (project / "data" / "season.db").read_bytes()

    result = CliRunner().invoke(cli.main, ["repair", "--weeks", "12"])

    assert result.exit_code == 0, result.output
    assert "54.5 -> 42.5" in result.output
    assert "--apply" in result.output
    assert (project / "data" / "season.db").read_bytes() == before


def test_apply_repairs_the_season(project):
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    bad = {**good, "1697": 54.5}
    archive(root, "2025", 12, [payload({1: good}), payload({1: bad}), payload({1: good})])
    seed_db(project, 12, {1: bad})

    result = CliRunner().invoke(cli.main, ["repair", "--weeks", "12", "--apply"])

    assert result.exit_code == 0, result.output
    assert "1 starter rows" in result.output
    with session(project / "data" / "season.db") as conn:
        value = conn.execute(
            "SELECT counted_points FROM weekly_matchups_latest"
            " WHERE week = 12 AND roster_id = 1 AND sleeper_id = '1697'"
        ).fetchone()["counted_points"]
    assert value == 42.5


def test_json_reports_the_votes(project):
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    bad = {**good, "1697": 54.5}
    archive(root, "2025", 12, [payload({1: good}), payload({1: bad}), payload({1: good})])
    seed_db(project, 12, {1: bad})

    result = CliRunner().invoke(cli.main, ["repair", "--weeks", "12", "--json"])

    body = json.loads(result.output)
    assert body["applied"] is False
    assert body["consensus"][0] == {
        "week": 12,
        "roster_id": 1,
        "sleeper_id": "1697",
        "database": 54.5,
        "archive": 42.5,
        "votes": 2,
        "observations": 3,
        "tied": False,
        "strength": "majority",
    }


def test_stats_never_opens_the_database(project, monkeypatch):
    """`--stats` is an argument about the archive. It needs nothing else."""
    root = project / "snapshots"
    good = {"1697": 42.5, "1809": 30.0, "2133": 20.0}
    archive(
        root,
        "2025",
        12,
        [payload({1: good}), payload({1: {**good, "1697": 54.5}}), payload({1: good})],
    )
    (project / "data" / "season.db").unlink()

    result = CliRunner().invoke(cli.main, ["repair", "--weeks", "12", "--stats"])

    assert result.exit_code == 0, result.output
    assert "reversion" in result.output
