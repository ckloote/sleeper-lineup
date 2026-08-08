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
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — the build plan, what was verified against the live API, and a write-up per completed phase

## Status

**Phases 0-3 complete.** Ingest, the scoring engine, retrospective lock inference and the
projection layer, all validated against the full 2025-26 season. Every nonzero counted
score in all 25 weeks is reproduced from box scores to the cent, 98.2% of starter
player-weeks resolve to a specific lock decision, and projected quantiles match realised
frequencies on held-out weeks — including the right tail, which is the only part that
decides whether banking a score is correct. Phases 4-6 (simulation, rollout, digest) are
deferred.

> ⚠️ **Sleeper mutates completed-season results.** Between 2026-08-05 and 2026-08-07 the
> finished 2025-26 season changed under us: 38% of week-12 starter values and every team
> total. Box scores were byte-identical, so only *which game counts* moved. The cause is
> unresolved — week renumbering, a mechanical fallback, and a dropped DNP rule were all
> tested and ruled out.
>
> Today's data is now treated as canonical, and the backtest measures **policy against
> policy** rather than against the human baseline — four of the five policies in the
> architecture doc read only box scores, which are stable. Every ingest now preserves raw
> payloads to `snapshots/`, and `lockin reconcile` reports drift since first observation.
> See [implementation-plan.md §12](docs/implementation-plan.md).

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
sweep only visits dates still missing something.

Each run also preserves the raw matchups payload under
`snapshots/matchups/{season}/wk{NN}/`, deduplicated by content — a file appears only when
Sleeper's answer actually changes, so the directory listing is the mutation history.

**The database is disposable; `snapshots/` is not.** You can delete `data/` and re-ingest
at any time. Deleting `snapshots/` destroys the only record of what the season looked like
before Sleeper rewrote it, and there is no historical endpoint to recover it from.

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

### `lockin verify`

Proves the scoring engine against the recorded season. Exits nonzero on any mismatch.

```bash
uv run lockin verify
uv run lockin verify --json
```

```
[PASS] per-attempt economics match the architecture doc
       three: made=5.5 missed=-1.0 break-even=15.4%; two: 2.5/-1.0 28.6%; ft: 2.0/-1.0 33.3%
[PASS] component-derived dd/td matches Sleeper       26665/26665 (1919 DD, 136 TD)
[PASS] derived and recorded scoring agree            26665/26665
[PASS] every nonzero counted score reproduced        2647/2647
```

The middle two checks are the ones that matter for what comes later. The scoring engine
has two paths — score a *recorded* line where Sleeper hands you `dd`/`td` precomputed,
and score a *component* line where they must be derived at the 10/10 boundary. The
simulator will only ever have components, so if the derivation were wrong, every
simulated score would be wrong in a way no downstream test would reveal.

### `lockin locks`

Recovers every manager's lock decisions for the whole season and profiles their stopping
tendency. Writes `lock_inferences` and `manager_profiles`.

```bash
uv run lockin locks --profiles
```

```
inferred 1500 starter player-weeks, 1473 resolved

  locked_early           811
  rode_to_end            641
  ambiguous               27
  single_game             21

manager lock tendency (higher lock_rate = banks earlier)
  roster  decisions  early   rode  lock_rate  mean_pos
       3        148    100     48      67.6%      0.48
       2        148     66     82      44.6%      0.68
```

The spread is the point: roster 3 banks early, roster 2 rides to Sunday. That is a
per-manager trait the live opponent model uses to sharpen its belief about whether an
opponent's frozen score is locked or merely unplayed.

It runs across **all ten rosters**, not just yours — Phase 5 replays every roster to get
105 matchups of evaluation power instead of 21.

### `lockin calibrate`

Projects every eligible player-game from its own past and checks the predicted quantiles
against what actually happened. Exits nonzero on any gate failure.

```bash
uv run lockin calibrate
uv run lockin calibrate --draws 4000 --json
```

```
projected 12298 player-games; 5126 held out (weeks 18-25)

  PIT deciles, held out (each should be 0.100)
    0.113 0.099 0.093 0.096 0.105 0.097 0.099 0.100 0.101 0.097

[PASS] right-tail quantiles match realised frequencies, out of sample
       q0.90: 0.0964 vs 0.100 (z=-0.87); q0.95: 0.0468 vs 0.050 (z=-1.04); q0.99: 0.0105 vs 0.010 (z=+0.38)
[PASS] central quantiles match realised frequencies, out of sample
[PASS] left-tail calibration (advisory)
[PASS] calibration holds through the fantasy playoffs
[PASS] DNP hazard is calibrated and informative
[PASS] projection is sharper than the naive predictors
[PASS] projections use no data at or after as_of
```

Weeks 1-17 chose the model's handful of hyperparameters; **weeks 18-25 are held out** and
are what the gate is scored on. A stopping policy always looks excellent in-sample.

The right tail is the one that matters. The engine's whole job is deciding whether
tonight's score beats what the rest of the week might produce, and that judgement lives
entirely in the upper quantiles — a projection with a perfect mean and a 20%
understatement of P(score > 55) would bank far too eagerly, and no accuracy metric would
notice.

Two of these checks exist to stop the gate passing for the wrong reason. **Sharpness**:
a distribution wide enough to cover anything calibrates trivially and decides nothing, so
the model must also beat the player's own history and the league marginal on CRPS.
**Leakage**: perfect calibration is also what reading the future looks like, so the gate
rebuilds the panel with the future deleted and asserts the projections come out
bit-identical.

### `lockin project`

One player, one game, from data strictly before that date.

```bash
uv run lockin project 1970 --as-of 2026-03-06 --week 20
```

```
player 1970  2026-03-06  week 20
  basis        own (50 prior played games)
  P(does not play) 13.9%
  mean         51.7
  q0.50        50.5
  q0.90        96.5
  q0.99        130.5
```

`basis` says where the component draws came from — `own`, or `pooled`/`mixed` when the
player has too little history and a league cohort matched on minutes and position fills
in.

Commands arriving with later phases: `digest`, `backtest`.

## Configuration

Environment variables, all optional:

| Variable | Default | Notes |
|---|---|---|
| `LOCKIN_DB` | `data/lockin.db` | SQLite path; disposable |
| `LOCKIN_SNAPSHOTS` | `snapshots` | Raw payload archive; **not** disposable |
| `LOCKIN_LEAGUE_ID` | `1283214955830575104` | The 2025-26 league |
| `LOCKIN_SEASON` | `2025` | Sleeper labels 2025-26 as `2025` |
| `LOCKIN_USER_ID` | `1283460931447164928` | |

The default league is `status: complete`. **The 2026-27 league will have a different
id** — Sleeper mints a new one at rollover — so set `LOCKIN_LEAGUE_ID` and
`LOCKIN_SEASON` when the new season starts rather than relying on the defaults.

## Development

```bash
uv run pytest              # 202 tests, ~1s
uv run ruff check lockin/ tests/
uv run ruff format lockin/ tests/
```

`lockin/core/` is pure — no network, no database, no clock, no `random`. That is enforced
by `tests/test_core_purity.py` rather than left as a convention, because a stopping policy
entangled with I/O cannot be replayed, and the entire backtest depends on replaying it.
Randomness always arrives as an explicit `numpy.random.Generator`, so the same seed gives
the same projection twice.

The gates themselves (`reconcile`, `verify`, `locks`, `calibrate`) are CLI commands rather
than tests — they need the ingested season, and `calibrate` takes about nine seconds. The
test suite covers the machinery underneath them, including that each gate would actually
*fail*: a check that cannot fail is not a gate.

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

**The stat feed contains team aggregate rows.** `TEAM_OKC` posts 125 points, 38 rebounds
and 29 assists as if it were a player. A naive double-double derivation reads every one
of them as a triple-double. They never appear in a lineup so nothing scores them, but any
validation comparing derived bonuses against Sleeper's flags has to exclude them. Kept
rather than dropped — team totals are useful context for the projection layer.

**`points` sums the six starter slots only.** `players_points` is populated for bench
players too, and a bench player's value can freeze mid-week without a lock having
happened. Lock inference reads `starters`/`starters_points`.

**`weekly_matchups` is append-only, so read it through `weekly_matchups_latest`.** After
two ingests every player-week has two rows, and summing the base table returns twice the
team's score. The history is deliberate — it is what lets live opponent lock state be
inferred — but no reader wants it by default.

**Slot eligibility is not published, and `fantasy_positions` is not quite it.** Derived
from 1,500 real assignments: `PG` and `G` take guards, `C` takes `C`/`PF`, `F` takes
`SF`/`PF`/`C`, `UTIL` takes anyone. Three slots came back exactly determined — zero
violations in 250 observations each. `F` did not: three players listed `['PG','SG']`
started there anyway, so Sleeper's real eligibility is broader than what it publishes for
them. Those are carried as per-player overrides rather than by loosening `F` for every
guard, because being too permissive recommends lineups Sleeper rejects.

**`/players/nba` is a live snapshot with no history.** Today's positions and team are not
January's. Anything reconstructing a past week reads `box_scores.pit_positions` and
`pit_team`, captured per game.

**`matchup_id` is nullable.** Teams eliminated from the playoff bracket have no matchup
in weeks 23-24, and week 25 is unscored entirely.

**There are 25 fantasy weeks, not 24.** Weeks 1-21 regular season, 22-24 playoffs, 25
unscored.

**There is no injury history, so the DNP model cannot use one.** `player_status` is empty
and `dnp_reason` is NULL on all 16,692 unplayed rows; `/players/nba` publishes only
today's designation, which is unusable for a replay. The hazard is therefore built from
observed availability, rest and load alone. That costs less than it sounds, because
absence is strongly autocorrelated: **76.4% of games following a DNP are also DNPs,
against 9.0% following a played game.**

**A quarter of rostered player-games are DNPs, and the rate climbs late.** 22.7% in weeks
1-7, **33.9% in weeks 22-25** — playoff-secured teams resting starters, exactly during the
fantasy playoffs. Any availability model fit early and left alone is miscalibrated
precisely when the decisions are worth the most, so the hazard is refit at every cutoff
and carries a season-stage feature.

## Layout

```
lockin/
  config.py        league, season, db path
  core/            pure — no network, no database, no clock
    scoring.py     score_line (derives bonuses) / score_recorded (as given)
                   score_matrix (the same, vectorised, for the simulator)
    eligibility.py which players may fill which slots, plus overrides
    locks.py       recover a lock decision from a counted score
    projections.py DNP hazard, minutes, component bootstrap; as_of enforced
  ingest/
    sleeper.py     league, rosters, players, matchups, box scores
    nba.py         schedule, tipoff times, exhibition detection
    validate.py    shape assertions; raises on drift, never warns
  store/
    schema.sql     the contract between ingest and every reader
    db.py          single-writer SQLite
    snapshots.py   raw payload archive, outside the database
  reconcile.py     the Phase 0 gates — ingest completeness
  verify.py        the Phase 1 gates — scoring against the recorded season
  locks.py         the Phase 2 gates — inference over all ten rosters
  projections.py   builds the point-in-time panel from SQLite
  calibrate.py     the Phase 3 gates — quantiles against realised frequencies
  cli.py
tests/
docs/
```

Two rules hold the design together: `core/` stays pure so the stopping policy can be
tested without I/O, and SQLite is the contract, so a dashboard is just a second reader
needing no changes to `core/`.
