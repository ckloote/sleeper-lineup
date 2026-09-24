"""The availability capture, which is the one record that cannot be backfilled.

Sleeper's `/players/nba` publishes only *today's* `injury_status` and keeps no
history (§17), so a day not written down is gone. That makes the capture unlike
everything else here: a bug in the scoring engine is found by re-running it, and
a bug in this is found next season when the data is not there.

These tests exist because the capture *did* fail silently. It sat behind
`--full`, the Phase 6 crontab omitted the flag, and the result was one day of
data and no error message. The behaviour they pin is therefore not "it works"
but "it cannot be switched off by forgetting something".
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from lockin.cli import main
from lockin.ingest.sleeper import designations_as_of, record_player_status, status_coverage
from lockin.store.db import session

PAYLOAD = {
    "1000": {"full_name": "Jimmy Butler", "injury_status": "DTD"},
    "1787": {"full_name": "Jarrett Allen", "injury_status": None},
    "2126": {"full_name": "Tyrese Maxey", "injury_status": "Out"},
    "1966": {"full_name": "Ray Spalding", "injury_status": "IR"},
    "_meta": "not a dict",
}


def _events(conn, sleeper_id):
    return [
        (r["observed_at"], r["designation"])
        for r in conn.execute(
            "SELECT observed_at, designation FROM player_status_events"
            " WHERE sleeper_id = ? ORDER BY observed_at",
            (sleeper_id,),
        )
    ]


def test_only_flagged_players_are_stored(tmp_path):
    """A player never flagged writes nothing; 2,000 nulls a day would bury the signal."""
    with session(tmp_path / "t.db") as conn:
        assert record_player_status(conn, PAYLOAD, "2026-10-21") == 3
        stored = {
            r["sleeper_id"]: r["designation"]
            for r in conn.execute("SELECT sleeper_id, designation FROM player_status_events")
        }
    assert stored == {"1000": "DTD", "2126": "Out", "1966": "IR"}


def test_rerunning_within_a_day_is_idempotent(tmp_path):
    """Cron retries, and a manual run after one should not double the day."""
    with session(tmp_path / "t.db") as conn:
        record_player_status(conn, PAYLOAD, "2026-10-21")
        record_player_status(conn, PAYLOAD, "2026-10-21")
        assert status_coverage(conn) == (1, 3)


def test_running_across_days_accumulates(tmp_path):
    """The point of the table. Each day is a separate, unrecoverable fact."""
    with session(tmp_path / "t.db") as conn:
        record_player_status(conn, PAYLOAD, "2026-10-21")
        record_player_status(conn, {"1000": {"injury_status": "Out"}}, "2026-10-22")
        days, rows = status_coverage(conn)
        history = _events(conn, "1000")
    assert (days, rows) == (2, 4)
    # A designation that changed is two rows, not an overwrite — the change is
    # the signal that start/sit evaluation needs.
    assert history == [("2026-10-21", "DTD"), ("2026-10-22", "Out")]


def test_a_stalled_capture_is_visible_in_the_day_count(tmp_path):
    """Rows alone cannot reveal a capture frozen months ago; days can.

    This is the number the ingest prints, and the reason it prints days rather
    than the row total it used to.
    """
    with session(tmp_path / "t.db") as conn:
        for _ in range(50):  # fifty re-runs, all on the same day
            record_player_status(conn, PAYLOAD, "2026-10-21")
        days, rows = status_coverage(conn)
    assert rows == 3
    assert days == 1, "a frozen capture must not look like a growing one"


def test_ingest_has_no_flag_that_could_switch_the_capture_off():
    """`--full` is gone, and must not come back.

    It was the mechanism of the silent failure: the designations rode in on the
    payload it gated, so forgetting it in a crontab meant losing the season's
    availability record without any error. If someone reintroduces a flag with
    this name, this test is the objection.
    """
    command = main.commands["ingest"]
    names = {option.name for option in command.params}
    assert "full" not in names
    assert names == {"weeks", "skip_nba", "skip_tipoffs"}


def test_passing_the_old_flag_fails_loudly_rather_than_being_ignored(monkeypatch):
    """A stale crontab must break, not silently skip the capture again.

    Click rejects the unknown option while parsing, so this never reaches the
    network — the failure is at argument level, which is where it belongs.
    """
    result = CliRunner().invoke(main, ["ingest", "--full", "--weeks", "1"])
    assert result.exit_code == 2
    assert "no such option" in result.output.lower()


def test_status_coverage_is_zero_on_a_fresh_database(tmp_path):
    with session(tmp_path / "t.db") as conn:
        assert status_coverage(conn) == (0, 0)


def test_click_is_the_only_thing_that_needs_the_flag_gone():
    """Guard against the option being removed from the signature but not the UI.

    A stray `--full` left in the decorator would make `ingest` raise TypeError
    on every invocation, which is loud — but the reverse, a parameter with no
    decorator, silently defaults and is exactly how this class of bug hides.
    """
    import inspect

    from lockin.cli import ingest

    params = set(inspect.signature(ingest.callback).parameters)
    declared = {option.name for option in ingest.params}
    assert params == declared, "CLI options and callback parameters must match"


@pytest.mark.parametrize("designation", ["DTD", "Out", "IR", "Questionable", "GTD"])
def test_designations_are_stored_verbatim(tmp_path, designation):
    """Sleeper's vocabulary is not ours to normalise.

    2025-26 shows DTD / Out / IR, but the field is free-form upstream and a
    mapping table would silently drop anything new. Store what arrives; decide
    what it means at read time.
    """
    with session(tmp_path / "t.db") as conn:
        record_player_status(conn, {"1": {"injury_status": designation}}, "2026-10-21")
        stored = conn.execute("SELECT designation FROM player_status_events").fetchone()
    assert stored["designation"] == designation


def test_the_command_group_still_exposes_ingest():
    assert isinstance(main.commands["ingest"], click.Command)


# ----------------------------------------------- review 2026-09-23, finding 8


def test_a_designation_cleared_later_the_same_day_is_recorded_as_cleared(tmp_path):
    """The review's reproduction: Out, then nothing. The old table kept Out all day."""
    with session(tmp_path / "t.db") as conn:
        record_player_status(conn, {"7": {"injury_status": "Out"}}, "2026-10-21T10:30:00+00:00")
        record_player_status(conn, {"7": {"injury_status": None}}, "2026-10-21T18:00:00+00:00")

        assert _events(conn, "7") == [
            ("2026-10-21T10:30:00+00:00", "Out"),
            ("2026-10-21T18:00:00+00:00", None),
        ]
        assert designations_as_of(conn, "2026-10-21T20:00:00+00:00") == {}


def test_the_status_known_before_a_decision_survives_a_later_update(tmp_path):
    """Out at 10:00, Questionable at 14:00: at noon he was Out, and that stays true."""
    with session(tmp_path / "t.db") as conn:
        record_player_status(conn, {"7": {"injury_status": "Out"}}, "2026-10-21T10:00:00+00:00")
        record_player_status(
            conn, {"7": {"injury_status": "Questionable"}}, "2026-10-21T14:00:00+00:00"
        )

        assert designations_as_of(conn, "2026-10-21T12:00:00+00:00") == {"7": "Out"}
        assert designations_as_of(conn, "2026-10-21T15:00:00+00:00") == {"7": "Questionable"}


def test_no_capture_is_not_the_same_as_nobody_injured(tmp_path):
    with session(tmp_path / "t.db") as conn:
        assert designations_as_of(conn, "2026-10-21T12:00:00+00:00") is None
        record_player_status(conn, {"7": {"injury_status": None}}, "2026-10-21T10:00:00+00:00")
        assert designations_as_of(conn, "2026-10-21T12:00:00+00:00") == {}


def test_a_healthy_day_is_still_a_captured_day(tmp_path):
    """The old coverage counter could not tell a day with no injuries from a missed one."""
    with session(tmp_path / "t.db") as conn:
        record_player_status(conn, {"7": {"injury_status": None}}, "2026-10-21T10:30:00+00:00")
        record_player_status(conn, {"7": {"injury_status": None}}, "2026-10-22T10:30:00+00:00")
        assert status_coverage(conn) == (2, 0)


def test_a_player_missing_from_the_payload_is_not_cleared(tmp_path):
    """Absent from one response is not evidence of recovery."""
    with session(tmp_path / "t.db") as conn:
        record_player_status(conn, {"7": {"injury_status": "Out"}}, "2026-10-21T10:30:00+00:00")
        record_player_status(conn, {"8": {"injury_status": None}}, "2026-10-22T10:30:00+00:00")
        assert designations_as_of(conn, "2026-10-23T00:00:00+00:00") == {"7": "Out"}
