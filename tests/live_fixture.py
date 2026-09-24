"""A synthetic, unfinished season, served through the real ingest code.

Every test that reads `data/lockin-2025.db` reads a season that is over. The
live paths never see one: on any morning they run, some games are final, some
are tonight, a player has been dropped since the last poll, and a fixture may be
postponed. The recorded database cannot represent any of that, which is how
the 2026-09-23 review found the digest ready for a completed season and broken
for an unfinished one.

`SyntheticSeason` is the unfinished one. It is a model of both upstreams —
Sleeper's league, roster, matchup, player and stat endpoints, and the NBA's
schedule — driven by a clock (`through`, the last night whose games are final).
`FakeSleeperClient` and `FakeNbaFeed` serve it to `lockin.ingest.run.run_ingest`
exactly as the real clients would, so a test exercises the same code the cron
does, from the payload onwards, into a fresh database in `tmp_path`.

Deterministic: every stat line is seeded from its (game, player) pair, so a
season built twice is byte-identical and a test can compute what a counted
score should be.

What Sleeper does mid-week is partly a hypothesis until the season starts, and
the fixture makes each assumption a switch rather than a fact:

``forward_rows``
    Sleeper publishes stat rows for games not yet played (§7.5). Default off:
    the live path must not depend on it.
``sparse_points``
    A starter with no game yet is absent from ``players_points``. Default on.
"""

from __future__ import annotations

import itertools
import sqlite3
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from lockin.config import Config
from lockin.core.scoring import line_from_stats, score_line
from lockin.ingest import run as ingest_run
from lockin.store.db import session

# The 2025-26 league's scoring, verbatim. The engine reads weights from the
# league payload and never hardcodes them; the fixture has to supply real ones
# or it would be testing a different game.
SCORING = {
    "ast": 1.0,
    "blk": 2.0,
    "bonus_pt_40p": 0.0,
    "bonus_pt_50p": 0.0,
    "dd": 10.0,
    "dreb": 1.0,
    "ff": -2.0,
    "fgm": 0.5,
    "fgmi": -1.0,
    "ftm": 1.0,
    "ftmi": -1.0,
    "oreb": 1.5,
    "pts": 1.0,
    "reb": 0.0,
    "stl": 2.0,
    "td": 20.0,
    "tf": -3.0,
    "to": -1.0,
    "tpa": 0.0,
    "tpm": 2.0,
    "tpmi": 0.0,
}
ROSTER_POSITIONS = ["PG", "G", "F", "C", "UTIL", "UTIL"] + ["BN"] * 6
TEAMS = (
    "ATL BKN BOS CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM "
    "MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS"
).split()
POSITIONS = (["PG", "G"], ["SG", "G"], ["SF", "F"], ["PF", "F"], ["C"], ["SG", "SF", "G", "F"])

LEAGUE_ID = "900000000000000026"
PREVIOUS_LEAGUE_ID = "1283214955830575104"
SEASON = "2026"
OPENING = date(2026, 10, 20)  # a Tuesday, as the real 2026-27 opener is

STATUS_SCHEDULED, STATUS_LIVE, STATUS_FINAL = 1, 2, 3


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_of(d: date, opening: date = OPENING) -> int:
    """Sleeper's fantasy week: Monday-to-Sunday, week 1 holding opening night."""
    return (d - monday_of(opening)).days // 7 + 1


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode())


@dataclass
class Player:
    sleeper_id: str
    name: str
    positions: list[str]
    team: str
    minutes: float
    dnp_rate: float = 0.08
    injury_status: str | None = None
    joined: date | None = None
    """First date he is on his team's books; no stat rows before it."""


@dataclass
class Fixture:
    """One NBA game, as both upstreams see it.

    ``sleeper_game_id`` is the id in Sleeper's stat rows. A postponed fixture
    keeps its Sleeper row on the original ``sleeper_date`` while the NBA lists
    the game only on ``date``, the day it is actually played — the shape all
    three of 2025-26's postponements had.
    """

    nba_game_id: str
    sleeper_game_id: str
    date: date
    home: str
    away: str
    status: int = STATUS_SCHEDULED
    sleeper_date: date | None = None

    @property
    def tipoff_utc(self) -> str:
        return f"{self.date.isoformat()}T23:30:00Z"

    def teams(self) -> tuple[str, str]:
        return self.home, self.away


@dataclass
class SyntheticSeason:
    n_rosters: int = 4
    per_roster: int = 8
    fillers_per_team: int = 2
    """Unrostered players on every team. Sleeper's feed carries the whole league,
    so a game that happened always has somebody's stat line; without these a
    game whose one rostered player sat would look postponed."""
    days: int = 42
    seed: int = 0
    through: date | None = None
    """The last night whose games are final. None: nothing has been played."""
    forward_rows: bool = False
    sparse_points: bool = True
    status: str = "in_season"
    season: str = SEASON
    league_id: str = LEAGUE_ID

    players: dict[str, Player] = field(default_factory=dict)
    rosters: dict[int, list[str]] = field(default_factory=dict)
    lineups: dict[tuple[int, int], list[str]] = field(default_factory=dict)
    fixtures: list[Fixture] = field(default_factory=list)
    locks: dict[tuple[int, int, str], date] = field(default_factory=dict)
    dnp: set[tuple[str, str]] = field(default_factory=set)
    """(sleeper_game_id, sleeper_id) pairs forced to a DNP."""
    settings: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        rng = np.random.default_rng(self.seed)
        n = 0
        for roster_id in range(1, self.n_rosters + 1):
            ids = []
            for k in range(self.per_roster):
                n += 1
                sid = str(1000 + n)
                self.players[sid] = Player(
                    sleeper_id=sid,
                    name=f"Player {sid}",
                    positions=list(POSITIONS[k % len(POSITIONS)]),
                    team=TEAMS[(n * 7) % len(TEAMS)],
                    minutes=float(rng.uniform(24, 36)),
                )
                ids.append(sid)
            self.rosters[roster_id] = ids
        for t, team in enumerate(TEAMS):
            for k in range(self.fillers_per_team):
                sid = str(5000 + t * 10 + k)
                self.players[sid] = Player(
                    sleeper_id=sid,
                    name=f"Filler {sid}",
                    positions=list(POSITIONS[k % len(POSITIONS)]),
                    team=team,
                    minutes=20.0,
                    dnp_rate=0.0,
                )
        self._schedule(rng)

    # ------------------------------------------------------------ schedule

    def _schedule(self, rng: np.random.Generator) -> None:
        """Roughly 3.3 games per team per week, two on opening night."""
        gid = 0
        for offset in range(self.days):
            day = OPENING + timedelta(days=offset)
            k = 2 if offset == 0 else int(rng.integers(5, 10))
            order = list(rng.permutation(TEAMS))
            for i in range(k):
                gid += 1
                home, away = order[2 * i], order[2 * i + 1]
                self.fixtures.append(
                    Fixture(
                        nba_game_id=f"00226{gid:05d}",
                        sleeper_game_id=f"77{gid:08d}",
                        date=day,
                        home=home,
                        away=away,
                    )
                )

    def fixture(self, sleeper_game_id: str) -> Fixture:
        return next(f for f in self.fixtures if f.sleeper_game_id == sleeper_game_id)

    def games_for(self, team: str, week: int | None = None) -> list[Fixture]:
        out = [f for f in self.fixtures if team in f.teams()]
        if week is not None:
            out = [f for f in out if week_of(f.sleeper_date or f.date) == week]
        return sorted(out, key=lambda f: f.sleeper_date or f.date)

    def postpone(self, sleeper_game_id: str, to: date) -> Fixture:
        """Move a game. Sleeper keeps the original fixture row; the NBA moves it."""
        original = self.fixture(sleeper_game_id)
        original.sleeper_date = original.date
        original.date = to
        replay = Fixture(
            nba_game_id=original.nba_game_id,
            sleeper_game_id=f"{sleeper_game_id}9",
            date=to,
            home=original.home,
            away=original.away,
        )
        self.fixtures.append(replay)
        return replay

    # --------------------------------------------------------------- clock

    def play_through(self, day: date) -> None:
        """Every game on or before ``day`` is final; later ones are scheduled."""
        self.through = day
        for f in self.fixtures:
            if f.sleeper_date is not None:
                continue  # the postponed original is never played under its old id
            f.status = STATUS_FINAL if f.date <= day else STATUS_SCHEDULED

    @property
    def today(self) -> date:
        """The morning after the last final night."""
        return (self.through or OPENING - timedelta(days=1)) + timedelta(days=1)

    def is_final(self, f: Fixture) -> bool:
        return f.sleeper_date is None and f.status == STATUS_FINAL

    # ---------------------------------------------------------------- stats

    def stats(self, f: Fixture, sleeper_id: str) -> dict:
        """The stat line Sleeper would publish; empty for a DNP or an unplayed game."""
        p = self.players[sleeper_id]
        if not self.is_final(f):
            return {}
        rng = np.random.default_rng(_seed(f.sleeper_game_id, sleeper_id, self.seed))
        if (f.sleeper_game_id, sleeper_id) in self.dnp or rng.random() < p.dnp_rate:
            return {}
        minutes = float(np.clip(rng.normal(p.minutes, 4.0), 8.0, 44.0))
        fga = int(rng.poisson(minutes * 0.42))
        fgm = int(rng.binomial(fga, 0.47))
        tpa = min(fga, int(rng.poisson(fga * 0.35)))
        tpm = min(fgm, int(rng.binomial(tpa, 0.36)))
        fta = int(rng.poisson(minutes * 0.12))
        ftm = int(rng.binomial(fta, 0.78))
        oreb, dreb = int(rng.poisson(minutes * 0.04)), int(rng.poisson(minutes * 0.16))
        line = {
            "sp": round(minutes * 60.0),
            "pts": 2 * (fgm - tpm) + 3 * tpm + ftm,
            "fga": fga,
            "fgm": fgm,
            "fgmi": fga - fgm,
            "tpa": tpa,
            "tpm": tpm,
            "tpmi": tpa - tpm,
            "fta": fta,
            "ftm": ftm,
            "ftmi": fta - ftm,
            "oreb": oreb,
            "dreb": dreb,
            "reb": oreb + dreb,
            "ast": int(rng.poisson(minutes * 0.12)),
            "stl": int(rng.poisson(minutes * 0.03)),
            "blk": int(rng.poisson(minutes * 0.02)),
            "to": int(rng.poisson(minutes * 0.05)),
            "pf": int(rng.poisson(minutes * 0.06)),
        }
        doubles = sum(line[k] >= 10 for k in ("pts", "reb", "ast", "stl", "blk"))
        # Sleeper omits zero-valued keys, and dd/td arrive only when earned.
        if doubles >= 2:
            line["dd"] = 1
        if doubles >= 3:
            line["td"] = 1
        return {k: float(v) for k, v in line.items() if v}

    def score(self, f: Fixture, sleeper_id: str) -> float:
        stats = self.stats(f, sleeper_id)
        return score_line(line_from_stats(stats), SCORING) if stats else 0.0

    def played(self, f: Fixture, sleeper_id: str) -> bool:
        return bool(self.stats(f, sleeper_id))

    # ----------------------------------------------------------- lineups

    def roster_of(self, sleeper_id: str) -> int | None:
        return next((r for r, ids in self.rosters.items() if sleeper_id in ids), None)

    def drop_add(
        self, roster_id: int, drop: str, *, team: str | None = None, joined: date | None = None
    ) -> str:
        """Replace a rostered player with a new one, in the same lineup spot.

        ``joined`` makes him a new signing, with no stat rows before that date.
        """
        dropped = self.players[drop]
        sid = str(4000 + len(self.players))
        self.players[sid] = Player(
            sleeper_id=sid,
            name=f"Player {sid}",
            positions=list(dropped.positions),
            team=team or dropped.team,
            minutes=dropped.minutes,
            joined=joined,
        )
        ids = self.rosters[roster_id]
        ids[ids.index(drop)] = sid
        for key, lineup in self.lineups.items():
            if key[1] == roster_id and drop in lineup:
                lineup[lineup.index(drop)] = sid
        return sid

    def starters(self, week: int, roster_id: int) -> list[str]:
        return self.lineups.get((week, roster_id), self.rosters[roster_id][:6])

    def countable(self, sleeper_id: str, week: int) -> list[Fixture]:
        """The player's fixtures that count this week, as of now, in date order.

        Postponed originals are excluded — they never happened. Games not yet
        final are excluded too: this is what the counted value can see.
        """
        p = self.players[sleeper_id]
        return [
            f
            for f in self.games_for(p.team, week)
            if self.is_final(f) and (p.joined is None or f.date >= p.joined)
        ]

    def counted(self, week: int, roster_id: int, sleeper_id: str) -> float | None:
        """What Sleeper's ``players_points`` would say right now.

        Locked: the locked game. Otherwise the latest final game counts, 0.0 if
        he sat it — the live reading of "the final game counts". None when he
        has no final game yet this week.
        """
        games = self.countable(sleeper_id, week)
        locked_on = self.locks.get((week, roster_id, sleeper_id))
        if locked_on is not None:
            hit = [f for f in games if f.date == locked_on]
            if hit:
                return self.score(hit[0], sleeper_id)
        if not games:
            return None
        return self.score(games[-1], sleeper_id)

    # ---------------------------------------------------------- payloads

    def league_payload(self) -> dict:
        leg = min(max(week_of(self.today), 1), 25)
        settings = {
            "leg": leg,
            "last_scored_leg": leg - 1 if leg > 1 else None,
            "start_week": 1,
            "playoff_week_start": 22,
            "playoff_teams": 6,
        }
        settings.update(self.settings)
        return {
            "league_id": self.league_id,
            "previous_league_id": PREVIOUS_LEAGUE_ID,
            "season": self.season,
            "status": self.status,
            "scoring_settings": dict(SCORING),
            "roster_positions": list(ROSTER_POSITIONS),
            "settings": settings,
            "total_rosters": self.n_rosters,
        }

    def rosters_payload(self) -> list[dict]:
        return [
            {"roster_id": r, "owner_id": f"u{r}", "players": list(ids)}
            for r, ids in self.rosters.items()
        ]

    def users_payload(self) -> list[dict]:
        return [{"user_id": f"u{r}", "display_name": f"manager{r}"} for r in self.rosters]

    def players_payload(self) -> dict:
        out = {}
        for sid, p in self.players.items():
            first, _, last = p.name.partition(" ")
            out[sid] = {
                "full_name": p.name,
                "first_name": first,
                "last_name": last,
                "fantasy_positions": list(p.positions),
                "team": p.team,
                "status": "Active",
                "injury_status": p.injury_status,
            }
        return out

    def matchups_payload(self, week: int) -> list[dict]:
        out = []
        for roster_id, ids in self.rosters.items():
            starters = self.starters(week, roster_id)
            points = {}
            for sid in ids:
                value = self.counted(week, roster_id, sid)
                if value is None and self.sparse_points:
                    continue
                points[sid] = value if value is not None else 0.0
            starters_points = [points.get(sid, 0.0) for sid in starters]
            out.append(
                {
                    "roster_id": roster_id,
                    "matchup_id": (roster_id + 1) // 2,
                    "players": list(ids),
                    "starters": list(starters),
                    "starters_points": starters_points,
                    "players_points": points,
                    "points": round(sum(starters_points), 2),
                    "custom_points": None,
                }
            )
        return out

    def stat_rows(self, week: int) -> list[dict]:
        rows = []
        for f in self.fixtures:
            listed_on = f.sleeper_date or f.date
            if week_of(listed_on) != week:
                continue
            passed = self.through is not None and listed_on <= self.through
            if not (self.is_final(f) or self.forward_rows or (f.sleeper_date and passed)):
                # Not yet played, and Sleeper does not publish ahead. A postponed
                # original is listed once its date passes, every player unplayed.
                continue
            for sid, p in self.players.items():
                if p.team not in f.teams() or (p.joined and listed_on < p.joined):
                    continue
                opponent = f.away if p.team == f.home else f.home
                rows.append(
                    {
                        "player_id": sid,
                        "game_id": f.sleeper_game_id,
                        "date": listed_on.isoformat(),
                        "week": week,
                        "season": self.season,
                        "season_type": "regular",
                        "team": p.team,
                        "opponent": opponent,
                        "stats": self.stats(f, sid),
                        "player": {"fantasy_positions": list(p.positions), "team": p.team},
                        "status": None,
                    }
                )
        return rows

    def schedule_payload(self) -> dict:
        by_date: dict[date, list[dict]] = {}
        for f in self.fixtures:
            if f.sleeper_date is not None:
                continue  # the NBA lists a moved game only on its new date
            by_date.setdefault(f.date, []).append(
                {
                    "gameId": f.nba_game_id,
                    "gameDateEst": f"{f.date.isoformat()}T00:00:00Z",
                    "gameDateTimeUTC": f.tipoff_utc,
                    "gameStatus": f.status,
                    "homeTeam": {"teamTricode": f.home},
                    "awayTeam": {"teamTricode": f.away},
                }
            )
        return {
            "leagueSchedule": {
                "gameDates": [
                    {"gameDate": d.isoformat(), "games": games}
                    for d, games in sorted(by_date.items())
                ]
            }
        }


class FakeSleeperClient:
    """`lockin.ingest.sleeper.SleeperClient`'s interface, served from a season."""

    def __init__(self, season: SyntheticSeason) -> None:
        self.season = season
        self.urls: list[str] = []

    def get(self, url: str):
        self.urls.append(url)
        if url.endswith("/users"):
            return self.season.users_payload()
        if url.endswith("/rosters"):
            return self.season.rosters_payload()
        raise AssertionError(f"unexpected GET {url}")

    def league(self, league_id: str) -> dict:
        self.urls.append(f"league/{league_id}")
        return self.season.league_payload()

    def rosters(self, league_id: str) -> list:
        return self.season.rosters_payload()

    def matchups(self, league_id: str, week: int) -> list:
        self.urls.append(f"matchups/{week}")
        return self.season.matchups_payload(week)

    def players(self) -> dict:
        return self.season.players_payload()

    def week_stats(self, season: str, week: int, season_type: str = "regular") -> list:
        self.urls.append(f"stats/{week}")
        return self.season.stat_rows(week)


class FakeNbaFeed:
    """`lockin.ingest.nba.NbaFeed`, served from a season."""

    def __init__(self, season: SyntheticSeason) -> None:
        self.season = season

    def schedule(self, season_label: str) -> dict:
        return self.season.schedule_payload()

    def scoreboard(self, game_date: str) -> list[dict]:
        day = date.fromisoformat(game_date)
        return [
            {
                "gameId": f.nba_game_id,
                "gameTimeUTC": f.tipoff_utc,
                "gameStatus": f.status,
                "homeTeam": {"teamTricode": f.home},
                "awayTeam": {"teamTricode": f.away},
            }
            for f in self.season.fixtures
            if f.sleeper_date is None and f.date == day
        ]


def config_for(tmp_path: Path, synthetic: SyntheticSeason, **overrides) -> Config:
    values = {
        "league_id": synthetic.league_id,
        "season": synthetic.season,
        "user_id": "u1",
        "db_path": tmp_path / f"lockin-{synthetic.season}.db",
        "timezone": "America/New_York",
        "snapshot_root": tmp_path / "snapshots",
    }
    values.update(overrides)
    return Config(**values)


_TICK = itertools.count()


def ingest(
    season: SyntheticSeason,
    cfg: Config,
    *,
    weeks: Iterable[int] | None = None,
    skip_nba: bool = False,
    at: str | None = None,
) -> list[str]:
    """One `lockin ingest` run against the synthetic upstreams. Returns its output.

    Observations are stamped at ``at`` — by default 10:30 UTC on the season's
    ``today``, when the cron ingest runs — not at the wall clock, so a poll
    belongs to the synthetic morning it describes. A microsecond counter keeps
    two runs on one morning distinct and in order.
    """
    base = at or f"{season.today.isoformat()}T10:30:00"

    def stamped() -> str:
        return f"{base}.{next(_TICK):06d}+00:00"

    lines: list[str] = []
    with (
        mock.patch("lockin.ingest.sleeper.now_iso", stamped),
        mock.patch("lockin.store.runs.now_iso", stamped),
        mock.patch("lockin.store.db.now_iso", stamped),
        session(cfg.db_path, create=True) as conn,
    ):
        ingest_run.run_ingest(
            conn,
            cfg,
            client=FakeSleeperClient(season),
            weeks=None if weeks is None else list(weeks),
            skip_nba=skip_nba,
            feed=FakeNbaFeed(season),
            echo=lines.append,
            today=season.today.isoformat(),
        )
    return lines


def connect(cfg: Config) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return conn
