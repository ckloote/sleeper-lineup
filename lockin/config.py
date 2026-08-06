"""Runtime configuration.

Nothing here is read by ``lockin.core`` — core takes plain values and
dataclasses. This module exists so the ingest and CLI layers have one place to
resolve the league, the season and the database path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The 2025-26 league. Sleeper labels that season "2025".
#
# This is a DEFAULT, not a constant. The league is `status: complete`, and the
# 2026-27 league will carry a different id once the commissioner rolls it over
# (see implementation-plan.md §7.3). Resolve by season rather than assuming this
# value stays current.
DEFAULT_LEAGUE_ID = "1283214955830575104"
DEFAULT_SEASON = "2025"
DEFAULT_USER_ID = "1283460931447164928"

# Weeks 1-21 are regular season, 22-24 playoffs, 25 exists in the stats feed but
# is unscored by the league (`last_scored_leg: 24`).
REGULAR_SEASON_WEEKS = range(1, 22)
PLAYOFF_WEEKS = range(22, 25)
ALL_STAT_WEEKS = range(1, 26)


@dataclass(frozen=True)
class Config:
    league_id: str
    season: str
    user_id: str
    db_path: Path

    @classmethod
    def from_env(cls) -> Config:
        db = os.environ.get("LOCKIN_DB", "data/lockin.db")
        return cls(
            league_id=os.environ.get("LOCKIN_LEAGUE_ID", DEFAULT_LEAGUE_ID),
            season=os.environ.get("LOCKIN_SEASON", DEFAULT_SEASON),
            user_id=os.environ.get("LOCKIN_USER_ID", DEFAULT_USER_ID),
            db_path=Path(db).expanduser(),
        )
