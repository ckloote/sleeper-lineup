# Sleeper NBA Lock-In Engine — Implementation Plan

**Companion to:** `sleeper-lockin-engine-architecture.md`
**Status:** Approved — **Phases 0-5 complete** (§9, §10, §11, §13, §14, §15). Phases 3-5 were
reassessed and taken on after Phases 0-2 landed, as §6 anticipated. Phase 6 (digest, deployment)
remains, and is mostly live-only work that cannot be backtested.
**Written:** 2026-08-05 (offseason — Sleeper global state is `season_type: off`, week 0)

This plan takes the architecture doc as the spec. Everything below either confirms it
against the live API, or proposes a change with the evidence for that change. Section 8
lists the decisions I need from you before starting.

---

## 1. What I verified against the live API

Run before writing this plan. All of it is reproducible; probe scripts land in `scripts/probe/`.

### Confirmed exactly as documented

| Claim in architecture doc | Result |
|---|---|
| Scoring table (all 21 settings) | Matches `scoring_settings` field-for-field |
| 10 teams, 6 starters, 6 bench, 2 IR, max 14 | Confirmed |
| Playoffs weeks 22-24, 8 of 10 teams | `playoff_week_start: 22`, `playoff_teams: 8` |
| Rolling waivers, `waiver_budget` vestigial | `waiver_type: 0`, `waiver_clear_days: 2` |
| Keeper, max 7, 3-round draft | `max_keepers: 7`, `draft_rounds: 3` |
| First season — no prior history | `previous_league_id: null` |
| Week 12: a matchup decided 289.5–287.5 | Roster 4 def. roster 5, exactly that |
| Week 12: roster 7 started a 0.0 and lost by 58 | Confirmed, 221.5 vs 279.5 |

The doc is accurate. I found no factual errors in it.

### Resolved: triple-double stacking (open question #1)

**Triple-doubles stack. A TD game pays `dd + td` = 30, not 20.**

TD games carry both `dd: 1.0` and `td: 1.0` in the stat line. Two week-12 games where
the stacked score matches Sleeper's recorded `players_points` to the cent and the
non-stacked score matches nothing:

```
player 1970  counted=88.5   stacked=88.5   superseded=78.5
player 4751  counted=71.0   stacked=71.0   superseded=61.0
```

Default the config flag to `td_stacks_dd = True`. It stays a flag, and the assertion
becomes a golden test, but the value is settled.

### Phase 1 and Phase 2 exit criteria already met on week 12

Reconstructing every rostered player's counted score from Sleeper per-game box scores:

```
nonzero scores reproduced exactly : 102 / 102
mismatches                        : 0
ambiguous lock inferences         : 0
```

Every nonzero `players_points` matched **exactly one** game index, so the lock decision
is recoverable without ambiguity. The doc targets ≥95% at high confidence for Phase 2;
week 12 came in at 100%. Worked example — player 1583, four scheduled games:

```
2026-01-05 vs POR   61.0   <-- players_points = 61.0, locked here
2026-01-07 vs OKC   47.5
2026-01-08 vs DAL   DNP
2026-01-10 vs CHA   DNP    <-- would have counted 0.0 if never locked
```

---

## 2. The one architectural change I want

### Sleeper serves per-game box scores keyed by `sleeper_id`

`GET /stats/nba/{season}/{week}?season_type=regular` returns **one row per player per
scheduled game** — 2,069 rows for week 12 — carrying `game_id`, `date`, `team`,
`opponent`, `sp` (seconds played), and the full component line: `oreb`/`dreb` split,
`fgm`/`fgmi`, `ftm`/`ftmi`, `tpm`, `stl`, `blk`, `to`, `tf`, `ff`, plus precomputed
`dd`/`td`. Rows exist for games a player sat out (empty `stats`), which means they are
generated from the team schedule rather than from participation.

That is the entire observational layer for the backtest, in 25 HTTP requests, **keyed by
Sleeper ID**.

### Consequence: the ID crosswalk stops being foundational

§6 calls the crosswalk a "discovery task, do this first" and §16 ranks it the top risk —
correctly, under the original design. It would also have been unpleasant: `espn_id`,
`yahoo_id` and `stats_id` are all `null` for NBA players in `/players/nba`, so it would
have come down to normalized-name matching with a manual override table, exactly as §6
feared.

Using Sleeper for box scores, the only crosswalk needed is **team tricode → NBA team**,
which is 30 rows and which I have already confirmed matches (Sleeper uses the standard
30 tricodes including `BKN`, `PHX`, `CHA`).

### But `nba_api` stays, for the schedule

**Correction to something I said earlier in this session:** I initially reported that
`stats.nba.com` and `cdn.nba.com` were blocked from this machine. That was wrong — it
was an artifact of testing with `curl`, whose TLS fingerprint Akamai rejects. From
Python both hosts work fine:

```
stats.nba.com  commonallplayers        200,  90 KB, 0.2s
cdn.nba.com    scheduleLeagueV2_1      200,  77 KB, 0.1s
nba_api        LeagueGameFinder        2,460 rows / 1,230 games, 2025-10-21 -> 2026-04-12
```

`nba_api` earns its place because **Sleeper's rows have no tipoff time** — only a date,
and a Sportradar `game_id` rather than an NBA one. The doc's `nba_schedule.tipoff_utc`
column needs a real source, and `cdn.nba.com`'s `scheduleLeagueV2_1.json` has
`gameDateTimeUTC` per game.

This does not matter for the backtest — a player never plays twice in one day, so date
ordering fully determines his game sequence. It matters for the **live digest**, which
has to tell you *when tonight's lock window closes* and in what order your players tip.

### Proposed split

| Concern | Source | Crosswalk needed |
|---|---|---|
| Player box scores, all history | Sleeper `/stats/nba/{season}/{week}` | none (native `sleeper_id`) |
| Counted scores / lock ground truth | Sleeper `/league/{id}/matchups/{week}` | none |
| Rosters, positions, injury status | Sleeper `/players/nba`, `/rosters` | none |
| Game schedule + tipoff times | `cdn.nba.com` static schedule; `nba_api` for history | team tricode (30 rows) |

Both sides sit behind the `ProjectionSource` / ingest adapter seams the doc already
requires, so this is reversible.

---

## 3. Point-in-time discipline — a gap in the current spec

The doc's leakage rule (§9) covers external projection feeds. There is a second leak it
does not mention.

**`/players/nba` is a live snapshot with no history.** Today's `fantasy_positions`,
`team` and `injury_status` are today's — not what they were in January. Reconstructing
2025-26 lineups from today's payload silently imports next-season roster moves into the
backtest. Team changes after free agency are common; position reclassifications happen.

The fix is already in the data: **each per-game stat row embeds a `player` object with
its own `last_modified`**, giving point-in-time `fantasy_positions` and `team` as of that
game. Week 12 showed zero drift between the embedded snapshot and today across all 60
starters, so the effect is small in that week — but "small in the week I checked" is not
a guarantee, and the correct source costs nothing extra since we are already reading
those rows.

**Rule to enforce in code:** the backtest reads player attributes only from embedded
per-game snapshots, never from `players`. Same hard-cutoff treatment the doc gives
projections.

---

## 4. Repo layout

```
pyproject.toml            uv-managed, Python 3.12 pinned, uv.lock committed
uv.lock
lockin/
  config.py               league_id, flags (td_stacks_dd, objective mode), env
  core/                   PURE — no I/O, no clock, no db
    scoring.py            score_line(stat_line, scoring_config) -> float
    eligibility.py        slot eligibility + bipartite assignment
    projections.py        ProjectionSource protocol, EWMA impl
    simulate.py           vectorised component-line Monte Carlo
    policy.py             base heuristic, rollout, threshold search
    winprob.py            margin distribution -> P(win)
  ingest/
    sleeper.py            league, rosters, matchups, players, per-game stats
    nba.py                schedule + tipoff times
    validate.py           shape assertions, fail-loud on drift
  store/
    schema.sql
    db.py                 single-writer SQLite, migrations
    read.py               read layer the Flask app will reuse (stub only)
  backtest/
    harness.py            policy replay, point-in-time gating
    report.py             wins flipped, zeroed-slot avoidance
  cli.py                  ingest / digest / explain / backtest / verify
  notify.py               ntfy or Pushover
tests/
  golden/                 recorded API fixtures + expected scores
scripts/probe/            the API probes behind this plan
```

`core/` purity is enforced by a test that imports every `core` module with `socket`,
`sqlite3` and `time` monkeypatched to raise.

---

## 5. Schema revisions

Keeping the doc's separation, with these changes:

- **`box_scores` keys on `(game_id, sleeper_id)`**, not `nba_id`. Sleeper is the source;
  `nba_id` becomes a nullable convenience column.
- **Add `seconds_played`** (Sleeper's `sp`) and derive minutes, rather than storing a
  lossy `minutes REAL`.
- **Add `pit_positions` / `pit_team`** to `box_scores` — the embedded point-in-time
  snapshot from §3.
- **Add `sportradar_game_id`** to `nba_schedule`, joined to NBA `game_id` on
  `(date, home_team, away_team)`. Sleeper and NBA use different game ID spaces.
- **`weekly_matchups` gains `slot_index`** — the position in the `starters` array, which
  is what maps a player to `PG`/`G`/`F`/`C`/`UTIL`/`UTIL`. The doc's `slot TEXT` is
  derived from it.
- **`lock_inferences` gains `n_games`, `matched_game_index`, `ambiguous_indices`** so a
  low-confidence row says *why*.

Append-only `weekly_matchups` with `observed_at` is preserved exactly as specified — it
is the irreplaceable input to live lock inference and must start accumulating the day the
2026-27 season opens.

---

## 6. Phase plan

Numbering follows the architecture doc. Exit criteria are gates, not suggestions.

> **Original committed scope: Phases 0-2.** These are the plumbing and validation the
> architecture doc says should consume most of the pre-season, and the layer where a
> silent error poisons everything downstream. Phases 3-6 were specified for continuity but
> held back pending a reassessment with real ingested data.
>
> **That reassessment happened and Phase 3 was taken on** (§13). Its gate also closes
> against the completed 2025-26 season, since the projection layer reads only box scores —
> which, unlike the counted-score field, Sleeper has not mutated (§12). Phases 4-6 remain
> deferred.

### Phase 0 — Environment, ingest, schema
`uv python install 3.12`, `uv venv`, deps via `uv add`, `uv.lock` committed, dev group
for pytest/ruff. Ingest all 25 weeks of 2025-26 from Sleeper plus the NBA schedule.
Shape validation that raises on drift.

*Exit:* `uv sync --frozen` reproduces the env; all 25 weeks ingested; every rostered
player-week has box-score rows or an explicit no-games record; schedule joins to Sleeper
game rows for ≥99% of player-games.

### Phase 1 — Scoring engine
`score_line()` driven entirely by `scoring_settings`. Two responsibilities the doc
implies but does not separate:

1. Score a **recorded** line, where `dd`/`td` arrive precomputed from Sleeper.
2. Score a **simulated** line, where `dd`/`td` must be computed from components.

These must agree. The simulator depends entirely on path 2, so path 2 is validated by
recomputing `dd`/`td` from components on every real game in the season and asserting it
reproduces Sleeper's flags. A disagreement here would silently corrupt every projection
downstream, and it is the kind of thing that only shows up at the 10/10 boundary.

**Amended after Phase 0:** the week-by-week reconciliation must exclude the All-Star Game.
It carries real stat lines but does not score (§9), so week 17 fails if it is treated as
a countable game. The exclusion is already recorded in the data as
`game_links.is_exhibition`, so this is a filter on the reconciliation query, not new
logic — but it has to be deliberate, because the symptom would be a handful of week-17
mismatches that look like a scoring bug rather than a fixture-semantics one.

*Exit:* all 25 weeks reconciled, not just week 12 — every nonzero `players_points`
reproduced to the cent, with exhibition fixtures excluded; component-derived `dd`/`td`
matches Sleeper's on every played game; per-attempt economics from §2 asserted in tests.

### Phase 2 — Retrospective lock inference
Match counted score against each game's computed score to recover the lock index.

**Correction to §10's method:** the doc infers from `players_points`, but that field is
populated for **bench** players too, while `points` sums only the six starter slots. I
found a bench player whose `players_points` froze at game 2 of 4 — inferring naively
would record a lock for a player who was not scoring. Inference runs off `starters` +
`starters_points`; `players_points` is corroborating evidence only.

Also derive the slot-eligibility rule empirically here: assert the standard mapping
(`PG→PG,G,UTIL`; `SG→G,UTIL`; `SF,PF→F,UTIL`; `C→C,UTIL`) explains 100% of observed
lineups across all 25 weeks × 10 rosters, and widen it if it does not. My week-12 spot
check was consistent with it; a full-season pass over later weeks showed combinations
worth chasing down before trusting the assignment module.

*Exit:* ≥95% of starter player-weeks resolved at high confidence **across all ten
rosters**, with a per-manager lock-tendency profile written out for each. Ambiguous cases
flagged, never guessed. Per §7.1 this is a gating output, not a byproduct — the deferred
Phase 5 evaluation depends on every manager being profiled, not just yours.

---

*Phase 6 below is recorded for continuity. **Not in this build.***

### Phase 3 — Projection layer (EWMA) — COMPLETE, see §13
`ProjectionSource` protocol with an `as_of` cutoff enforced in the interface signature,
not by convention. DNP hazard, minutes distribution, component-rate bootstrap conditioned
on minutes bucket and role. Season-stage-specific rest calibration per §9.

*Exit:* out-of-sample quantile calibration — predicted quantiles match realised
frequencies, checked specifically in the right tail, since the tail is the whole reason
passing is ever correct. **Met** — realised P(score > predicted q₀.₉₉) is 1.05% against a
1.00% nominal on held-out weeks 18-25.

### Phase 4 — Simulation and base policy — COMPLETE, see §14
Vectorised numpy: DNP gate → minutes → correlated components → `score_line`. Bipartite
slot assignment via `scipy.optimize.linear_sum_assignment` at each simulated lock event.
Backtest harness with the five policies from §12.

*Exit:* greedy threshold beats never-lock on points, out of sample.

> ⚠️ **Hard constraint carried over from Phase 3 — read §13 before starting.**
> `ProjectionSource.project()` returns a **marginal**: one player-game, from one cutoff.
> That is what the Phase 3 gate certifies and it is correct. It is *not* what a week
> simulation needs.
>
> Calling it once per remaining game and treating the draws as independent understates
> P(the player misses the entire week) by **3.1× / 11.0× / 28.1×** for 2 / 3 / 4-game
> weeks, and prices the "rode to Sunday and collected a 0.0" disaster at ~2% when it
> happens **13.4%** of the time. Availability is a persistent state, not a per-game coin
> flip.
>
> Phase 4 must simulate **paths**: fit the hazard once at `as_of` (coefficients stay
> point-in-time, so no leakage), then walk it forward along each path with the drawn
> outcome fed back into the state. `dnp_feature_row` already takes
> `(prior_days, prior_played, target_day, fantasy_week)` as arrays, so this needs a
> vectorised feature builder, not a redesign.

### Phase 5 — Rollout and opponent model — COMPLETE, see §15
Rollout policy improvement over the base policy; opponent belief state; threshold by
binary search over hypothetical `S`.

*Exit:* see §7.1 — the doc's stated criterion needs restating to be measurable.

### Phase 6 — CLI, digest, deployment — DEFERRED
`ingest / digest / explain / backtest / verify`, push notification, cron via
`uv run --frozen`.

*Exit:* digest fires daily and thresholds render legibly on a phone.

**Note:** Phases 0-3 still ship a CLI, but only the read-only subset the gates need —
`ingest`, `reconcile`, `verify`, `locks`, `calibrate`, plus `project` for inspecting a
single distribution. `digest` and `backtest` arrive with the phases that give them
something to say.

---

## 7. Issues I want to raise

### 7.1 Phase 5's exit criterion is not measurable as written — RESOLVED, adopted

"Rollout beats greedy on **wins** in held-out weeks." Your team plays **21 regular-season
matchups**. Hold out a contiguous block and you have perhaps 5 or 6. The doc itself
predicts the honest effect size — "a modest points gain converting to a small number of
flipped weeks." A small number of flipped weeks out of six is indistinguishable from
noise, and the gate will be passed or failed by coin flips.

**Proposed fix:** replay every policy for **all ten rosters**, not just yours. Phase 2
recovers every manager's actual lock decisions, so every roster becomes a valid replay
subject with a known human baseline. That turns 21 matchups into **210 team-weeks and 105
matchups**, which can actually resolve a small effect. Report per-roster and pooled, with
a paired test on the same weeks rather than raw win counts.

This costs almost nothing — the simulator does not care whose roster it is — and it is
the difference between a gate that means something and a gate that does not.

**Adopted.** Although Phase 5 is deferred, this lands a requirement on Phase 2 now: lock
inference must resolve and profile **all ten managers**, not just yours. That is already
the plan, but it moves from "useful byproduct" to "gating output," and the Phase 2 exit
criterion below is stated over all rosters accordingly.

### 7.2 Multi-day thresholds are conditional, and the digest must say so

§11 wants thresholds for the next 2-3 nights. A threshold for Thursday depends on what
happens Tuesday and Wednesday — including whether you acted on Tuesday's threshold. Those
numbers are only valid under an assumed policy for the intervening nights.

Since the entire point of the threshold output is surviving a missed check-in, the
assumption should be the pessimistic one: **forward thresholds are computed assuming you
do nothing on the intervening nights**, and the digest states that explicitly. A threshold
computed assuming you followed yesterday's advice is worthless precisely when you needed
it.

### 7.3 The 2026-27 league does not exist yet

`/user/1283460931447164928/leagues/nba/2026` returns `[]`; the league in the doc is
`status: complete` for season 2025. Nothing can be hardcoded to it. Config takes a season
and resolves the league by walking the user's leagues, following `previous_league_id` to
confirm lineage once the commissioner rolls it over. Until then, live paths can only be
smoke-tested against the completed season.

### 7.4 `roster_positions` does not contain the IR slots

It returns 12 entries — `PG, G, F, C, UTIL, UTIL` + 6 × `BN`. The two reserve slots live
in `settings.reserve_slots`, and `reserve_allow_dtd: 0` is what encodes "DTD not IR-
eligible." Slot parsing that reads only `roster_positions` will be two short.

### 7.5 Unverifiable until October: forward-looking stat rows

The digest depends on Sleeper publishing rows for *upcoming* games. The evidence is
encouraging — rows exist for games players sat out, so they track the team schedule, not
participation — but I cannot confirm it in the offseason. If it turns out rows appear only
post-game, tonight's slate must come from the NBA schedule feed instead. Cheap to absorb,
but it needs checking on day one of the season, and the `nba_api` schedule ingest should
be built anyway so the fallback already exists.

### 7.6 Lock-in mechanic — RESOLVED

**A player must remain in a starting slot to lock.** Benching him before locking forfeits
the score. (Confirmed by the league owner.)

This tightens the policy's state definition considerably, and it is the stricter of the
two readings: the nightly assignment is a real commitment, not a free option to be
resolved later. Slot occupancy binds from the moment a game tips, so the coupling §3.2
describes starts earlier in the week than a permissive reading would imply.

It also independently corroborates the Phase 2 method change in §6. The bench player I
found whose `players_points` froze at game 2 of 4 was **not** locked — under this rule
that value is simply the score of the last game he played while occupying a starting
slot, and it correctly contributes nothing to `points`. That reading is consistent with
roster 1's `points` (301.5) summing its six `starters_points` exactly.

Useful side effect: `players_points` on a *non-starter* is therefore evidence about slot
history — it dates an occupancy. That is extra signal for reconstructing an opponent's
slot usage later, and is worth persisting rather than discarding.

### 7.7 Smaller notes

- **25 fantasy weeks, not 24.** `last_scored_leg: 24` with matchup data present for week
  25. Weeks 1-21 regular, 22-24 playoffs, 25 unscored. Handle explicitly rather than
  assuming a 24-week array.
- **Light-slate weeks are real.** Weeks 8 and 18 are single-game weeks for at least some
  players — §15.3's "no decision" degradation will get exercised, not just tested.
- **252 C-only players** in the pool confirms §3.2's claim that `C` is the structural
  bottleneck.
- **Backtest ≠ next season's roster.** Post-keeper-draft, your 2026-27 roster differs
  from the one being replayed. The backtest validates the *policy*, not the team.

---

## 8. Decisions taken

| Decision | Resolution | Effect |
|---|---|---|
| **Scope** | Phases 0-2, then 3 | Ingest, scoring engine, lock inference. Phase 3 taken on after the reassessment and complete (§13); 4-6 still deferred. |
| **Phase 5 gate** | Replay all ten rosters | 105 matchups instead of 21. Lands on Phase 2 now as a gating requirement (§7.1). |
| **Lock mechanic** | Must stay in a slot to lock | Stricter state definition; confirms the Phase 2 inference method (§7.6). |
| **Deployment** | Develop here, deploy to Pi later | Hold the `uv` discipline and cron form; no Pi work in this build. |
| **Notifications** | Deferred with Phase 6 | ntfy is the default when we get there — no account needed. |

### Still open

- **§7.5 — forward-looking stat rows.** Unverifiable until the season opens. Does not
  block Phases 0-2, but the NBA schedule ingest built in Phase 0 is what makes the
  fallback free, so it gets built regardless.
- **§7.3 — 2026-27 league.** Config resolves the league by season rather than hardcoding.
  Nothing further needed until the commissioner rolls it over.

---

## 9. Phase 0 — complete

All gates closed against the full 2025-26 season. 45,835 box-score rows, 1,231 NBA games,
3,295 matchup rows, 26.6 MB.

```
[PASS] all 25 fantasy weeks ingested                     25/25
[PASS] all 25 weeks of matchups ingested                 25/25
[PASS] every rostered player resolves                    all resolved
[PASS] every started player-week has box-score rows      1500/1500
[PASS] played fixtures link to NBA schedule (>=99%)      1231/1231 (100.00%)
[PASS] postponed fixtures agree between Sleeper and NBA  3 postponed, 0 disagreeing
[PASS] non-NBA fixtures identified and excluded          1 exhibition
[PASS] tipoff times present (advisory)                   1231/1231
```

`uv sync --frozen` reproduces the environment; 45 tests pass in under a second.

### What Phase 0 found that the plan did not anticipate

Four fixture-semantics facts, none of which are visible without looking at real data, and
each of which would have produced confidently wrong recommendations. All are now enforced
by tests in `tests/test_season_invariants.py`.

**The All-Star Game is published but does not count.** It appears in Sleeper's stat feed
as an ordinary fixture with real stat lines (teams `STP`/`STR`, 2026-02-15) and falls at
the *end* of fantasy week 17 — so a naive reading makes it every All-Star's final game of
the week. Of the 15 rostered participants, not one counted it: Anthony Edwards counted
30.0 rather than 16.5, Jalen Johnson 56.0 rather than 9.0. LeBron and Cade Cunningham
show genuine early locks in the same week, so this is not 15 managers all locking. Left
unflagged, the engine would think an All-Star's week ends on a low exhibition score and
bank far too eagerly before the break.

This sharpens architecture doc §3.1, which says All-Star *weeks* are played as normal
scored weeks. True — but the All-Star *game* is not a scoring event, and the doc does not
draw that distinction.

**The NBA Cup final does count** — the opposite conclusion, reached the same way. It is
not a regular-season game, so `LeagueGameFinder` omits it entirely, and it was the only
game on its date, so a schedule-driven sweep never visits that date. Karl-Anthony Towns
and Josh Hart both locked on it in week 9, which is only possible for a real scoring
game. Now backfilled from `ScoreboardV3`, driven off Sleeper's fixture dates rather than
the NBA's.

**Postponed is not DNP.** Sleeper retains the original fixture with every player
unplayed, which is indistinguishable from a DNP unless checked. The difference is worth
real points: an unplayed *real* game scores 0.0 for an unlocked starter, while a
postponed fixture is excluded and the prior game counts. Verified both directions in week
12 — Jamal Murray counted 0.0 after scoring 61.0 because his final game was real and he
sat; Bam Adebayo counted his last played game because his final fixture was postponed to
2026-01-29. Three postponements in the season.

**`matchup_id` is nullable.** Rosters eliminated from the playoff bracket have no matchup
in weeks 23-24, and week 25 is unscored entirely. A `NOT NULL` column here aborts the
ingest at week 23, which is exactly what it did.

### Two bugs the gates caught

Worth recording because both were silent and both would have survived a less specific
check.

*Malformed home/away.* Three games (`0022500147`, `0022500578`, `0022500602`) carry the
same away-perspective `MATCHUP` string on *both* of their rows. Reading the away team
from `TEAM_ABBREVIATION` let the home team's row overwrite away with itself, producing
`DET @ DET` — which then failed to link. Both teams are now parsed from the string, with
a self-matchup rejected outright.

*Masked exceptions.* `session()` rolled back unconditionally on error, but `executescript`
implicitly commits, so the `ROLLBACK` raised `OperationalError` and hid the original
exception. Now guarded on `conn.in_transaction`.

### Deviations from the plan as written

- **§5 said `box_scores` keys on `(game_id, sleeper_id)`.** It does, but `game_links`
  gained `occurred` and `is_exhibition` — the two fixture-semantics flags above. Neither
  was foreseen.
- **Tipoff ingest is scoreboard-driven, not schedule-driven.** The plan assumed
  `LeagueGameFinder` yields the full fixture list. It yields played *regular-season*
  games, which is not the same set.
- **`lockin reconcile` exists** as the Phase 0 gate runner. The plan listed only `ingest`
  and `verify` for Phases 0-2.

---

## 10. Phase 1 — complete

All four gates closed against the full 2025-26 season.

```
[PASS] per-attempt economics match the architecture doc
       three: 5.5/-1.0 break-even 15.4%; two: 2.5/-1.0 28.6%; ft: 2.0/-1.0 33.3%
[PASS] component-derived dd/td matches Sleeper       26665/26665 (1919 DD, 136 TD)
[PASS] derived and recorded scoring agree            26665/26665
[PASS] every nonzero counted score reproduced        2647/2647
```

The architecture doc asked for week 12 reproduced exactly; this is all 25 weeks. 76 tests
pass in under a second.

### The two-path split, and why it earns its keep

`score_recorded` scores a Sleeper line as given, reading `dd`/`td` and the missed-shot
counts straight from the payload. `score_line` takes components only and derives all of
them. The simulator will only ever have components, so a wrong derivation would corrupt
every projection — and would do it precisely at the 10/10 boundary, where the
double-double cliff makes the distribution's shape matter most (architecture doc §3.3).

`score_recorded` deliberately derives nothing, so comparing the two on 26,665 real games
is a genuine test rather than a tautology. They agree on all of them, including all 136
triple-doubles.

`score_line` also refuses rather than skipping when `scoring_settings` weights a stat the
component model cannot produce. Silently ignoring it would under-score every simulated
game by a constant, which is the kind of error that survives a long time.

### One more thing the data turned up

**Sleeper's stat feed contains team aggregate rows.** `TEAM_OKC` posts 125 points, 38
rebounds and 29 assists as though it were a player — 2,469 rows across 31 ids. Every one
reads as a triple-double under a component-derived bonus rule, which is exactly how it
surfaced: the first run of the derivation check failed on 20 of them.

They never appear in a lineup, so nothing was ever mis-scored. But they had to be
excluded from the validation, and they would have polluted any per-player rate model
built off `box_scores` without a filter. Kept rather than dropped, since team totals are
real context for the Phase 3 projection layer (pace, usage share), and flagged with
`box_scores.is_team_row`.

The flag is set by "the id does not resolve in `players`", which is self-maintaining —
but that rule would also silently reclassify a real player who went missing from the
player table. `reconcile.check_team_rows` asserts the two signals agree, so that failure
mode is visible rather than silent.

### `core/` purity is enforced, not documented

`tests/test_core_purity.py` parses every module under `lockin/core/` and fails on any
import of `socket`, `requests`, `sqlite3`, `time`, `datetime`, `os`, `pathlib`,
`subprocess`, or the project's own I/O packages — plus a runtime check that calling into
core with `socket.socket` and `sqlite3.connect` stubbed to raise still works.

`random` is barred as well. Simulation must take an explicit numpy `Generator`, or a
backtest cannot be reproduced exactly, and an irreproducible backtest of a stopping policy
is worth very little.

---

## 11. Phase 2 — complete

The committed scope is finished. All four gates closed across **all ten rosters**.

```
[PASS] slot-eligibility rule explains every observed lineup   1500/1500 (3 overrides)
[PASS] no counted score fails to match a game                 0 unresolved
[PASS] starter player-weeks resolved at high confidence       1476/1500 (98.40%)
[PASS] every roster has a lock-tendency profile               10/10
```

Target was ≥95%. Breakdown of the 1,500 starter player-weeks:

```
locked_early    858     an unambiguous, deliberate lock
rode_to_end     597     counted the final game's outcome
ambiguous        24     several games share the counted value
single_game      21     one game scheduled; no decision existed
```

### Manager profiles

```
roster  decisions  early  rode  lock_rate  mean_pos
     6        144    110    34      76.4%      0.38
     3        147    107    40      72.8%      0.45
     4        148    103    45      69.6%      0.43
    10        145     98    47      67.6%      0.51
     7        142     95    47      66.9%      0.49
     1        149     87    62      58.4%      0.51
     2        149     81    68      54.4%      0.60
     5        147     75    72      51.0%      0.62
     8        145     67    78      46.2%      0.66
     9        146     42   104      28.8%      0.77
```

A 48-point spread between the earliest banker and the latest rider, and `mean_pos` (where
in the week the counted game sat, 0.0 first to 1.0 last) moves with it. This is the
"locks early and safe vs. rides to Sunday" trait architecture doc §10 wants, and it is
real signal rather than noise around a common strategy.

> **Corrections (2026-08-08).** Two errors in the original version of this section.
>
> **The table above was stale.** These figures were computed before the 2026-08-07
> re-ingest and describe the *pre-mutation* data. §12 warned the profiles would move and
> nobody updated them here. The numbers shown now are what `lockin locks` reproduces
> today; the resolution rate also rose from 98.20% to 98.40%. Inference is deterministic —
> re-running gives identical output — so this was staleness, not instability.
>
> **The user's roster is 4, not 1.** `LOCKIN_USER_ID` maps to `roster_id = 4`, which locks
> at **69.6%** — the third most eager of ten, not "close to the middle". The original claim
> was wrong on both the identity and the characterisation. The broader point survives: the
> spread is wide but nobody sits at an extreme, so no manager here is playing an obviously
> exploitable stopping policy.

### What ambiguity actually looks like

27 cases, all the same shape: a player scored the identical value in two games that week,
so which one was locked is unrecoverable. Karl-Anthony Towns in week 9 scored 45.0 twice
and counted 45.0 — locking is certain (riding would have counted 3.0), the game is not.

These are flagged with `confidence = 0.5` and `matched_game_index = NULL`, never guessed.
A separate and subtler case: when an *earlier* game matches the ride score, riding
explains the outcome but so does locking early at the same value. The outcome is
identical, so `matched_game_index` is still the final game — but `locked_early` stays
NULL rather than defaulting to False, or the tendency profile would inherit a systematic
bias toward "rides".

### Slot eligibility, derived

Sleeper does not publish the rule. From 1,500 real starter-slot assignments:

| slot | allowed positions | violations |
|---|---|---|
| PG | PG, SG | 0 / 250 |
| G | PG, SG | 0 / 250 |
| F | SF, PF, C | **17** / 250 |
| C | C, PF | 0 / 250 |
| UTIL | any | 0 / 500 |

Three slots are exactly determined. `F` is not: 17 assignments put a player listed
`['PG','SG']` into it, from exactly three players — Amen Thompson (2574), Nickeil
Alexander-Walker (2055), Ayo Dosunmu (2255). Sleeper's own eligibility is broader than
the `fantasy_positions` it publishes for them.

They are carried as per-player overrides rather than by widening `F`. The asymmetry
matters: too strict silently removes legal lineups from consideration, which is
invisible; too permissive recommends a lineup Sleeper rejects, which the user hits
immediately. Keeping the base rule tight and enumerating the exceptions fails in the
visible direction.

A detour worth recording: the first pass tested whether each `starters[i]` matched
`roster_positions[i]` and found 56 violations, which looked like the rule being wrong.
The better test was whether a *valid assignment exists* for the lineup — and the decisive
evidence that `starters` really is slot-ordered is that a pure guard never once appeared
at C in 250 observations. If the ordering were meaningless, guards would land at C at
roughly their population rate.

### The Cup final, now load-bearing

Phase 0 argued the NBA Cup championship counts because two managers appeared to lock on
it. That was half right. Karl-Anthony Towns is one of the 27 ambiguous cases and proves
nothing — he scored 45.0 twice that week.

**Josh Hart is the real evidence.** His week-9 counted score of 30.0 matches *only* the
Cup final; riding would have given 46.0. Remove that game from the sequence and his score
matches nothing, so the "0 unresolved" gate would fail. The gate now carries that claim
directly, which is stronger than an argument in a comment.

### Deviation from the plan

The plan said inference "runs off `starters` + `starters_points`". In practice it runs
off `weekly_matchups` filtered to `is_starter = 1`, which is the same six players per
roster-week — the ingest already resolved the `starters` array into per-player rows with
`slot_index`. The substance of the correction (never infer from bench players'
`players_points`) holds.

---

## 12. Sleeper mutates completed-season data — found 2026-08-07

**This affects the backtest premise and needs a decision.**

Between 2026-08-05 and 2026-08-07, Sleeper changed the recorded results of the completed
2025-26 season. Not stat corrections — **which game counts** for each player changed.

### Evidence

Week 12, comparing a raw API snapshot taken 2026-08-05 against today:

```
  roster  1: 4/6 starters changed   points 301.5 -> 293.5
  roster  2: 3/6 starters changed   points 276.0 -> 295.5
  roster  3: 2/6 starters changed   points 346.5 -> 331.0
  roster  4: 1/6 starters changed   points 289.5 -> 291.5
  roster  5: 1/6 starters changed   points 287.5 -> 287.0
  roster  6: 2/6 starters changed   points 237.5 -> 242.5
  roster  7: 3/6 starters changed   points 221.5 -> 289.0
  roster  8: 3/6 starters changed   points 279.5 -> 310.5
  roster  9: 3/6 starters changed   points 341.5 -> 313.0
  roster 10: 1/6 starters changed   points 275.5 <- 278.5

  23 of 60 starter values changed (38%). Every team total changed.
```

The `starters` arrays are identical. The **box scores are byte-identical**: 2,069 rows,
zero added, zero removed, zero with different stats. So this is not a stat correction
rippling through — the underlying games are unchanged and only the selection of which
game counts moved.

### The 2026-08-05 values are the earliest we have

Corroborated three independent ways by a source written before this project started: the
architecture doc's own week-12 figures.

| architecture doc claim | 2026-08-05 | today |
|---|---|---|
| "one of five matchups finished 289.5 to 287.5" | roster 4 = 289.5, roster 5 = 287.5 ✓ | 291.5 / 287.0 ✗ |
| "roster 7 started a player who finished 0.0 and lost by 58" | 221.5 vs 279.5, diff 58.0 ✓ | 289.0 vs 310.5, diff 21.5 ✗ |
| "team totals were roughly 286 ± 37 across ten teams" | mean 286, sd 39 ✓ | mean 293, sd 24 ✗ |

**Correction to an earlier draft of this section**, which claimed the 2026-08-05 values
"are the real ones" and that today's are "not what happened during the season". That is
stronger than the evidence supports. Both observations are from the **offseason**, four
months after week 12 was played, and the architecture doc was also written post-season.
2026-08-05 is therefore the *earliest available* record, not a verified in-season one. No
in-season observation of this league exists, so which version matches what managers
actually did on the night is not determinable.

### Mechanism: unresolved, and three hypotheses ruled out

An earlier draft asserted Sleeper "swapped which games people locked in". That describes a
mechanism, and no evidence here supports one. What is established is the observation; the
cause is not.

Ruled out:

- **Week renumbering.** Compared the 2026-08-05 week-12 payload against all 25 of today's
  weeks. Week 12 is uniquely the match — 37/60 starter values and 10/10 identical lineups,
  against ≤2/46 values and 0/10 lineups for every other week. Lineups genuinely vary week
  to week, so identical lineups is strong evidence. Same week, same rosters.
- **A mechanical fallback replacing lock semantics.** If the offseason value were simply
  "best game" or "last played game", it would fit one rule cleanly. Restricted to the 58
  week-12 starters with ≥2 played games, both versions match "max" ~60% and no rule above
  that. Today's data behaves like lock decisions; they are just *different* decisions.
- **The DNP-zeroing rule being dropped.** 64 starters still count 0.0 today despite having
  played that week, so an unplayed final game still zeroes an unlocked starter. And the
  changed players share no trailing-DNP pattern: 3 of 23 changed have one, versus 6 of 37
  unchanged.

What the shape suggests, without proving it: today's distribution is **compressed**. Mean
rose from 286 to 293 while standard deviation fell from 39 to 24, and the spread narrowed
from 125 points to 88.5 — roster 7's 221.5 disaster became 289.0. Extremes pulled toward
the middle is what regeneration-toward-a-default looks like; it is not what a set of human
decisions, including the bad ones, looks like.

The likeliest explanation is therefore something dull — an offseason migration, re-index
or archival job in which stored lock state was lost and regenerated — rather than a
deliberate rewrite. That remains a guess. Absent an in-season observation or a Sleeper
changelog, this can be narrowed but probably not settled, and it is not worth more time.

### What this costs

- **The Phase 2 lock inference ran against the mutated data.** Its 98.40% resolution rate
  is genuine — the values still match real games — but the decisions it recovered are not
  necessarily the managers' actual decisions, and the manager profiles inherit that.
- **Only week 12 survives.** A raw snapshot happened to be sitting in a scratch directory;
  the other 24 weeks' original values are gone. Both surviving files are now committed
  under `tests/golden/`.
- **The loss was self-inflicted.** Architecture doc §7 insists `weekly_matchups` be
  append-only with `observed_at` precisely because "the polling history is irreplaceable
  after the fact". The schema honours that. The workflow did not: re-ingesting with
  `rm -f data/lockin.db*` destroyed the earlier observations. Rebuilding from scratch is
  no longer a safe operation on this table.

### What it does not cost

Phases 0 and 1 are unaffected in substance. Box scores are stable, so the scoring engine's
proof still holds — `lockin verify` reproduces every counted value from box scores under
both versions, because both versions select a real game. The ingest, schema and scoring
function need no changes.

### Open question

Whether the season's results are recoverable at all. Sleeper publishes no historical
endpoint, so absent a snapshot the original values are simply gone. Options, in rough
order of how much they salvage:

1. **Treat today's data as canonical** and accept the backtest measures policy against a
   plausible-but-counterfactual season. Cheapest; the human baseline becomes fictional.
2. **Restrict the backtest to week 12**, the one week with verified ground truth. Honest
   but far too small to support any conclusion.
3. **Poll and preserve from here**, accept 2025-26 is compromised as a human baseline, and
   treat the backtest as validating policy-vs-policy rather than policy-vs-human. The
   never-lock / lock-first / greedy / rollout comparison only needs box scores, which are
   stable — so most of §12's backtest survives. Only the "Actual" column is unreliable.

Option 3 preserves the most: architecture doc §12 lists five policies and only **Actual**
depends on the mutated field.

### Decision (2026-08-07): today's data is canonical

Option 3 adopted. The backtest measures **policy against policy**, not policy against the
human baseline.

This costs less than it sounds. Architecture doc §12 lists five policies, and only
**Actual** reads the mutated field:

| policy | depends on | survives |
|---|---|---|
| Actual | `players_points` (mutated) | ✗ unreliable |
| Never lock | box scores + schedule | ✓ |
| Lock first | box scores + schedule | ✓ |
| Greedy threshold | box scores + schedule | ✓ |
| Rollout | box scores + schedule | ✓ |

Box scores are byte-identical across the observed mutation, so four of five policies
replay exactly as intended. What is lost is "did we beat the humans"; what remains is
"does rollout beat greedy beats never-lock", which is the comparison the Phase 5 gate
actually turns on.

The `Actual` column stays in the report, labelled as reconstructed-from-possibly-mutated
data rather than quietly dropped. Week 12 keeps a verified baseline via
`tests/golden/`, so at least one week can be checked honestly.

Two consequences for the human-baseline-dependent parts:

- **Manager profiles are now "how this manager's decisions look in the current data",**
  not a certified behavioural record. They remain useful as an opponent prior — the
  spread across rosters is large and consistent — but they should not be presented to
  the user as fact about what a rival did.
- **Phase 5's all-roster evaluation is unaffected**, because it compares policies replayed
  over the same rosters and schedules rather than against recorded human choices.

### Mitigation: snapshots, outside the database

`lockin/store/snapshots.py`. Every ingest preserves the raw matchups payload under
`snapshots/matchups/{season}/wk{NN}/{stamp}.json`, deduplicated by content — a snapshot is
written only when the payload differs from the previous one.

Three properties, each chosen against a specific way this went wrong:

- **Outside the database.** `rm data/lockin.db` is what destroyed 24 weeks. Snapshots are
  files under version control, so rebuilding the database cannot touch them.
- **Deduplicated.** A stable season costs 25 small files rather than one per ingest, so
  in-season daily polling does not churn. The directory listing *is* the mutation history.
- **Earliest is preserved, never overwritten.** Drift is always measured against first
  observation, not against the last run, so a slow sequence of small changes cannot
  accumulate unnoticed.

`lockin reconcile` grew an advisory drift check that reports how many starter values have
changed since first observation. Advisory rather than failing, since today's data is
canonical by decision — but never silent, which is the failure mode that cost us the
season.

Box scores are not snapshotted: they were byte-identical across the mutation and run ~2MB
per week, so they are refetchable rather than irreplaceable.

---

## 13. Phase 3 — complete

The projection layer is built and its gate is closed. All six gates pass on **held-out
weeks 18-25** (5,126 player-games), with weeks 1-17 used to choose the model's
hyperparameters and never scored.

```
[PASS] right-tail quantiles match realised frequencies, out of sample
       q0.90: 0.0964 vs 0.100 (z=-0.87); q0.95: 0.0468 vs 0.050 (z=-1.04); q0.99: 0.0105 vs 0.010 (z=+0.38)
[PASS] central quantiles match realised frequencies, out of sample
       q0.25: 0.7435 vs 0.750 (z=-1.08); q0.50: 0.4941 vs 0.500 (z=-0.84); q0.75: 0.2483 vs 0.250 (z=-0.27)
[PASS] left-tail calibration (advisory)
       q0.05: 0.9444 vs 0.950 (z=-1.84); q0.10: 0.8872 vs 0.900 (z=-3.04); 82% of sub-decile mass is DNP outcomes
[PASS] calibration holds through the fantasy playoffs
       n=2022; q0.90: 0.0984 (z=-0.24); q0.95: 0.0440 (z=-1.23); q0.99: 0.0089 (z=-0.50)
[PASS] DNP hazard is calibrated and informative
       predicted 0.2898 realised 0.2991 (z=-1.44); log loss 0.3660 vs base rate 0.6101
[PASS] projection is sharper than the naive predictors
       CRPS model=8.132 own-history=10.074 (+19.3%) league=11.082 (+26.6%)
[PASS] projections use no data at or after as_of
       36/36 projections identical with the future removed, across 3 cutoffs
```

The exit criterion was "out-of-sample quantile calibration, checked specifically in the
right tail". The 99th percentile lands at 1.05% realised against 1.00% nominal, which is
the number that matters most: the tail is the entire reason passing on a game is ever
correct.

### Measuring calibration on a distribution with an atom

The obvious method is wrong here. This predictive distribution is a mixture — about a
quarter of its mass sits exactly on 0.0 (the player did not play) and the rest lives on a
half-point lattice — and the textbook probability integral transform is **not** uniform
for a discrete distribution even under a perfect model. Using it would have reported a
correct model as broken, and the natural response to that would have been to "fix" the
model until the diagnostic looked right, which would have broken it for real.

The gate uses the **randomised PIT**: `u = F(y⁻) + v·(F(y) − F(y⁻))` with `v ~ U(0,1)`.
That restores exact uniformity under a correct model, atom included, and every quantile
check falls out of it as a binomial proportion with an honest standard error.
`tests/test_projections.py` pins this by driving the PIT with a zero-inflated
distribution that *is* its own predictive distribution — so any departure from uniformity
there is a bug in the diagnostic rather than in a model.

An earlier attempt checked exceedance directly — count how often the realised score beat
the predicted `q_τ`. It reported the model as badly biased at every level at once, which
was the tell. `np.quantile` interpolates, so `q_τ` lands *between* two points of the
half-point lattice and `P(Y > q_τ)` is then systematically wrong by construction. The
estimator was broken, not the model.

### What the model is

Three stages, all reconstructible point-in-time from box scores:

1. **DNP hazard** — ridge-penalised logistic regression, nine features, refit from scratch
   at every distinct `as_of`. Log loss 0.366 against a 0.610 base rate.
2. **Minutes** — a recency-weighted EWMA *level* (half-life 14 games) multiplied by a
   shock drawn from the empirical distribution of (actual ÷ trailing EWMA) ratios pooled
   across the league.
3. **Components** — whole stat lines resampled from games in the same minutes bucket and
   position group, scaled to the drawn minutes, then rounded back onto the integer
   lattice and widened by a lognormal form factor (σ = 0.11).

### Four things the data forced, none of which the plan anticipated

**`player_status` is empty, so the planned DNP features do not exist.** The architecture
doc's §9 feature list opens with "injury designation", and `/players/nba` publishes only
*today's* — no history, so it is unusable for a replay under §3's point-in-time rule.
`dnp_reason` is NULL on all 16,692 unplayed rows. The hazard is therefore built entirely
from observed availability, rest and load. This turned out to be much less costly than it
sounds, because availability is strongly autocorrelated: 76.4% of games following a DNP
are also DNPs, against 9.0% following a played game.

**An EWMA of availability is the wrong shape for injuries.** The first hazard used three
EWMAs of the DNP indicator and was over-confident in its 0.10-0.20 band — precisely the
game-time-decision cases. Adding the **consecutive-DNP streak** cut log loss from 0.333 to
0.330 and tightened the right tail (q0.99 z from +1.69 to +0.38). An EWMA blurs
"mid-injury" and "just back", which are the two states that matter, and the streak
separates them.

> **Correction (found while designing the Phase 4 path simulator).** That change originally
> added *two* features, the streak and "games since last played". They are the same number
> by construction — byte-identical on all 15,847 rows — so the model carried nine features
> under ten names and the ridge penalty quietly split one coefficient between two columns.
> Nothing failed loudly, which is the point. Re-expressing the second in calendar days made
> it genuinely distinct (correlation 0.94) and still bought nothing: log loss 0.3299 against
> 0.3297 without it. It is dropped rather than kept for appearances. The gate is unchanged
> to three decimal places. `test_no_two_hazard_features_are_the_same_number` now checks the
> whole feature matrix pairwise, because this class of bug is invisible to every other test.

**Minutes shocks are left-skewed, and a symmetric shock breaks the bottom of the
distribution.** Multiplicative Gaussian noise on minutes left the left tail far too thin.
The realised (minutes ÷ trailing EWMA) ratio has its 1st percentile at 0.38 and its 99th
at 1.70 — foul trouble, blowouts and in-game knocks have no counterpart on the upside.
Replacing the parametric shock with a bootstrap of that empirical ratio distribution
dropped the PIT uniformity χ² from 34.7 to 21.1 and moved the median from z = +1.7 to
z = −0.05.

**A bootstrap cannot exceed its own sample maximum, and the tail is the whole point.** A
player has around forty games of history, so resampling his own lines understates how
good his best game can be. Unfixed, realised P(score > predicted q₀.₉₉) was **1.76%
against a nominal 1%** — the model would have been systematically over-confident that
tonight's good score was unbeatable, and would have banked too eagerly. A median-preserving
lognormal factor on the whole line (σ = 0.11, chosen on weeks 1-17) fixes it without
shifting the centre.

### The late-season rest shift is real, and the model tracks it

Architecture doc §9 warned that a DNP model fit on November behaviour would be badly
miscalibrated in the fantasy playoffs, when playoff-secured NBA teams start resting
starters. That warning was correct and the effect is large: the DNP rate among rostered
players runs 22.7% in weeks 1-7 and **33.9% in weeks 22-25**.

Because the hazard is refit at every cutoff and carries a season-stage feature, it
follows: over the holdout it predicted 28.98% against a realised 29.91%, and the tail
checks restricted to weeks 22-24 alone (n = 2,022) come in at z = −0.24, −1.23 and −0.50.
This is the check the doc asked for, and it is now pinned by
`test_late_season_dnp_rate_rises_sharply`.

### Known bias: the model is slightly too optimistic about busts

The bottom PIT decile holds 11.3% of the mass instead of 10% (z = −3.04), and 82% of that
excess is DNP outcomes. The hazard is mildly under-confident in its 0.10-0.20 band. This
does not fail the run — the Phase 3 criterion is the right tail, which is clean — but it
is reported on every run rather than left to be rediscovered, because **the direction is
decision-relevant**: understating bust probability overstates the value of *passing* on a
game, so the engine's residual bias is toward riding rather than banking. Anything in
Phase 4 or 5 that looks oddly reluctant to lock should suspect this first.

Two things were tried and did not fix it. The streak features improved discrimination and
the right tail but left the left tail unchanged (z −3.14 → −3.04). Post-hoc recalibration
was rejected rather than attempted: fitting it honestly needs cross-validated predictions
at every cutoff, and fitting it on in-sample predictions would produce a recalibration
that is itself wrong — a lot of machinery for roughly one percentage point.

### The leakage rule is now executable

Architecture doc §9 calls the point-in-time rule non-negotiable and asks for "a hard date
cutoff in the projection interface, not a convention". As built:

- `as_of` is a **required positional argument** of `ProjectionSource.project`. An
  optional cutoff is a cutoff that eventually gets left out.
- The implementation truncates history itself via `PlayerHistory.before(as_of)` rather
  than trusting the caller. Every quantity — hazard coefficients, shock library, donor
  pool, minutes level — is refit from rows strictly before the cutoff.
- The cutoff is by **day**, not timestamp, which is slightly conservative: an evening game
  cannot learn from an afternoon one. That is the correct direction to err.
- `lockin calibrate` rebuilds the panel with the future deleted and asserts the
  projections are bit-identical. Calibration this good is also what leakage looks like,
  so the claim needed a test rather than an argument.

### Deviations from the plan

- **No season-stage-specific rest *calibration*, in the sense of separate fits.** The plan
  said "calibrate rest risk separately by season stage". Refitting at every cutoff with a
  stage feature achieves the same end with one model, and the playoff-week check confirms
  it. Splitting the fit would have thinned the training set exactly where it is needed.
- **"Role" is the minutes bucket plus the position group**, not a richer role model. For a
  player's own donors he is his own cohort, so role conditioning only bites in the pooled
  fallback for thin histories.
- **A pooled fallback was added, which the plan did not specify.** Players with fewer than
  12 prior played games draw donors partly from a league cohort matched on minutes bucket
  and position, in proportion to how much own history exists. Without it the layer simply
  refuses on early-season and newly-acquired players, and Phase 4 would have hit that
  immediately. `basis` on every distribution records whether it was `own`, `mixed` or
  `pooled`.
- **Blowout truncation is not implemented.** The plan listed it as a later refinement and
  it stays deferred; the donor-scaling approach partly absorbs it, since a blown-out
  starter's short line is itself in the donor pool.

### What Phase 4 inherits

`ScoreDistribution.prob_above(threshold)` is the quantity a lock threshold is defined by,
and it is now measured rather than assumed. `score_matrix` scores simulated component
lines about three orders of magnitude faster than the per-line path while being proven
equal to it, which is what makes a rollout policy tractable.

**Correction to an earlier draft of this section**, which said the open risk was
cross-player correlation and filed it under Phase 5. That understated it and put it in the
wrong phase. The larger dependence is *within* a player's own week, and it blocks Phase 4
rather than Phase 5.

### The marginal is calibrated; the joint is not, and Phase 4 needs the joint

`project()` returns the distribution of **one** player-game from **one** cutoff, and that
is what the Phase 3 gate measures. It is correct and sufficient for what it claims. It is
not sufficient for simulating a week, because calling it once per remaining game and
treating the draws as independent is badly wrong:

```
             observed        under          understated
games/week   P(all DNP)      independence   by
    2          0.1994          0.0633          3.1x
    3          0.1609          0.0147         11.0x
    4          0.1579          0.0056         28.1x
```

Availability is a persistent *state*, not a per-game coin flip: an injured player misses
the whole week. Independent draws price a four-game washout at 0.6% when it happens 15.8%
of the time.

The decision-relevant form of this is stark. Given a player who played at least one game
earlier in the week, **P(his final game is a DNP) = 13.4%** — that is the rate at which
riding to Sunday collects a 0.0, and it is precisely the disaster the tool exists to
prevent (architecture doc §12's roster-7 example). Independent sampling would put it near
2%.

**Both known biases push the same way.** The left-tail DNP bias overstates the value of
passing, and independent within-week draws overstate it again — more independent chances
at an outlier makes the maximum of the remaining games stochastically larger, while the
washout risk vanishes. They compound rather than cancel, and the direction is toward
riding.

Note the two consequences are not the same kind of thing:

- As a *test*, this makes Phase 4's gate harder to pass — greedy will lock less than it
  should, so the measured gap over never-lock shrinks. Passing anyway would be a strong
  result.
- As a *product*, it is not conservative at all. It biases the policy toward the exact
  failure mode the engine is meant to avoid.

**The fix is small and the API already allows it.** `dnp_feature_row` takes
`(prior_days, prior_played, target_day, fantasy_week)` as arrays, so a simulated path can
feed its own drawn outcomes back in: fit the hazard once at `as_of` (no leakage — the
coefficients are still point-in-time), then apply it along each simulated path with the
state updated after every drawn game. The minutes EWMA extends the same way. What Phase 4
must not do is call `project()` n times and multiply.

### Cross-player correlation is also more common than assumed

**42% of roster-weeks (105/250) start two or more players from the same NBA team.** That
is far from the rare case worth deferring. It still does not affect a marginal calibration
check, and it can stay deferred through Phase 4 — but Phase 5's win-probability model
needs the variance of a *team* total, and at 42% overlap a teammate-independence
assumption there is not defensible.

---

## 14. Phase 4 — complete

Simulation and the base policy are built, and the gate closes on **held-out weeks
18-25** — 80 roster-weeks, 480 starter-weeks, all ten rosters.

> The table below averages each policy over **all 80** held-out roster-weeks, which was
> well defined while every policy ran everywhere. Phase 5's rollout does not — it needs an
> opponent — so §15 and the CLI report over the common subset instead. The gate numbers are
> paired and are unaffected either way.

```
  policy         points   zeroed   locked      wins
  never_lock      193.0       71        0     33/66
  lock_first      227.8       16      464     42/66
  greedy          272.8       36      355     61/66
  oracle          300.0       16        -         -   perfect foresight, not attainable
  actual          244.1        -        -         -   advisory: reads the field Sleeper rewrote

[PASS] greedy threshold beats never-lock on points, out of sample
       +79.78 points per roster-week (se 5.52, t=14.44) over 80 roster-weeks
[PASS] greedy threshold beats lock-first on points
       +45.03 points per roster-week (se 4.77, t=9.44)
[PASS] greedy does not approach perfect foresight
       never-lock 193.04 < greedy 272.82 < oracle 300.04; greedy captures 74.6% of the headroom
[PASS] greedy zeroes no more starter slots than never-lock
       480 starter-weeks; never_lock: 71 (14.8%); lock_first: 16 (3.3%); greedy: 36 (7.5%)
[PASS] greedy actually chooses when to bank
       locks 355/480 starter-weeks (74.0%); lock-first 96.7%
```

The exit criterion was "greedy threshold beats never-lock on points, out of sample". Met,
by a wide margin, which is itself the thing that needed checking.

### The gain is large, and that had to be explained rather than celebrated

Architecture doc §12 says an honest backtest shows a *modest* points gain and that a large
one should be treated as leakage. +79.78 points per roster-week is 41% over never-lock and
greedy also beats the human baseline by 28.7. On the doc's own advice that is a result to
distrust.

Two things resolve it, and neither is "the model is good".

**Never-lock is genuinely awful in this format.** It zeroes 14.8% of starter slots —
roughly one starter in seven counts nothing — because an unlocked player's final game
counts even when he does not play it. Most of the headroom is not clever stopping; it is
declining to throw away a slot.

**The real check is against perfect foresight, not against a feeling about size.** An
oracle that banks each player's best game scores 300.04. Greedy captures **74.6%** of the
gap between never-lock and that ceiling. For iid draws the *optimal* stopping rule captures
70.7% / 74.4% / 76.9% of that gap at 2 / 3 / 4 games, which weights to ≈75.3% at the
observed mix of 3.44 games per starter-week.

So greedy lands just *below* the theoretical optimum for a policy with no foresight at all —
exactly where a correct implementation should sit, and where a leaking one could not. That
comparison is now the `check_no_foresight` gate rather than a note, with a 90% ceiling:
nothing without foresight gets past it, and anything that does is reading ahead.

### One walk, three policies

All three replayed policies are the same function over a week under different thresholds:

```
never lock        no threshold ever clears      ride to the end
lock first        every threshold is -inf       bank the first played game
greedy threshold  E[value of continuing]        bank when tonight beats it
```

Writing them separately would have invited them to differ for uninteresting reasons — a
mishandled DNP in one and not another — and the comparison would then be measuring the
difference between three pieces of code rather than between three policies.

The greedy threshold is backward induction over simulated paths:

```
V[last] = E[S_last]                the last game counts whether or not you bank it
V[k]    = E[max(S_k, V[k+1])]      bank it, or carry on
```

`V[0]` is what tonight's score has to beat. It is a **scalar** — the policy compares a
realised score against an unconditional expectation — which is precisely what makes this
the base heuristic rather than the optimal policy. Conditioning the continuation on the
state is what Phase 5's rollout adds, and it is the one improvement still on the table.

### The path simulator, and why `project()` could not be used

Built as specified in §13's constraint. Availability is propagated: each drawn outcome is
fed back into the hazard's feature vector before the next game is drawn, with coefficients
fit once at `as_of` and never refit, so nothing reaches past the cutoff. Against the
observed dependence:

```
games/week   observed P(all DNP)   simulated   independent draws of the same marginals
    2              0.1994            0.1673              0.1194
    3              0.1609            0.1884              0.1249
    4              0.1579            0.1501              0.0838
```

Not exact — 3-game weeks come out high and 2-game weeks low — but in the right
neighbourhood and far away from what independence gives. The residual is the piece not
propagated: minutes shocks are still drawn per game, and a minutes restriction really does
persist across a week. Splitting the shock into persistent and per-game parts would need a
parameter, and there are no held-out weeks left to fit one honestly.

A **form factor** is also held fixed per path — one lognormal line factor across a
player's week, so a heavy week is heavy in all his games. Because each game still sees a
draw from the same distribution, this changes the joint without disturbing the marginal
that Phase 3 certified. `test_a_path_game_has_the_same_marginal_as_a_direct_projection`
pins that.

### Slot assignment

`assign_slots` solves the bipartite problem exactly via
`scipy.optimize.linear_sum_assignment`, as the plan specified. Greedy assignment fails here
for a concrete reason rather than a theoretical one: `C` accepts only C/PF, so filling
`UTIL` with the best player left can strand the only centre. Duplicate slots keep separate
identities (`UTIL#1`, `UTIL#2`) — a dict keyed on the bare name silently drops one of this
league's two UTILs.

It is **not** used in the backtest. The replay holds each roster's actual lineup fixed and
varies only the stopping rule, which is what isolates the decision the engine makes.
Letting the policy pick lineups too would confound stopping with assignment and would
compare against lineups nobody fielded. Assignment earns its place in the live digest and
in Phase 5.

### What the numbers say about the format

- **Zeroed slots are the dominant failure.** Never-lock 14.8%, greedy 7.5%, lock-first
  3.3%. Lock-first zeroes least and still scores 45 points less than greedy, so the goal is
  not to minimise zeros — it is to avoid them while still riding for upside.
- **Greedy locks 74% of starter-weeks.** Between the degenerate ends, and close to the
  human lock rates from Phase 2 (44.6%-67.6%), which is mild corroboration that the
  threshold is in a sane place rather than a corroboration of the humans.
- **Wins move a lot** — 61/66 against a never-lock opponent, against 33/66 for never-lock
  itself. That is the unilateral question and it is flattering by construction; the
  opponent is playing the worst available policy. Phase 5's gate is wins against a
  *competent* opponent, which is the harder and more meaningful comparison.

### Deviations from the plan

- **No `simulate.py`.** The plan's layout put a vectorised Monte Carlo module in `core/`.
  The path simulator belongs to the projection model — it shares the donor, minutes and
  hazard machinery — so it is `EWMAProjectionSource.project_path`, and `core/policy.py`
  holds the decision logic. A separate `simulate.py` would only have forwarded calls.
- **Rollout is absent**, as planned; it is Phase 5.
- **The five-policy table from architecture doc §12 has four rows here.** Rollout is Phase
  5, and `Actual` is reported but never gated on, per §12's decision.

### What Phase 5 inherits

The gate to beat is wins, not points, and against a real opponent rather than never-lock.
Three things are already in place: `manager_profiles` from Phase 2 gives a per-manager
prior on how each rival stops, `ScoreDistribution.prob_above` gives the marginal a
win-probability model needs, and `assign_slots` handles the coupling once players start
locking into slots.

The open problem is the one §13 flagged and Phase 4 did not need: **teammate correlation**.
42% of roster-weeks start two or more players from the same NBA team. A team total is a sum
over six correlated players, and P(win) depends on the variance of that sum, not on six
marginals. Phase 4's gate is a mean comparison and is unaffected — means add regardless of
correlation — but Phase 5's is not.

---

## 15. Phase 5 — complete

Rollout, the opponent model and the threshold output are built. The gate closes on the
restated §7.1 criterion — **all ten rosters, paired**, because the doc's literal "held-out
weeks" is not measurable at this effect size.

```
  means over the 66 of 80 held-out roster-weeks where every policy ran
  policy         points   zeroed   locked      wins
  never_lock      213.6       30        0     33/66
  lock_first      233.5        9      387     42/66
  greedy          289.5       13      303     61/66
  rollout         284.1       15      264     59/66
  oracle          310.0        9        -         -   perfect foresight, not attainable

  rollout vs greedy, both against a greedy opponent, all ten rosters:
    236 team-weeks — rollout 127 wins, greedy 118; flipped +14/-5, McNemar z=+2.06

[PASS] rollout beats greedy on wins, all ten rosters, paired
[PASS] rollout does not lose on wins in held-out weeks 18+
       66 team-weeks: rollout 35, greedy 33; flipped +3/-1, z=+1.00
       — only 4 discordant pairs, too few to resolve significance (§7.1)
[PASS] rollout trades points for win probability, as the objective intends
       -1.74 points per roster-week against greedy (se 1.22, t=-1.43)
```

**Rollout gives up 1.74 points per roster-week — 5.4 on the held-out block — and gains
nine wins.** That is the
objective working, not failing. Architecture doc §4 asks for P(win), not points, and the
two diverge exactly where it matters: trailing badly the correct play is to take variance
and pass on a safe score, leading comfortably it is to bank everything. A rollout that
matched greedy on points would be evidence it was ignoring the opponent.

It also zeroes far fewer starter slots — **15 against greedy's 38** — which is the same
mechanism seen from the other side. A win-probability objective hates a zero more than a
points objective does, because a zeroed slot loses a week outright rather than shaving a
margin.

### §7.1 was right, and it was still not quite enough

The adopted fix — replay all ten rosters — turns 21 matchups into 105 and is what makes
the pooled test resolve at all (z = +2.06 over 236 team-weeks). But the *held-out block on
its own* yields 66 team-weeks and only **4 discordant pairs**, which cannot resolve
anything. That is exactly the failure §7.1 predicted, one level up.

So the gate is stated honestly rather than stretched: the pooled comparison is the
criterion, the holdout is checked for **direction only**, and the number of discordant
pairs is printed so nobody reads `z=+1.00` as evidence of anything. Weeks 1-17 informed
the projection layer's hyperparameters, but both policies consume the same projections, so
the *comparison* is not obviously advantaged by that.

### The finding that made the first build fail: a lineup slot is evidence

The first working rollout scored **worse** than the policy it was meant to improve on —
−7.3 points and a losing win record. Rollout policy improvement is supposed to be no worse
than its base policy in expectation, so this was a model error, not a tuning problem.

It was, and the cause is worth recording because it is not a bug:

```
                   simulated P(DNP)   realised   bias
started players          0.172          0.085   +0.087
benched players          0.363          0.391   -0.028
```

**The hazard predicts twice the absence rate that started players actually have.** Managers
read the injury report before setting a lineup; the model cannot, because `player_status`
is empty for the whole season and `/players/nba` publishes only today's designation (§13).
The lineup decision therefore *encodes* information the projection lacks, and conditioning
on it is not optional — it is worth about 4.97 points per started player-game, or **26
points on a six-man team total**.

The consequence was precisely the observed failure. An underestimated opponent makes the
matchup look winnable, which makes banking a safe score look sufficient, so the engine
banked too eagerly and gave up the upside it needed.

The correction is `starter_dnp_scale`: the realised-over-predicted DNP ratio among started
player-games in **strictly earlier weeks**, applied as a multiplier on the hazard. It is
point-in-time by construction, has nothing to tune, and estimates a directly observable
quantity. It is best understood as a stand-in for the injury feed the live engine will
actually have — which makes it a backtest artefact that should *shrink* in production, not
a permanent fudge.

Diagnosing this took four measurements and is the most valuable thing in the phase. The
first three were wrong turns worth recording: the projection is unbiased at week start
(−0.02 on the per-game marginal), unbiased conditional on an early DNP (+0.012 on the
hazard), and unbiased conditional on "greedy has not fired yet" (+1.33 on the stopping
value). Only splitting by *started vs benched* found it.

### Teammate correlation: measured, and it does not matter

§14 left this as Phase 5's open problem — 42% of roster-weeks start two or more players
from the same NBA team, and P(win) depends on the variance of a team total rather than on
six marginals. Measured, it is a non-issue:

```
teammate pairs in the same game            34,198
corr of standardised scores                 -0.012   (both played: +0.004)
corr of availability                        +0.073
```

Usage and pace effects cancel almost exactly. Decomposing the variance of real team totals
confirms it end to end: total Var(z) = 1.23 splits into a **within-week** component of
**0.86** — where a teammate correlation would show up, and where independence is if
anything mildly conservative — and a **between-week** component of 0.47, which is the
light-slate effect and is modelled explicitly, since the simulator is given each player's
actual remaining fixtures.

**Independence across players is therefore kept, with evidence rather than as an
assumption.** This is the rare case where a flagged risk turns out to cost nothing.

### Threshold output: closed form, not a search

§11 asks for a binary search over hypothetical *S* to find where `V(lock|S) = V(pass|S)`.
No search is needed. Writing `D = opponent − banked − others`, the value of locking at *S*
is `P(S > D)` — the CDF of `D` at *S*, increasing in *S* — while the value of passing does
not depend on *S* at all. The crossing is exactly the `p_pass`-quantile of `D`: one sort,
exact rather than converged to a tolerance. `test_threshold_matches_a_brute_force_scan`
checks it against the search the doc proposed.

Per §7.2 forward thresholds assume **no action on the intervening nights**. A threshold
that quietly assumed you had followed yesterday's advice is wrong exactly when you needed
it.

### Deviations from the plan

- **Decisions are taken once a day**, at the end of the day, rather than per game. That is
  the product — the tool is read once a day — and a policy assuming intra-day check-ins
  would measure something nobody will do.
- **The opponent is a policy, not a belief state.** §10's live inference reads a frozen
  `players_points` to detect a lock one game later, sharpened by the manager profile.
  Retrospectively there is nothing to infer from, and §12 makes that field unreliable
  anyway, so the opponent is simulated under the base policy with already-played games
  *resolved* rather than sampled. `manager_profiles` is therefore built and validated but
  not yet consumed; wiring it in is Phase 6 work on live data.
- **`assign_slots` is still not used in the replay.** Lineups stay fixed so the comparison
  isolates stopping. It is exercised by tests and waits for the digest.

### What Phase 6 inherits

`standing_thresholds` and `decision_for` are the digest's two calls, and both are already
in the shape §11's daily digest needs. What is missing is live-only and cannot be
backtested: today's injury designations, the polling loop that makes `weekly_matchups`
accumulate the freeze history live lock inference needs, and the opponent belief that
history feeds. The `starter_dnp_scale` correction is the seam where the real injury feed
should replace the proxy.


---

## 16. Manager decision quality — an analysis, not a phase

`lockin managers` ranks the ten managers on how well they decided, with roster talent
divided out. It is not part of any phase gate; it exists because the machinery Phase 5
built for the engine turns out to answer a question about the humans.

### Two metrics, and why the obvious one is wrong

The first version scored **points capture** — of the upside a decision could have won,
how much did they take:

```
share = (counted − riding to the end) / (their best game − riding to the end)
```

Single-game weeks, and weeks where riding was already optimal, contribute nothing to
either half and drop out on their own, so this measures only decisions that could be got
wrong. That much is sound. What is not sound is the objective:

> A manager 40 points down on Sunday should **decline** to bank a safe 45 and ride a
> boom-or-bust game instead, because banking it still loses. That is the right call, and
> points capture scores it as a blunder.

So the ranking is sorted on **win-probability regret** instead — for each real decision,
V(lock) and V(pass) from the Phase 5 rollout given the actual state, and the manager is
charged the win probability they forfeited. Points capture is still reported for contrast
and never sorted on.

### The correction is not cosmetic

```
decisions evaluated: 2293
  points-optimal and win-optimal AGREE : 2043 (89.1%)
  they DISAGREE (high leverage)        :  250 (10.9%)

  win-optimal says PASS where points says lock : 190   (mean best P(win) 22.3%)
  win-optimal says LOCK where points says pass :  60   (mean best P(win) 62.6%)
```

190 of 250 are exactly the case above. Switching metrics moves four managers by two or
more places and changes who is first.

**A nuance that cuts the other way:** mean regret is *lower* on divergent decisions
(0.755%) than on concordant ones (1.329%). Divergence arises precisely when a matchup is
already lopsided, so the marginal win probability between banking and riding is small.
High-leverage decisions are real, and individually cheaper than ordinary ones.

### What the season says

The spread is wide and the ends are cleanly separated; the middle is not. Bootstrapped 90%
bands overlap from roughly rank 2 through rank 8, so the table should be read as three
groups rather than an ordering. The most interesting single result is roster 10, who ties
for first on points capture and is *last* in the league on divergent decisions (31.8%) —
excellent at the points game, blind to the matchup.

### Three limits, printed with every run

1. **It reads the field Sleeper rewrote** (§12). This ranks how the current data makes
   each manager's decisions look, not a certified record.
2. **Model error is charged to the manager.** A call scored wrong may reflect injury news
   the projection cannot see — the blind spot worth 26 points per team total in §15.
3. **The engine must not be benchmarked on this scale.** Greedy's thresholds come from the
   same projection model that computes the win probabilities grading it, so its errors are
   shared between deciding and being judged. It scores 0.172% regret against the best
   human's 0.511%, and that gap is an artefact of the shared model rather than a finding.
   Manager-versus-manager is fair because all ten are graded by a model none of them share.

### A bug this surfaced

Week 25 is in the stats feed with a full set of starters, but the league never scored it —
`last_scored_leg: 24`, and 37 of its 60 starter values are 0.0. Counting those as decisions
read every one as a catastrophic blunder and cost about eight points of apparent points
capture per manager. `last_scored_week()` now reads the league's own setting rather than
hardcoding a number, since a season that ended early would move it. The regret ranking was
never affected: week 25 has no `matchup_id`, so no decisions were evaluated there.
