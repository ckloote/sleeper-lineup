# sleeper-lineup

Lock-in lineup assistant for a Sleeper NBA fantasy team.

Sleeper's Lock-In mode counts exactly one game per player per week. After a player's
game finishes you may **lock** that score — irreversibly, and only before his next game
tips. If you never lock, his **final game of the week** counts, including 0.0 if he
doesn't play. This tool decides, once a day: who starts tonight, who to lock, and — for
the days you don't check in — the score each player would have to clear for locking to be
correct.

The Sleeper API is read-only, so nothing here acts on your behalf. Every recommendation
is executed by hand in the app.

- [`docs/sleeper-lockin-engine-architecture.md`](docs/sleeper-lockin-engine-architecture.md) — the design
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — the build plan and what was verified against the live API

## Status

**Phase 0 complete.** Ingest, storage and reconciliation, validated against the full
2025-26 season. Phases 1-2 (scoring engine, retrospective lock inference) are next;
3-6 are deferred.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) — manages the Python interpreter, the virtualenv and
  all dependencies
- Network access to `api.sleeper.app` and `stats.nba.com`

Nothing else. Do not install into a system Python.

> If you are checking whether an NBA host is reachable, test with **Python**, not `curl`.
> Akamai rejects curl's TLS fingerprint, so `curl` hangs or returns `403 Access Denied`
> from a network where `requests` works fine.

## Installation

```bash
git clone git@github.com:ckloote/sleeper-lineup.git
cd sleeper-lineup

uv python install 3.12      # pinned interpreter, not the system one
uv sync --frozen            # exact dependency set from uv.lock
uv sync --group dev         # adds pytest and ruff
```

`uv sync --frozen` resolves to a byte-identical dependency set everywhere, which is the
point: a stopping policy that behaves differently across machines is not debuggable.

Run everything through `uv run` — never activate the venv by hand:

```bash
uv run lockin --help
```

### Adding a dependency

```bash
uv add <package>                 # runtime
uv add --group dev <package>     # tooling
```

Commit the resulting `uv.lock`. Never `pip install`.

## Usage

### `lockin ingest`

Fetches league settings, rosters, players, every week of matchups and per-game box
scores, plus the NBA schedule and tipoff times. Writes to SQLite.

```bash
uv run lockin ingest                     # everything, all 25 weeks
uv run lockin ingest --weeks 12          # one week
uv run lockin ingest --weeks 1-10,22-25  # ranges and lists
uv run lockin ingest --full              # also refetch the ~2.5MB player reference
uv run lockin ingest --skip-tipoffs      # skip the slow per-date scoreboard sweep
uv run lockin ingest --skip-nba          # Sleeper only
```

A full run takes a few minutes, most of it the tipoff sweep. Re-runs are cheaper: the
sweep only visits dates still missing something. The database is disposable — delete it
and re-ingest at any time.

### `lockin reconcile`

Checks ingest completeness and exits nonzero if a gate fails. Run it after every ingest.

```bash
uv run lockin reconcile
uv run lockin reconcile --json
```

```
[PASS] all 25 fantasy weeks ingested
[PASS] all 25 weeks of matchups ingested
[PASS] every rostered player resolves in the player table
[PASS] every started player-week has box-score rows      1500/1500
[PASS] played fixtures link to NBA schedule (>=99%)      1231/1231 (100.00%)
[PASS] postponed fixtures agree between Sleeper and NBA  3 postponed, 0 disagreeing
[PASS] non-NBA fixtures identified and excluded          1 exhibition fixture
[PASS] tipoff times present                              1231/1231
```

Finding one exhibition fixture is the **correct** result — it's the All-Star Game. The
check exists so that a new kind of non-NBA fixture surfaces for a human rather than being
silently scored.

Commands arriving with later phases: `digest`, `explain`, `backtest`, `verify`.

## Configuration

Environment variables, all optional:

| Variable | Default | Notes |
|---|---|---|
| `LOCKIN_DB` | `data/lockin.db` | SQLite path |
| `LOCKIN_LEAGUE_ID` | `1283214955830575104` | The 2025-26 league |
| `LOCKIN_SEASON` | `2025` | Sleeper labels 2025-26 as `2025` |
| `LOCKIN_USER_ID` | `1283460931447164928` | |

The default league is `status: complete`. **The 2026-27 league will have a different
id** — Sleeper mints a new one at rollover — so set `LOCKIN_LEAGUE_ID` and
`LOCKIN_SEASON` when the new season starts rather than relying on the defaults.

## Development

```bash
uv run pytest              # 45 tests, <1s
uv run ruff check lockin/ tests/
uv run ruff format lockin/ tests/
```

Tests in `tests/test_season_invariants.py` run against the real ingested database and
skip automatically when it is absent. They pin the data semantics below, so that a change
to ingest — or upstream — cannot quietly undo them.

## What the data actually does

Established by inspecting the 2025-26 season, not assumed. Each cost real points to get
wrong, and each is enforced by a test.

**Box scores come from Sleeper, keyed by `sleeper_id`.** `GET /stats/nba/{season}/{week}`
returns one row per player per scheduled game with the full component line, including the
OREB/DREB split. This removes the player ID crosswalk the architecture doc identified as
the project's top risk. `nba_api` supplies only the schedule and tipoff times, which
Sleeper's date-only rows lack.

**Triple-doubles stack.** A triple-double pays `dd + td` = 30, not 20. Confirmed against
recorded scores; the architecture doc left this open.

**The All-Star Game is published but does not count.** It appears as an ordinary fixture
with real stat lines (`STP` vs `STR`) and falls at the *end* of a fantasy week, so a
naive reading makes it every All-Star's final game. Of 15 rostered participants in week
17, not one counted it. Treating it as real would bank far too eagerly before the break.

**The NBA Cup final does count.** Opposite conclusion, and it needs care: it is not a
regular-season game, so `LeagueGameFinder` omits it, and it was the only game on its
date. Two managers locked on it. It is backfilled from the scoreboard.

**Postponed is not the same as DNP.** Sleeper keeps the original fixture with every
player unplayed. An unplayed *real* game scores 0.0 for an unlocked starter; a postponed
fixture is excluded and the prior game counts. Conflating them mis-scores the end of a
week.

**`points` sums the six starter slots only.** `players_points` is populated for bench
players too, and a bench player's value can freeze mid-week without a lock having
happened. Lock inference reads `starters`/`starters_points`.

**`/players/nba` is a live snapshot with no history.** Today's positions and team are not
January's. Anything reconstructing a past week reads `box_scores.pit_positions` and
`pit_team`, captured per game.

**`matchup_id` is nullable.** Teams eliminated from the playoff bracket have no matchup
in weeks 23-24, and week 25 is unscored entirely.

**There are 25 fantasy weeks, not 24.** Weeks 1-21 regular season, 22-24 playoffs, 25
unscored.

## Layout

```
lockin/
  config.py        league, season, db path
  core/            pure — no network, no database, no clock (arrives in Phase 1)
  ingest/
    sleeper.py     league, rosters, players, matchups, box scores
    nba.py         schedule, tipoff times, exhibition detection
    validate.py    shape assertions; raises on drift, never warns
  store/
    schema.sql     the contract between ingest and every reader
    db.py          single-writer SQLite
  reconcile.py     the Phase 0 gates
  cli.py
tests/
docs/
```

Two rules hold the design together: `core/` stays pure so the stopping policy can be
tested without I/O, and SQLite is the contract, so a dashboard is just a second reader
needing no changes to `core/`.
