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

**Phases 0-5 complete.** Ingest, the scoring engine, retrospective lock inference, the
projection layer, the stopping policy and the rollout engine, all validated against the
full 2025-26 season. Every nonzero counted score in all 25 weeks is reproduced from box
scores to the cent, 98.4% of starter player-weeks resolve to a specific lock decision,
projected quantiles match realised frequencies on held-out weeks — including the right
tail, which is the only part that decides whether banking a score is correct — the greedy
threshold beats never-lock by 79.8 points per roster-week out of sample, and the rollout
engine converts that into **9 extra wins over 236 team-weeks** while deliberately giving
up points. Phase 6 (daily digest, deployment) remains.

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
inferred 1500 starter player-weeks, 1476 resolved

  locked_early           858
  rode_to_end            597
  ambiguous               24
  single_game             21

manager lock tendency (higher lock_rate = banks earlier)
  roster  decisions  early   rode  lock_rate  mean_pos
       6        144    110     34      76.4%      0.38
       4        148    103     45      69.6%      0.43
       9        146     42    104      28.8%      0.77
```

The spread is the point: roster 6 banks early, roster 9 rides to Sunday. That is a
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

> ⚠️ **This is a marginal — one player, one game.** Do not simulate a week by calling it
> once per remaining game and multiplying. Availability is a persistent state, so
> independent draws understate P(a player misses his whole week) by up to **28×**, and
> price the "rode to Sunday and collected a 0.0" disaster at ~2% when it really happens
> **13.4%** of the time. A week simulation has to walk the hazard forward along each
> simulated path. See [implementation-plan.md §13](docs/implementation-plan.md).

### `lockin backtest`

Replays every roster under each stopping policy. Exits nonzero on any gate failure.

```bash
uv run lockin backtest
uv run lockin backtest --paths 1000 --json
```

```
replayed 250 roster-weeks; 80 held out (weeks 18-25), 480 starter-weeks

  means over the 66 of 80 held-out roster-weeks where every policy ran
  policy         points   zeroed   locked      wins
  never_lock      213.6       30        0     33/66
  lock_first      233.5        9      387     42/66
  greedy          289.5       13      303     61/66
  rollout         284.1       15      264     59/66
  oracle          310.0        9        -         -   perfect foresight, not attainable
  actual          244.1        -        -         -   advisory: reads the field Sleeper rewrote

  rollout vs greedy, both against a greedy opponent, all ten rosters:
    236 team-weeks — rollout 127 wins, greedy 118; flipped +14/-5, McNemar z=+2.06
```

Each roster's **actual lineup is held fixed** and only the stopping rule varies. That is
what isolates the decision the engine makes; letting the policy pick lineups too would
confound stopping with assignment and compare against lineups nobody fielded.

The three replayed policies are one walk over the week under different thresholds — never
lock clears no threshold, lock-first clears every threshold, and greedy compares tonight's
score against the expected value of riding on. Writing them separately would have let them
differ for uninteresting reasons.

**Why the gain is not too good to be true.** The architecture doc warns that a large
backtest gain usually means leakage, and +79.8 is not modest. Two things explain it. Never
lock is genuinely awful here — it zeroes one starter slot in seven, because an unlocked
player's final game counts even when he doesn't play it. And the real check is the
`oracle` row: perfect foresight banks each player's best game and scores 300.0, so greedy
captures 74.6% of the headroom above never-lock. Optimal stopping on iid draws captures
about 75%. Greedy sits just under the theoretical ceiling for a policy with no foresight,
which is where a correct one belongs and where a leaking one could not. That comparison is
a gate, not a comment.

**Rollout gives up points and gains wins.** Paired over the same roster-weeks it scores
1.7 fewer points than greedy across the season (5.4 fewer on the held-out block) and wins
nine more matchups. Every mean in the table above is taken over the roster-weeks where
*all* policies ran — rollout needs an opponent, so it is absent from weeks 23-24's
eliminated teams and from unscored week 25, and averaging each policy over its own rows
would compare different sets of weeks. That is the objective working: the engine
maximises P(win), not points, and the two diverge exactly where it matters — trailing
badly, the right play is to take variance and pass on a safe score; leading comfortably,
it is to bank everything. A rollout that matched greedy on points would be evidence it was
ignoring the opponent. It also zeroes 15 starter slots against greedy's 38, because a
zeroed slot loses a week outright rather than shaving a margin.

The win comparison pools **all ten rosters** — one roster's held-out block is five or six
matchups, which cannot resolve an effect this size. The held-out block is still reported,
and reported honestly: 4 discordant pairs, too few to conclude anything from on its own.

### `lockin managers`

Ranks the ten managers on **decision quality**, holding roster talent constant.
Read-only analysis, not a gate.

```bash
uv run lockin managers --names
```

```
   # roster manager           squander   wrong   stake   regret     n  hi-lev  pts cap  zeros
   1      3 yinzknow             6.8%  14.3%  7.54%  0.511%   224    71%   85.0%      0
   2      4 ckloote              9.2%  13.1%  9.57%  0.883%   213    80%   89.0%      0
   3     10 jordany32           10.0%  16.0%  9.00%  0.904%   244    32%   89.0%      0
   ...
  10      9 coopermycupp        31.1%  30.3%  7.50%  2.332%   218    67%   39.4%      9

  points and win probability disagree on 10.9% of decisions;
  mean regret 0.755% there against 1.329% elsewhere.
```

The ranking is the **share of at-stake win probability thrown away**, not points and not
raw regret. With a binary choice, regret on a decision is either zero or exactly the gap
between the options, so `mean regret = P(wrong) × E[stake]` — and the second term is
circumstance, not skill. Lopsided matchups carry a mean stake of 3.0% against 10.4% in a
live one, so being blown out repeatedly earns low regret for free. Dividing by the stakes
removes that; `--competitive` restricts to live matchups as a stronger check, and
reproduces the ranking at Spearman +0.94.

Underneath that, the unit is **win probability, not points**. That
distinction is the whole point of the command. A manager 40 down on Sunday should
*decline* to bank a safe 45 and ride a boom-or-bust game instead, because banking
it still loses — and a points metric scores that correct call as a blunder.

Over the real season the two objectives disagree on **10.9%** of decisions, and
190 of those 250 are exactly that case: pass where points says bank, at a mean win
probability of 22%. Correcting for it moves four managers two or more places. The
older points-capture metric is still shown (`pts cap`) so the two can be compared,
but it is never sorted on.

One nuance the other way: mean regret is *lower* on divergent decisions (0.755%)
than concordant ones (1.329%). Divergence arises when a matchup is already
lopsided, so the marginal win probability at stake is small. High-leverage
decisions are real but individually cheaper than ordinary ones.

**The ranking covers lock/pass decisions only.** Who to start is a separate and probably
larger decision, and it is deliberately not scored: the fair point-in-time version was
built and it fails, because the model's own lineup picks are about **20 points a week
worse** than the managers'. Players it wanted and managers benched were four times more
likely to miss the whole week — managers read the injury report and the projection layer
cannot. Measuring lineup quality needs a live availability feed first; see
[implementation-plan.md §16](docs/implementation-plan.md).

Three limits, which the command prints alongside its output:

- **It reads the field Sleeper rewrote** (§12) — this is how the current data makes
  each manager *look*, not a certified record of what they did.
- **Model error is charged to the manager.** A call scored wrong may reflect injury
  news the projection cannot see.
- **Do not benchmark the engine on this scale.** Greedy's thresholds come from the
  same model that computes the win probabilities grading it, so its errors are
  shared between deciding and being judged. Manager-versus-manager is fair;
  manager-versus-engine is not.

It also ranks the **teams** — a different question, and the interesting one is the
contrast. `ceiling` is the best legal six from the whole roster with every lock perfect;
`oracle` is the same over only the six actually started, so the gap is the price of lineup
selection, which is a decision rather than roster quality. Nine rosters give up 18-28
points a week there; one gives up 53.3, ranks 3rd on roster and 8th on the plain oracle,
and is also last on decision quality. Schedule density spans only 3.25-3.40 games a week
here, so it is reported but changes little; health and form are unavoidably included,
since every number is built from what players actually did.

Every run writes `manager_scorecards`, `manager_decisions` and `roster_strength`. **A dashboard should
read those tables rather than recomputing** — producing them costs several seconds of
Monte Carlo, and the design rule is that SQLite is the contract, so a reader needs
nothing from `core`. `manager_decisions` holds one row per call with both win
probabilities, so a drill-down is a `WHERE` clause. Rendering guidance, including which
column must never be sortable, is in
[implementation-plan.md §6](docs/implementation-plan.md) under Phase 6.

Commands arriving with later phases: `digest`.

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
uv run pytest              # 293 tests, ~1.5s
uv run ruff check lockin/ tests/
uv run ruff format lockin/ tests/
```

`lockin/core/` is pure — no network, no database, no clock, no `random`. That is enforced
by `tests/test_core_purity.py` rather than left as a convention, because a stopping policy
entangled with I/O cannot be replayed, and the entire backtest depends on replaying it.
Randomness always arrives as an explicit `numpy.random.Generator`, so the same seed gives
the same projection twice.

The gates themselves (`reconcile`, `verify`, `locks`, `calibrate`, `backtest`) are CLI
commands rather than tests — they need the ingested season, and `calibrate` takes about
nine seconds. The
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
    eligibility.py which players may fill which slots, plus slot assignment
    locks.py       recover a lock decision from a counted score
    projections.py DNP hazard, minutes, component bootstrap; as_of enforced
    policy.py      when to bank a score and when to ride
    winprob.py     P(win), the rollout decision, and the standing threshold
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
  rollout.py       walks a week under the rollout policy, with an opponent
  managers.py      ranks the managers on decision quality, not results
  backtest.py      the Phase 4-5 gates — policy against policy over the season
  cli.py
tests/
docs/
```

Two rules hold the design together: `core/` stays pure so the stopping policy can be
tested without I/O, and SQLite is the contract, so a dashboard is just a second reader
needing no changes to `core/`.
