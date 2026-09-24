"""A database refuses a season that is not its own (review 2026-09-23, finding 5).

One file per season was only ever an instruction in day-one.md. The failure it
guarded against is silent: `weekly_matchups` has no season column, so ingesting
2026-27 into last season's file mixes the two and nothing errors. These tests
pin the refusal — and, as much as the refusal, that it happens **before the
first write**, because a guard that fires after the damage is a log message.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from click.testing import CliRunner
from live_fixture import OPENING, SyntheticSeason, config_for, connect, ingest

from lockin import cli
from lockin.ingest.run import IngestRefused
from lockin.store import identity
from lockin.store.db import apply_schema


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {n: conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}


@pytest.fixture
def last_season(tmp_path):
    """A database ingested for 2025, under the old league id."""
    old = SyntheticSeason(season="2025", league_id="1283214955830575104")
    old.play_through(OPENING + timedelta(days=6))
    cfg = config_for(tmp_path, old, db_path=tmp_path / "lockin.db")
    ingest(old, cfg, weeks=[1])
    return cfg


def test_the_first_ingest_claims_the_database(tmp_path):
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=3))
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[1])

    with connect(cfg) as conn:
        assert identity.read(conn) == identity.Identity(season.league_id, "2026")


def test_last_seasons_file_refuses_the_new_season_before_writing(last_season, tmp_path):
    """The rollover mistake: new league and season configured, old LOCKIN_DB kept."""
    new = SyntheticSeason()
    new.play_through(OPENING + timedelta(days=3))
    cfg = config_for(tmp_path, new, db_path=last_season.db_path)

    with connect(cfg) as conn:
        before = table_counts(conn)
    with pytest.raises(IngestRefused, match="belongs to league 1283214955830575104 season 2025"):
        ingest(new, cfg, weeks=[1])
    with connect(cfg) as conn:
        assert table_counts(conn) == before
    assert not (cfg.snapshot_root / "matchups" / "2026").exists(), "no snapshot may be saved"


def test_a_league_from_another_season_is_refused(tmp_path):
    """New league id, last season's LOCKIN_SEASON still set."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=3))
    cfg = config_for(tmp_path, season, season="2025")

    with pytest.raises(IngestRefused, match="Sleeper's 2026 league, but LOCKIN_SEASON is 2025"):
        ingest(season, cfg, weeks=[1])
    with connect(cfg) as conn:
        counts = table_counts(conn)
    assert counts["db_identity"] == 0
    assert counts["league_settings"] == 0
    assert counts["box_scores"] == 0


def test_a_legacy_database_adopts_its_identity(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    conn.execute(
        "INSERT INTO league_settings VALUES ('L', '2025', '{}', '2026-08-01T00:00:00+00:00')"
    )

    assert identity.check(conn, "L", "2025") == identity.Identity("L", "2025")
    assert identity.read(conn) == identity.Identity("L", "2025")
    with pytest.raises(identity.IdentityMismatch):
        identity.check(conn, "L", "2026")


def test_a_legacy_database_holding_two_seasons_is_refused(tmp_path):
    """It may already be mixed, so it must not pick one and carry on."""
    conn = sqlite3.connect(tmp_path / "mixed.db")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    conn.executemany(
        "INSERT INTO league_settings VALUES (?, ?, '{}', '2026-08-01T00:00:00+00:00')",
        [("L", "2025"), ("M", "2026")],
    )

    with pytest.raises(identity.IdentityMismatch, match="several seasons"):
        identity.check(conn, "L", "2025")
    assert identity.read(conn) is None


def test_settings_are_read_for_this_databases_season_not_the_first_row(tmp_path):
    """`LIMIT 1` would have returned whichever season SQLite found first."""
    conn = sqlite3.connect(tmp_path / "db.db")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    conn.executemany(
        "INSERT INTO league_settings VALUES (?, ?, ?, '2026-08-01T00:00:00+00:00')",
        [
            ("A", "2025", '{"scoring_settings": {"pts": 1.0}}'),
            ("B", "2026", '{"scoring_settings": {"pts": 2.0}}'),
        ],
    )
    conn.execute("INSERT INTO db_identity VALUES (1, 'B', '2026', '2026-10-01T00:00:00+00:00')")

    assert identity.league_payload(conn)["scoring_settings"] == {"pts": 2.0}


def test_every_command_refuses_a_configuration_for_another_season(
    last_season, tmp_path, monkeypatch
):
    """`.env` still naming last season's file, and the season moved on."""
    monkeypatch.chdir(tmp_path)  # away from the repository's own .env
    monkeypatch.setenv("LOCKIN_DB", str(last_season.db_path))
    monkeypatch.setenv("LOCKIN_LEAGUE_ID", "900000000000000026")
    monkeypatch.setenv("LOCKIN_SEASON", "2026")

    result = CliRunner().invoke(cli.main, ["reconcile"])

    assert result.exit_code != 0
    assert "belongs to league 1283214955830575104 season 2025" in result.output
    assert "export LOCKIN_DB=data/lockin-2026.db" in result.output
