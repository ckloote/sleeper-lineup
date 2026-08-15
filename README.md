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

**Phases 0-6 complete.** Ingest, the scoring engine, retrospective lock inference, the
projection layer, the stopping policy, the rollout engine and the daily digest, all
validated against the full 2025-26 season. Every nonzero counted score in all 25 weeks is
reproduced from box scores to the cent, 98.4% of starter player-weeks resolve to a specific
lock decision, projected quantiles match realised frequencies on held-out weeks — including
the right tail, which is the only part that decides whether banking a score is correct —
the greedy threshold beats never-lock by 79.8 points per roster-week out of sample, and the
rollout engine converts that into **9 extra wins over 236 team-weeks** while deliberately
giving up points.

> **The 2026-27 league does not exist yet** — `/user/.../leagues/nba/2026` still returns
> `[]` — so `lockin digest` takes an as-of date and reconstructs the *morning* of it from
> the recorded season. That is the same code path that will run live; `--date` defaults to
> today. Three things remain unverifiable until October, all of them live-only: today's
> injury designations, the poll history that live opponent-lock inference needs, and
> whether Sleeper publishes stat rows for upcoming games. See
> [implementation-plan.md §20](docs/implementation-plan.md).
>
> **The digest does not recommend a lineup.** It was measured and it is negative: following
> the model's lineup picks would have made nine of the ten teams worse, by 20.4 points a
> week, because the projection layer cannot read the injury report a manager reads. What
> ships instead is a DNP warning on unlocked starters facing their last game of the week —
> the same quantity, framed as a risk to check rather than an instruction to follow.

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

### `lockin teams`

Ranks teams on **roster quality** — how good the side was, not how it was run. Needs no
simulation, so it returns in about a second.

```bash
uv run lockin teams --names
```

```
   # roster manager           ceiling  available  pts/game  talent/gm  lineup cost
   1      3 yinzknow            383.5     70.6%      39.3      293.2         20.0
   2     10 jordany32           357.1     80.3%      36.7      272.7         20.4
   3      9 coopermycupp        353.3     80.0%      36.1      261.1         53.3
   ...
  10      1 smorgan83           310.9     60.1%      32.9      239.1         27.7
```

`ceiling` is the best legal six from the **whole** roster with every lock perfect, so both
lineup selection and stopping skill are removed. It is produced by two things and both are
shown: how often the roster was **available**, and how much it scored when it was.

**Durability is already inside `ceiling`** — a missed week counts zero, so a star who scores
100 a night and misses the season is correctly worth nothing. Measuring that needs no injury
data: `played` says who suited up. Injury data would say *why*, and whether it was known
before tip, which is what start/sit needs and team quality does not.

Availability spans 60.1% to 85.2% here and correlates +0.36 with ceiling against +0.90 for
scoring rate. Roster 1 is last largely *because* of durability; his points per game played
is mid-table.

What it cannot do: separate durability skill from health luck, or predict next season. It is
a record, not a forecast. `lineup cost` is a decision rather than roster quality — that
belongs with `lockin managers`.

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

### `lockin advice`

What the last digest said, as a page. Reads `recommendations` and `digest_runs`; it never
recomputes.

```bash
uv run lockin advice          # writes advice.html
```

That distinction is a correctness rule here, not a performance one. Recomputing would give
a *different* answer — the reconstructed banked state is a chain of near-tied calls and
thresholds carry a few points of Monte Carlo noise — so a page that recomputed would
quietly disagree with the notification you acted on. And §12 means the inputs are rewritten
upstream, so "what did it say on the day" stops being rebuildable once the day passes.

**Staleness is the banner, not a footnote.** The failure mode of a recommendations page is
showing yesterday's calls as though they were today's, so age is measured from the morning
the digest describes and stated in colour before any advice appears.

It shows the latest run only. That is deliberate: the question is "what am I supposed to
do", and a date picker invites reading a stale answer on purpose.

### `lockin serve`

Both pages over HTTP, so a phone can read them.

```bash
uv run lockin serve                     # all interfaces, port 8080
uv run lockin serve --host 127.0.0.1    # this machine only
```

```
serving roster 4 from data/lockin.db (read-only)
  http://127.0.0.1:8080
  http://phaedrus:8080
  /            what to do tonight
  /dashboard   who decided well last season
```

**Do not use `python -m http.server` instead.** Pointed at this directory it would publish
`data/lockin.db` — the entire season — plus `snapshots/` and the source, with directory
listing on. This server holds no document root and never opens a file, so there is no path
handling to get wrong; every path that is not one of the two routes is a 404.

**Pages are rendered per request, not served from disk.** `advice` is a reader — two SQL
queries, no simulation — so regenerating on each request is free, and it removes the
failure this area keeps producing: a page quietly older than the data behind it. The
served copy cannot lag the last digest.

The database is opened **read-only**, enforced by SQLite rather than by the handlers being
careful, and WAL means it coexists with the cron ingest writing at the same moment.

**On exposure.** Binding all interfaces is the default because that is the entire point;
it means the LAN, and it means Tailscale when that interface is up, which is the sensible
way to reach it from outside the house. There is no authentication — the network is the
boundary, so do not port-forward it. `--host 127.0.0.1` restricts it to the machine.

To keep it running on the Pi, a unit rather than a cron entry:

```ini
# /etc/systemd/system/lockin-serve.service
[Unit]
Description=Lock-in pages
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/lockin
ExecStart=/home/pi/.local/bin/uv run --frozen lockin serve --quiet
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now lockin-serve
```

### `lockin dashboard`

Renders that ranking as a self-contained HTML page — no server, no build step, opens over
`file://` from a phone.

```bash
uv run lockin managers      # compute; several seconds of Monte Carlo
uv run lockin dashboard     # render what it stored
```

It **reads** `manager_scorecards` and `roster_strength` and never recomputes. The ordering
is on `squandered_share` and there is no way to change it: no script, no sort control, no
header link. Points capture is shown for contrast only, and a page that let you sort by it
would have published a wrong ranking. Each row's 90% bootstrap band is drawn on one shared
axis, because the bands overlap across ranks 1-8 and a bare ordered list would assert a
precision the data does not have.

### `lockin digest`

The daily recommendation. Reconstructs the **morning** of a date: what to lock now, the
standing rules for the next few nights, and where the matchup stands.

```bash
uv run lockin digest --date 2026-01-08 --locked 1000:46.0,1787:47.5
```

```
LOCK-IN  Thu 8 Jan  wk 12
roster 4 v 5   P(win) 54%

LAST NIGHT (Wed) — do this now
  pass  Karl-Anthony Towns  49.0  need 55
  pass  Tyrese Maxey        42.5  need 59
  pass  OG Anunoby          31.0  need 41
  pass  Devin Booker        19.0  need 42

FRI 9 — lock if he clears
  Tyrese Maxey             49  53%
  Karl-Anthony Towns       48  41%
  Devin Booker             38  50%
  OG Anunoby               34  42%

BANKED 93.5 across 2 of 6
PROJECTED 286 v 281
margin p10/p50/p90  -54 / +4 / +71
```

Nothing on or after the as-of date is read. Post-cutoff scores are blanked out of the data
structure before anything sees them, and a test overwrites them with garbage and asserts
the digest is byte-identical — a leak is prevented by construction rather than by care.

**Forward thresholds assume you act on none of the nights in between.** That is deliberate
and it is the whole point: a threshold computed on the assumption that you followed
yesterday's advice is worthless exactly when you needed it. The count of idle nights
assumed is printed with each rule.

**Pass `--locked` whenever you know what you have banked.** Without it the state is
reconstructed by replaying the week under the engine's own policy, and that is the noisiest
number the digest produces — a chain of near-tied calls, which resampling flips often enough
that the same date reconstructs 1 to 3 locks across seeds. Live this never arises: you know
what you locked. Everything downstream is stable once the state is fixed — the lock/pass
calls are identical across seeds at the default 400 simulations. Thresholds still carry 1-3
points of Monte Carlo noise, which is why they print as whole numbers.

Each run appends to `recommendations`, keyed by timestamp, so a re-run records a second
opinion rather than overwriting the first — which matters because Sleeper rewrites
completed seasons, and this table is the only record of what was advised on the day.

### `lockin explain`

The reasoning behind one call, built from the same digest rather than a second code path.

```bash
uv run lockin explain Maxey --date 2026-01-08 --locked 1000:46.0,1787:47.5
```

```
Tyrese Maxey  (2126)   week 12, as of 2026-01-08
  roster 4 v 5, P(win) 54.2%

  his week
    2026-01-05  played   54.0
    2026-01-07  played   42.5
    2026-01-09  to come
    2026-01-11  to come

  distribution for one game, as of today
    basis own (33 prior played games), P(DNP) 9.6%
    mean 46.1   q25 36  q50 48  q75 61  q90 73

  last night (2026-01-07): scored 42.5
    PASS — P(win) 41.1% locking, 54.2% passing
    break-even 59.0: below it, riding is worth more.
    The gap is 13.13% of win probability, which is what
    the call is worth — not the 16.5 points.
```

Give it the same `--locked` you gave the digest. It rebuilds the whole digest and reads one
player out of it rather than recomputing — a diagnostic that can disagree with the thing it
is diagnosing is worse than no diagnostic — so a different state would describe a different
week.

## Configuration

Environment variables, all optional:

| Variable | Default | Notes |
|---|---|---|
| `LOCKIN_DB` | `data/lockin.db` | SQLite path; disposable |
| `LOCKIN_SNAPSHOTS` | `snapshots` | Raw payload archive; **not** disposable |
| `LOCKIN_LEAGUE_ID` | `1283214955830575104` | The 2025-26 league |
| `LOCKIN_SEASON` | `2025` | Sleeper labels 2025-26 as `2025` |
| `LOCKIN_USER_ID` | `1283460931447164928` | Resolves to roster 4 |
| `LOCKIN_TZ` | `America/New_York` | The timezone NBA game dates are filed under &mdash; not where you are |
| `LOCKIN_NTFY_TOPIC` | *unset* | Enables `digest --notify`. **The topic name is the secret** |
| `LOCKIN_NTFY_SERVER` | `https://ntfy.sh` | Point at a self-hosted instance if you have one |

The default league is `status: complete`. **The 2026-27 league will have a different
id** — Sleeper mints a new one at rollover — so set `LOCKIN_LEAGUE_ID` and
`LOCKIN_SEASON` when the new season starts rather than relying on the defaults.

Notifications are off unless `LOCKIN_NTFY_TOPIC` is set. ntfy topics are public and
unauthenticated by default, so anyone who knows or guesses the topic can read your digest —
pick something long and unguessable, and keep it out of the repo.

### Running it daily

The full Pi runbook, with the checks that matter, is in
[docs/deployment.md](docs/deployment.md).

`uv run` resolves the environment itself, which avoids the classic cron failure where an
unactivated venv silently falls back to system Python. Use absolute paths:

```cron
# Post-game ingest, and the morning digest.
30 6 * * *  cd /home/pi/lockin && /home/pi/.local/bin/uv run --frozen lockin ingest --weeks $(date +\%V) >> logs/ingest.log 2>&1
0  9 * * *  cd /home/pi/lockin && LOCKIN_NTFY_TOPIC=$(cat ~/.lockin-topic) /home/pi/.local/bin/uv run --frozen lockin digest --notify >> logs/digest.log 2>&1
5  9 * * *  cd /home/pi/lockin && /home/pi/.local/bin/uv run --frozen lockin advice >> logs/digest.log 2>&1
```

The ingest runs first because the digest reads what it wrote, and `advice` runs last
because it reads what the digest wrote — it re-renders the page without recomputing, so a
missed notification is still readable. `--frozen` pins the lockfile
so a cron run can never resolve a different dependency set from the one that was tested.
Keep the topic in a file only your user can read rather than in the crontab, which is
world-readable on some systems.

**Every ingest records today's injury designations**, and there is no flag that skips
it. There used to be: the player payload sat behind `--full`, the designations rode in on
it, and a crontab without the flag captured nothing while reporting success every morning.
That record cannot be backfilled and is the prerequisite for ever ranking start/sit
decisions (§19), so it no longer depends on anyone remembering. The ingest prints the day
count each run — see [day-one.md](docs/day-one.md) step 6 for what to check.

## Development

```bash
uv run pytest              # 330 tests, ~13s
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

**`/players/nba` is a live snapshot with no history — and so is the `player` object
embedded in each stat row.** Sleeper writes that object when the request is served, not
when the game was played: `fantasy_positions`, `team` and `injury_status` are identical
across weeks 3, 12 and 20 for all 519 players, and match today's live values exactly. So
`box_scores.pit_positions` / `pit_team` are **not** point-in-time despite the name, and 0
of 633 players ever show a change in them.

The one genuinely point-in-time player attribute is the stat row's own `team`, stored as
`box_scores.team` — 104 of 602 players changed team mid-season there. Prefer it over
`pit_team`. There is no equivalent for positions or injury status; see
[implementation-plan.md §17](docs/implementation-plan.md).

**Other sources exist but do not close the gap.** `nba_api`'s `BoxScoreTraditionalV3`
carries a real DNP reason (`DNP - Coach's Decision`, `DND - Injury/Illness`), but it covers
only 25% of our unplayed rows — 75% of players Sleeper marks unplayed never appear in an
NBA box score, because it lists the gameday roster. It documents healthy scratches well and
the injured poorly, and it reintroduces the ID crosswalk (`players.nba_id` is 0 of 2107)
that Phase 0 was glad to avoid. The official NBA injury report is the right source and is
reachable but JavaScript-rendered. See
[implementation-plan.md §18](docs/implementation-plan.md) for the full survey — including
why none of it is needed for **live** use.

**There is no historical availability data at all.** `dnp_reason` is NULL on all 16,692
unplayed rows and `player_status` was empty until now. This is why start/sit decisions
cannot be evaluated (§16). `lockin ingest` now records today's designations into
`player_status` so the history starts accumulating — useless for 2025-26, and the only way
to have it for 2026-27.

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

## What cannot be validated yet

**Start/sit algorithms cannot be backtested**, and will not be until 2026-27 accumulates
point-in-time availability. This is the leakage rule in mirror image: the project guards
everywhere against the backtest knowing *more* than the live engine, and here it knows
*less*. A replay against 2025-26 lacks the injury information a live recommender would
have, so a poor result would indict the missing input rather than the algorithm, and a good
one would be good at a task nobody will ask for.

Lock/pass survives because a lock decision is made *after* a game — the score is known, and
the only forecast is over the games still to come, which is exactly what the projection
layer is calibrated for. A start/sit decision is made before anything and is entirely
forecast.

The one start/sit input needing no availability data is the published schedule, and it is
weak here: starters averaged 3.31 scheduled games against 3.23 for the bench, and games
scheduled correlates just +0.10 with the week's best game. Lock-in counts one game per
player, so a busier schedule buys less than it would in a points format.

Consequence: a start/sit feature would be the first thing in this project to ship without a
gate. See [implementation-plan.md §19](docs/implementation-plan.md) for the sequencing that
avoids that. This is why `lockin digest` emits no lineup advice.

**Three live-only paths have never run against a live league**, because there is not one to
run against. Each is built and each is unexercised. They all come due on one morning, so
they are assembled as an ordered checklist in [docs/day-one.md](docs/day-one.md) — read
that before the first ingest of 2026-27, because step 2 is destructive if skipped.

- **Today's injury designations.** Every `lockin ingest` writes `player_status`, so the
  capture works — it simply has no history yet, and cannot be
  backfilled. Until it does, `starter_dnp_scale` stands in, correcting a hazard that
  otherwise predicts 17.2% absence for started players against a realised 8.5%.
- **The poll history.** `weekly_matchups` is append-only so that a frozen `players_points`
  can reveal an opponent's lock one game later. Nothing has been polled. The digest
  substitutes a base policy for that belief, and assumes you followed the engine when
  reconstructing what you have already banked.
- **Stat rows for upcoming games.** Whether Sleeper publishes them is still unconfirmed
  (§7.5). The `nba_api` schedule ingest exists as the fallback, so absorbing a "no" is
  cheap — but it must be checked on day one of the season.

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
  managers.py      ranks managers on decision quality, and teams on roster quality
  backtest.py      the Phase 4-5 gates — policy against policy over the season
  digest.py        the Phase 6 product — the morning of a date, point-in-time
  dashboard.py     the manager ranking as static HTML; reads, never computes
  notify.py        ntfy push; opt-in, and never fatal
  cli.py
tests/
docs/
```

Two rules hold the design together: `core/` stays pure so the stopping policy can be
tested without I/O, and SQLite is the contract, so a dashboard is just a second reader
needing no changes to `core/` — which is exactly what `dashboard.py` turned out to be.

`rollout.py` carries a third, added in Phase 6: every simulation entry point takes
`known_through` (the last day observed) and `act_from` (the first night a lock may be
taken) rather than one date for both. The backtest only ever asks about the end of a
completed day, where they coincide; a morning digest is the case where they do not, and
collapsing them reads scores out of games that have not been played.
