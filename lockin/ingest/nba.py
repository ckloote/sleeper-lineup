"""NBA schedule ingest.

Sleeper's per-game stat rows carry a date but no tipoff time, and use their own
game id space. This module supplies the missing half: real game ids and
``tipoff_utc``.

Tipoff times do not matter for the backtest — a player never plays twice in one
day, so date ordering fully determines his game sequence. They matter for the
live digest, which has to say when tonight's lock window closes.

**A schedule, not a results feed.** Fixtures come from ScheduleLeagueV2, which
lists games that have not been played yet and carries their tipoff times. The
per-date ScoreboardV3 sweep is a backstop for whatever that misses, not the
source. `ingest_schedule` explains what the distinction cost before it was
noticed.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from typing import NamedTuple, Protocol

from lockin.ingest.validate import SchemaDriftError
from lockin.store.db import log_ingest, now_iso

PAUSE_SECONDS = 0.6  # nba_api is the fragile upstream; be gentle

# The NBA encodes the game type in the first three characters of a game id.
PRESEASON, REGULAR_SEASON, ALL_STAR = "001", "002", "003"

# Not the competition Sleeper scores, and actively harmful if ingested — both
# bring non-NBA tricodes into the table `mark_exhibitions` reads. See
# `ingest_schedule`.
SKIPPED_GAME_TYPES = frozenset({PRESEASON, ALL_STAR})

# ScheduleLeagueV2's gameStatus: 1 scheduled, 2 in progress, 3 final.
GAME_STATUS_SCHEDULED = 1


class NbaFeed(Protocol):
    """Where the NBA's schedule comes from.

    The one seam in this module. `nba_api` is the only implementation that talks
    to the network; tests pass a feed that returns payloads they built, which is
    what lets a fresh season be ingested end to end without the NBA's servers —
    and lets it be a season that has not finished, which is the case every live
    path depends on and the recorded 2025-26 database cannot represent.
    """

    def schedule(self, season_label: str) -> dict: ...

    def scoreboard(self, game_date: str) -> list[dict]: ...


class NbaApiFeed:
    """The real feed: ScheduleLeagueV2 and ScoreboardV3 through `nba_api`."""

    def schedule(self, season_label: str) -> dict:
        from nba_api.stats.endpoints import scheduleleaguev2

        return scheduleleaguev2.ScheduleLeagueV2(season=season_label, league_id="00").get_dict()

    def scoreboard(self, game_date: str) -> list[dict]:
        from nba_api.stats.endpoints import scoreboardv3

        return scoreboardv3.ScoreboardV3(game_date=game_date).get_dict()["scoreboard"]["games"]


class ScheduleIngest(NamedTuple):
    """What one schedule fetch did.

    `unplayed` is the number that makes the forward-looking property visible in
    the ingest log. A schedule feed that has quietly reverted to a results feed
    reports zero here on a season in progress, which is the symptom that was
    invisible for the whole of the project's life.
    """

    written: int
    unplayed: int
    undecided: int


def _season_label(season: str) -> str:
    """Sleeper's "2025" is the NBA's "2025-26"."""
    start = int(season)
    return f"{start}-{str(start + 1)[-2:]}"


def ingest_schedule(
    conn: sqlite3.Connection, season: str, *, feed: NbaFeed | None = None
) -> ScheduleIngest:
    """Fetch the season's fixtures, including games that have not been played.

    **This used to read LeagueGameFinder, which is a results feed wearing a
    schedule's name.** It returns games that have been *played*, and because
    this project had only ever run against a finished season, three
    consequences went unnoticed until the 2026-27 rollover was planned:

    1. **It raises on a season with no results.** `lockin ingest` would have
       failed on day one with `SchemaDriftError: LeagueGameFinder returned no
       rows for 2026-27` — reproduced against the live endpoint on 2026-09-20,
       a month before opening night.
    2. **`nba_schedule` could never hold tonight's fixture.** So the §7.5
       fallback in day-one.md step 5 — "tonight's slate must come from the NBA
       schedule instead" — had nothing to read. The contingency the project
       budgeted an afternoon for was resting on a table that is empty for every
       date that matters.
    3. Tipoffs had to be swept per-date from ScoreboardV3, which is why
       `--skip-tipoffs` exists at all.

    ScheduleLeagueV2 answers all three in one request, and it is already in the
    pinned `nba_api` — 174 dates and 1,274 games for 2026-27, published weeks
    ahead, tipoff included. Validated against the season this project was built
    on: every one of the 1,231 rows LeagueGameFinder produced for 2025-26 comes
    back with an identical date, home/away pair and tipoff, and nothing it had
    is missing.

    **Preseason and All-Star games are skipped, and that filter is load-bearing
    rather than tidiness.** `mark_exhibitions` decides what counts as a real
    fixture by asking which tricodes appear in this table. 2025-26's preseason
    brings in GUA, HAP, MEL and SEM; All-Star weekend brings in STP and STR —
    which are *precisely* the two tricodes `mark_exhibitions` exists to catch.
    Ingest them and the All-Star game reads as a real game, the engine believes
    an All-Star's week ends on a low exhibition score, and it banks far too
    eagerly before the break.

    Playoff and play-in games are kept. They are real games between real teams,
    and their absence before was an artifact of LeagueGameFinder's
    ``season_type_nullable="Regular Season"`` rather than a decision.

    **A fixture with no teams yet is skipped, not fatal.** The 2026-27 NBA Cup
    final is already on the calendar with both tricodes empty, and the previous
    implementation raised `SchemaDriftError` on exactly that shape. It gets
    written on a later run, once the bracket resolves.

    Postponements need no handling here: the feed reflects the schedule as it
    now stands rather than annotating the old one. All three of 2025-26's
    postponed fixtures appear only on the date they were replayed, never on the
    date Sleeper still lists — which is what keeps `reconcile`'s postponement
    check meaningful.
    """
    started = now_iso()
    label = _season_label(season)
    payload = (feed or NbaApiFeed()).schedule(label)

    game_dates = (payload.get("leagueSchedule") or {}).get("gameDates")
    if game_dates is None:
        raise SchemaDriftError(
            f"ScheduleLeagueV2 has no leagueSchedule.gameDates for {label};"
            f" got top-level keys {sorted(payload)}"
        )

    written = unplayed = undecided = 0
    for day in game_dates:
        for game in day.get("games") or []:
            gid = game.get("gameId")
            if not gid:
                raise SchemaDriftError(
                    f"ScheduleLeagueV2 returned a game with no gameId on {day.get('gameDate')!r}"
                )
            if gid[:3] in SKIPPED_GAME_TYPES:
                continue

            home = (game.get("homeTeam") or {}).get("teamTricode")
            away = (game.get("awayTeam") or {}).get("teamTricode")
            if not (home and away):
                undecided += 1
                continue
            if home == away:
                raise SchemaDriftError(f"game {gid} has {home} playing itself")

            date = (game.get("gameDateEst") or "")[:10]
            try:
                # Parsed rather than length-checked: "next Tuesday"[:10] is ten
                # characters long and would have sailed through.
                dt.date.fromisoformat(date)
            except ValueError as exc:
                raise SchemaDriftError(
                    f"game {gid}: gameDateEst is {game.get('gameDateEst')!r}, expected a date"
                ) from exc

            status = game.get("gameStatus")
            conn.execute(
                "INSERT INTO nba_schedule"
                " (nba_game_id, season, game_date, tipoff_utc, home_team, away_team, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(nba_game_id) DO UPDATE SET"
                "   season=excluded.season, game_date=excluded.game_date,"
                "   home_team=excluded.home_team, away_team=excluded.away_team,"
                # Never let a re-fetch blank out a tipoff we already know. A
                # rescheduled game briefly carries no time, and the digest needs
                # one to say when tonight's lock window closes.
                "   tipoff_utc=COALESCE(excluded.tipoff_utc, nba_schedule.tipoff_utc),"
                "   status=COALESCE(excluded.status, nba_schedule.status)",
                (
                    gid,
                    season,
                    date,
                    game.get("gameDateTimeUTC") or None,
                    home,
                    away,
                    status if isinstance(status, int) else None,
                ),
            )
            written += 1
            if game.get("gameStatus") == GAME_STATUS_SCHEDULED:
                unplayed += 1

    log_ingest(conn, "nba", f"schedule:{label}", written, started)
    return ScheduleIngest(written=written, unplayed=unplayed, undecided=undecided)


def ingest_scoreboard(
    conn: sqlite3.Connection,
    season: str,
    only_missing: bool = True,
    *,
    feed: NbaFeed | None = None,
) -> tuple[int, int]:
    """Sweep ScoreboardV3 by date to fill tipoff times and backfill missing games.

    **A backstop, not the source.** `ingest_schedule` now returns tipoff times
    with the fixtures, so on a healthy run `only_missing` leaves this with
    almost nothing to visit. It stays because it is driven off SLEEPER's fixture
    dates rather than the NBA's, and so can still reach a game the NBA feed
    does not list under this season at all.

    That mattered more when the schedule came from LeagueGameFinder, which
    returns only regular-season games: the NBA Cup championship — game
    0062500001, SAS @ NYK, 2025-12-16 — is the sole game on its date, so a sweep
    driven off `nba_schedule` would never have visited that date. The schedule
    feed carries the Cup final now, but the asymmetry it exposed is real and
    cheap to keep covered.

    That game counts. Karl-Anthony Towns and Josh Hart both locked on it in week
    9 of 2025-26, which is only possible for a real scoring game — so unlike the
    All-Star Game it must stay in the sequence, and it needs a tipoff time like
    any other.

    Tolerant by design: a date that fails is skipped rather than aborting the
    run. Returns (tipoffs_filled, games_backfilled).
    """
    feed = feed or NbaApiFeed()
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
            games = feed.scoreboard(date)
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
            status = g.get("gameStatus")
            if isinstance(status, int):
                conn.execute(
                    "UPDATE nba_schedule SET status = ? WHERE nba_game_id = ?", (status, gid)
                )
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
    # A postponed fixture has no NBA row to link to, and that is not a failure.
    # The schedule feed reflects the calendar as it now stands rather than
    # annotating the old one, so a game Sleeper still lists on its original date
    # appears only on the date it was replayed — checked against all three of
    # 2025-26's postponements. Counting those as unlinked would be wrong.
    #
    # So only a fixture somebody actually played counts as unlinked. Asked of the
    # stat lines directly rather than of `state`, which is classified *from* the
    # link and would make this question circular.
    linked = conn.execute(
        "SELECT COUNT(*) c FROM game_links WHERE nba_game_id IS NOT NULL"
    ).fetchone()["c"]
    unlinked = conn.execute(
        "SELECT COUNT(*) c FROM game_links g"
        " WHERE g.nba_game_id IS NULL AND COALESCE(g.is_exhibition, 0) = 0"
        "   AND EXISTS (SELECT 1 FROM box_scores b"
        "                WHERE b.sleeper_game_id = g.sleeper_game_id AND b.played = 1)"
    ).fetchone()["c"]
    log_ingest(conn, "nba", f"link_games:{season}", linked, started)
    return linked, unlinked
