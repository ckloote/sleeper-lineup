# Sleeper NBA Lock-In Lineup Engine — Architecture

**Status:** Draft for implementation handoff
**Scope:** v1 — lineup assignment and lock-in decisions only
**Target implementer:** Claude Code
**League:** Fantasy Basketball Keeper League (`league_id` 1283214955830575104)
**Owner:** ckloote (`user_id` 1283460931447164928)

---

## 1. Context and goals

This is a decision-support tool for a single Sleeper NBA fantasy team playing in **Lock-In Mode**. It does not act on the user's behalf — the Sleeper API is read-only, so every recommendation is executed manually in the app.

The tool must answer, once per day:

1. Which rostered players should occupy the six starting slots for tonight's games?
2. For each starter who played last night and is not yet locked: **lock or pass?**
3. If the user skips tomorrow's check-in, what standing rule should he apply from his phone?

Output 3 is a hard requirement, not a nicety. The user checks in roughly once daily and will miss days.

### In scope
- Slot assignment under positional eligibility
- Lock/pass decisions and forward-looking lock thresholds
- Projection layer sufficient to support the above
- Opponent modeling for win-probability estimation
- Backtest harness over the 2025-26 season

### Explicitly out of scope for v1
- Free agent / FAAB acquisition
- Trade evaluation
- Keeper selection and draft prep
- Flask dashboard (stub the read layer so it can be added; build nothing)

---

## 2. League ground truth

These are constants, pulled from the live API and verified. Hardcode nothing — read from the league object at startup — but these values are what the engine must be correct against.

### Roster
| Property | Value |
|---|---|
| Teams | 10 |
| Starting slots | `PG, G, F, C, UTIL, UTIL` (6) |
| Bench | 6 |
| Reserve / IR | 2 (DTD **not** IR-eligible) |
| Max roster | 14 |
| Season length | 24 weeks; playoffs weeks 22-24 |
| Playoff teams | 8 of 10 |
| Format | Keeper, max 7 keepers, 3-round draft |
| Waivers | **Rolling priority**, 2-day clear, no FAAB (the `waiver_budget: 100` field is vestigial and must be ignored) |

### Scoring
| Stat | Value | Stat | Value |
|---|---|---|---|
| Points | 1.0 | FG made | 0.5 |
| Assists | 1.0 | FG missed | -1.0 |
| Off. rebound | 1.5 | FT made | 1.0 |
| Def. rebound | 1.0 | FT missed | -1.0 |
| Total rebound | **0.0** | 3PT made | 2.0 |
| Steals | 2.0 | 3PT att. | 0.0 |
| Blocks | 2.0 | Double-double | **10.0** |
| Turnovers | -1.0 | Triple-double | **20.0** |
| Technical foul | -3.0 | Flagrant foul | -2.0 |

### Derived per-attempt economics

These follow from the above and should be asserted in tests:

| Shot type | Made | Missed | Break-even % | EV at typical rate |
|---|---|---|---|---|
| Three-pointer | 5.5 | -1.0 | 15.4% | ~1.28 @ 35% |
| Two-pointer | 2.5 | -1.0 | 28.6% | ~0.75 @ 50% |
| Free throw | 2.0 | -1.0 | 33.3% | ~1.40 @ 80% |

**Implication:** this format overweights three-point volume and free-throw volume relative to standard scoring, and undertaxes turnovers (2.0 for a steal vs -1.0 for a turnover). Any player valuation surfaced by the tool must be computed from this scoring function, never inherited from published fantasy rankings.

---

## 3. The core problem

### 3.1 Lock-In mechanics

- Exactly one game per player per week counts toward the matchup score.
- After a player's game completes, the manager may **lock** that score. Locking must happen before that player's next game tips.
- Locking is irreversible and fixes the player to his current slot for the week.
- If never locked, the player's **final game of the week** counts — including 0.0 if he does not play.
- A player must already be in a starting slot when the game tips; bench players cannot be promoted retroactively.
- Unlocked players may be freely reassigned among starting slots.
- Waiver/FA adds and trades confer no retroactive points.
- All-Star weeks are scored separately and are not merged. **Confirmed for this league: they are played as normal scored weeks, not zeroed.** They stay in the backtest, tagged so results can be reported with and without them.

### 3.2 Why this is not six independent stopping problems

Locking a player fixes his slot for the remainder of the week. Locking a center into `UTIL` on Tuesday removes `UTIL` from the pool Wednesday through Sunday. The six stopping problems are **coupled through slot occupancy**.

Consequences the implementation must respect:
- State includes which slots remain unlocked, not just which players remain unlocked.
- Multi-position-eligible players carry option value above their raw projection, because they preserve assignment freedom.
- `C` is the structural bottleneck: C-eligible players can only fill `C` or `UTIL`.

### 3.3 The double-double discontinuity

`dd = 10.0` is awarded at a discrete threshold and is roughly a quarter of a strong night's score. Fantasy points are therefore **not a smooth function of minutes**, and two players with identical means but different proximity to the 10/10 cliff have materially different distribution shapes.

**Hard requirement:** the simulator generates correlated *component stat lines* and applies the scoring function per simulated game. Do not fit a distribution to historical fantasy point totals. Do not interpolate fantasy points from minutes.

Note `reb` is zeroed and offensive boards pay 1.5 — the projection source must split OREB and DREB.

---

## 4. Objective function

Maximize **P(win) for the current matchup**, not expected points.

The two coincide early in the week, when remaining variance is wide and the win-probability curve is locally linear in margin. They diverge sharply in the last day or two: trailing badly, the correct policy takes variance and passes on safe scores; leading comfortably, it banks everything. Implementing win-probability directly gives expected-points behavior for free as the early-week special case.

**Season-level caveat to encode as a config flag, not v1 logic:** 8 of 10 teams make the playoffs and there are no byes, so regular-season wins are close to decorative. The season is decided in weeks 22-24. A later version should let the objective shift toward asset preservation once playoff position is settled. v1 optimizes each week in isolation.

---

## 5. System architecture

```
                    ┌──────────────────┐
   Sleeper API ────►│                  │
   (league, roster, │   ingest/        │──► SQLite (local, on Pi)
    matchups)       │   adapters       │        │
                    │                  │        │
   nba_api ────────►│                  │        │
   (box scores,     └──────────────────┘        │
    schedule,                                    ▼
    injuries)                          ┌──────────────────┐
                                       │  core/           │
                                       │  (pure, no I/O)  │
                                       │                  │
                                       │  scoring         │
                                       │  projections     │
                                       │  simulation      │
                                       │  decision engine │
                                       └──────────────────┘
                                                │
                              ┌─────────────────┴─────────────────┐
                              ▼                                   ▼
                        CLI (v1)                          Flask (later)
                        + push digest                     read-only
```

### Design rules

1. **`core/` is pure.** Functions take arrays and dataclasses, return arrays and dataclasses. Zero network, zero database, zero clock reads. A stopping policy entangled with I/O cannot be validated.
2. **SQLite is the contract** between the nightly job and every reader. The dashboard, when it arrives, is a second reader over the same tables and requires no changes to `core/`.
3. **Projection sources sit behind an interface.** A `ProjectionSource` protocol returning per-player per-game component distributions, with at least two implementations (trailing EWMA, external feed). Swapping sources must cost one class.
4. **Fail loud on schema drift.** `nba_api` hits an unofficial endpoint that changes without notice. Validate shape on ingest and raise; a stale cache degrading gracefully is acceptable, silent wrong numbers are not.

### Runtime
- Python 3.12, numpy / scipy / pandas
- Raspberry Pi, cron-triggered nightly (post-game) and each morning (digest)
- SQLite, single writer
- Notification via ntfy or Pushover for the morning digest
- No Airflow, no Prefect, no Ray, no GPU. Monte Carlo at 50k paths per player-week runs in milliseconds under vectorized numpy.

### Environment and dependency management — required

**Use `uv` for everything: the Python installation itself, the virtual environment, and all package management.** This is not a preference to be traded away for convenience during implementation.

- Install and pin the interpreter with `uv python install 3.12`; do not rely on the Pi's system Python, and never install packages into it.
- The project is a `uv`-managed venv rooted at the repo. `uv venv` creates it; it is never activated manually in scripts.
- Declare dependencies in `pyproject.toml`. Commit `uv.lock`. Dependencies are added with `uv add`, never with bare `pip install`.
- Sync environments with `uv sync --frozen` so the Pi and any dev machine resolve to byte-identical dependency sets. A stopping policy that behaves differently across environments is not debuggable.
- Invoke everything through `uv run`, including from cron. Crontab entries use absolute paths and take the form:
  ```
  30 9 * * * cd /home/pi/lockin && /home/pi/.local/bin/uv run --frozen lockin digest >> logs/digest.log 2>&1
  ```
  `uv run` resolves the environment itself, which avoids the classic cron failure where an unactivated venv silently falls back to system Python.
- Dev tooling (pytest, ruff) belongs in a dev dependency group, installed via `uv sync --group dev`.

**Pi-specific note:** the Pi is `aarch64`. `numpy` and `scipy` publish manylinux aarch64 wheels, so no source builds should be needed — if a dependency starts compiling from source during `uv sync`, stop and find out why rather than waiting it out. A surprise source build on a Pi is usually a sign the resolver picked something unintended.

---

## 6. Data sources and ingest

| Source | Purpose | Cadence | Notes |
|---|---|---|---|
| `api.sleeper.app/v1/league/{id}` | Settings, scoring, roster slots | Weekly | Source of truth for scoring config |
| `.../league/{id}/rosters` | Current rosters | Daily | |
| `.../league/{id}/matchups/{week}` | Opponent state, counted scores | Multiple times daily in-season | See §10 |
| `.../players/nba` | Player ID map | Daily at most | ~5MB, cache locally |
| `nba_api` | Box scores, schedule, injury status | Nightly | Unofficial; rate-limit with backoff |

**Rate limits:** Sleeper tolerates up to ~1000 calls/minute; we will use a tiny fraction. `nba_api` is the fragile one — cache aggressively and never call it in the hot path of a decision.

### ID crosswalk (discovery task, do this first)

Sleeper player IDs (`"1970"`, `"2580"`) are internal. Build a persistent crosswalk to `nba_api` player IDs from the `/players/nba` payload, which should carry external ID fields. Fall back to normalized-name matching with a manual override table for collisions and suffixes. **Every downstream component depends on this being right**; a silent mismatch produces confident wrong recommendations. Write a reconciliation report that flags unmapped rostered players.

---

## 7. Storage schema

Indicative DDL; adjust as needed but keep the separation.

```sql
-- Reference
CREATE TABLE players (
    sleeper_id      TEXT PRIMARY KEY,
    nba_id          INTEGER UNIQUE,
    full_name       TEXT NOT NULL,
    positions       TEXT NOT NULL,   -- JSON array of eligible slots
    team            TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE nba_schedule (
    game_id         TEXT PRIMARY KEY,
    game_date       TEXT NOT NULL,
    tipoff_utc      TEXT NOT NULL,
    home_team       TEXT NOT NULL,
    away_team       TEXT NOT NULL,
    fantasy_week    INTEGER NOT NULL
);

-- Observations
CREATE TABLE box_scores (
    game_id         TEXT NOT NULL,
    nba_id          INTEGER NOT NULL,
    minutes         REAL,
    pts INTEGER, ast INTEGER, oreb INTEGER, dreb INTEGER,
    stl INTEGER, blk INTEGER, tov INTEGER,
    fgm INTEGER, fga INTEGER, ftm INTEGER, fta INTEGER, tpm INTEGER,
    tech INTEGER, flagrant INTEGER,
    dnp_reason      TEXT,
    PRIMARY KEY (game_id, nba_id)
);

CREATE TABLE player_status (
    nba_id          INTEGER NOT NULL,
    as_of           TEXT NOT NULL,
    designation     TEXT,            -- OUT / DOUBTFUL / QUESTIONABLE / PROBABLE / null
    PRIMARY KEY (nba_id, as_of)
);

-- League state
CREATE TABLE weekly_matchups (
    week            INTEGER NOT NULL,
    roster_id       INTEGER NOT NULL,
    matchup_id      INTEGER NOT NULL,
    sleeper_id      TEXT NOT NULL,
    counted_points  REAL,
    is_starter      INTEGER NOT NULL,
    slot            TEXT,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (week, roster_id, sleeper_id, observed_at)
);
```

`weekly_matchups` is **append-only with an `observed_at` timestamp**. Do not upsert. The polling history is what lets us infer opponent lock state (§10) and is irreplaceable after the fact.

```sql
-- Derived
CREATE TABLE lock_inferences (
    week INTEGER, roster_id INTEGER, sleeper_id TEXT,
    locked_game_id TEXT, locked_after_game_index INTEGER,
    confidence REAL,
    PRIMARY KEY (week, roster_id, sleeper_id)
);

CREATE TABLE recommendations (
    generated_at TEXT, week INTEGER, sleeper_id TEXT,
    action TEXT,                  -- LOCK / PASS / START / SIT
    threshold REAL,               -- lock if tonight's score exceeds this
    ev_lock REAL, ev_pass REAL, win_prob_delta REAL,
    rationale TEXT
);
```

---

## 8. Scoring engine

A single pure function, `score_line(stat_line, scoring_config) -> float`, driven entirely by the league's `scoring_settings`.

Must handle:
- OREB/DREB split with `reb` zeroed
- Missed-shot derivation: `fga - fgm`, `fta - ftm`
- Double-double and triple-double detection across PTS / REB (total) / AST / STL / BLK

**Open question to resolve empirically:** whether a triple-double also pays the double-double bonus (30 total) or supersedes it (20). Resolve by finding a known triple-double in the 2025-26 data and reconciling against the recorded `players_points`. Encode as a config flag with the verified value as default, and make this the first golden test.

Validate the whole engine by reconstructing all of week 12's `players_points` from box scores. Every nonzero value must match to the cent. Do not proceed to modeling until this passes.

---

## 9. Projection layer

Produces, for a given player and scheduled game, **a sample of correlated component stat lines** — not a mean.

### Structure

```
P(DNP) ───────────────► zero-inflation gate
                            │
minutes distribution ───────┼──► per-minute rate vector ──► component draws ──► score_line()
                            │         (correlated)
usage / role context ───────┘
```

1. **DNP hazard.** Logistic or gradient-boosted classifier. Features: injury designation, back-to-back flag, games in trailing 5/7 days, player's own historical DNP rate, age, season stage, and the NBA team's playoff situation. **Calibrate rest risk separately by season stage** — the tool's decisions matter most in weeks 22-24 (late March / April), precisely when playoff-secured NBA teams start resting starters. A model fit on November behavior will be badly miscalibrated exactly when it counts.

2. **Minutes distribution.** Not a point estimate. EWMA of recent minutes, role indicator, adjustment for teammate availability, back-to-back and rest flags. This is the hardest homegrown component and the highest-leverage one.

3. **Component rates.** Per-minute production with covariance preserved — minutes drive everything, and PTS/FGA/FTA are tightly linked. Bootstrap empirically from box scores conditioned on minutes bucket and role rather than assuming parametric forms. Fantasy production is right-skewed with a fat tail, and the tail is the entire reason passing on a game is ever correct. A Gaussian approximation will systematically recommend banking too early.

4. **Blowout truncation** (later refinement). Stars sitting fourth quarters clips the top of the distribution invisibly in season averages.

### Source strategy

- **v1: trailing EWMA**, reconstructible point-in-time from box scores.
- **Later: external per-minute talent feed** (DARKO is the natural candidate — a public daily-updating box-score projection system, and a per-minute talent system that expects the user to supply minutes, which is exactly the seam we want to own). Verify current terms of use before depending on it.

**Leakage rule — non-negotiable.** External daily-updating feeds do not publish historical point-in-time snapshots. Backtesting against present-day values leaks the season's outcome and will make the policy look far better than it is. Therefore: **the backtest runs only on projections reconstructible from data available as of the simulated date.** External feeds are live-only enhancements whose benefit is accepted on faith, never measured in the backtest. Enforce this with a hard date cutoff in the projection interface, not a convention.

---

## 10. Opponent model

The matchup payload exposes no lock flag. It exposes `players_points` — the counted score per player — plus `starters_points` aligned to slots, summing to `points`.

### Retrospective inference (high value, do this early)

Given the scoring function and full box scores, compute each player's fantasy points for every game he played in a week and match against his recorded `players_points`. A match on game *k* of *n* means he was locked after game *k*. Half-point granularity makes collisions rare; flag ambiguous cases with reduced confidence rather than guessing.

This recovers **every lock decision by every manager across all 21 completed regular-season weeks**, which gives:
- Ground truth for the backtest, including a human baseline to beat
- A per-manager lock-tendency profile (locks early and safe vs. rides to Sunday) that sharpens in-season win-probability estimates

### Live inference (one-game lag)

In-season, a player's `players_points` freezing across a subsequent completed game reveals he was locked; updating reveals he was not. Before his next tip, a nonzero value is ambiguous — locked, or played-once-and-unlocked.

Model this as a latent per-player belief `P(locked at current value)`, informed by the manager's fitted tendency and games remaining. The ambiguity collapses naturally as the week ends: any opponent player with no games left is a known constant. Precision is highest on Sunday, which is when the steepest decisions land. Design for tolerance of uncertainty early and sharpness late.

Polling cadence should be frequent enough to catch freezes — every few hours during game windows — which is cheap and well within rate limits.

---

## 11. Decision engine

### Formulation

At each decision point: player *p* has just scored *S*, and must be locked before his next tip or passed.

State: `(unlocked slots, unlocked players with remaining schedules, banked points, opponent belief state, day index)`.

Exact backward induction over this state space is intractable. **Use rollout policy improvement:**

1. Define a base heuristic policy — e.g. lock if *S* exceeds the expected maximum of the player's remaining-game distributions, with the slot-eligibility constraint respected greedily.
2. At each real decision point, estimate `V(lock)` and `V(pass)` by simulating the remainder of the week *N* times under the base policy, simulating opponent totals from the belief state, and scoring **win probability**, not points.
3. Take the higher.

Rollout is guaranteed in expectation to be no worse than the base policy, is straightforward to test, and sidesteps the state-space explosion. Slot assignment among *unlocked* players is free, so solve it as a bipartite matching at each simulated lock event rather than carrying it in the state.

### Threshold output

For each unlocked starter with a game tonight, binary-search over hypothetical *S* to find the value where `V(lock|S) = V(pass|S)`. That crossing point is the **standing rule** the user applies from his phone: *"lock Player X tonight if he clears 47."*

This is a first-class output, not a diagnostic. It is what makes a missed check-in survivable.

### Daily digest contents

1. Lock/pass calls for last night's completed games, with the implied break-even printed alongside
2. Tonight's recommended slot assignment, including any bench promotions (must be set before tip)
3. Forward thresholds for the next 2-3 nights
4. Current win probability and the margin distribution against this week's opponent
5. Warnings: unlocked starters facing a final game with elevated DNP risk

---

## 12. Backtest harness

A first-class deliverable, not an afterthought. The 2025-26 season is complete and fully retrievable.

Replay every week of 2025-26 under strict point-in-time discipline and compare:

| Policy | Description |
|---|---|
| **Actual** | What the user really scored (from `weekly_matchups`) |
| **Never lock** | Take each player's final game of the week |
| **Lock first** | Lock the first completed game for every player |
| **Greedy threshold** | Base heuristic policy |
| **Rollout** | The proposed engine |

**Report wins flipped, not points gained.** Points are the intermediate variable; the league pays out wins. Week 12 of last season is instructive: team totals were roughly 286 ± 37 across ten teams, yet one of five matchups finished 289.5 to 287.5 — a two-point margin. The value of a better policy shows up in the weeks that are already coin flips, and an honest backtest will show a modest points gain converting to a small number of flipped weeks. If it claims more, suspect leakage.

Also report: frequency of zeroed starter slots avoided. Week 12 contains a real instance (roster 7 started a player who finished 0.0 and lost by 58).

Hold out a contiguous block of weeks for validation. A stopping policy will look excellent in-sample.

---

## 13. CLI surface

```
lockin ingest [--full]          # refresh players, schedule, box scores, matchups
lockin digest [--date DATE]     # the daily recommendation (default: today)
lockin explain PLAYER           # distribution, threshold, and the reasoning behind a call
lockin backtest [--policy P] [--weeks RANGE]
lockin verify                   # scoring-engine reconciliation against known weeks
```

`digest` writes to `recommendations` and emits the push notification. Everything else is read-only against the DB.

---

## 14. Build order

Each phase has an exit criterion. Do not begin a phase before the prior one passes.

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **0** | `uv` environment + ingest + ID crosswalk + schema | `uv sync --frozen` reproduces the environment on the Pi; every rostered player maps to an `nba_id`; reconciliation report clean |
| **1** | Scoring engine | Week 12 `players_points` reproduced exactly from box scores; DD/TD stacking resolved |
| **2** | Retrospective lock inference | Lock decisions recovered for ≥95% of player-weeks in 2025-26 at high confidence |
| **3** | Projection layer (EWMA) | Calibration check: predicted quantiles match realized frequencies out-of-sample |
| **4** | Simulation + base policy | Backtest runs; greedy threshold beats never-lock on points |
| **5** | Rollout engine + opponent model | Rollout beats greedy on **wins** in held-out weeks |
| **6** | CLI + digest + cron on Pi | Digest fires daily, thresholds render correctly on phone |

Phases 0-2 are pure plumbing and validation and should consume most of the pre-season time. They are also where a silent error would poison everything downstream.

---

## 15. Open questions

1. **Triple-double stacking** — 20 or 30? Resolve in Phase 1.
2. **Calibration data thinness** — `previous_league_id` is null, so 2025-26 was the league's first season. One season of league history is adequate for lock-inference and opponent profiling but thin for DNP modeling; supplement with league-independent NBA data, since rest behavior is a property of players and teams rather than of this league.
3. **Light-slate weeks** — in All-Star weeks (played normally in this league) many players have one game or none. With exactly one scheduled game, lock and pass are outcome-equivalent and the engine must report "no decision" rather than emit a threshold. With zero games the slot scores 0.0 regardless of assignment. In these weeks value shifts almost entirely from the stopping module to the assignment module: the objective is simply to field six starters whose teams actually play. Verify the engine degrades to that behavior rather than producing false precision.
4. **Season-stage objective shift** — deferred to v2, but the config surface should anticipate it.

---

## 16. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| ID crosswalk errors | Confident wrong recommendations | Reconciliation report; fail on unmapped starters |
| `nba_api` schema drift mid-season | Ingest breaks in January | Shape validation, loud failure, graceful stale-cache degradation |
| Leakage via present-day external projections | Backtest overstates edge | Hard date cutoff enforced in the projection interface |
| Gaussian residual assumption | Systematically banks too early | Empirical bootstrap; test against realized tail frequencies |
| DNP model fit on early-season behavior | Fails exactly in playoff weeks | Season-stage-specific calibration |
| Over-fitting the stopping policy | Looks great, loses in March | Held-out weeks; report flipped wins, not points |
