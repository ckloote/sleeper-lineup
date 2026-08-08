"""Raw payload snapshots.

These exist because Sleeper rewrites completed seasons and publishes no history
(implementation-plan.md §12). The properties that matter are: snapshots live
outside the database so a rebuild cannot destroy them, they deduplicate so a
stable season does not churn, and the earliest one is always recoverable.
"""

from __future__ import annotations

from lockin.store import snapshots


def payload(*values: float) -> list[dict]:
    """A minimal matchups payload: one roster, six starters."""
    return [
        {
            "roster_id": 1,
            "matchup_id": 1,
            "starters": [f"p{i}" for i in range(len(values))],
            "starters_points": list(values),
            "players_points": {f"p{i}": v for i, v in enumerate(values)},
            "points": sum(values),
        }
    ]


def test_first_save_writes_a_file(tmp_path):
    p = snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, payload(1.0), stamp="A")
    assert p is not None and p.exists()
    assert snapshots.latest(tmp_path, snapshots.MATCHUPS, "2025", 12) == payload(1.0)


def test_identical_payload_is_not_rewritten(tmp_path):
    """A stable season must not churn one file per ingest."""
    snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, payload(1.0), stamp="A")
    second = snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, payload(1.0), stamp="B")
    assert second is None
    assert len(snapshots.list_snapshots(tmp_path, snapshots.MATCHUPS, "2025", 12)) == 1


def test_changed_payload_appends_rather_than_overwriting(tmp_path):
    """The whole point: the earlier observation must survive the later one."""
    snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, payload(1.0), stamp="A")
    snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, payload(2.0), stamp="B")
    assert len(snapshots.list_snapshots(tmp_path, snapshots.MATCHUPS, "2025", 12)) == 2
    assert snapshots.earliest(tmp_path, snapshots.MATCHUPS, "2025", 12) == payload(1.0)
    assert snapshots.latest(tmp_path, snapshots.MATCHUPS, "2025", 12) == payload(2.0)


def test_key_order_does_not_count_as_a_change(tmp_path):
    """Dedup compares content, not serialisation, so JSON key order is irrelevant."""
    snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, payload(1.0), stamp="A")
    reordered = [dict(reversed(list(payload(1.0)[0].items())))]
    assert snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, reordered, stamp="B") is None


def test_snapshots_sort_oldest_first(tmp_path):
    for stamp, v in [("20260806T000000Z", 1.0), ("20260101T000000Z", 2.0)]:
        snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, payload(v), stamp=stamp)
    paths = snapshots.list_snapshots(tmp_path, snapshots.MATCHUPS, "2025", 12)
    assert [p.stem for p in paths] == ["20260101T000000Z", "20260806T000000Z"]


def test_missing_week_returns_nothing(tmp_path):
    assert snapshots.list_snapshots(tmp_path, snapshots.MATCHUPS, "2025", 3) == []
    assert snapshots.earliest(tmp_path, snapshots.MATCHUPS, "2025", 3) is None


def test_weeks_are_kept_separate(tmp_path):
    snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 12, payload(1.0), stamp="A")
    snapshots.save(tmp_path, snapshots.MATCHUPS, "2025", 13, payload(9.0), stamp="A")
    assert snapshots.latest(tmp_path, snapshots.MATCHUPS, "2025", 12) == payload(1.0)
    assert snapshots.latest(tmp_path, snapshots.MATCHUPS, "2025", 13) == payload(9.0)


# --- drift ------------------------------------------------------------------


def test_counted_values_reads_starters_only():
    """Bench players' players_points can hold a stale value from a game played
    while started, so drift must be measured on starters."""
    p = payload(1.0, 2.0)
    p[0]["players_points"]["bench"] = 99.0
    values = snapshots.counted_values(p)
    assert values == {(1, "p0"): 1.0, (1, "p1"): 2.0}


def test_counted_values_skips_empty_slots():
    p = payload(1.0)
    p[0]["starters"] = ["0"]
    assert snapshots.counted_values(p) == {}


def test_diff_reports_changed_starters():
    before, after = payload(1.0, 2.0), payload(1.0, 5.0)
    assert snapshots.diff_counted(before, after) == [(1, "p1", 2.0, 5.0)]


def test_diff_is_empty_for_identical_payloads():
    assert snapshots.diff_counted(payload(1.0, 2.0), payload(1.0, 2.0)) == []


def test_diff_ignores_sub_cent_noise():
    assert snapshots.diff_counted(payload(1.0), payload(1.0001)) == []


def test_diff_handles_a_roster_absent_from_one_side():
    """Comparing only shared keys keeps a schema change from reading as drift."""
    before = payload(1.0)
    after = payload(1.0)
    after.append({"roster_id": 2, "matchup_id": 1, "starters": ["x"], "starters_points": [3.0]})
    assert snapshots.diff_counted(before, after) == []
