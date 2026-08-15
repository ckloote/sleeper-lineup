"""Sleeper API client and ingest.

Sleeper is the source of truth for scoring config, league state, and — the part
that matters most — per-player-per-game box scores keyed natively by
``sleeper_id``. That native keying is why this project needs no player ID
crosswalk; see implementation-plan.md §2.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from lockin.ingest.validate import (
    check_shot_consistency,
    validate_league,
    validate_matchups,
    validate_stat_rows,
)
from lockin.store import snapshots
from lockin.store.db import log_ingest, now_iso


def snapshot_stamp() -> str:
    """Filesystem-safe, lexicographically sortable UTC stamp."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


V1 = "https://api.sleeper.app/v1"
BASE = "https://api.sleeper.app"

# Sleeper tolerates ~1000 calls/minute and we make a few dozen. The pause is
# politeness, not throttling.
PAUSE_SECONDS = 0.05


class SleeperClient:
    def __init__(self, timeout: int = 60, retries: int = 3) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "lockin/0.1 (personal fantasy tool)"
        self.timeout = timeout
        self.retries = retries

    def get(self, url: str) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                time.sleep(PAUSE_SECONDS)
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                last = exc
                time.sleep(2**attempt)
        raise RuntimeError(f"GET {url} failed after {self.retries} attempts") from last

    def league(self, league_id: str) -> dict:
        return self.get(f"{V1}/league/{league_id}")

    def rosters(self, league_id: str) -> list:
        return self.get(f"{V1}/league/{league_id}/rosters")

    def matchups(self, league_id: str, week: int) -> list:
        return self.get(f"{V1}/league/{league_id}/matchups/{week}")

    def players(self) -> dict:
        return self.get(f"{V1}/players/nba")

    def week_stats(self, season: str, week: int, season_type: str = "regular") -> list:
        return self.get(f"{BASE}/stats/nba/{season}/{week}?season_type={season_type}")


# --------------------------------------------------------------------- ingest


def ingest_league(conn: sqlite3.Connection, client: SleeperClient, league_id: str) -> dict:
    started = now_iso()
    league = validate_league(client.league(league_id))
    conn.execute(
        "INSERT OR REPLACE INTO league_settings (league_id, season, payload_json, fetched_at)"
        " VALUES (?, ?, ?, ?)",
        (league["league_id"], league["season"], json.dumps(league), now_iso()),
    )
    log_ingest(conn, "sleeper", f"league:{league_id}", 1, started)
    return league


def ingest_rosters(conn: sqlite3.Connection, client: SleeperClient, league_id: str) -> int:
    started, observed = now_iso(), now_iso()
    rows = client.rosters(league_id)
    n = 0
    for roster in rows:
        for sleeper_id in roster.get("players") or []:
            conn.execute(
                "INSERT OR REPLACE INTO rosters"
                " (league_id, roster_id, owner_id, sleeper_id, observed_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (league_id, roster["roster_id"], roster.get("owner_id"), sleeper_id, observed),
            )
            n += 1
    log_ingest(conn, "sleeper", f"rosters:{league_id}", n, started)
    return n


def record_player_status(conn: sqlite3.Connection, payload: dict, as_of: str) -> int:
    """Append today's injury designations to `player_status`.

    Keyed on (sleeper_id, as_of) so re-running in a day is idempotent and
    running across days accumulates. Only players carrying a designation are
    stored — the absence of a row means "nothing reported", which is what an
    empty ``injury_status`` means anyway, and storing 2,000 nulls a day would
    bury the signal.
    """
    rows = [
        (sleeper_id, as_of, p.get("injury_status"))
        for sleeper_id, p in payload.items()
        if isinstance(p, dict) and p.get("injury_status")
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO player_status (sleeper_id, as_of, designation) VALUES (?, ?, ?)",
        rows,
    )
    return len(rows)


def status_coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """How many distinct days of availability data exist, and how many rows.

    Printed by every ingest because the *day count* is the number that reveals a
    stalled capture. Rows alone do not: a capture frozen since October still
    reports thousands of them, and the failure this exposes — a season of
    designations never recorded — is silent, permanent, and otherwise looks
    exactly like a working system.
    """
    row = conn.execute("SELECT COUNT(DISTINCT as_of) d, COUNT(*) n FROM player_status").fetchone()
    return int(row["d"]), int(row["n"])


def ingest_players(conn: sqlite3.Connection, client: SleeperClient) -> int:
    """Refresh the player reference table.

    This is a LIVE SNAPSHOT with no history — and so, it turns out, is the
    ``player`` object embedded in each stat row, so ``box_scores.pit_*`` is the
    same data and offers no protection (implementation-plan.md §17). The only
    genuinely point-in-time player attribute Sleeper publishes is the stat row's
    own ``team``, stored as ``box_scores.team``.

    Each run therefore also appends today's injury designation to
    ``player_status``. That cannot recover the past, but it starts the record
    that evaluating start/sit decisions will need, and it is unrecoverable if
    nobody starts it.

    **Called on every ingest, unconditionally.** This used to sit behind
    `--full` so a re-ingest could skip the 2.5MB fetch. The designations ride in
    on that same payload, so the flag was really a switch on whether to record
    the one thing that cannot be recovered later — and the Phase 6 crontab
    omitted it, which would have cost a season of data without a single error
    message. 2.5MB a run is not a price worth a failure mode.
    """
    started = now_iso()
    payload = client.players()
    updated = now_iso()
    n = 0
    for sleeper_id, p in payload.items():
        if not isinstance(p, dict):
            continue
        name = (
            p.get("full_name") or f"{p.get('first_name') or ''} {p.get('last_name') or ''}"
        ).strip()
        if not name:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO players"
            " (sleeper_id, nba_id, full_name, positions, team, status, injury_status, updated_at)"
            " VALUES (?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                sleeper_id,
                name,
                json.dumps(p.get("fantasy_positions") or []),
                p.get("team"),
                p.get("status"),
                p.get("injury_status"),
                updated,
            ),
        )
        n += 1

    # Start the availability record. It cannot be backfilled, so the only way
    # to have it next season is to begin now.
    flagged = record_player_status(conn, payload, updated[:10])
    log_ingest(conn, "sleeper", "players", n, started)
    log_ingest(conn, "sleeper", "player_status", flagged, started)
    return n


def ingest_matchups(
    conn: sqlite3.Connection,
    client: SleeperClient,
    league_id: str,
    week: int,
    roster_positions: list[str],
    *,
    snapshot_root: Path | None = None,
    season: str | None = None,
) -> tuple[int, Path | None]:
    """Append a matchup observation. Never upserts — see schema.sql.

    Also preserves the raw payload to `snapshot_root` when it differs from the
    last one seen. That file, not the database row, is what survives a rebuild —
    and Sleeper rewrites completed seasons, so it is the only defence against
    silently losing what actually happened.
    """
    started, observed = now_iso(), now_iso()
    payload = client.matchups(league_id, week)
    rows = validate_matchups(payload, week)

    written = None
    if snapshot_root is not None and season is not None:
        written = snapshots.save(
            snapshot_root,
            snapshots.MATCHUPS,
            season,
            week,
            payload,
            stamp=snapshot_stamp(),
        )

    n = 0
    for team in rows:
        conn.execute(
            "INSERT OR REPLACE INTO weekly_matchup_teams"
            " (week, roster_id, matchup_id, points, custom_points, observed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                week,
                team["roster_id"],
                team["matchup_id"],
                team.get("points"),
                team.get("custom_points"),
                observed,
            ),
        )
        starters = team.get("starters") or []
        slot_of = {}
        for idx, sleeper_id in enumerate(starters):
            if sleeper_id and sleeper_id != "0":
                slot_of[sleeper_id] = (
                    idx,
                    roster_positions[idx] if idx < len(roster_positions) else None,
                )

        for sleeper_id, points in (team.get("players_points") or {}).items():
            slot_index, slot = slot_of.get(sleeper_id, (None, None))
            conn.execute(
                "INSERT OR REPLACE INTO weekly_matchups"
                " (week, roster_id, matchup_id, sleeper_id, counted_points, is_starter,"
                "  slot_index, slot, observed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    week,
                    team["roster_id"],
                    team["matchup_id"],
                    sleeper_id,
                    points,
                    1 if sleeper_id in slot_of else 0,
                    slot_index,
                    slot,
                    observed,
                ),
            )
            n += 1
    log_ingest(conn, "sleeper", f"matchups:week={week}", n, started)
    return n, written


# Stat keys we promote to columns. Anything else stays in raw_stats.
_STAT_COLUMNS = {
    "pts": "pts",
    "ast": "ast",
    "oreb": "oreb",
    "dreb": "dreb",
    "reb": "reb",
    "stl": "stl",
    "blk": "blk",
    "to": "tov",
    "fgm": "fgm",
    "fga": "fga",
    "fgmi": "fgmi",
    "ftm": "ftm",
    "fta": "fta",
    "ftmi": "ftmi",
    "tpm": "tpm",
    "tpa": "tpa",
    "tpmi": "tpmi",
    "tf": "tech",
    "ff": "flagrant",
    "pf": "pf",
    "dd": "dd",
    "td": "td",
}


def ingest_week_stats(
    conn: sqlite3.Connection, client: SleeperClient, season: str, week: int
) -> tuple[int, int]:
    """Ingest one fantasy week of per-player-per-game box scores.

    Returns (rows, games_played). Rows exist for every SCHEDULED game, including
    ones the player sat out — that is what makes "the final game of the week
    counts, even a 0.0" computable.
    """
    started = now_iso()
    rows = validate_stat_rows(client.week_stats(season, week), week)
    ingested = now_iso()
    n = played_n = 0

    for row in rows:
        stats = row["stats"] or {}
        played = bool(stats)
        context = f"week={week} player={row['player_id']} game={row['game_id']}"
        if played:
            check_shot_consistency(stats, context)
            played_n += 1

        pit = row.get("player") or {}
        cols = {col: stats.get(key) for key, col in _STAT_COLUMNS.items()}

        conn.execute(
            "INSERT OR REPLACE INTO box_scores ("
            " sleeper_game_id, sleeper_id, season, season_type, fantasy_week, game_date,"
            " team, opponent, played, seconds_played,"
            " pts, ast, oreb, dreb, reb, stl, blk, tov,"
            " fgm, fga, fgmi, ftm, fta, ftmi, tpm, tpa, tpmi,"
            " tech, flagrant, pf, dd, td, plus_minus,"
            " pit_positions, pit_team, dnp_reason, raw_stats, ingested_at"
            ") VALUES (" + ", ".join(["?"] * 38) + ")",
            (
                row["game_id"],
                row["player_id"],
                row["season"],
                row["season_type"],
                int(row["week"]),
                row["date"],
                row.get("team"),
                row.get("opponent"),
                1 if played else 0,
                stats.get("sp"),
                cols["pts"],
                cols["ast"],
                cols["oreb"],
                cols["dreb"],
                cols["reb"],
                cols["stl"],
                cols["blk"],
                cols["tov"],
                cols["fgm"],
                cols["fga"],
                cols["fgmi"],
                cols["ftm"],
                cols["fta"],
                cols["ftmi"],
                cols["tpm"],
                cols["tpa"],
                cols["tpmi"],
                cols["tech"],
                cols["flagrant"],
                cols["pf"],
                cols["dd"],
                cols["td"],
                stats.get("plus_minus"),
                json.dumps(pit.get("fantasy_positions") or []) if pit else None,
                pit.get("team") if pit else None,
                row.get("status"),
                json.dumps(stats),
                ingested,
            ),
        )
        n += 1

        team, opp, date = row.get("team"), row.get("opponent"), row["date"]
        if team and opp:
            a, b = sorted((team, opp))
            # ON CONFLICT rather than INSERT OR REPLACE so a re-ingest keeps any
            # nba_game_id already resolved by link_games().
            conn.execute(
                "INSERT INTO game_links"
                " (sleeper_game_id, nba_game_id, game_date, team_a, team_b)"
                " VALUES (?, NULL, ?, ?, ?)"
                " ON CONFLICT(sleeper_game_id) DO UPDATE SET"
                "   game_date=excluded.game_date,"
                "   team_a=excluded.team_a, team_b=excluded.team_b",
                (row["game_id"], date, a, b),
            )

    log_ingest(conn, "sleeper", f"stats:week={week}", n, started)
    return n, played_n


def refresh_row_kinds(conn: sqlite3.Connection) -> tuple[int, int]:
    """Mark team-aggregate rows in box_scores.

    Sleeper's feed mixes team totals in with players ("TEAM_OKC": 125 pts, 38
    reb, 29 ast). Scored naively they read as a triple-double every night.

    A row is a player row iff its id resolves in `players`. That definition is
    self-maintaining, unlike a "TEAM_" prefix check — and on the 2025-26 season
    the two agree exactly, which `reconcile.check_team_rows` asserts so a real
    player going missing from `players` surfaces instead of being silently
    reclassified as a team.

    Returns (player_rows, team_rows).
    """
    conn.execute(
        "UPDATE box_scores SET is_team_row ="
        " CASE WHEN sleeper_id IN (SELECT sleeper_id FROM players) THEN 0 ELSE 1 END"
    )
    team = conn.execute("SELECT COUNT(*) c FROM box_scores WHERE is_team_row = 1").fetchone()["c"]
    player = conn.execute("SELECT COUNT(*) c FROM box_scores WHERE is_team_row = 0").fetchone()["c"]
    return player, team


def refresh_game_occurrence(conn: sqlite3.Connection) -> tuple[int, int]:
    """Mark which fixtures actually happened.

    A fixture where not one player recorded a stat line did not take place —
    it was postponed. This matters because an unplayed REAL game scores 0.0 for
    an unlocked starter, while a postponed fixture is excluded entirely. See
    schema.sql for the week-12 evidence.

    Returns (occurred, postponed).
    """
    conn.execute(
        """
        UPDATE game_links
           SET occurred = (
               SELECT CASE WHEN SUM(b.played) > 0 THEN 1 ELSE 0 END
                 FROM box_scores b
                WHERE b.sleeper_game_id = game_links.sleeper_game_id
           )
        """
    )
    occurred = conn.execute("SELECT COUNT(*) c FROM game_links WHERE occurred = 1").fetchone()["c"]
    postponed = conn.execute("SELECT COUNT(*) c FROM game_links WHERE occurred = 0").fetchone()["c"]
    return occurred, postponed
