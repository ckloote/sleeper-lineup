"""Phase 0 reconciliation report.

The architecture doc asks for a report that "flags unmapped rostered players"
because a silent ID or coverage gap produces confident wrong recommendations.
This is that report, extended to the rest of the Phase 0 exit criteria.

Read-only. Every check returns a Check so the CLI can render it and tests can
assert on it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

EXPECTED_WEEKS = set(range(1, 26))
GAME_LINK_THRESHOLD = 0.99


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    offenders: list[str] = field(default_factory=list)


def check_weeks_present(conn: sqlite3.Connection, season: str) -> Check:
    weeks = {
        r["fantasy_week"]
        for r in conn.execute(
            "SELECT DISTINCT fantasy_week FROM box_scores WHERE season = ?", (season,)
        )
    }
    missing = sorted(EXPECTED_WEEKS - weeks)
    return Check(
        name="all 25 fantasy weeks ingested",
        passed=not missing,
        detail=f"{len(weeks)}/25 weeks present" + (f", missing {missing}" if missing else ""),
    )


def check_matchups_present(conn: sqlite3.Connection) -> Check:
    weeks = {r["week"] for r in conn.execute("SELECT DISTINCT week FROM weekly_matchups")}
    missing = sorted(EXPECTED_WEEKS - weeks)
    return Check(
        name="all 25 weeks of matchups ingested",
        passed=not missing,
        detail=f"{len(weeks)}/25 weeks present" + (f", missing {missing}" if missing else ""),
    )


def check_starter_coverage(conn: sqlite3.Connection, season: str) -> Check:
    """Every started player-week must have box-score rows, or be a genuine no-games week.

    A starter with no scheduled game scores 0.0 and cannot be locked, so the
    distinction between "we failed to ingest him" and "his team was idle" is
    exactly the distinction that matters.
    """
    rows = conn.execute(
        """
        SELECT m.week, m.roster_id, m.sleeper_id,
               COALESCE(p.full_name, '?') AS name,
               (SELECT COUNT(*) FROM box_scores b
                 WHERE b.sleeper_id = m.sleeper_id
                   AND b.fantasy_week = m.week
                   AND b.season = ?) AS n_games
          FROM weekly_matchups m
          LEFT JOIN players p ON p.sleeper_id = m.sleeper_id
         WHERE m.is_starter = 1
         GROUP BY m.week, m.roster_id, m.sleeper_id
        """,
        (season,),
    ).fetchall()

    total = len(rows)
    gaps = [r for r in rows if r["n_games"] == 0]
    offenders = [
        f"week {r['week']} roster {r['roster_id']} {r['name']} ({r['sleeper_id']})"
        for r in gaps[:20]
    ]
    return Check(
        name="every started player-week has box-score rows",
        passed=not gaps,
        detail=f"{total - len(gaps)}/{total} starter player-weeks covered"
        + (f", {len(gaps)} with no scheduled games" if gaps else ""),
        offenders=offenders,
    )


def check_game_links(conn: sqlite3.Connection) -> Check:
    """Link rate over fixtures that actually happened.

    Postponed fixtures are excluded rather than counted as failures:
    LeagueGameFinder returns played games, not the schedule, so a postponed
    fixture has no NBA counterpart by construction.
    """
    real = "occurred = 1 AND COALESCE(is_exhibition, 0) = 0"
    total = conn.execute(f"SELECT COUNT(*) c FROM game_links WHERE {real}").fetchone()["c"]
    linked = conn.execute(
        f"SELECT COUNT(*) c FROM game_links WHERE {real} AND nba_game_id IS NOT NULL"
    ).fetchone()["c"]
    rate = linked / total if total else 0.0
    offenders = [
        f"{r['game_date']} {r['team_a']}/{r['team_b']} ({r['sleeper_game_id']})"
        for r in conn.execute(
            f"SELECT * FROM game_links WHERE {real} AND nba_game_id IS NULL"
            " ORDER BY game_date LIMIT 20"
        )
    ]
    return Check(
        name=f"played fixtures link to NBA schedule (>={GAME_LINK_THRESHOLD:.0%})",
        passed=rate >= GAME_LINK_THRESHOLD,
        detail=f"{linked}/{total} linked ({rate:.2%})",
        offenders=offenders,
    )


def check_postponements(conn: sqlite3.Connection) -> Check:
    """Two independent signals for "this fixture happened" must agree.

    Sleeper says a game happened if any player recorded a stat line. The NBA
    says so by having a game row at all. A fixture where those disagree means
    either the ingest is incomplete or the team-pair join is wrong, and the
    lock engine would then mis-score an unplayed final game.
    """
    rows = conn.execute(
        "SELECT * FROM game_links WHERE occurred = 0 AND nba_game_id IS NOT NULL"
    ).fetchall()
    postponed = conn.execute("SELECT COUNT(*) c FROM game_links WHERE occurred = 0").fetchone()["c"]
    return Check(
        name="postponed fixtures agree between Sleeper and NBA",
        passed=not rows,
        detail=f"{postponed} postponed fixture(s), {len(rows)} disagreeing",
        offenders=[
            f"{r['game_date']} {r['team_a']}/{r['team_b']} has NBA game {r['nba_game_id']}"
            for r in rows[:20]
        ],
    )


def check_player_coverage(conn: sqlite3.Connection) -> Check:
    """Any player who appears in a lineup but not in the player table."""
    rows = conn.execute(
        """
        SELECT DISTINCT m.sleeper_id
          FROM weekly_matchups m
          LEFT JOIN players p ON p.sleeper_id = m.sleeper_id
         WHERE p.sleeper_id IS NULL
        """
    ).fetchall()
    return Check(
        name="every rostered player resolves in the player table",
        passed=not rows,
        detail=f"{len(rows)} unresolved" if rows else "all resolved",
        offenders=[r["sleeper_id"] for r in rows[:20]],
    )


def check_exhibitions(conn: sqlite3.Connection) -> Check:
    """Surface non-NBA fixtures so they are never silently scored.

    Advisory: finding the All-Star Game here is the expected, correct outcome.
    The check exists so that a NEW kind of exhibition (or a tricode change that
    makes a real team look fake) shows up as something a human looks at rather
    than as silently dropped games.
    """
    rows = conn.execute(
        "SELECT * FROM game_links WHERE is_exhibition = 1 ORDER BY game_date"
    ).fetchall()
    return Check(
        name="non-NBA fixtures identified and excluded from scoring",
        passed=True,
        detail=f"{len(rows)} exhibition fixture(s)",
        offenders=[f"{r['game_date']} {r['team_a']}/{r['team_b']}" for r in rows[:20]],
    )


def check_team_rows(conn: sqlite3.Connection) -> Check:
    """Rows classified as team aggregates must actually look like teams.

    `is_team_row` is set by "the id does not resolve in `players`", which is
    self-maintaining but would also silently reclassify a real player who went
    missing from the player table. Asserting that every such row carries the
    TEAM_ prefix turns that failure mode into a visible one.
    """
    strays = conn.execute(
        "SELECT DISTINCT sleeper_id FROM box_scores"
        " WHERE is_team_row = 1 AND sleeper_id NOT LIKE 'TEAM_%'"
    ).fetchall()
    n_team = conn.execute("SELECT COUNT(*) c FROM box_scores WHERE is_team_row = 1").fetchone()["c"]
    return Check(
        name="team-aggregate rows are identified and excluded from scoring",
        passed=not strays,
        detail=f"{n_team} team-aggregate rows, {len(strays)} unrecognised",
        offenders=[r["sleeper_id"] for r in strays[:20]],
    )


def check_tipoffs(conn: sqlite3.Connection, season: str) -> Check:
    """Advisory only — no Phase 0-2 gate depends on tipoff times."""
    total = conn.execute(
        "SELECT COUNT(*) c FROM nba_schedule WHERE season = ?", (season,)
    ).fetchone()["c"]
    filled = conn.execute(
        "SELECT COUNT(*) c FROM nba_schedule WHERE season = ? AND tipoff_utc IS NOT NULL", (season,)
    ).fetchone()["c"]
    return Check(
        name="tipoff times present (advisory, needed from Phase 6)",
        passed=True,
        detail=f"{filled}/{total} games have tipoff_utc",
    )


def run(conn: sqlite3.Connection, season: str) -> list[Check]:
    return [
        check_weeks_present(conn, season),
        check_matchups_present(conn),
        check_player_coverage(conn),
        check_starter_coverage(conn, season),
        check_game_links(conn),
        check_postponements(conn),
        check_exhibitions(conn),
        check_team_rows(conn),
        check_tipoffs(conn, season),
    ]
