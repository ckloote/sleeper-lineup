"""A database that is not there must not be invented.

The other half of the 2026-08-31 deployment failure. `LOCKIN_DB` never reached
the process, so the default path was used; `connect` created it, `apply_schema`
furnished it, and five gates then reported `0/25 weeks ingested` against a
valid, empty, 176 KB season sitting beside the real 27 MB one.

Every symptom pointed at the ingest. Nothing pointed at the setting. The fix is
that only `lockin ingest` — the command by which a season legitimately comes
into being — is allowed to create.

It cost the test suite too: suites guarded by `skipif(not cfg.db_path.exists())`
found the empty file, declined to skip, and errored.
"""

from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

from lockin import cli
from lockin.store.db import DatabaseMissing, connect, session


def test_create_false_refuses_rather_than_inventing(tmp_path):
    missing = tmp_path / "lockin-2025.db"

    with pytest.raises(DatabaseMissing):
        connect(missing, create=False)

    assert not missing.exists(), "refusing must not leave the file behind"


def test_the_error_names_the_path_it_was_given(tmp_path):
    missing = tmp_path / "typo.bd"
    with pytest.raises(DatabaseMissing) as exc:
        session(missing, create=False).__enter__()
    assert exc.value.db_path == missing
    assert "typo.bd" in str(exc.value)


def test_ingest_still_creates(tmp_path):
    """A fresh clone has no database, and `lockin ingest` is how a season starts."""
    fresh = tmp_path / "nested" / "lockin-2026.db"

    with session(fresh, create=True) as conn:
        conn.execute("SELECT 1")

    assert fresh.exists(), "the parent directory is created too"


def test_an_existing_database_still_opens_for_writing(tmp_path):
    """`create=False` is not read-only — `digest` and `managers` write through it."""
    db = tmp_path / "season.db"
    with session(db, create=True) as conn:
        conn.execute("SELECT 1")

    with session(db, create=False) as conn:
        conn.execute(
            "INSERT INTO digest_runs (generated_at, roster_id, as_of, week)"
            " VALUES ('2026-01-08T09:00:00Z', 1, '2026-01-08', 12)"
        )
    with sqlite3.connect(db) as check:
        assert check.execute("SELECT COUNT(*) FROM digest_runs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "command",
    ["reconcile", "verify", "locks", "calibrate", "backtest", "digest", "advice", "dashboard"],
)
def test_no_gate_reports_an_empty_season_instead_of_a_bad_path(command, tmp_path, monkeypatch):
    """The regression itself: the message must be about the path, not the ingest."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCKIN_DB", "data/lockin-2025.db")

    result = CliRunner().invoke(cli.main, [command])

    assert result.exit_code != 0
    assert "no database at data/lockin-2025.db" in result.output
    assert "gate(s) failed" not in result.output, "it must not have run the gates at all"
    assert not (tmp_path / "data").exists(), "a failed command must create nothing"


def test_the_message_points_at_the_three_ways_to_be_wrong(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCKIN_DB", "data/lockin-2025.db")

    result = CliRunner().invoke(cli.main, ["verify"])

    assert "LOCKIN_DB" in result.output
    assert "deployment.md step 3" in result.output
    assert "day-one.md step 2" in result.output


def test_serve_rejects_a_bad_dashboard_db_at_startup(tmp_path, monkeypatch):
    """deployment.md step 8 runs this under systemd; a typo must fail the unit.

    Otherwise it surfaces as a 500 the next time a phone opens /dashboard.
    """
    season = tmp_path / "season.db"
    with session(season, create=True) as conn:
        conn.execute("SELECT 1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCKIN_DB", str(season))

    result = CliRunner().invoke(
        cli.main, ["serve", "--dashboard-db", str(tmp_path / "lockin-2025.bd")]
    )

    assert result.exit_code != 0
    assert "no database at" in result.output
    assert "lockin-2025.bd" in result.output
