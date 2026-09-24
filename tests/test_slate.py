"""Games still to come are read from the NBA schedule (review finding 1).

Two kinds of evidence. Against the recorded 2025-26 season, the schedule-built
slate must reproduce the box-score panel's future games for every starter-week
— wherever the two can be expected to agree. Against the synthetic season, it
must work where the panel cannot: Sleeper publishing no rows for unplayed games,
a fixture whose stats are missing, and a schedule the two sources disagree on.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from live_fixture import OPENING, STATUS_FINAL, SyntheticSeason, config_for, connect, ingest

from lockin import calendar
from lockin import digest as digest_mod
from lockin.backtest import player_games
from lockin.projections import date_of, day_index
from lockin.slate import team_of, week_slate
from lockin.store.db import apply_schema

# ------------------------------------------------------- the recorded season


@pytest.fixture(scope="module")
def season_ctx(season_db):
    conn = sqlite3.connect(season_db)
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    yield conn, digest_mod.load_context(conn, "2025")
    conn.close()


def _traded(conn, season, pid, known_through, week_end):
    """Did his team change between the cutoff and the end of the week?

    A replay schedules the rest of a player's week with the team he had at the
    cutoff — it cannot know about a trade before it happened — so across a trade
    the two sources legitimately disagree.
    """
    was = team_of(conn, season, pid, known_through, live=False)
    later = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT team FROM box_scores WHERE season = ? AND sleeper_id = ?"
            "   AND game_date > ? AND game_date <= ?",
            (season, pid, date_of(known_through), week_end),
        )
    }
    return bool(later - {was})


def test_the_schedule_reproduces_every_future_game_the_panel_knew(season_ctx):
    """4,500 starter-week-mornings; every disagreement must be a trade."""
    conn, ctx = season_ctx
    opening = calendar.opening_night(conn, "2025")
    checked, unexplained, trades = 0, [], 0
    for (week, roster), starters in sorted(ctx.lineups.items()):
        monday, sunday = calendar.week_bounds(week, opening)
        for offset in (0, 2, 4):
            kt = day_index((monday + timedelta(days=offset)).isoformat()) - 1
            slate = week_slate(conn, ctx.panel, ctx.scores, "2025", week, starters, kt)
            for pid in starters:
                checked += 1
                panel = [
                    g.day for g in player_games(ctx.panel, ctx.scores, pid, week) if g.day > kt
                ]
                sched = [g.day for g in slate.games.get(pid, []) if g.day > kt]
                if panel == sched:
                    continue
                if _traded(conn, "2025", pid, kt, sunday.isoformat()):
                    trades += 1
                    continue
                unexplained.append((week, roster, pid, date_of(kt), panel, sched))
    assert checked == 4500
    assert not unexplained, unexplained[:5]
    assert trades < 50, "trades should be the rare exception, not the rule"


def test_resolve_week_matches_every_recorded_row(season_ctx):
    """The Monday-to-Sunday rule, against all 26,665 played games and their DNPs."""
    conn, _ = season_ctx
    opening = calendar.opening_night(conn, "2025")
    rows = conn.execute("SELECT game_date, fantasy_week FROM box_scores WHERE season = '2025'")
    misses = [(d, w) for d, w in rows if calendar.week_of(date.fromisoformat(d), opening) != w]
    assert misses == []


# ------------------------------------------------------ the unfinished season


def _built(tmp_path, **kwargs):
    season = SyntheticSeason(**kwargs)
    season.play_through(OPENING + timedelta(days=8))  # Wednesday of week 2
    cfg = config_for(tmp_path, season)
    ingest(season, cfg, weeks=[2])
    return season, cfg


def _slate(cfg, season, starters, *, live=True):
    conn = connect(cfg)
    ctx = digest_mod.load_context(conn, season.season)
    kt = day_index(season.through.isoformat())
    return week_slate(conn, ctx.panel, ctx.scores, season.season, 2, starters, kt, live=live)


def test_the_rest_of_the_week_needs_no_stat_rows_for_it(tmp_path):
    """§7.5 no longer matters: Sleeper publishes nothing ahead, the week is still whole."""
    season, cfg = _built(tmp_path, forward_rows=False)
    starters = season.starters(2, 1)

    slate = _slate(cfg, season, starters)

    for pid in starters:
        expected = [f.date.isoformat() for f in season.games_for(season.players[pid].team, 2)]
        got = [date_of(g.day) for g in slate.games[pid]]
        assert got == expected
        future = [g for g in slate.games[pid] if g.day > day_index(season.through.isoformat())]
        assert all(not g.played and g.score == 0.0 for g in future)
        assert all((pid, g.day) in slate.tipoff for g in future), "every scheduled game tips"


def test_a_fixture_missing_its_stats_is_unsettled_not_a_dnp(tmp_path):
    """NBA final, Sleeper blank: the panel cannot see it, so the slate must say so."""
    season = SyntheticSeason(forward_rows=True)
    season.play_through(OPENING + timedelta(days=8))
    pid = season.starters(2, 1)[0]
    last = [f for f in season.games_for(season.players[pid].team, 2) if f.date <= season.through]
    assert last, "the fixture needs a game this week before the cutoff"
    blanked = last[-1]
    cfg = config_for(tmp_path, season)
    real_stats = season.stats
    season.stats = lambda f, sid: {} if f is blanked else real_stats(f, sid)
    assert blanked.status == STATUS_FINAL
    ingest(season, cfg, weeks=[2])

    slate = _slate(cfg, season, [pid])

    assert (pid, blanked.date.isoformat()) in slate.unsettled
    with connect(cfg) as conn:
        state = conn.execute(
            "SELECT state FROM game_links WHERE sleeper_game_id = ?", (blanked.sleeper_game_id,)
        ).fetchone()[0]
    assert state == "unknown"


def test_a_disagreement_between_the_sources_is_reported(tmp_path):
    """A trade one source knows about and the other does not.

    Sleeper's forward rows still carry his old team's games; `players.team`
    already names the new one. Neither is silently preferred.
    """
    season, cfg = _built(tmp_path, forward_rows=True)
    pid = season.starters(2, 1)[0]
    old = season.players[pid].team
    new = next(t for t in ("BOS", "LAL", "DEN") if t != old)
    with connect(cfg) as conn:
        conn.execute("UPDATE players SET team = ? WHERE sleeper_id = ?", (new, pid))

    slate = _slate(cfg, season, [pid])

    assert any(who == pid and "Sleeper lists his games on" in w for who, w in slate.warnings), (
        slate.warnings
    )
