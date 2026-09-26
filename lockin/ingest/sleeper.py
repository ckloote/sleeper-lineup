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

from lockin.config import ALL_STAT_WEEKS
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


def fetch_league(client: SleeperClient, league_id: str) -> dict:
    """The league payload, validated but not stored.

    Split from `store_league` so an ingest can check the payload against the
    database's identity before its first write (lockin/store/identity.py).
    """
    return validate_league(client.league(league_id))


def store_league(conn: sqlite3.Connection, league: dict, started: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO league_settings (league_id, season, payload_json, fetched_at)"
        " VALUES (?, ?, ?, ?)",
        (league["league_id"], league["season"], json.dumps(league), now_iso()),
    )
    log_ingest(conn, "sleeper", f"league:{league['league_id']}", 1, started)


def ingest_league(conn: sqlite3.Connection, client: SleeperClient, league_id: str) -> dict:
    started = now_iso()
    league = fetch_league(client, league_id)
    store_league(conn, league, started)
    return league


def mark_final_weeks(league: dict, root: Path, season: str, *, stamp: str) -> list[int]:
    """Record the first sighting of each week Sleeper has finished scoring.

    `settings.last_scored_leg` is Sleeper's own statement that a week is done.
    The marker it leaves beside the archive is the boundary `lockin repair`
    reads evidence from (lockin/repair.py). Returns the weeks newly marked.
    """
    last = (league.get("settings") or {}).get("last_scored_leg")
    if not isinstance(last, int):
        return []
    return [
        w
        for w in ALL_STAT_WEEKS
        if w <= last and snapshots.mark_finalized(root, snapshots.MATCHUPS, season, w, stamp=stamp)
    ]


def current_weeks(league: dict) -> list[int]:
    """Which weeks are still moving, from Sleeper's own view of its calendar.

    The cron used to pass ``$(date +%V)``, the ISO calendar week. Fantasy weeks
    are 1-25 and nothing maps between the two: in October that asked for week 40
    and got nothing, and in January it asked for week 3 and cheerfully
    re-ingested October every morning. Sleeper publishes the answer, so ask it.

    ``settings.leg`` is the week being played now; ``settings.last_scored_leg``
    is the last one it has finished scoring. Usually the same week, and this
    returns one. Around a week boundary they differ — ``leg`` has rolled over
    while the week just gone is still being settled — and both come back, so the
    finished week gets its final numbers instead of keeping whatever the last
    run happened to see.

    Two weeks at most, by construction. This runs unattended every morning, and
    a job whose cost depends on the calendar is one that surprises somebody in
    March.
    """
    settings = league.get("settings") or {}
    leg = settings.get("leg")
    if not isinstance(leg, int):
        raise ValueError(
            "league.settings.leg is missing or not a number, so there is no way to "
            "tell which fantasy week it is; pass --weeks explicitly"
        )
    weeks = {leg}
    last_scored = settings.get("last_scored_leg")
    if isinstance(last_scored, int):
        weeks.add(last_scored)

    known = sorted(w for w in weeks if w in ALL_STAT_WEEKS)
    if not known:
        raise ValueError(
            f"Sleeper reports leg={leg}, outside the {min(ALL_STAT_WEEKS)}-"
            f"{max(ALL_STAT_WEEKS)} weeks this league scores; if the season has not "
            "started there is nothing to ingest yet"
        )
    return known


def ingest_users(conn: sqlite3.Connection, client: SleeperClient, league_id: str) -> int:
    """Display names for each roster, so a page can say who a manager is.

    Sleeper publishes these live on /league/{id}/users and nowhere else, and no
    other table carries them. They used to be fetched per render, which
    `lockin serve` cannot do: it holds a read-only connection and a request
    handler must not make network calls. So the served dashboard could only
    label rows "roster 3".

    Rows are replaced, not appended. A display name is a live attribute the user
    can change and it is always refetchable, so only the newest is worth having.

    Members without a roster are skipped: a league can carry a commissioner or a
    co-owner who never owns one.
    """
    started = now_iso()
    users = client.get(f"{V1}/league/{league_id}/users")
    rosters = client.get(f"{V1}/league/{league_id}/rosters")
    owner_to_roster = {r["owner_id"]: r["roster_id"] for r in rosters if r.get("owner_id")}

    observed, n = now_iso(), 0
    for user in users:
        roster_id = owner_to_roster.get(user.get("user_id"))
        if roster_id is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO league_users"
            " (league_id, roster_id, owner_id, display_name, observed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (league_id, roster_id, user.get("user_id"), user.get("display_name"), observed),
        )
        n += 1
    log_ingest(conn, "sleeper", f"users:{league_id}", n, started)
    return n


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


def _latest_designations(conn: sqlite3.Connection, before: str | None = None) -> dict:
    """Each player's most recent designation event, optionally strictly before a time."""
    where, args = ("WHERE observed_at < ?", (before,)) if before is not None else ("", ())
    return {
        r[0]: r[1]
        for r in conn.execute(
            f"""
            SELECT sleeper_id, designation FROM (
                SELECT sleeper_id, designation,
                       ROW_NUMBER() OVER (
                           PARTITION BY sleeper_id ORDER BY observed_at DESC
                       ) AS rn
                  FROM player_status_events {where}
            ) WHERE rn = 1
            """,
            args,
        )
    }


def record_player_status(conn: sqlite3.Connection, payload: dict, observed_at: str) -> int:
    """Record one read of the injury designations. Returns how many were flagged.

    **A capture, then the changes.** Every call writes a `status_captures` row,
    so a day on which nobody was hurt is still visibly a day that was captured.
    Then an event for each player whose designation differs from his last one —
    including a change to NULL, so an Out that clears at 2pm is recorded as
    clearing rather than standing until tomorrow.

    This replaced a table keyed on (player, date) that stored flagged players
    only (review finding 8). There, a cleared designation wrote nothing and the
    morning's Out stood all day; a later update overwrote an earlier one, so a
    backtest could not tell what was known before tip from what arrived after
    it; and a healthy day looked like a missed one. None of that is
    recoverable afterwards, which is why it had to be right before the season.

    `observed_at` is a timestamp, not a date. Rows are small because unchanged
    players write nothing: 2,000 nulls a day would bury the signal, and so, in
    the old table, did never writing the one null that mattered.
    """
    players = {sid: p for sid, p in payload.items() if isinstance(p, dict)}
    flagged = sum(1 for p in players.values() if p.get("injury_status"))
    cur = conn.execute(
        "INSERT INTO status_captures (observed_at, players_seen, flagged) VALUES (?, ?, ?)",
        (observed_at, len(players), flagged),
    )
    capture_id = cur.lastrowid

    known = _latest_designations(conn)
    changes = [
        (sleeper_id, observed_at, p.get("injury_status") or None, capture_id)
        for sleeper_id, p in players.items()
        if (p.get("injury_status") or None) != known.get(sleeper_id)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO player_status_events"
        " (sleeper_id, observed_at, designation, capture_id) VALUES (?, ?, ?, ?)",
        changes,
    )
    return flagged


def designations_as_of(conn: sqlite3.Connection, before: str) -> dict[str, str] | None:
    """Every designation in force strictly before ``before``, as it was known then.

    None when no capture happened before that moment — "nobody was flagged" and
    "nobody looked" are different answers, and only the first is healthy.
    """
    seen = conn.execute(
        "SELECT 1 FROM status_captures WHERE observed_at < ? LIMIT 1", (before,)
    ).fetchone()
    if seen is None:
        return None
    return {sid: d for sid, d in _latest_designations(conn, before).items() if d}


def status_coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """Distinct days with a capture, and how many designation changes exist.

    Printed by every ingest because the *day count* is the number that reveals a
    stalled capture. Rows alone do not: a capture frozen since October still
    reports thousands of them, and the failure this exposes — a season of
    designations never recorded — is silent, permanent, and otherwise looks
    exactly like a working system.

    Days come from captures, not designations, so a healthy day counts; the
    legacy `player_status` days are included so the history does not restart.
    """
    days = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT substr(observed_at, 1, 10) FROM status_captures"
        "  UNION SELECT as_of FROM player_status"
        ")"
    ).fetchone()[0]
    changes = conn.execute("SELECT COUNT(*) FROM player_status_events").fetchone()[0]
    return int(days), int(changes)


def ingest_players(conn: sqlite3.Connection, client: SleeperClient) -> int:
    """Refresh the player reference table.

    This is a LIVE SNAPSHOT with no history — and so, it turns out, is the
    ``player`` object embedded in each stat row, so ``box_scores.pit_*`` is the
    same data and offers no protection (implementation-plan.md §17). The only
    genuinely point-in-time player attribute Sleeper publishes is the stat row's
    own ``team``, stored as ``box_scores.team``.

    Each run therefore also records the injury designations, timestamped, in
    ``player_status_events``. That cannot recover the past, but it starts the record
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
    flagged = record_player_status(conn, payload, updated)
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
    """Append a matchup observation — one whole poll. Never upserts; see schema.sql.

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
            " (week, roster_id, matchup_id, points, custom_points, observed_at, poll_complete)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                week,
                team["roster_id"],
                team["matchup_id"],
                team.get("points"),
                team.get("custom_points"),
                observed,
                int(
                    len(team.get("starters") or [])
                    == sum(p not in {"BN", "IR"} for p in roster_positions)
                    and all(p and p != "0" for p in team.get("starters") or [])
                    and set(team.get("starters") or []).issubset(team.get("players") or [])
                ),
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

        # The poll's membership is everyone the payload names, not everyone with
        # a score. Early in a week `players_points` is sparse or empty while
        # `starters` already lists a valid lineup; reading only the scores
        # recorded no lineup at all, and made a starter with no game yet
        # indistinguishable from one who was not there. No score is NULL.
        points_of = team.get("players_points") or {}
        members = [p for p in team.get("players") or [] if p and p != "0"]
        for sleeper_id in dict.fromkeys([*members, *slot_of, *points_of]):
            points = points_of.get(sleeper_id)
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
    conn: sqlite3.Connection,
    client: SleeperClient,
    season: str,
    week: int,
    *,
    run_id: int | None = None,
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
    if run_id is not None:
        conn.execute(
            "INSERT INTO ingest_stats_fetches (run_id, week, started_at, finished_at)"
            " VALUES (?, ?, ?, ?)",
            (run_id, week, started, now_iso()),
        )
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


FIXTURE_STATES = ("final", "in_progress", "scheduled", "postponed", "unknown")

# What `occurred` means under each state, for readers that predate `state`.
_OCCURRED = {"final": 1, "in_progress": 1, "postponed": 0}


def fixture_state(
    *,
    played: bool,
    linked: bool,
    nba_status: int | None,
    game_date: str,
    today: str,
    schedule_loaded: bool,
) -> str:
    """What one fixture is, from the three pieces of evidence there are.

    Nobody having a stat line used to mean "postponed". It means that for a
    game in the past, and it also means "not played yet" for every game in the
    future — the rule removed all 681 of them from the digest (review finding
    1). So absence of stats is read against the NBA's status and date:

    - Stat lines exist: the game happened (``in_progress`` while the NBA says
      it is still being played).
    - Linked to an NBA game with no stat lines: ``scheduled`` if it is not due
      yet, but ``unknown`` if the NBA calls it final or its date has passed.
      That is an incomplete Sleeper feed, and guessing either way mis-scores a
      final game.
    - No NBA game on this date for this pair: ``postponed``. The NBA files a
      moved game only under its new date — true of all three 2025-26
      postponements. Without a schedule to consult, only a past date can say so.
    """
    if played:
        return "in_progress" if nba_status == 2 else "final"
    if linked:
        if nba_status == 3:
            return "unknown"
        if nba_status == 2:
            return "in_progress"
        return "unknown" if game_date < today else "scheduled"
    if schedule_loaded or game_date < today:
        return "postponed"
    return "scheduled"


def classify_fixtures(conn: sqlite3.Connection, today: str) -> dict[str, int]:
    """Set every fixture's `state` (and `occurred`, in step). Returns counts.

    ``today`` is the NBA date — the schedule's timezone, not the host's; see
    lockin/clock.py. Run after `link_games`, whose links are part of the evidence.
    """
    schedule_loaded = conn.execute("SELECT EXISTS (SELECT 1 FROM nba_schedule)").fetchone()[0]
    rows = conn.execute(
        """
        SELECT g.sleeper_game_id, g.game_date, s.status,
               s.nba_game_id IS NOT NULL AS linked,
               EXISTS (SELECT 1 FROM box_scores b
                        WHERE b.sleeper_game_id = g.sleeper_game_id AND b.played = 1) AS played,
               COALESCE(g.is_exhibition, 0) AS exhibition
          FROM game_links g
          LEFT JOIN nba_schedule s ON s.nba_game_id = g.nba_game_id
        """
    ).fetchall()
    counts = dict.fromkeys(FIXTURE_STATES, 0)
    updates = []
    for r in rows:
        # The NBA schedule leaves exhibitions out on purpose (lockin/ingest/nba.py),
        # so for them "not in the schedule" is not evidence of a postponement:
        # only the date can speak. The All-Star Game is scheduled until played.
        exhibition = bool(r[5])
        state = fixture_state(
            played=bool(r[4]),
            linked=bool(r[3]) and not exhibition,
            nba_status=None if exhibition else r[2],
            game_date=r[1],
            today=today,
            schedule_loaded=bool(schedule_loaded) and not exhibition,
        )
        counts[state] += 1
        updates.append((state, _OCCURRED.get(state), r[0]))
    conn.executemany(
        "UPDATE game_links SET state = ?, occurred = ? WHERE sleeper_game_id = ?", updates
    )
    return counts
