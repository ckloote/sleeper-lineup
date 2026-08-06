"""NBA schedule ingest.

Sleeper's per-game stat rows carry a date but no tipoff time, and use their own
game id space. This module supplies the missing half: real game ids and
``tipoff_utc``.

Tipoff times do not matter for the backtest — a player never plays twice in one
day, so date ordering fully determines his game sequence. They matter for the
live digest, which has to say when tonight's lock window closes.

Two-step on purpose: the skeleton comes from one LeagueGameFinder call so the
schedule is complete even if the per-date tipoff sweep is interrupted.
"""

from __future__ import annotations

import sqlite3
import time

from lockin.ingest.validate import SchemaDriftError
from lockin.store.db import log_ingest, now_iso

PAUSE_SECONDS = 0.6  # nba_api is the fragile upstream; be gentle


def _season_label(season: str) -> str:
    """Sleeper's "2025" is the NBA's "2025-26"."""
    start = int(season)
    return f"{start}-{str(start + 1)[-2:]}"


def parse_matchup(matchup: str, game_id: str = "?") -> tuple[str, str]:
    """Parse a LeagueGameFinder MATCHUP string into (home, away).

    BOTH teams come from the string. Taking one side from TEAM_ABBREVIATION
    instead looks equivalent but is not: a handful of games (0022500147,
    0022500578, 0022500602 in 2025-26) carry the SAME away-perspective string
    on both of their rows. Trusting TEAM_ABBREVIATION for the away side then
    lets the home team's row overwrite away with itself, yielding a nonsense
    "DET @ DET" fixture that silently fails to link.
    """
    if " vs. " in matchup:
        home, away = (s.strip() for s in matchup.split(" vs. ", 1))
    elif " @ " in matchup:
        away, home = (s.strip() for s in matchup.split(" @ ", 1))
    else:
        raise SchemaDriftError(f"unparseable MATCHUP {matchup!r} for game {game_id}")
    if not home or not away or home == away:
        raise SchemaDriftError(f"MATCHUP {matchup!r} for game {game_id} -> home={home} away={away}")
    return home, away


def ingest_schedule(conn: sqlite3.Connection, season: str) -> int:
    """Fetch the season's game list. One request, one row per game."""
    from nba_api.stats.endpoints import leaguegamefinder

    started = now_iso()
    label = _season_label(season)
    frames = leaguegamefinder.LeagueGameFinder(
        season_nullable=label, league_id_nullable="00", season_type_nullable="Regular Season"
    ).get_data_frames()
    if not frames or frames[0].empty:
        raise SchemaDriftError(f"LeagueGameFinder returned no rows for {label}")

    df = frames[0]
    for col in ("GAME_ID", "GAME_DATE", "MATCHUP", "TEAM_ABBREVIATION"):
        if col not in df.columns:
            raise SchemaDriftError(f"LeagueGameFinder missing column {col}; got {list(df.columns)}")

    games: dict[str, dict] = {}
    for row in df.itertuples(index=False):
        gid, date, matchup = row.GAME_ID, row.GAME_DATE, row.MATCHUP
        home, away = parse_matchup(matchup, gid)

        prior = games.get(gid)
        if prior and (prior["home"], prior["away"]) != (home, away):
            raise SchemaDriftError(
                f"game {gid}: rows disagree, {prior['away']}@{prior['home']} vs {away}@{home}"
            )
        games[gid] = {"date": date, "home": home, "away": away}

    n = 0
    for gid, g in games.items():
        if not (g["home"] and g["away"]):
            raise SchemaDriftError(f"game {gid} resolved to home={g['home']} away={g['away']}")
        conn.execute(
            "INSERT INTO nba_schedule"
            " (nba_game_id, season, game_date, tipoff_utc, home_team, away_team)"
            " VALUES (?, ?, ?, NULL, ?, ?)"
            " ON CONFLICT(nba_game_id) DO UPDATE SET"
            "   season=excluded.season, game_date=excluded.game_date,"
            "   home_team=excluded.home_team, away_team=excluded.away_team",
            (gid, season, g["date"], g["home"], g["away"]),
        )
        n += 1

    log_ingest(conn, "nba", f"schedule:{label}", n, started)
    return n


def ingest_scoreboard(
    conn: sqlite3.Connection, season: str, only_missing: bool = True
) -> tuple[int, int]:
    """Sweep ScoreboardV3 by date to fill tipoff times and backfill missing games.

    Driven off SLEEPER's fixture dates, not the NBA schedule's, because
    LeagueGameFinder returns only regular-season games and some real, countable
    games are not regular-season games. The NBA Cup championship is the case in
    point: game 0062500001 (SAS @ NYK, 2025-12-16) is the sole game on its date,
    so a sweep driven off nba_schedule would never visit that date at all.

    That game counts. Karl-Anthony Towns and Josh Hart both locked on it in week
    9 of 2025-26, which is only possible for a real scoring game — so unlike the
    All-Star Game it must stay in the sequence, and it needs a tipoff time like
    any other.

    Tolerant by design: a date that fails is skipped rather than aborting the
    run. Returns (tipoffs_filled, games_backfilled).
    """
    from nba_api.stats.endpoints import scoreboardv3

    started = now_iso()
    if only_missing:
        # Dates where something is still missing: an unlinked Sleeper fixture,
        # or a known game with no tipoff yet.
        q = """
            SELECT DISTINCT game_date FROM game_links
             WHERE nba_game_id IS NULL AND COALESCE(is_exhibition, 0) = 0
            UNION
            SELECT DISTINCT game_date FROM nba_schedule
             WHERE season = ? AND tipoff_utc IS NULL
        """
    else:
        q = """
            SELECT DISTINCT game_date FROM game_links
            UNION
            SELECT DISTINCT game_date FROM nba_schedule WHERE season = ?
        """
    dates = [r["game_date"] for r in conn.execute(q + " ORDER BY game_date", (season,))]

    filled = backfilled = 0
    for date in dates:
        try:
            games = scoreboardv3.ScoreboardV3(game_date=date).get_dict()["scoreboard"]["games"]
        except Exception:  # noqa: BLE001 - a missing date is not fatal
            time.sleep(PAUSE_SECONDS)
            continue
        for g in games:
            gid, tip = g.get("gameId"), g.get("gameTimeUTC")
            home = (g.get("homeTeam") or {}).get("teamTricode")
            away = (g.get("awayTeam") or {}).get("teamTricode")
            if not (gid and home and away):
                continue
            cur = conn.execute(
                "INSERT INTO nba_schedule"
                " (nba_game_id, season, game_date, tipoff_utc, home_team, away_team)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(nba_game_id) DO UPDATE SET tipoff_utc = excluded.tipoff_utc"
                " WHERE nba_schedule.tipoff_utc IS NULL",
                (gid, season, date, tip, home, away),
            )
            if cur.rowcount:
                filled += 1
        time.sleep(PAUSE_SECONDS)

    backfilled = conn.execute(
        "SELECT COUNT(*) c FROM nba_schedule WHERE season = ? AND nba_game_id NOT LIKE '002%'",
        (season,),
    ).fetchone()["c"]
    log_ingest(conn, "nba", f"scoreboard:{_season_label(season)}", filled, started)
    return filled, backfilled


def mark_exhibitions(conn: sqlite3.Connection, season: str) -> int:
    """Flag fixtures that are not real NBA games.

    The All-Star Game shows up in Sleeper's stat feed as an ordinary fixture
    with real stat lines (2026-02-15, teams "STP" and "STR"), and it falls at
    the END of a fantasy week — so a naive reading makes it every All-Star's
    final game of the week.

    It does not count. Verified across week 17 of 2025-26: of the 15 rostered
    All-Star participants, not one counted their All-Star line. Anthony Edwards
    counted 30.0 (his last real game) rather than 16.5; Jalen Johnson 56.0
    rather than 9.0 — while LeBron and Cade Cunningham show genuine early locks
    in the same week, so this is not 15 managers all locking early.

    Left unflagged, the engine would think an All-Star's week ends on a low
    exhibition score and would bank far too eagerly before the break.

    Detection: both sides of a real fixture are among the NBA's 30 tricodes.
    """
    real_teams = {
        r["t"]
        for r in conn.execute(
            "SELECT home_team t FROM nba_schedule WHERE season = ?"
            " UNION SELECT away_team FROM nba_schedule WHERE season = ?",
            (season, season),
        )
    }
    if not real_teams:
        raise SchemaDriftError(
            "no NBA teams in schedule; ingest the schedule before marking exhibitions"
        )

    marked = 0
    for row in conn.execute("SELECT sleeper_game_id, team_a, team_b FROM game_links"):
        exhibition = row["team_a"] not in real_teams or row["team_b"] not in real_teams
        conn.execute(
            "UPDATE game_links SET is_exhibition = ? WHERE sleeper_game_id = ?",
            (1 if exhibition else 0, row["sleeper_game_id"]),
        )
        marked += 1 if exhibition else 0
    return marked


def link_games(conn: sqlite3.Connection, season: str) -> tuple[int, int]:
    """Resolve Sleeper game ids to NBA game ids.

    Sleeper stat rows expose (team, opponent) with no home/away marker, so the
    join key is (date, unordered team pair) — unique, since two teams meet at
    most once on a given date.

    Returns (linked, unlinked).
    """
    started = now_iso()
    conn.execute(
        """
        UPDATE game_links
           SET nba_game_id = (
               SELECT s.nba_game_id FROM nba_schedule s
                WHERE s.game_date = game_links.game_date
                  AND MIN(s.home_team, s.away_team) = game_links.team_a
                  AND MAX(s.home_team, s.away_team) = game_links.team_b
           )
        """
    )
    # Only fixtures that actually happened can link: LeagueGameFinder returns
    # played games, not the schedule, so a postponed fixture has no NBA row by
    # construction. Counting it as an unlinked failure would be wrong.
    linked = conn.execute(
        "SELECT COUNT(*) c FROM game_links WHERE nba_game_id IS NOT NULL"
    ).fetchone()["c"]
    unlinked = conn.execute(
        "SELECT COUNT(*) c FROM game_links"
        " WHERE nba_game_id IS NULL AND COALESCE(occurred, 1) = 1"
        "   AND COALESCE(is_exhibition, 0) = 0"
    ).fetchone()["c"]
    log_ingest(conn, "nba", f"link_games:{season}", linked, started)
    return linked, unlinked
