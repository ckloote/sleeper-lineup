"""MATCHUP parsing.

The regression test here is `test_both_rows_of_a_game_agree`: three 2025-26
games carry the same away-perspective string on both of their rows, which broke
an earlier implementation that read the away team from TEAM_ABBREVIATION.
"""

from __future__ import annotations

import pytest

from lockin.ingest.nba import _season_label, parse_matchup
from lockin.ingest.validate import SchemaDriftError


def test_home_perspective():
    assert parse_matchup("DET vs. DAL") == ("DET", "DAL")


def test_away_perspective():
    assert parse_matchup("DAL @ DET") == ("DET", "DAL")


def test_both_rows_of_a_game_agree():
    """Game 0022500147 carried 'DAL @ DET' on BOTH rows.

    Parsing must depend only on the string, so both rows resolve identically
    rather than the second overwriting the first with a self-matchup.
    """
    row_dal = parse_matchup("DAL @ DET")
    row_det = parse_matchup("DAL @ DET")
    assert row_dal == row_det == ("DET", "DAL")


def test_rejects_self_matchup():
    with pytest.raises(SchemaDriftError):
        parse_matchup("DET @ DET")


def test_rejects_unparseable():
    with pytest.raises(SchemaDriftError, match="unparseable"):
        parse_matchup("DET / DAL")


def test_season_label_maps_sleeper_season_to_nba_season():
    assert _season_label("2025") == "2025-26"
    assert _season_label("2026") == "2026-27"
    assert _season_label("1999") == "1999-00"
