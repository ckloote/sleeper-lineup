# Sleeper NBA Lock-In Engine — Implementation Plan

**Companion to:** `sleeper-lockin-engine-architecture.md`
**Status:** Approved — **Phases 0-5 complete** (§9, §10, §11, §13, §14, §15). Phases 3-5 were
reassessed and taken on after Phases 0-2 landed, as §6 anticipated. Phase 6 (digest, deployment)
remains, and is mostly live-only work that cannot be backtested.
**Written:** 2026-08-05 (offseason — Sleeper global state is `season_type: off`, week 0)

This plan takes the architecture doc as the spec. Everything below either confirms it
against the live API, or proposes a change with the evidence for that change. Section 8
lists the decisions I need from you before starting.

**Phase write-ups:** §9 Phase 0 · §10 Phase 1 · §11 Phase 2 · §13 Phase 3 · §14 Phase 4 ·
§15 Phase 5. **Findings that changed the design:** §12 Sleeper mutates completed seasons ·
§16 manager decision quality · §17 `pit_*` is not point-in-time · §18 where availability
data could come from · §19 why start/sit cannot be backtested.

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

> ⚠️ **This section is wrong. See §17.** The embedded `player` object is *also* a live
> snapshot — Sleeper writes it at fetch time, not at game time — so `pit_positions` and
> `pit_team` are today's values stamped on a historical row, and preferring them over
> `players` protects nothing. The "zero drift in week 12" reported above is zero **by
> construction**, which is the tell that was missed: a null result read as reassurance
> when it was diagnostic of a broken measurement.

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

### Phase 6 — CLI, digest, deployment — COMPLETE, see §20
`ingest / digest / explain / backtest / verify`, push notification, cron via
`uv run --frozen`.

*Exit:* digest fires daily and thresholds render legibly on a phone.

> Met in every part that does not need a live league. There is none — the 2026-27
> league still returns `[]` — so `digest` is built **as-of** a date and smoke-tested
> against the recorded season, which is what §7.3 prescribed. §20 records what that
> turned up: §7.2's idle-nights rule was not expressible in the Phase 5 signature,
> and §11's recommended lineup does not ship.

**Dashboard requirement — manager decision quality (§16).** The league standings
already tell you who won. What nobody can see is who *decided* well, and the
Phase 5 machinery answers it. Surface the `lockin managers` ranking in the
dashboard.

Read it from `manager_scorecards` and `manager_decisions`; do not call
`evaluate_managers` from a request. Producing those rows costs several seconds
of Monte Carlo — fine for a command, hopeless for a page load — and the design
rule is that SQLite is the contract, so a dashboard is just a second reader.

Four things the rendering must get right, each of which is a way to make the
data lie:

- **Sort on `mean_regret`, never on `upside_share`.** Points capture scores a
  correct variance-taking decision as a blunder (§16). The column is stored for
  contrast; a dashboard that lets someone sort by it has published a wrong
  ranking.

  > **Superseded in part — see §20.** The *first* half is stale: §16 replaced raw
  > regret with `squandered_share`, because mean regret is P(wrong) × E[stake] and
  > the second factor is circumstance. The shipped page sorts on the share, as
  > `lockin managers` and the schema already did. The prohibition on
  > `upside_share` stands and is enforced — the page carries no script, no sort
  > control and no header link.
- **Render `regret_lo`/`regret_hi`.** The bands overlap from about rank 2 to
  rank 8. A bare ordered list asserts a precision the data does not have; show
  it as groups, or show the bars.

  > **Amended — see §20.** Right in substance, wrong in column, and for the same
  > reason as above: `regret_lo`/`regret_hi` band raw regret, which is no longer
  > what the table is ordered by. `share_lo`/`share_hi` were added so the bars
  > express uncertainty about the number that actually sets the order. The
  > overlap is worse than predicted — ranks 1-4 are one tie.
- **Carry the §12 caveat on the page, not in a footnote.** This ranks how the
  rewritten data makes each manager look, not what they did.
- **Do not put the engine on the same axis.** Greedy is graded by the model that
  sets its thresholds; a column showing it beating every human would be an
  artefact.

`manager_decisions` is the drill-down: one row per call, with `p_win_lock`,
`p_win_pass` and what the points policy would have done, so "show me the 20
high-leverage decisions roster 4 faced" is a `WHERE` clause.

**Note:** Phases 0-5 ship a read-only CLI — `ingest`, `reconcile`, `verify`, `locks`,
`calibrate`, `backtest`, plus `project`, `teams` and `managers` for inspection and
analysis. Only `digest` remains, and it arrives with Phase 6.

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
| **Scope** | Phases 0-6 complete | Committed scope was 0-2. Phases 3, 4, 5 and 6 were each taken on after reassessment and each closed its gate (§13, §14, §15, §20). |
| **Phase 5 gate** | Replay all ten rosters | 105 matchups instead of 21. Lands on Phase 2 now as a gating requirement (§7.1). |
| **Lock mechanic** | Must stay in a slot to lock | Stricter state definition; confirms the Phase 2 inference method (§7.6). |
| **Deployment** | Develop here, deploy to Pi later | Hold the `uv` discipline and cron form; no Pi work in this build. Unchanged by Phase 6 — the cron form ships, the Pi does not (§20). |
| **Notifications** | ntfy, opt-in | Shipped in Phase 6. No account and no key: the topic name *is* the secret, so it is read from the environment and off by default. A digest posting to a guessable public topic would publish the lineup. |
| **Digest mode** | As-of a date, not live | There is no live league to be live against (§7.3, re-checked 2026-08-15). One code path, `--date` defaulting to today, so October changes nothing (§20). |
| **Lineup advice** | Not shipped | §16 measured it at 20.4 points a week *worse* than the managers. Ships as a DNP warning instead of a recommendation (§20). |

### Still open

Everything below is blocked on the 2026-27 season existing. Nothing is blocked on work.
All of it is due on one morning, so it is assembled as an ordered checklist in
[day-one.md](day-one.md) — including two hazards found while validating it: a second season
ingested into the same database silently hides the first, and the shipped crontab captured
no availability data at all.

- **§7.5 — forward-looking stat rows.** Unverifiable until the season opens. Does not
  block Phases 0-2, but the NBA schedule ingest built in Phase 0 is what makes the
  fallback free, so it gets built regardless. **Check on day one**: the digest's tonight
  section depends on it, and the fallback exists but has never been exercised.
- **§7.3 — 2026-27 league.** Config resolves the league by season rather than hardcoding.
  Nothing further needed until the commissioner rolls it over. Re-checked 2026-08-15:
  still `[]`.
- **§15/§20 — the polling loop.** `weekly_matchups` is append-only so a frozen
  `players_points` can reveal an opponent's lock one game later. Nothing has been polled,
  because there has been nothing to poll. Until then the digest's opponent model stands in
  a base policy for the belief, and the banked state assumes you followed the engine.
- **§17/§20 — availability history.** The daily capture into `player_status` is in place
  and running; it simply has no history yet. It cannot be backfilled, and it is the
  prerequisite for ever ranking start/sit decisions (§19).

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

### Circumstance, and how much of the ranking it explains

Raw win-probability regret is not a clean skill measure, and the reason is structural.
With a binary choice the regret on a decision is either zero or exactly the gap between
the two options, so

```
mean regret = P(wrong) × E[stake | wrong]
               ^skill      ^circumstance
```

and the second term is not the manager's doing. Lopsided matchups carry small stakes: a
mean of **3.0%** at P(win) under 20%, against **10.4%** in a live one. A manager who spends
the season being blown out collects low regret for free.

So the ranking sorts on **squandered share** — regret as a fraction of the win probability
that was actually at stake — which divides circumstance out.

Three measurements bound how much this ever mattered:

**Stakes are not what a stronger roster buys.** Across the ten managers, stakes per
decision correlate **+0.34** with win rate, not negatively. The effect is symmetric in
*competitiveness*, not in strength — hopeless situations are the low-stakes ones, so the
confound favours the league's worst teams more than its best.

**The metric partly self-corrects, because expensive decisions are easy.** P(wrong) falls
monotonically as the stakes rise:

```
   stakes band          n   P(wrong)
   [0.00%, 1.67%]     469     32.6%
   [1.67%, 4.00%]     472     30.1%
   [4.00%, 7.17%]     466     20.2%
   [7.17%, 12.50%]    467     14.6%
   [12.50%, 85.0%]    468     12.6%
```

The calls that would be costly to botch tend to have an obvious right answer, so the
regret metric is not as exposed to circumstance as the decomposition suggests. Raw regret
correlates **+0.81** with P(wrong) and only **−0.33** with stakes.

**Matching on difficulty reproduces the ranking.** `--competitive` keeps only decisions
taken with the matchup live (P(win) 30-70%), discarding about half the season and putting
every manager on comparable footing. Spearman against the full table is **+0.94**. Exactly
one manager moves at the top — roster 3, who went 20-1 and was favoured in 59.7% of his
decisions, drops from first to second. That is precisely the case the confound predicts,
and it is the only one.

### What the season says

The spread is wide and the ends are cleanly separated; the middle is not. Bootstrapped 90%
bands overlap from roughly rank 2 through rank 8, so the table should be read as three
groups rather than an ordering. The most interesting single result is roster 10, who ties
for first on points capture and is *last* in the league on divergent decisions (31.8%) —
excellent at the points game, blind to the matchup.

### Teams on paper, which is a different question

`lockin managers` also ranks the *teams*, because "was this side well run or merely good"
needs both halves. The obvious measure is the oracle already in the backtest — what a team
would have scored with every lock perfect — and it is close to right. Its three confounds
are worth stating, because only one of them turned out to matter:

**Schedule density: not a problem in this league.** Games per starter-week spans 3.25 to
3.40 across the ten rosters — a 4.6% range — and correlates −0.36 with the oracle. Over 24
weeks the schedule evens out. `talent_per_game` values the same lineup per game rather than
per week and is reported anyway, so a short or lopsided season would show it.

**Health and form: unavoidable.** Every number here is built from what players actually
did, so a team whose stars stayed fit looks better. A genuinely ex-ante measure would have
to come from the projection layer, and that is a different (noisier) thing.

**Lineup selection: the real confound, and it hides a story.** The plain oracle is taken
over the six the manager *chose*, so starting the wrong players depresses it — which is a
decision, not the roster. `ceiling` therefore picks the best legal six from the whole
roster via `assign_slots`, and the gap is reported separately:

```
   # roster manager           ceiling   oracle  lineup cost  talent/gm
   1      3 yinzknow            383.5    363.4         20.0      293.2
   2     10 jordany32           357.1    336.7         20.4      272.7
   3      9 coopermycupp        353.3    299.9         53.3      261.1
   ...
  10      1 smorgan83           310.9    283.2         27.7      239.1
```

Nine of the ten rosters give up 18-28 points a week to their lineups. **Roster 9 gives up
53.3** — and on the plain oracle he ranks 8th while his roster is the 3rd best in the
league. He is also last on decision quality (31.1% squandered). Two independent failures of
management on a genuinely good team, and a plain oracle would have shown only a mediocre
one.

That is the argument for separating the two: the confound is not noise, it is the finding.

### The decision ranking covers lock/pass only — and lineup quality cannot be measured yet

The ranking takes the six starters as given. Who to *start* is a separate decision and
probably a larger one: `lineup_gap` spans 18 to 53 points a week, against a lock-decision
spread that is smaller in points. So it was attempted properly, and it does not work.

`lineup_gap` itself is an oracle comparison, so it charges a manager for lacking foresight
— the same flaw that disqualifies points capture. The fair version is point-in-time: value
every rostered player with the projection layer at the start of the week, pick the best
legal six with `assign_slots`, and charge the manager the expected points forgone. That
produces a clean-looking table, 8.9 to 35.8 points per week, and it is **wrong**:

```
group                                n  played 0 games  projected  actual best
both started                      1025           0.5%       47.7         55.0
model wanted, manager benched      415           9.9%       41.3         35.4
manager started, model benched     415           2.7%       29.6         46.7
```

Players the model wanted and the manager benched were **four times more likely to miss the
entire week** (9.9% against 2.7%). The model rated the manager's picks at 29.6 and they
delivered 46.7 — it underrated them by 17 points each. Across roughly 1.7 swaps per
roster-week that is about 20 points a week, and **following the model's lineup would have
made nine of the ten teams worse** (mean −20.4/week; only roster 9 gained, by 0.9).

Two tells that the metric measures the model rather than the manager:

- The manager with the **highest** lineup regret (35.8) is roster 3, who went 20-1 and
  leads every other metric in the project.
- Removing the `starter_dnp_scale` correction changes the result by 0.1 points, so this is
  not an artefact of that correction being misapplied to bench players.

The cause is the blind spot already priced twice in this project: `player_status` is empty
and `/players/nba` publishes only today's designation (§13), which cost 26 points per team
total in §15 and costs ~20 points per week here. Managers read the injury report before
setting a lineup; the projection layer cannot.

**So no lineup-quality metric ships.** Ranking managers by deviation from advice that is
worse than their own would be actively misleading, and it would be *most* punitive toward
the best managers. `lineup_gap` is reported as what the lineups cost, with no claim about
whether they were mistakes.

**Prerequisite for ever measuring this:** a point-in-time availability feed, captured
daily during the season into `player_status` — which is exactly the table that exists in
the schema and has sat empty since Phase 0. That is Phase 6 work and it cannot be
backfilled, so it has to start the day the 2026-27 season opens.

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


---

## 17. There is no historical availability data — and `pit_*` is not point-in-time

Asked whether manager start/sit decisions could be evaluated, given §16 could not do it.
Checking properly turned up something larger than the original question.

### The embedded player object is a live snapshot

§3 is built on the claim that each stat row embeds a `player` object giving point-in-time
`fantasy_positions` and `team` "as of that game". It does not. Comparing the same 519
players across weeks 3, 12 and 20:

```
field                identical across all three weeks
  fantasy_positions    519/519  ALL IDENTICAL
  position             519/519  ALL IDENTICAL
  team                 519/519  ALL IDENTICAL
  injury_status        519/519  ALL IDENTICAL
  news_updated         519/519  ALL IDENTICAL
```

And against today's live `/players/nba`: **100%** agreement on both `fantasy_positions` and
`team`. The `news_updated` timestamps on week-12 rows have a median of **2026-06-30** — six
months after those games were played. The object is written when the request is served.

In our own database, **0 of 633** players ever had `pit_positions` or `pit_team` change
across the whole season. In a league with mid-season trades that is impossible for real
data, and it should have been noticed earlier.

§3's evidence was "week 12 showed zero drift between the embedded snapshot and today across
all 60 starters, so the effect is small in that week". The drift is zero *by construction*.
A null result was read as reassurance when it was diagnostic of the measurement being
broken — the same error shape as §16's week-25 zeros.

### What *is* point-in-time

The stat row's **own** `team` field, stored as `box_scores.team`, is genuine:

```
players whose stat-row `team` changed during the season : 104/602
players whose embedded `pit_team` changed              :   0/602

  James Harden     LAC (2025-10-22..2026-02-02, 49g)  CLE (2026-02-04..2026-04-12, 31g)
                   pit_team = CLE   <- applied to all 49 LAC games
```

So the guidance was exactly inverted: the column described as a "nullable convenience"
carries the history, and the columns named `pit_*` do not. The schema comment and §3 now
say so, and two invariant tests pin it — asserting the *broken* behaviour deliberately, so
that if Sleeper ever starts publishing real per-game snapshots the tests fail and that
becomes visible as good news rather than passing unnoticed.

> **Corrected 2026-08-15: the good-news detector had a false-positive mode, and it fired.**
> Re-ingesting a single week made `pit_team` vary across a player's games — player 1254
> reads `CLE` for 24 weeks fetched on 2026-08-08 and `CHA` for week 12 fetched a week later,
> because he was traded in between and only that week was refetched. The test reported
> exactly what it was built to report: per-game variance in `pit_team`. The variance was
> real; the inference would have been wrong.
>
> This is the same shape of error §3 made — a measurement whose result is determined by how
> the data was collected rather than by what it describes. There the drift was zero *by
> construction*; here the drift is nonzero by construction, and both read as findings.
>
> The test now partitions by ingest date, because "today's value stamped at fetch time"
> means rows from different fetches legitimately disagree. **Incremental weekly ingest makes
> that the normal state of the database**, so uncorrected this would have fired every week
> of 2026-27 and been believed the first time.
>
> Partitioning costs something, so a second test guards the cost: the claim only has force
> when some batch spans several weeks, and a database rebuilt purely by weekly increments
> would satisfy the assertion trivially. A test that can quietly become vacuous still
> reports success, which is the worst available outcome.

### What this does and does not cost

**It does not change any result.** Every number in this project was computed from the same
live values either way, because `pit_*` and `players` are the same data. Nothing needs
recomputing.

**It removes a protection that was believed to exist.** The projection layer's
`position_group` uses today's positions for every historical game, so a player
reclassified over the summer is grouped wrongly for the whole replayed season. That
affects the pooled donor cohort for thin histories and `assign_slots` in §16. The effect is
small — positions move less than teams — but it is real and it is not mitigated.

**`box_scores.team` is available as a genuine fix for team**, and is not yet used anywhere;
the projection layer does not need team today, so this is recorded rather than acted on.

### What this does NOT block: rating team quality

A natural next thought is that team quality is also unmeasurable, since durability is part
of it — a player who scores 100 a night and misses the season is worth nothing. The premise
is right and the conclusion does not follow. **Durability needs no injury data.** The
`played` flag says who suited up, and `ceiling` counts a missed week as zero, so a
season-ending injury already reduces a team's rating to exactly what it should be.

Injury data would say *why* a player sat and *whether it was known before tip*. That is
what start/sit evaluation needs. Team quality only needs to know that he sat.

`lockin teams` now breaks `ceiling` into the two things that produce it, so this is visible
rather than asserted:

```
   # roster manager           ceiling  available  pts/game  talent/gm  lineup cost
   1      3 yinzknow            383.5     70.6%      39.3      293.2         20.0
   2     10 jordany32           357.1     80.3%      36.7      272.7         20.4
   ...
  10      1 smorgan83           310.9     60.1%      32.9      239.1         27.7
```

Availability spans **60.1% to 85.2%** and correlates +0.36 with ceiling, against +0.90 for
scoring rate. Roster 1 is last mostly *because* of durability — his points per game played
is mid-table. The measure is already doing the work.

What team quality genuinely cannot do, and none of it is about injury data:

- **Separate durability skill from health luck.** Did roster 1 draft fragile players or get
  unlucky? Ten rosters over one season cannot tell.
- **Predict.** Every number is a record of what happened. An ex-ante rating needs a
  durability forecast, which is hard in a way this project has not attempted.
- **Attribute an absence.** Rest, injury and coach's decision are indistinguishable here.
  That one *is* the missing `dnp_reason`, and it is a nice-to-have for team quality where it
  is a blocker for start/sit.

### The answer to the original question: no, and it cannot be backfilled

Evaluating start/sit needs to know what a manager knew: who was out, who was a game-time
decision, who was a late scratch. None of it exists retrospectively.

- `dnp_reason` is NULL on all 16,692 unplayed rows.
- `player_status` has been empty since Phase 0.
- The embedded `injury_status` is live, as above — the one place it might have hidden.
- Sleeper publishes no historical endpoint (§12 established this for scores; it holds here).

So §16's refusal to ship a lineup-quality metric is not caution, it is a hard limit.

**What has changed: the record has started.** `lockin ingest` now writes today's
designations to `player_status`, keyed `(sleeper_id, as_of)` so repeated runs in a day are
idempotent and runs across days accumulate. The first run captured 110 designations (100
DTD, 9 Out, 1 IR). That does nothing for 2025-26 and everything for 2026-27, and it is the
kind of thing that is free to start now and impossible to start later — the same lesson
§12 paid for by losing 24 weeks of matchup history.

Evaluating start/sit decisions is therefore a **2026-27 capability**, gated on daily
capture beginning the day the season opens.


---

## 18. Where availability data could come from

§17 established that our own dataset has no historical availability. That is not the same
as it not existing. Surveyed, and probed where probing was cheap.

First, be clear what three different questions need answering, because the sources differ:

| question | needed for | have it? |
|---|---|---|
| Did he play? | everything | **yes** — Sleeper `played`, complete |
| *Why* didn't he play? | attribution, team-quality colour | partially, see below |
| Was it **known before tip**? | start/sit evaluation (§16) | **no** — this is the blocker |

### Verified: `nba_api` box scores carry a DNP reason

`BoxScoreTraditionalV3` has a `comment` field, and it is populated:

```
DNP - Coach's Decision
DND - Injury/Illness
DNP - Injury/Illness
```

`nba_api` is already a dependency, and `game_links` already maps our fixtures to NBA game
ids, so the plumbing exists. Two findings from probing eight random games, though:

**Coverage is 25%, and it misses the players we care about.** Of 102 rows Sleeper marks
unplayed, only 26 appear in the NBA box score at all. The other **75% are absent
entirely** — the NBA box score lists the gameday roster, while Sleeper generates a row for
every rostered player against every scheduled fixture. So the comment field documents
*healthy scratches* well and the *injured* poorly, which is the wrong half. Of the 26
reasons found, 22 were Coach's Decision.

**It reintroduces the ID crosswalk.** `players.nba_id` is 0 of 2107 populated. Architecture
doc §16 ranked the crosswalk the project's top risk and §2 dissolved it by taking box
scores from Sleeper. Any NBA-side *player* data brings it back — name matching with
accents, suffixes and nicknames. Fixture-level data (schedule, tipoffs) does not, which is
why the current `nba_api` use is cheap.

**Also: `BoxScoreTraditionalV2` is dead.** It is deprecated and returns zero rows for
2025-26. Anything written against v2 silently produces nothing.

### The right source, not yet retrieved: the official NBA injury report

The league has published a mandatory injury report since 2017, listing every player with a
designation (Out / Doubtful / Questionable / Probable) before tip. That is exactly the
"was it known" signal, and nothing else surveyed provides it.

`https://official.nba.com/nba-injury-report-2025-26-season/` returns 200 and is titled
"NBA Injury Report: 2025-26 Season", so the archive exists and is reachable. But the page
is a JavaScript app and its PDF links are client-rendered — a plain GET returns no
`playerinjury` URLs. A guessed `ak-static.cms.nba.com/referral/playerinjury/...` pattern
returned 403. Retrieving it needs either the API the page calls or a rendered fetch, and
the reports are PDFs that would then need parsing.

**Not attempted further.** It is a real lead, not a solved one.

### Others, known but unverified here

- **Basketball-Reference** — carries DNP reasons and per-player injury history. Check the
  terms of use before any automated access; they are explicit about scraping.
- **ProSportsTransactions.com** — a free, searchable archive of NBA injury and transaction
  history going back decades, widely used in published research. Probably the lowest-effort
  historical injury source.
- **Community archives** of the official injury report PDFs exist on GitHub and Kaggle,
  which would sidestep the retrieval problem if one covers 2025-26.
- **DARKO** — projections, not availability. Already noted in architecture doc §9 as a
  later enhancement with a leakage warning attached.

### Recommendation: do not chase this for 2025-26

The tool's job is deciding tonight, and **for live use none of this is needed** — Sleeper
publishes `injury_status` on `/players/nba`, and as of §17 `lockin ingest` records it daily
into `player_status`. From day one of 2026-27 the engine will have the availability signal
it was missing, from a source already integrated.

Historical injury data buys only the ability to *evaluate* start/sit decisions on a season
that is already over. That is an interesting analysis, not a capability the engine needs,
and the cost is an ID crosswalk plus PDF parsing for partial coverage. The honest ordering
is: keep capturing daily, revisit ProSportsTransactions or an injury-report archive if the
retrospective question ever becomes worth that price.


---

## 19. The cost: start/sit algorithms cannot be backtested

The practical consequence of §17 and §18, stated plainly because it changes the roadmap.

### It is the leakage rule in mirror image

Everywhere else this project guards against the backtest knowing **more** than the live
engine would — that is what §3's cutoff, `as_of`, and `check_no_leakage` are for. Here the
backtest knows **less**. A start/sit algorithm shipped for 2026-27 will have Sleeper's live
`injury_status`; replayed against 2025-26 it would not. Both directions break the same
thing: correspondence between what is tested and what would run.

So a 2025-26 backtest of a start/sit algorithm is uninformative in *both* directions. If it
looks bad, that is the missing input, not the algorithm — §17 measured the model's own
lineup picks at 20 points a week worse than the managers'. If it looks good, it is good at
a task nobody will ask it to do.

### Why lock/pass survives and start/sit does not

The two decisions sit on opposite sides of the information they need:

- A **lock** decision is made *after* a game. The score is known; the only forecast is over
  the games still to come, and the projection layer is calibrated for exactly that (§13).
- A **start/sit** decision is made *before* anything. It is entirely forecast, and its single
  largest input — is he playing tonight — is the one we lack.

That is the whole asymmetry, and it is why §16 can rank lock decisions and cannot rank
lineups.

### The salvageable part is small

One start/sit input needs no availability data at all: the published schedule. Starting a
player with four games rather than two is a real decision, knowable weeks ahead, and fully
backtestable. It is also weak in this format, because lock-in counts one game per player:

```
games scheduled that week   started 3.31   benched 3.23   edge +0.08
roster-weeks where starters averaged more games : 55% (against 37% fewer)
correlation, games scheduled vs that week's best game : +0.10
```

A +0.10 correlation and a 0.08-game edge is not an algorithm worth validating. Managers
barely favour the busier schedule, and they are close to right not to.

### Consequence for the roadmap

Every capability so far shipped behind a gate that could fail. A start/sit feature for
2026-27 would be the **first to ship unvalidated**, and that should be a deliberate choice
rather than something noticed later.

The sequencing that follows:

1. **Capture from day one.** In place as of §17 and unconditional as of §20: every
   `lockin ingest` records today's designations, with no flag that skips it. Nothing else
   here is possible without it, and it cannot be backfilled.
2. **Ship lock/pass, which is validated.** The digest can recommend locks from the opening
   week on Phase 3-5 evidence.
3. **Hold start/sit until it has its own gate.** By roughly week 10 of 2026-27 there would
   be ~100 roster-weeks of lineup decisions with genuine point-in-time availability —
   enough to build the gate the other phases had, in-season, before trusting it.

Shipping start/sit advice in week 1 on the strength of a backtest that could not have
tested it would be exactly the failure this project has spent five phases avoiding.

### What is actually built for this, and what is not — audited 2026-08-15

Asked whether step 3 will simply start working once the data exists. It will not. Only the
prerequisite is built, and the prerequisite is the *only* part that had to be.

| | state |
|---|---|
| Daily capture into `player_status` | **built** — unconditional since §20, tested |
| `assign_slots`, exact best-legal-six | **built** — `lockin/core/eligibility.py` |
| Anything that *reads* `player_status` | **nothing does** |
| Point-in-time lineup metric | removed when §16 showed it failing |
| A gate for start/sit | described here, not implemented |
| `START` / `SIT` in the digest | deliberately absent |

The third row is the one that matters. Every mention of `player_status` in the codebase is
a comment saying it is empty. The DNP hazard's features are schedule, rest days, 7-day
load, trailing-DNP run and season stage — **there is no injury-designation feature**.
Capturing the data does not make the model use it.

**And it is not a bolt-on.** `starter_dnp_scale` is a *proxy* for exactly this signal — a
blunt correction that scales the hazard down for started players, because a lineup slot is
evidence a manager read something the model could not (§15). Real availability data should
retire that proxy, which moves the **lock/pass** thresholds, not just lineup advice. So
this touches the validated core: Phase 3's calibration must be re-checked and Phases 4-5's
gates re-closed. Budget accordingly, and expect it to be genuinely uncertain — the honest
possibility is that the designations do not help enough.

The sequence, once there is data:

1. Add designation as a hazard feature; refit; check §13's PIT deciles still hold. **If
   they do not, stop** — the model got worse and nothing downstream is safe.
2. Rebuild the point-in-time lineup metric: value every rostered player at the start of the
   week, `assign_slots` the best six, charge the difference.
3. **The gate is that it now beats the managers**, where §16 measured it losing by 20.4
   points a week across nine of ten teams. Not "looks plausible" — beats them.
4. Re-run the Phase 4-5 gates, because the thresholds moved.
5. Only then emit `START` / `SIT`.

Steps 1-4 are the work. Step 5 is trivial by comparison, which is worth remembering when it
is tempting to do step 5 first.

### The reminder lives on the page, and reports readiness rather than the date

Because none of the above will happen unless something says so. `lockin advice` shows a
callout from **week 10** (`advice.REVISIT_WEEK`), and it deliberately checks the data
rather than the calendar:

- **Capture healthy** — at least 20 of the last 30 days carry designations — it invites the
  work, quoting how many days have accumulated.
- **Capture stalled** — fewer than that — it says so instead, and says nothing about
  modelling. Week 10 with no data is not "time to build the model", it is "your
  irreplaceable data stopped arriving", and that is the more urgent message. An invitation
  to build something that cannot be gated would bury it.

Twenty of thirty rather than thirty, because cron misses mornings and a prompt that cried
failure over one would be ignored by the time it mattered. It appears every day from week
10 onward, which is intended: there is no dismiss button, because a reminder you can wave
away is one you will wave away. Silence it by doing the work or by raising `REVISIT_WEEK`.


---

## 20. Phase 6 — complete

`digest`, `explain` and `dashboard` ship, with ntfy notification and a cron form. The
exit criterion — *digest fires daily and thresholds render legibly on a phone* — is met
in every part that can be met without a live league, and §7.3's fallback is what makes
that a real test rather than an excuse.

### The league still does not exist, and that shaped the whole phase

Re-checked on 2026-08-15: `/user/1283460931447164928/leagues/nba/2026` returns `[]`, and
the 2025-26 league is still `status: complete`. So nothing can be run "live" today.

§7.3 already prescribed the answer — *live paths can only be smoke-tested against the
completed season* — and the digest is built as an **as-of** command rather than a live one
with a test mode bolted alongside. `--date` defaults to today; the same code path
reconstructs the morning of any date in the recorded season. In October it will simply
start landing on days whose games have not been played, and nothing about it changes.

That decision is what made the next finding findable.

### §7.2 is not a caption. It changes the computation, and Phase 5 could not express it

The inherited `standing_thresholds` took one date, `day`, and used it for two different
things: what is *known*, and what is *past banking*. In the backtest those coincide by
construction — decisions are taken at the end of a day that has finished — so the
conflation was invisible and correct for two phases.

A **morning** digest separates them, and the separation is not cosmetic:

| | backtest, end of day *d* | digest, morning of day *d* |
|---|---|---|
| Tonight's games | played, scores known | not tipped |
| Teammate playing tonight | resolved | must be simulated |
| Nights between now and a forward threshold | none | §7.2 says assume idle |

Under the old signature a threshold for tonight would have read teammates' *unplayed*
scores out of the season record, and a threshold for Thursday would have quietly assumed
you acted on Tuesday and Wednesday — the exact failure §7.2 was written to forbid, in the
exact function that cites it.

`SimulationCache.contribution` now takes `known_through` (observed through here) and
`act_from` (first night a lock may be taken). An idle night is expressed as a threshold no
score can clear, which composes cleanly with the base policy: masking a night leaves every
later threshold untouched, because each is a continuation value over strictly later
columns.

**The refactor is inert where the two coincide.** `lockin backtest --json` is byte-identical
before and after, so the Phase 4-5 gates still measure the policy they were closed on.
`test_backtest_cutoffs_are_unchanged` pins that against the raw base policy.

### The asymmetry that makes a standing rule correct

Within one night, the player being asked about is priced differently from everyone else,
and this is the substance of the fix rather than an implementation detail:

- **Teammates** get `act_from = night`. Their game that night is live and bankable, so the
  base policy may take it.
- **The player under the rule** gets `act_from = night + 1`. The threshold *is* the
  question of whether to bank his game, so the pass branch must be his continuation from
  the following day — a contribution that re-banked the score under test would be pricing
  the option against itself.

Same player, same cutoff, two different numbers. The digest uses both, and
`test_the_asked_player_is_priced_differently_from_his_teammates` pins the direction:
forfeiting a bankable game can never be worth more than keeping the option.

### Leakage is a property of the data structure, not of the reader

`lineup_as_of` blanks `score` and `played` on every game after the cutoff before anything
else sees them. The schedule survives — which nights a player has a game is known in
advance and the continuation value needs the horizon — but the outcomes do not.

The test that matters is not a code review. `test_no_future_score_reaches_the_digest`
overwrites every post-cutoff score with 999.0 and asserts the rendered digest is
byte-identical. A break-even is a quantile of a deficit, so a single leaked 999 moves it
visibly; the check would catch a leak reintroduced by an edit nobody re-reviews.

### §11's second item does not ship, and the evidence was already ours

The architecture doc asks the digest for "tonight's recommended slot assignment, including
any bench promotions". §16 measured what that advice is worth: following the model's
lineup would have made **nine of the ten teams worse**, by 20.4 points a week, because
`player_status` is empty and the projection layer cannot read the injury report the manager
reads.

So the digest emits no `START`/`SIT` row. What ships instead is the same underlying
quantity framed as a risk rather than an instruction: an unlocked starter whose **last**
game of the week is still to come and who carries elevated DNP risk. That exposure is
genuinely asymmetric — there is no later game to make it back, and the slot counts 0.0 —
which is why it survives when the recommendation does not.

Calibration of the warning, measured over the season on the population it fires against:

```
corrected P(DNP), unlocked starters facing their last game   n = 237
  p50 0.059   p75 0.076   p90 0.120   p95 0.335   p99 0.454
  share at or above the 0.25 threshold : 8.4%
```

The distribution is bimodal and 0.25 sits in the gap between p90 and p95, which is why it
is not tuned. Two things to hold about it: the hazard still over-predicts absence for
started players even after the `starter_dnp_scale` correction, so the warning is
conservative by construction; and it is a probability, not a prediction — the first two
occurrences in the season fired at 43% and 46% and both players played.

### Correction to §6: the dashboard sorts on `squandered_share`, not `mean_regret`

§6's Phase 6 requirement says "Sort on `mean_regret`, never on `upside_share`". The first
half is stale — it predates §16, which replaced raw regret as the ranking precisely because
`mean regret = P(wrong) × E[stake]` and the second factor is circumstance. `lockin managers`
and the `manager_scorecards` comment both already rank on the normalised share. The
dashboard follows them. **The second half stands unchanged**, and is enforced: the page
carries no script, no sort control and no header link, so there is no way to reorder it by
points capture at all.

That correction had a consequence worth recording. `regret_lo`/`regret_hi` band *raw
regret*, which is no longer the ranked quantity — drawing those bars beside a
`squandered_share` ordering would express uncertainty about a different number from the one
setting the order, which looks rigorous and is not. `bootstrap_squandered` and the
`share_lo`/`share_hi` columns were added, resampling the ratio of sums so a decision's
regret stays paired with the stake it was taken at.

The bands are why the rule exists:

```
 1  roster  3   6.78%  [ 4.06,  10.10]
 2  roster  4   9.23%  [ 4.03,  16.08]
 3  roster 10  10.04%  [ 6.39,  14.26]
 4  roster  7  12.81%  [ 8.09,  17.98]
 5  roster  5  15.83%  [11.22,  20.67]
 6  roster  1  16.60%  [11.30,  22.51]
 7  roster  6  19.89%  [14.33,  26.12]
 8  roster  8  21.26%  [15.89,  26.96]
 9  roster  2  22.60%  [16.90,  28.53]
10  roster  9  31.08%  [22.72,  39.63]
```

Ranks 1-4 are a single tie, and only roster 9 is cleanly separated. §6 predicted overlap
"from about rank 2 to rank 8" and it is worse than that. The page draws the intervals as
bars on one shared axis so the overlap is the first thing seen rather than a caveat under
the table.

### The reconstructed banked state is the noisiest number here, and it is the one nobody needs

Offline the digest does not know what you locked, so it reconstructs it by replaying the
week under the rollout policy through yesterday. That reconstruction is a *chain* of
lock/pass calls, most of them near-tied, and resampling flips enough of them to move the
answer materially. Same date, same data, varying only the seed:

```
sims    locks reconstructed        P(win)
 200    [2, 3, 3, 2, 2]            0.470 - 0.567
 400    [1, 1, 2, 1, 2]            0.469 - 0.527
 800    [2, 2, 2, 2, 1]            0.499 - 0.547
1600    [2, 2, 1, 2, 2]            0.492 - 0.529
```

It converges slowly and does not converge to a point. **Everything downstream of it is
stable**, which is the finding that makes this manageable — fixing the state and varying
only the seed:

```
sims   lock/pass calls identical    threshold sd    P(win) sd
 200   no                           1.8 - 4.6 pts   0.024
 400   yes, across 6 seeds          1.2 - 2.0 pts   0.017
 800   yes, across 6 seeds          1.0 - 2.7 pts   0.019
```

Three consequences, all shipped:

- **`--locked` is a first-class option** on `digest` and `explain`. Live the state is
  simply known, and supplying it removes the noise rather than averaging over it. It is
  validated: an id that is not a starter that week is an error, because it would otherwise
  add its score to the banked total *and* leave the player counted among the unlocked —
  double-counting him, and moving every number in the flattering direction. Found by typing
  a wrong id by hand.
- **400 simulations is the floor, and it is the default.** At 200 the calls themselves
  disagree across seeds, which would make the digest's headline advice a coin flip.
- **Thresholds print as whole numbers.** A number carrying 1-3 points of Monte Carlo
  standard deviation does not get a decimal place, particularly one the user applies from
  memory on a phone.

### A bug this phase surfaced, and the gap that let it through

`evaluate_managers` broke on the `SimulationCache` signature change and **no test caught
it** — it was the one entry point in the project reachable only by running the command.
`test_evaluate_managers_runs_end_to_end` now calls it at 20 simulations: far too few for
the numbers to mean anything, which is the point. It asserts the call graph holds together;
the estimates are gated by `lockin backtest`.

The `recommendations` table also needed its primary key widened. `(generated_at, week,
sleeper_id)` was a placeholder from Phase 0, and one digest legitimately writes several
rows for one player — a call on last night plus a standing rule for each of the next
nights — which collided and were silently dropped by `INSERT OR REPLACE`. The rebuild is
guarded on the table being empty: §12 makes this the only record of what was advised on a
given day, and that is not recoverable by recomputation.

### Deviations from the plan

- **`START`/`SIT` actions are not emitted.** §16's evidence, above. The vocabulary stays in
  the schema because the finding is about today's blind spot, not about the idea.
- **Three forward nights, not two.** The marginal night is nearly free once the simulation
  is cached, and the failure the output exists to survive is a check-in missed for longer
  than expected.
- **The digest reconstructs the banked state by replaying the week under rollout.** Live it
  should come from the poll history §10 describes. Nothing has been polled, so the
  assumption is "you followed this engine so far", stated in the output and overridable.
- **No Pi deployment.** Per §8, the cron form is held and the deployment is not done here.

### The notification had never been tested on its success path — and it was broken

Asked, on 2026-08-15, how recommendations are actually delivered. The answer exposed that
`notify.send` had exactly two tests, `disabled` and `failed`, so **the code that sends had
never executed once**. The first real run would have been on a Raspberry Pi at 9am.

A live round-trip against ntfy.sh — random throwaway topic, synthetic body — showed the
happy path works and the body survives UTF-8 verbatim, em dash and section sign included.
It also found a bug the negative tests were structurally incapable of finding:

```
UnicodeEncodeError: 'latin-1' codec can't encode character '—'
  http/client.py, putheader  <- a *title* containing an em dash
```

`http.client` encodes header values as latin-1. `UnicodeEncodeError` is a `ValueError`, and
the guard named `URLError` and `OSError`, so it **propagated** — out of the one function
whose documented contract is that it never fails the digest, in the one deployment where
failing means cron mails a traceback every morning until someone switches it off.

Two fixes, and the second is the more interesting one:

- Non-ASCII header values are RFC 2047 encoded-words. ntfy decodes them back to the
  original; verified against the live service, title returned byte-identical.
- **The guard is now deliberately broad.** Enumerating exception types is what caused the
  bug, so the implementation stops enumerating and the *test* enumerates instead —
  `URLError`, `OSError`, `UnicodeEncodeError`, `TimeoutError` and a plain `RuntimeError`,
  all of which must come back as `failed: ...` rather than escape.

The live round-trip is now a test, opt-in behind `LOCKIN_LIVE_NTFY=1`. A test that silently
depends on a third party fails for reasons unrelated to the code; one that is never run
proves nothing. Opt-in is the only version of it worth having.

### `lockin advice`: the reader `recommendations` never had

The same conversation turned up that `recommendations` was **written by `digest` and read
by nothing**. The advice existed in two places — a push notification that scrolls away, and
a table nothing surfaced. Re-running `digest` is not a substitute: it recomputes, and §20's
Monte Carlo instability means it will not reproduce what you were told.

There was an assumption worth correcting explicitly, because it is a natural one: the
dashboard is *not* where recommendations live. It renders `manager_scorecards` — last
season's decision quality — and would not change during a live week. Three surfaces, three
questions:

| command | question | delivery |
|---|---|---|
| `digest` | what do I do tonight | ntfy push, plus terminal |
| `advice` | what did it say | static page, re-readable |
| `dashboard` | who decided well last season | static page, retrospective |

Designing the reader exposed two schema gaps. `recommendations` had no `roster_id`, so two
rosters digested on the same day interleaved indistinguishably; it is now a nullable column,
added additively so the rows written before it survive. And nothing stored the *state* the
calls were taken against — P(win), projected totals, what was banked — without which a
reader cannot render a past digest without recomputing it, which is the one thing it must
not do. `digest_runs` holds that, one row per run, and is written even when there is
nothing to decide: "no matchup this week" is a real answer, and a blank page would be
indistinguishable from a cron that never ran.

**Staleness is the loudest element on the page.** The failure mode of a recommendations
page is showing yesterday's calls as today's, so age is measured from the morning the
digest *describes* rather than from when the process ran — a run re-rendered at midnight
for the previous morning is a day old whatever its timestamp says — and it appears as a
coloured banner above the advice, not a footnote below it.

Deliberately not a history browser. The question is "what am I supposed to do"; a date
picker invites reading a stale answer on purpose.

### `lockin serve`, and why it is not `python -m http.server`

Wanting the page on a phone is a two-line problem — until you notice what the obvious
answer publishes. `python -m http.server` in this project's directory serves
`data/lockin.db`, all 27MB of the season, plus `snapshots/` and the source, with directory
listing on. The naive advice is actively harmful, and that is the justification for writing
something narrow.

`lockin serve` **never opens a file**. It holds no document root, has a two-entry route
table, and renders both pages from the database. There is no path handling, so there is no
path handling to get wrong; `/../data/lockin.db` is a 404 for the same reason `/nope` is.

Two properties beyond that:

- **Rendered per request.** `advice` is a reader — two queries, no simulation — so this
  costs nothing and removes the staleness step entirely. A served page cannot be older
  than the last digest, which is the failure this area has now produced twice.
- **Read-only, enforced by SQLite** rather than by handlers being careful. A bug in a
  request handler cannot corrupt the season, and WAL means it coexists with the cron
  ingest writing at that moment.

Binding all interfaces is the default, because a loopback default would be a command that
does not do the thing it exists for. That is the LAN, and Tailscale when the interface is
up — which is how it should be reached from outside the house. There is no authentication:
adding a password field would imply more safety than it delivers, so the boundary is the
network and the command says so at startup, every time.

Writing the tests found a rendering bug worth noting, because it is the shape this project
keeps producing. `render` guarded the projected-totals line on `p_win` rather than on the
fields it actually formats, so a `digest_runs` row carrying a win probability and no
projections raised `TypeError` and turned into a 500. `persist` never writes that shape —
but "the writer never produces it" is not a property a page served over HTTP should depend
on, and each line now guards its own field.

### Three things found by asking what deployment would need

Asked, before starting Pi work, what else was worth considering. Measuring rather than
guessing found that the expected risk was not one and three unexpected ones were.

**Performance is a non-issue.** The digest is 0.54s and 61MB peak on a laptop; several
times slower on a Pi is still seconds. Readers never block either: WAL lets `serve` render
while an ingest is mid-write, which was checked rather than assumed.

**1. "Today" had three different answers.** The CLI used `date.today()` (the machine's),
`advice` used `datetime.now(UTC).date()`. On a US Eastern host those disagree every evening
after 7pm, so a digest generated that morning was labelled a day old — in red, telling you
to re-run something that had already run — the moment the page was opened after dinner.

The right basis is neither: **945 of the season's 1231 games (77%) tip on a different UTC
date than the one they are filed under**, because they start 23:00-04:00 UTC. Game dates
are US Eastern. `lockin/clock.py` now owns the question, `LOCKIN_TZ` names the *schedule's*
timezone rather than the operator's, and a host with a wrong clock can no longer shift a
digest by a day.

A timing rule falls out and is stated in code as `too_early_for`: the digest treats games
dated `today - 1` as complete, the last of them finish around 06:30 UTC, so it must not run
before ~07:00 UTC. At 9am US Eastern there is seven hours of headroom. East of UTC+2 there
would not be.

**2. A digest firing mid-ingest failed outright.** Measured: `database is locked` after
exactly 5.0 seconds, sqlite3's default. `session` holds one transaction for a whole
multi-week ingest, so the window is minutes of network-bound work. Two fixes, because
either alone is insufficient — a 60-second `busy_timeout` on the connection, and
`db.checkpoint` between weeks so the lock is held for seconds rather than the whole run.
Re-measured after: a writer holding for 8 seconds is now waited out rather than fatal.

**3. A failed ingest was invisible to the digest.** If the 6:30 ingest dies, the 9:00
digest still runs, still finds box scores and still makes confident calls — on yesterday's
data minus yesterday. Nothing on the page would look wrong. `digest_runs.last_ingest_at`
now records how fresh the data was, and `advice` renders a warning beside the staleness
banner: same question asked of the other input.

The two warnings look alike and are marked apart in the markup (`data-warning="age"` versus
`"ingest"`), because a test that cannot tell them apart is a test that passes for the wrong
reason — a mistake made twice already in this phase.

### The Pi needs last season, and copying it is not the same as rebuilding it

Asked whether the deployment runbook would put 2025-26 in the production database. It would
not: `data/` is gitignored, so a fresh clone has no database at all, and the runbook only
ingested one week as a smoke test. `snapshots/` *is* committed, so the §12 archive travels
with the clone — but the data it guards did not.

**Copy the file; do not re-ingest.** §12 is the reason: Sleeper rewrites completed seasons,
so rebuilding 2025-26 on the Pi would fetch today's version and produce a database that
disagrees with this one and with the committed snapshots. Copying preserves what was
observed, and `reconcile` then confirms it still matches the archive.

**It does not help next season's projections**, which is the natural assumption and is
wrong. `load_panel` filters `WHERE season = ?`, so the 2026-27 panel ignores 2025-26
entirely. The opening-weeks cold start is real and having last season on disk does not
soften it; that would need cross-season panel support, which does not exist.

What it does buy is that **every gate can run on the Pi** — `verify`, `locks`, `calibrate`
and `backtest`, not just an import check. A `backtest` that passes on the target hardware is
the strongest deployment evidence available, and it was previously impossible there.

**And it exposed a papercut worth fixing.** Seasons cannot share a database (day-one step
2), but manager scorecards are *retrospective*: the only ones that exist during 2026-27
describe 2025-26 and live in that season's file. Served from the current database,
`/dashboard` would read "No scorecards yet" for an entire season — true, and useless.
`lockin serve --dashboard-db` points that one route at the previous season while tonight's
advice keeps coming from this one.

### What is still live-only, and therefore still unverified

Unchanged from §15, and none of it is closable before October:

1. **Today's injury designations.** The capture is built; there is no history yet.
   `starter_dnp_scale` is the seam the real feed replaces.

   **Corrected 2026-08-15, while validating the day-one checklist.** Earlier wording here
   and in §19 said the capture "runs daily". It does not, and could not have: designations
   are written by `ingest_players`, which the CLI calls only under `--full` or against an
   empty `players` table. The database holds **one** date, 2026-08-08 — the day `--full`
   was last run — and the crontab shipped with Phase 6 omitted the flag entirely, so
   deploying it as written would have captured nothing for a season while every command
   reported success. The cron form is fixed and `docs/day-one.md` step 6 verifies the row
   count is growing rather than assuming it.

   **Decided the same day: `--full` is gone and the capture is unconditional.** The flag
   existed to save a 2.5MB fetch on a re-ingest, and the designations rode in on the payload
   it gated — so what it really controlled was whether to record the one thing that cannot
   be recovered later. That is not a trade worth offering. Every ingest now fetches the
   payload and writes the designations, and passing `--full` is a parse error rather than a
   no-op, so a stale crontab breaks loudly instead of silently skipping a season.

   The ingest also prints **days** of coverage, not just rows. Rows cannot reveal a stalled
   capture — a feed frozen since October still reports thousands — and the day count is the
   number that goes flat when something has gone wrong.
2. **The polling loop.** `weekly_matchups` is append-only so that a frozen `players_points`
   can reveal an opponent's lock one game later. That needs in-season polling, which needs
   a season.
3. **Forward-looking stat rows (§7.5).** Whether Sleeper publishes rows for *upcoming*
   games is still unconfirmed. The digest reads the schedule from the panel, and the
   `nba_api` schedule ingest exists as the fallback, so absorbing a "no" is cheap — but it
   must be checked on day one.
