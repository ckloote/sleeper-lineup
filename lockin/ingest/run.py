"""One ingest run, from league payload to linked fixtures.

The sequence used to live in the `lockin ingest` command body, where the only
way to exercise it was to point it at Sleeper and the NBA. Everything this
project knows about an *unfinished* season — the only kind the live paths ever
see — therefore went untested, because the recorded 2025-26 database cannot
represent one. Here the network is two arguments, `client` and `feed`, and
`tests/live_fixture.py` supplies both.

The CLI keeps the parts that are about being a command: opening the database,
turning a refusal into an exit status, and printing the closing line.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, timedelta

from lockin import calendar, clock
from lockin.config import ALL_STAT_WEEKS, PLAYOFF_WEEKS, Config
from lockin.ingest import nba as nba_ingest
from lockin.ingest import sleeper as sleeper_ingest
from lockin.store import db, identity, runs
from lockin.store.db import now_iso


class IngestRefused(Exception):
    """The run stopped before writing anything, for a reason a person must fix."""


def week_structure(league: dict) -> list[str]:
    """The league's own week numbering, and whether `lockin.config` agrees.

    `config.ALL_STAT_WEEKS` and friends are 2025-26's structure written down. A
    changed playoff format moves them, and day-one.md step 3 used to check that
    by reading the new database before the step that creates it. Printing it
    from the payload every run is the check, and it cannot come too early.
    """
    settings = league.get("settings") or {}
    start, playoffs = settings.get("start_week"), settings.get("playoff_week_start")
    lines = [
        f"  structure   week {start} first, playoffs from {playoffs},"
        f" last scored {settings.get('last_scored_leg')}"
    ]
    if start not in (None, ALL_STAT_WEEKS.start) or playoffs not in (None, PLAYOFF_WEEKS.start):
        lines.append(
            f"  WARNING     lockin/config.py expects week {ALL_STAT_WEEKS.start} first and"
            f" playoffs from {PLAYOFF_WEEKS.start}; update ALL_STAT_WEEKS,"
            " REGULAR_SEASON_WEEKS and PLAYOFF_WEEKS before trusting the weekly gates"
        )
    return lines


def run_ingest(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    client: sleeper_ingest.SleeperClient,
    weeks: list[int] | None,
    skip_nba: bool = False,
    skip_tipoffs: bool = False,
    feed: nba_ingest.NbaFeed | None = None,
    echo: Callable[[str], None] = print,
    today: str | None = None,
) -> None:
    """Refresh league state, box scores, matchups and the NBA schedule.

    ``weeks`` None means the weeks Sleeper says are moving (`--weeks current`),
    resolved from the league payload this run fetches anyway — so it costs no
    extra call and cannot disagree with the run it belongs to.

    ``today`` is the NBA date the run classifies fixtures against; it defaults
    to the clock, in the schedule's timezone. Tests pass the synthetic season's.
    """
    today = today or clock.today_iso(cfg.timezone)
    echo(f"league {cfg.league_id} season {cfg.season} -> {cfg.db_path}")

    # Nothing is written, and no snapshot saved, until the payload, the
    # configuration and the database agree on which league and season this is.
    started = now_iso()
    league = sleeper_ingest.fetch_league(client, cfg.league_id)
    try:
        identity.check_payload(league, cfg.league_id, cfg.season)
        identity.check(conn, cfg.league_id, cfg.season, claim=True, db_path=cfg.db_path)
    except identity.IdentityMismatch as exc:
        raise IngestRefused(str(exc)) from None
    sleeper_ingest.store_league(conn, league, started)
    roster_positions = league["roster_positions"]
    echo(f"  league      slots={' '.join(roster_positions[:6])}")
    for line in week_structure(league):
        echo(line)
    finals = sleeper_ingest.mark_final_weeks(
        league, cfg.snapshot_root, cfg.season, stamp=sleeper_ingest.snapshot_stamp()
    )
    if finals:
        echo(f"  final       week(s) {', '.join(map(str, finals))} now scored; marked for repair")

    if weeks is None:
        try:
            weeks = sleeper_ingest.current_weeks(league)
        except ValueError as exc:
            raise IngestRefused(str(exc)) from None
        echo(f"  weeks       current -> {', '.join(str(w) for w in weeks)}")

    # Running until the last step finishes. A failure part-way leaves it so,
    # whatever the between-week checkpoints committed — which is what stops a
    # partial ingest vouching for a digest (review finding 9).
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    ingest_run = runs.start(conn, weeks, slate_through=yesterday)

    n = sleeper_ingest.ingest_rosters(conn, client, cfg.league_id)
    echo(f"  rosters     {n} roster-player rows")

    n = sleeper_ingest.ingest_users(conn, client, cfg.league_id)
    echo(f"  managers    {n} display names")

    n = sleeper_ingest.ingest_players(conn, client)
    echo(f"  players     {n} (live snapshot)")
    days, changes = sleeper_ingest.status_coverage(conn)
    echo(f"  status      {changes} designation change(s) across {days} captured day(s)")

    total_rows = total_played = snapshots_written = 0
    for week in weeks:
        rows, played = sleeper_ingest.ingest_week_stats(conn, client, cfg.season, week)
        _, snap = sleeper_ingest.ingest_matchups(
            conn,
            client,
            cfg.league_id,
            week,
            roster_positions,
            snapshot_root=cfg.snapshot_root,
            season=cfg.season,
        )
        total_rows += rows
        total_played += played
        snapshots_written += 1 if snap else 0
        marker = "  *snapshot changed*" if snap else ""
        echo(f"  week {week:>2}     {rows:>5} player-games ({played} played){marker}")
        # Release the write lock between weeks. Holding it for the whole run
        # is what would make a digest firing mid-ingest fail outright.
        db.checkpoint(conn)
    echo(f"  box scores  {total_rows} rows, {total_played} played")
    echo(
        f"  snapshots   {snapshots_written} new/changed of {len(weeks)} weeks"
        f" -> {cfg.snapshot_root}"
    )

    player_rows, team_rows = sleeper_ingest.refresh_row_kinds(conn)
    echo(f"  row kinds   {player_rows} player, {team_rows} team-aggregate")

    if skip_nba:
        _classify(conn, today, echo)
        runs.finish(conn, ingest_run, skipped=["nba"])
        return

    sched = nba_ingest.ingest_schedule(conn, cfg.season, feed=feed)
    note = f"{sched.written} NBA games, {sched.unplayed} not yet played"
    if sched.undecided:
        note += f", {sched.undecided} without teams yet"
    echo(f"  schedule    {note}")
    exhibitions = nba_ingest.mark_exhibitions(conn, cfg.season)
    echo(f"  exhibitions {exhibitions} non-NBA fixture(s) excluded")

    # Link once to find what is missing, sweep the scoreboard to fill tipoffs
    # and backfill non-regular-season games, then link again.
    nba_ingest.link_games(conn, cfg.season)
    if not skip_tipoffs:
        filled, non_rs = nba_ingest.ingest_scoreboard(conn, cfg.season, feed=feed)
        echo(f"  tipoffs     {filled} filled, {non_rs} non-regular-season game(s)")
    linked, unlinked = nba_ingest.link_games(conn, cfg.season)
    echo(f"  game links  {linked} linked, {unlinked} unlinked")
    _classify(conn, today, echo)
    runs.finish(conn, ingest_run, skipped=["tipoffs"] if skip_tipoffs else [])


def _classify(conn: sqlite3.Connection, today: str, echo: Callable[[str], None]) -> None:
    """Fixture states, last: the NBA link and status are part of the evidence.

    Also where the week calendar is checked against Sleeper's own week, since
    both inputs — the schedule and the league payload — are fresh here. A
    disagreement warns rather than fails: failing would also lose today's
    availability capture, and the digest refuses to advise on it anyway.
    """
    season = conn.execute("SELECT season FROM db_identity").fetchone()
    problem = calendar.disagreement(conn, season[0], today) if season else None
    if problem:
        echo(f"  WARNING     {problem}")
    counts = sleeper_ingest.classify_fixtures(conn, today)
    note = (
        f"{counts['final']} final, {counts['scheduled']} scheduled, {counts['postponed']} postponed"
    )
    if counts["in_progress"]:
        note += f", {counts['in_progress']} in progress"
    if counts["unknown"]:
        note += f", {counts['unknown']} UNKNOWN (stats missing for a game that is due)"
    echo(f"  fixtures    {note}")
