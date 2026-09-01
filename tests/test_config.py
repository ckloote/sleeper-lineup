"""`.env`, and why the environment outranks it.

The 2026-08-31 deployment failed step 4 with `0/25 weeks ingested` on a Pi that
had a complete 27 MB season sitting next to the empty database it was reading.
`LOCKIN_DB=data/lockin-2025.db` had been written to `.env` exactly as
deployment.md §3 instructs, and nothing in the project opened that file. The
default path won, `store.db.connect` created the file it could not find, and a
configuration error surfaced as a data error.

Cron and systemd are the reason this belongs in code rather than in the
runbook: neither sources a shell profile, so `export` in the operator's
`.bashrc` would have fixed the terminal and left both scheduled paths broken.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lockin.config import Config, load_env_file


@pytest.fixture(autouse=True)
def _restore_environ():
    """`load_env_file` mutates `os.environ` directly; monkeypatch cannot undo that."""
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_deployment_bug(tmp_path, monkeypatch):
    """The regression, stated as it happened: §3's line must reach `Config`."""
    _write(tmp_path, "LOCKIN_TZ=America/New_York\nLOCKIN_DB=data/lockin-2025.db\n")
    monkeypatch.delenv("LOCKIN_DB", raising=False)
    monkeypatch.chdir(tmp_path)

    load_env_file()

    assert Config.from_env().db_path == Path("data/lockin-2025.db")


def test_it_is_found_in_the_working_directory(tmp_path, monkeypatch):
    """Same rule as `LOCKIN_DB=data/...` itself. Cron and systemd cd in first."""
    _write(tmp_path, "LOCKIN_DB=data/lockin-2025.db\n")
    monkeypatch.delenv("LOCKIN_DB", raising=False)
    monkeypatch.chdir(tmp_path)

    assert load_env_file() == Path(".env")
    assert os.environ["LOCKIN_DB"] == "data/lockin-2025.db"


def test_an_existing_variable_wins(tmp_path, monkeypatch):
    """day-one.md §2 rolls the season over with `export LOCKIN_DB=...`.

    If `.env` outranked that, the new season's ingest would write into last
    season's database — and `weekly_matchups` has no season column to keep the
    two apart, so the damage would be silent.
    """
    _write(tmp_path, "LOCKIN_DB=data/lockin-2025.db\n")
    monkeypatch.setenv("LOCKIN_DB", "data/lockin-2026.db")
    monkeypatch.chdir(tmp_path)

    load_env_file()

    assert os.environ["LOCKIN_DB"] == "data/lockin-2026.db"
    assert Config.from_env().db_path == Path("data/lockin-2026.db")


def test_an_inline_assignment_wins_too(tmp_path, monkeypatch):
    """deployment.md §6 and §7 both pass the ntfy topic in ahead of the command."""
    _write(tmp_path, "LOCKIN_NTFY_TOPIC=from-the-file\n")
    monkeypatch.setenv("LOCKIN_NTFY_TOPIC", "from-the-crontab")
    monkeypatch.chdir(tmp_path)

    load_env_file()

    assert os.environ["LOCKIN_NTFY_TOPIC"] == "from-the-crontab"


def test_no_env_file_is_not_an_error(tmp_path, monkeypatch):
    """A fresh clone has none, and every setting has a default."""
    monkeypatch.chdir(tmp_path)
    assert load_env_file() is None


def test_the_format_a_hand_written_file_actually_takes(tmp_path, monkeypatch):
    """Comments, blanks, quotes, and an `export` prefix pasted from a shell."""
    path = _write(
        tmp_path,
        "\n".join(
            [
                "# the host's database",
                "",
                "  LOCKIN_DB = data/lockin-2025.db  ",
                'LOCKIN_TZ="America/New_York"',
                "export LOCKIN_SEASON='2025'",
                "LOCKIN_LEAGUE_ID=123=456",
            ]
        )
        + "\n",
    )
    for key in ("LOCKIN_DB", "LOCKIN_TZ", "LOCKIN_SEASON", "LOCKIN_LEAGUE_ID"):
        monkeypatch.delenv(key, raising=False)

    load_env_file(path)

    assert os.environ["LOCKIN_DB"] == "data/lockin-2025.db"
    assert os.environ["LOCKIN_TZ"] == "America/New_York"
    assert os.environ["LOCKIN_SEASON"] == "2025"
    assert os.environ["LOCKIN_LEAGUE_ID"] == "123=456", "only the first = separates"


def test_nothing_is_expanded(tmp_path, monkeypatch):
    """`$(cat ~/.lockin-topic)` belongs in the crontab, where a shell runs it."""
    path = _write(tmp_path, "LOCKIN_NTFY_TOPIC=$(cat ~/.lockin-topic)\n")
    monkeypatch.delenv("LOCKIN_NTFY_TOPIC", raising=False)

    load_env_file(path)

    assert os.environ["LOCKIN_NTFY_TOPIC"] == "$(cat ~/.lockin-topic)"


def test_a_malformed_line_names_itself(tmp_path):
    """Loud beats lenient. This file exists because a silent setting cost a day."""
    path = _write(tmp_path, "LOCKIN_DB=data/lockin-2025.db\nLOCKIN_TZ America/New_York\n")

    with pytest.raises(ValueError, match=r"\.env:2"):
        load_env_file(path)
