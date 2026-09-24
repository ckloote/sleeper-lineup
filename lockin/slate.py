"""A starter's week: the games he has played, and the NBA's schedule for the rest.

The digest used to read a player's whole week out of the box-score panel,
blanking the scores after its cutoff. That makes the schedule of games still to
come depend on Sleeper having published stat rows for them — the §7.5
assumption nobody could check before opening night — and on those rows
surviving the occurrence rule, which they did not (review finding 1).

Here the two halves come from the two places that actually know them:

- **Observed games**, on or before ``known_through``, from the panel: final
  fixtures only, with their real scores.
- **Scheduled games**, after it, from `nba_schedule`: the NBA publishes the
  season months ahead, with tipoffs, which the call expiry needs anyway.

What joins a player to the schedule is his team. On a live morning that is
`players.team`, today's roster. In a replay of a past date it is his team in his
last game on or before the cutoff — `box_scores.team`, the one point-in-time
player attribute Sleeper publishes (§17) — so a replay never learns about a
trade before it happened.

Sleeper's own forward rows, when they exist, become a cross-check: a
disagreement is a warning, not a silent preference for either source.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np

from lockin import calendar
from lockin.backtest import player_games
from lockin.core.policy import Game
from lockin.core.projections import SeasonPanel
from lockin.projections import FINAL_FIXTURE, date_of, day_index


@dataclass(slots=True)
class WeekSlate:
    games: dict[str, list[Game]] = field(default_factory=dict)
    """Each player's countable games this week, observed then scheduled."""
    tipoff: dict[tuple[str, int], str] = field(default_factory=dict)
    """(sleeper_id, day) -> tipoff_utc, for scheduled games that have one."""
    unsettled: list[tuple[str, str]] = field(default_factory=list)
    """(sleeper_id, date) for fixtures on or before the cutoff that are not final
    — in progress, or unknown. Their outcome is not in the panel, so a digest
    reading this morning's state must not proceed as though it were."""
    warnings: list[tuple[str, str]] = field(default_factory=list)
    """(sleeper_id, detail) where the schedule sources disagree; "" for the whole slate."""

    def next_tipoff(self, sleeper_id: str, after_day: int) -> str | None:
        """When his next game after ``after_day`` tips — when a lock window closes."""
        later = [g.day for g in self.games.get(sleeper_id, []) if g.day > after_day]
        return self.tipoff.get((sleeper_id, min(later))) if later else None


def team_of(
    conn: sqlite3.Connection, season: str, sleeper_id: str, known_through: int, *, live: bool
) -> str | None:
    """The team a player's future games are scheduled with."""
    if live:
        row = conn.execute(
            "SELECT team FROM players WHERE sleeper_id = ?", (sleeper_id,)
        ).fetchone()
        if row and row[0]:
            return row[0]
    # Real, final fixtures only. An All-Star's last row before the break is the
    # All-Star Game, whose "team" is STP or STR; reading it put six starters'
    # week 18 on a schedule no NBA team plays (found by the slate equivalence
    # test against 2025-26).
    real = (
        f"SELECT b.team FROM box_scores b JOIN game_links g USING (sleeper_game_id)"
        f" WHERE b.season = ? AND b.sleeper_id = ? AND b.team IS NOT NULL"
        f"   AND COALESCE(g.is_exhibition, 0) = 0 AND {FINAL_FIXTURE}"
    )
    row = conn.execute(
        real + " AND b.game_date <= ? ORDER BY b.game_date DESC LIMIT 1",
        (season, sleeper_id, date_of(known_through)),
    ).fetchone()
    if row is None:
        # No game yet: his first game's team. That is hindsight about roster
        # membership only — no outcome is read — and on a live morning
        # `players.team` above answers first.
        row = conn.execute(real + " ORDER BY b.game_date LIMIT 1", (season, sleeper_id)).fetchone()
    return row[0] if row else None


def week_slate(
    conn: sqlite3.Connection,
    panel: SeasonPanel,
    scores: np.ndarray,
    season: str,
    week: int,
    sleeper_ids: list[str],
    known_through: int,
    *,
    live: bool = False,
) -> WeekSlate:
    """Every starter's games this week, as of the morning after ``known_through``.

    Nothing after the cutoff is read from the panel, so no future score can
    leak into the result — the future half is built from a schedule that has
    no scores in it.
    """
    slate = WeekSlate()
    opening = calendar.opening_night(conn, season)
    if opening is None:
        # No NBA schedule (`--skip-nba`): the box scores are all there is, and
        # only a past season's are complete. Blank the future, as before.
        for pid in sleeper_ids:
            games = player_games(panel, scores, pid, week)
            if games:
                slate.games[pid] = [
                    g if g.day <= known_through else Game(g.index, g.day, False, 0.0) for g in games
                ]
        slate.warnings.append(("", "no NBA schedule ingested; future games read from box scores"))
        return slate

    monday, sunday = calendar.week_bounds(week, opening)
    cutoff = date_of(known_through)
    for pid in sleeper_ids:
        observed = [g for g in player_games(panel, scores, pid, week) if g.day <= known_through]
        team = team_of(conn, season, pid, known_through, live=live)
        scheduled: list[tuple[int, str | None]] = []
        if team is not None:
            for row in conn.execute(
                "SELECT game_date, tipoff_utc FROM nba_schedule"
                " WHERE season = ? AND ? IN (home_team, away_team)"
                "   AND game_date > ? AND game_date BETWEEN ? AND ?"
                " ORDER BY game_date",
                (season, team, cutoff, monday.isoformat(), sunday.isoformat()),
            ):
                scheduled.append((day_index(row[0]), row[1]))

        games = list(observed)
        # Continue the panel's numbering — a game's index is its position in
        # his season, and greedy thresholds are keyed by it. Numbering from the
        # week's length could collide with an observed game's index.
        hist = panel.histories.get(pid)
        index = int((hist.day <= known_through).sum()) if hist is not None else 0
        for day, tip in scheduled:
            games.append(Game(index=index, day=day, played=False, score=0.0))
            index += 1
            if tip:
                slate.tipoff[(pid, day)] = tip
        if games:
            slate.games[pid] = games

        for row in conn.execute(
            "SELECT DISTINCT b.game_date FROM box_scores b"
            "  JOIN game_links g ON g.sleeper_game_id = b.sleeper_game_id"
            " WHERE b.season = ? AND b.sleeper_id = ? AND b.fantasy_week = ?"
            "   AND b.game_date <= ? AND g.state IN ('in_progress', 'unknown')",
            (season, pid, week, cutoff),
        ):
            slate.unsettled.append((pid, row[0]))
        # Last night by the NBA's calendar, with no final result for him at all:
        # an ingest that died before fetching it leaves no fixture to call
        # unknown, so the schedule has to be the one to notice. Last night only —
        # further back a trade legitimately puts games on his new team's
        # calendar that he was never part of.
        if team is not None and monday.isoformat() <= cutoff:
            due = conn.execute(
                "SELECT 1 FROM nba_schedule WHERE season = ? AND ? IN (home_team, away_team)"
                "   AND game_date = ?",
                (season, team, cutoff),
            ).fetchone()
            seen = any(g.day == known_through for g in observed)
            # And only for someone already on that team: a player signed today
            # has no row for last night because he was not there for it.
            belongs = conn.execute(
                "SELECT 1 FROM box_scores WHERE season = ? AND sleeper_id = ? AND team = ?"
                "   AND game_date < ? LIMIT 1",
                (season, pid, team, cutoff),
            ).fetchone()
            if due and belongs and not seen and (pid, cutoff) not in slate.unsettled:
                slate.unsettled.append((pid, cutoff))

        forward = {
            day_index(r[0])
            for r in conn.execute(
                "SELECT DISTINCT b.game_date FROM box_scores b"
                "  JOIN game_links g ON g.sleeper_game_id = b.sleeper_game_id"
                " WHERE b.season = ? AND b.sleeper_id = ? AND b.fantasy_week = ?"
                "   AND b.game_date > ? AND COALESCE(g.state, '') NOT IN ('postponed')"
                "   AND COALESCE(g.is_exhibition, 0) = 0",
                (season, pid, week, cutoff),
            )
        }
        if forward and forward != {d for d, _ in scheduled}:
            slate.warnings.append(
                (
                    pid,
                    f"Sleeper lists his games on {', '.join(date_of(d) for d in sorted(forward))},"
                    f" the NBA schedule on {', '.join(date_of(d) for d, _ in scheduled) or 'none'}"
                    " — a trade or a move one source has not caught up with",
                )
            )
    return slate
