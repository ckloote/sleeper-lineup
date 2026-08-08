"""Assemble the projection panel from SQLite.

The database side of the projection layer: read box scores, turn them into the
plain arrays ``lockin.core.projections`` works in, and hand them over. No
modelling happens here.

Two rules from the architecture doc are enforced at this boundary, because this
is the only place they *can* be enforced:

**Point-in-time attributes.** Positions and team come from ``box_scores``'s
per-game ``pit_positions`` snapshot, never from the live ``players`` table.
``/players/nba`` has no history, so reading it would import next season's roster
moves into a replay of last season (implementation-plan.md §3).

**Fixture semantics.** Exhibitions, postponed fixtures and team-aggregate rows
are excluded here, once, rather than in every caller. The All-Star Game carries
real stat lines but does not count, a postponed fixture never happened, and a
``TEAM_OKC`` row reads as a triple-double every night.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import numpy as np

from lockin.core.projections import (
    POS_CENTRE,
    POS_FORWARD,
    POS_GUARD,
    PlayerHistory,
    ProjectionParams,
    SeasonPanel,
)
from lockin.core.scoring import COMPONENT_ORDER

# Sleeper's stat rows and StatLine happen to agree on every component name, so
# the column list is derived rather than restated. Assert it, because a rename
# on either side would otherwise silently shift a column.
_COLUMNS = ", ".join(f"b.{name}" for name in COMPONENT_ORDER)


def day_index(iso_date: str) -> int:
    """ISO date to a proleptic Gregorian ordinal.

    ``core`` may not import ``datetime`` (it would be a clock read away from
    impurity), so dates cross the boundary as integers and all point-in-time
    comparisons downstream are integer comparisons.
    """
    y, m, d = (int(part) for part in iso_date.split("-"))
    return date(y, m, d).toordinal()


def date_of(day: int) -> str:
    return date.fromordinal(day).isoformat()


def position_group(positions: list[str]) -> int:
    """Collapse Sleeper's eligibility list to a role code.

    Centre first: a centre-eligible player's per-minute production looks like a
    centre's whatever else he is listed at, and ``C`` is the structural
    bottleneck in this roster anyway (architecture doc §3.2).

    Every rostered box-score row in 2025-26 carries a position snapshot,
    including DNP rows, so the empty case is a data problem rather than a
    routine one. It falls back to forward — the middle of the three — and
    ``tests/test_season_invariants.py`` pins the coverage so a regression in
    ingest surfaces as a failing test rather than as a quietly reclassified
    player.
    """
    marks = set(positions)
    if "C" in marks:
        return POS_CENTRE
    if marks & {"PG", "SG"}:
        return POS_GUARD
    return POS_FORWARD


def rostered_player_ids(conn: sqlite3.Connection) -> list[str]:
    """Everyone who held a roster spot at any point in the season.

    The projection layer only ever needs to speak about players who can be
    started, and restricting the panel keeps the league donor pool free of
    deep-bench players nobody would ever field.
    """
    return [
        row["sleeper_id"]
        for row in conn.execute(
            "SELECT DISTINCT sleeper_id FROM weekly_matchups_latest ORDER BY sleeper_id"
        )
    ]


def load_panel(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
) -> SeasonPanel:
    """Build the point-in-time panel for every rostered player."""
    rows = conn.execute(
        f"""
        SELECT b.sleeper_id, b.game_date, b.fantasy_week, b.played,
               b.seconds_played, b.pit_positions, {_COLUMNS}
          FROM box_scores b
          JOIN game_links g ON g.sleeper_game_id = b.sleeper_game_id
         WHERE b.season = ?
           AND COALESCE(g.is_exhibition, 0) = 0
           AND COALESCE(g.occurred, 1) = 1
           AND COALESCE(b.is_team_row, 0) = 0
           AND b.sleeper_id IN (SELECT DISTINCT sleeper_id FROM weekly_matchups_latest)
         ORDER BY b.sleeper_id, b.game_date
        """,
        (season,),
    ).fetchall()
    if not rows:
        raise RuntimeError("no box scores for rostered players; run `lockin ingest`")

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["sleeper_id"], []).append(row)

    histories: dict[str, PlayerHistory] = {}
    for sleeper_id, player_rows in grouped.items():
        played = np.array([bool(r["played"]) for r in player_rows])
        groups = [position_group(json.loads(r["pit_positions"] or "[]")) for r in player_rows]
        histories[sleeper_id] = PlayerHistory(
            sleeper_id=sleeper_id,
            day=np.array([day_index(r["game_date"]) for r in player_rows]),
            week=np.array([r["fantasy_week"] for r in player_rows]),
            played=played,
            minutes=np.array([(r["seconds_played"] or 0.0) / 60.0 for r in player_rows]),
            components=np.array(
                [[float(r[name] or 0) for name in COMPONENT_ORDER] for r in player_rows],
                dtype=np.float64,
            ),
            pos_group=np.array(groups, dtype=np.int64),
        )
    return SeasonPanel(histories=histories, params=params or ProjectionParams())
