"""Phase 2: run lock inference over the recorded season.

The I/O half of `lockin.core.locks` — reads box scores and counted scores,
writes `lock_inferences` and `manager_profiles`, and reports the Phase 2 gates.

Runs across ALL TEN rosters, not just ours. Every manager's decisions are
recoverable, which is what makes the deferred Phase 5 evaluation statistically
meaningful (105 matchups rather than 21) and what supplies the per-manager
tendency the live opponent model needs.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from lockin.core.eligibility import OBSERVED_EXTRA_SLOTS, eligible
from lockin.core.locks import (
    Game,
    LockInference,
    LockStatus,
    ManagerProfile,
    infer_lock,
    is_resolved,
    profile_manager,
)
from lockin.core.scoring import score_recorded
from lockin.store.db import now_iso
from lockin.verify import Check, scoring_settings

RESOLVED_THRESHOLD = 0.95


def _game_sequence(conn: sqlite3.Connection, season: str) -> dict[tuple[str, int], list[dict]]:
    """Every player's week, as an ordered list of games that count.

    Excludes postponed fixtures (they never happened), exhibitions (the All-Star
    Game does not score), and team-aggregate rows. Keeps unplayed REAL games —
    a DNP in the final slot is a genuine 0.0.
    """
    seq: dict[tuple[str, int], list[dict]] = defaultdict(list)
    rows = conn.execute(
        """
        SELECT b.sleeper_id, b.fantasy_week, b.game_date, b.played,
               b.raw_stats, b.sleeper_game_id
          FROM box_scores b
          JOIN game_links g ON g.sleeper_game_id = b.sleeper_game_id
         WHERE b.season = ?
           AND COALESCE(b.is_team_row, 0) = 0
           AND COALESCE(g.is_exhibition, 0) = 0
           AND COALESCE(g.occurred, 1) = 1
         ORDER BY b.sleeper_id, b.fantasy_week, b.game_date
        """,
        (season,),
    )
    for r in rows:
        seq[(r["sleeper_id"], r["fantasy_week"])].append(dict(r))
    return seq


def run_inference(conn: sqlite3.Connection, season: str) -> tuple[int, int]:
    """Infer and persist every starter's lock decision. Returns (rows, resolved)."""
    scoring = scoring_settings(conn)
    seq = _game_sequence(conn, season)

    starters = conn.execute(
        """
        SELECT week, roster_id, sleeper_id, counted_points
          FROM weekly_matchups_latest
         WHERE is_starter = 1
         GROUP BY week, roster_id, sleeper_id
         ORDER BY week, roster_id
        """
    ).fetchall()

    resolved = 0
    for row in starters:
        raw = seq.get((row["sleeper_id"], row["week"]), [])
        games = [
            Game(
                index=i,
                played=bool(g["played"]),
                score=score_recorded(json.loads(g["raw_stats"]), scoring) if g["played"] else 0.0,
            )
            for i, g in enumerate(raw)
        ]
        counted = row["counted_points"] if row["counted_points"] is not None else 0.0
        inf = infer_lock(counted, games)
        if is_resolved(inf):
            resolved += 1

        locked_game_id = (
            raw[inf.matched_index]["sleeper_game_id"]
            if inf.matched_index is not None and inf.matched_index < len(raw)
            else None
        )
        conn.execute(
            "INSERT OR REPLACE INTO lock_inferences"
            " (week, roster_id, sleeper_id, status, n_games, matched_game_index,"
            "  locked_game_id, locked_early, counted_points, ambiguous_indices, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["week"],
                row["roster_id"],
                row["sleeper_id"],
                str(inf.status),
                inf.n_games,
                inf.matched_index,
                locked_game_id,
                None if inf.locked_early is None else int(inf.locked_early),
                counted,
                json.dumps(list(inf.candidates)) if len(inf.candidates) > 1 else "[]",
                inf.confidence,
            ),
        )
    return len(starters), resolved


def build_profiles(conn: sqlite3.Connection) -> list[ManagerProfile]:
    """Summarise each manager's stopping behaviour from the inferences."""
    by_roster: dict[int, list[LockInference]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM lock_inferences"):
        by_roster[r["roster_id"]].append(
            LockInference(
                status=LockStatus(r["status"]),
                n_games=r["n_games"],
                matched_index=r["matched_game_index"],
                candidates=tuple(json.loads(r["ambiguous_indices"] or "[]")),
                confidence=r["confidence"],
                locked_early=None if r["locked_early"] is None else bool(r["locked_early"]),
            )
        )

    profiles = [profile_manager(rid, infs) for rid, infs in sorted(by_roster.items())]
    stamp = now_iso()
    for p in profiles:
        conn.execute(
            "INSERT OR REPLACE INTO manager_profiles"
            " (roster_id, decisions, locked_early, rode_to_end, lock_rate,"
            "  mean_lock_position, computed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                p.roster_id,
                p.decisions,
                p.locked_early,
                p.rode_to_end,
                p.lock_rate,
                p.mean_lock_position,
                stamp,
            ),
        )
    return profiles


# ------------------------------------------------------------------- the gates


def check_resolution_rate(conn: sqlite3.Connection) -> Check:
    """>=95% of starter player-weeks resolved at full confidence."""
    total = conn.execute("SELECT COUNT(*) c FROM lock_inferences").fetchone()["c"]
    resolved = conn.execute(
        "SELECT COUNT(*) c FROM lock_inferences WHERE confidence >= 1.0 AND status != ?",
        (str(LockStatus.UNRESOLVED),),
    ).fetchone()["c"]
    rate = resolved / total if total else 0.0
    return Check(
        name=f"starter player-weeks resolved at high confidence (>={RESOLVED_THRESHOLD:.0%})",
        passed=rate >= RESOLVED_THRESHOLD,
        detail=f"{resolved}/{total} resolved ({rate:.2%})",
    )


def check_no_unresolved(conn: sqlite3.Connection) -> Check:
    """A counted score matching no game is a bug, not an outcome."""
    rows = conn.execute(
        "SELECT * FROM lock_inferences WHERE status = ? ORDER BY week LIMIT 20",
        (str(LockStatus.UNRESOLVED),),
    ).fetchall()
    n = conn.execute(
        "SELECT COUNT(*) c FROM lock_inferences WHERE status = ?",
        (str(LockStatus.UNRESOLVED),),
    ).fetchone()["c"]
    return Check(
        name="no counted score fails to match a game",
        passed=n == 0,
        detail=f"{n} unresolved",
        offenders=[
            f"week {r['week']} roster {r['roster_id']} player {r['sleeper_id']}:"
            f" counted={r['counted_points']} over {r['n_games']} games"
            for r in rows
        ],
    )


def check_all_rosters_profiled(conn: sqlite3.Connection) -> Check:
    """Phase 5 replays every roster, so every roster needs a profile."""
    rosters = conn.execute(
        "SELECT COUNT(DISTINCT roster_id) c FROM weekly_matchups_latest"
    ).fetchone()["c"]
    profiled = conn.execute("SELECT COUNT(*) c FROM manager_profiles").fetchone()["c"]
    thin = [
        f"roster {r['roster_id']} has only {r['decisions']} decisions"
        for r in conn.execute("SELECT * FROM manager_profiles WHERE decisions < 20")
    ]
    return Check(
        name="every roster has a lock-tendency profile",
        passed=profiled == rosters and not thin,
        detail=f"{profiled}/{rosters} rosters profiled",
        offenders=thin,
    )


def check_eligibility_rule(conn: sqlite3.Connection, season: str) -> Check:
    """The derived slot-eligibility rule must explain every observed lineup.

    Reads point-in-time positions, never today's — a player reclassified over
    the summer would otherwise look retroactively ineligible.
    """
    pit: dict[str, dict[int, list[str]]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT sleeper_id, fantasy_week, pit_positions FROM box_scores"
        " WHERE pit_positions IS NOT NULL AND COALESCE(is_team_row, 0) = 0 AND season = ?",
        (season,),
    ):
        pit[r["sleeper_id"]][r["fantasy_week"]] = json.loads(r["pit_positions"])

    def positions(pid: str, week: int) -> list[str]:
        weeks = pit.get(pid, {})
        prior = [w for w in weeks if w <= week]
        if prior:
            return weeks[max(prior)]
        return weeks[min(weeks)] if weeks else []

    checked = 0
    offenders: list[str] = []
    for m in conn.execute(
        "SELECT week, roster_id, sleeper_id, slot FROM weekly_matchups_latest"
        " WHERE is_starter = 1 AND slot IS NOT NULL"
        " GROUP BY week, roster_id, sleeper_id"
    ):
        pos = positions(m["sleeper_id"], m["week"])
        if not pos:
            continue
        checked += 1
        if not eligible(m["slot"], pos, sleeper_id=m["sleeper_id"]):
            if len(offenders) < 20:
                name = conn.execute(
                    "SELECT full_name FROM players WHERE sleeper_id = ?", (m["sleeper_id"],)
                ).fetchone()
                offenders.append(
                    f"week {m['week']} roster {m['roster_id']}"
                    f" {name['full_name'] if name else m['sleeper_id']}"
                    f" ({m['sleeper_id']}) {pos} started at {m['slot']}"
                )
    return Check(
        name="slot-eligibility rule explains every observed lineup",
        passed=not offenders,
        detail=f"{checked - len(offenders)}/{checked} assignments legal"
        f" ({len(OBSERVED_EXTRA_SLOTS)} player overrides)",
        offenders=offenders,
    )


def run(conn: sqlite3.Connection, season: str) -> list[Check]:
    return [
        check_eligibility_rule(conn, season),
        check_no_unresolved(conn),
        check_resolution_rate(conn),
        check_all_rosters_profiled(conn),
    ]


def status_breakdown(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (r["status"], r["n"])
        for r in conn.execute(
            "SELECT status, COUNT(*) n FROM lock_inferences GROUP BY status ORDER BY n DESC"
        )
    ]
