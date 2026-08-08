"""Phase 1 gate: prove the scoring engine against the recorded season.

Three checks, in increasing order of what they would cost to get wrong:

1. The two scoring paths agree on every played game. This validates the derived
   dd/td and missed-shot logic that the simulator will depend on entirely.
2. Every nonzero `players_points` in every week is reproduced to the cent from
   box scores. This is the architecture doc's stated gate, widened from week 12
   to all 25 weeks.
3. The per-attempt economics in architecture doc §2 follow from the league's
   actual scoring settings.

Read-only. Returns Checks so the CLI can render them and tests can assert.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from lockin.core.scoring import (
    break_even_rate,
    derive_bonuses,
    line_from_stats,
    score_line,
    score_recorded,
    shot_values,
)

CENT = 0.005


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    offenders: list[str] = field(default_factory=list)


def scoring_settings(conn: sqlite3.Connection) -> dict[str, float]:
    row = conn.execute("SELECT payload_json FROM league_settings LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("no league settings ingested; run `lockin ingest`")
    return json.loads(row["payload_json"])["scoring_settings"]


def _played_rows(conn: sqlite3.Connection, season: str):
    """Every played PLAYER game that counts.

    Two exclusions, both of which otherwise masquerade as scoring bugs:

    - Exhibitions. The All-Star Game carries real stat lines but does not score,
      so including it produces week-17 mismatches.
    - Team-aggregate rows. Sleeper mixes team totals in with players, and a team
      line reads as a triple-double every night.
    """
    return conn.execute(
        """
        SELECT b.sleeper_id, b.fantasy_week, b.game_date, b.raw_stats, b.dd, b.td
          FROM box_scores b
          JOIN game_links g ON g.sleeper_game_id = b.sleeper_game_id
         WHERE b.season = ? AND b.played = 1
           AND COALESCE(g.is_exhibition, 0) = 0
           AND COALESCE(b.is_team_row, 0) = 0
        """,
        (season,),
    )


def check_paths_agree(conn: sqlite3.Connection, season: str) -> Check:
    """score_line (derived) must equal score_recorded (as given) on real games.

    The simulator only ever has components, so if the derivation is wrong every
    simulated score is wrong — and wrong specifically at the 10/10 boundary,
    where the double-double cliff makes the distribution's shape matter most.
    """
    scoring = scoring_settings(conn)
    n = 0
    offenders: list[str] = []
    for row in _played_rows(conn, season):
        stats = json.loads(row["raw_stats"])
        recorded = score_recorded(stats, scoring)
        derived = score_line(line_from_stats(stats), scoring)
        n += 1
        if abs(recorded - derived) > CENT and len(offenders) < 20:
            offenders.append(
                f"week {row['fantasy_week']} {row['game_date']} player {row['sleeper_id']}:"
                f" recorded={recorded} derived={derived}"
            )
    return Check(
        name="derived and recorded scoring agree on every played game",
        passed=not offenders,
        detail=f"{n - len(offenders)}/{n} games agree",
        offenders=offenders,
    )


def check_bonus_derivation(conn: sqlite3.Connection, season: str) -> Check:
    """Component-derived dd/td must reproduce Sleeper's flags exactly."""
    n = dd_n = td_n = 0
    offenders: list[str] = []
    for row in _played_rows(conn, season):
        stats = json.loads(row["raw_stats"])
        dd, td = derive_bonuses(line_from_stats(stats))
        want_dd, want_td = int(row["dd"] or 0), int(row["td"] or 0)
        n += 1
        dd_n += want_dd
        td_n += want_td
        if (dd, td) != (want_dd, want_td) and len(offenders) < 20:
            offenders.append(
                f"week {row['fantasy_week']} {row['game_date']} player {row['sleeper_id']}:"
                f" derived=({dd},{td}) sleeper=({want_dd},{want_td})"
            )
    return Check(
        name="component-derived dd/td matches Sleeper on every played game",
        passed=not offenders,
        detail=f"{n - len(offenders)}/{n} games match"
        f" ({dd_n} double-doubles, {td_n} triple-doubles)",
        offenders=offenders,
    )


def check_weeks_reconcile(conn: sqlite3.Connection, season: str) -> Check:
    """Every nonzero counted score must equal some played game's score, to the cent.

    This is the architecture doc's gate: reconstruct `players_points` from box
    scores. A counted value that matches no game means either the scoring
    function is wrong or the game set for that player-week is wrong.
    """
    scoring = scoring_settings(conn)

    # Pre-compute every played game's score, keyed by (player, week).
    by_player_week: dict[tuple[str, int], list[float]] = {}
    for row in _played_rows(conn, season):
        key = (row["sleeper_id"], row["fantasy_week"])
        by_player_week.setdefault(key, []).append(
            score_recorded(json.loads(row["raw_stats"]), scoring)
        )

    counted = conn.execute(
        """
        SELECT week, roster_id, sleeper_id, counted_points
          FROM weekly_matchups_latest
         WHERE counted_points IS NOT NULL AND counted_points != 0
         GROUP BY week, roster_id, sleeper_id
        """
    ).fetchall()

    offenders: list[str] = []
    for row in counted:
        scores = by_player_week.get((row["sleeper_id"], row["week"]), [])
        if not any(abs(s - row["counted_points"]) <= CENT for s in scores):
            if len(offenders) < 20:
                offenders.append(
                    f"week {row['week']} roster {row['roster_id']} player {row['sleeper_id']}:"
                    f" counted={row['counted_points']} not among {sorted(scores)}"
                )
    return Check(
        name="every nonzero counted score reproduced from box scores",
        passed=not offenders,
        detail=f"{len(counted) - len(offenders)}/{len(counted)} counted scores reproduced",
        offenders=offenders,
    )


def check_shot_economics(conn: sqlite3.Connection, season: str) -> Check:
    """The per-attempt economics in architecture doc §2, from live settings."""
    scoring = scoring_settings(conn)
    values = shot_values(scoring)
    expected = {
        "three": (5.5, -1.0, 0.1538),
        "two": (2.5, -1.0, 0.2857),
        "free_throw": (2.0, -1.0, 0.3333),
    }

    offenders = []
    lines = []
    for shot, (want_made, want_missed, want_be) in expected.items():
        made, missed = values[shot]
        be = break_even_rate(made, missed)
        lines.append(f"{shot}: made={made} missed={missed} break-even={be:.1%}")
        if abs(made - want_made) > CENT or abs(missed - want_missed) > CENT:
            offenders.append(
                f"{shot}: got ({made}, {missed}), expected ({want_made}, {want_missed})"
            )
        elif abs(be - want_be) > 0.001:
            offenders.append(f"{shot}: break-even {be:.4f}, expected {want_be}")
    return Check(
        name="per-attempt economics match the architecture doc",
        passed=not offenders,
        detail="; ".join(lines),
        offenders=offenders,
    )


def run(conn: sqlite3.Connection, season: str) -> list[Check]:
    return [
        check_shot_economics(conn, season),
        check_bonus_derivation(conn, season),
        check_paths_agree(conn, season),
        check_weeks_reconcile(conn, season),
    ]
