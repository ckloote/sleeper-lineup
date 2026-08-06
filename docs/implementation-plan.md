# Sleeper NBA Lock-In Engine — Implementation Plan

**Companion to:** `sleeper-lockin-engine-architecture.md`
**Status:** Approved — committed scope is **Phases 0-2**. Phase 0 complete (see §9).
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

> **Committed scope: Phases 0-2.** These are the plumbing and validation the architecture
> doc says should consume most of the pre-season, and the layer where a silent error
> poisons everything downstream. They are also the only phases whose gates can be closed
> today against the completed 2025-26 season. Phases 3-6 are specified below for
> continuity but are **not** in this build — we reassess with real ingested data in hand.

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

*Exit:* all 25 weeks reconciled, not just week 12 — every nonzero `players_points`
reproduced to the cent; component-derived `dd`/`td` matches Sleeper's on every game;
per-attempt economics from §2 asserted in tests.

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

*Phases 3-6 below are recorded for continuity. **Not in this build.***

### Phase 3 — Projection layer (EWMA) — DEFERRED
`ProjectionSource` protocol with an `as_of` cutoff enforced in the interface signature,
not by convention. DNP hazard, minutes distribution, component-rate bootstrap conditioned
on minutes bucket and role. Season-stage-specific rest calibration per §9.

*Exit:* out-of-sample quantile calibration — predicted quantiles match realised
frequencies, checked specifically in the right tail, since the tail is the whole reason
passing is ever correct.

### Phase 4 — Simulation and base policy — DEFERRED
Vectorised numpy: DNP gate → minutes → correlated components → `score_line`. Bipartite
slot assignment via `scipy.optimize.linear_sum_assignment` at each simulated lock event.
Backtest harness with the five policies from §12.

*Exit:* greedy threshold beats never-lock on points, out of sample.

### Phase 5 — Rollout and opponent model — DEFERRED
Rollout policy improvement over the base policy; opponent belief state; threshold by
binary search over hypothetical `S`.

*Exit:* see §7.1 — the doc's stated criterion needs restating to be measurable.

### Phase 6 — CLI, digest, deployment — DEFERRED
`ingest / digest / explain / backtest / verify`, push notification, cron via
`uv run --frozen`.

*Exit:* digest fires daily and thresholds render legibly on a phone.

**Note:** Phases 0-2 still ship a CLI, but only the read-only subset the gates need —
`lockin ingest` and `lockin verify`. `digest`, `explain` and `backtest` arrive with the
phases that give them something to say.

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
| **Scope** | Phases 0-2 only | Ingest, scoring engine, lock inference. Reassess with real data before committing to 3-6. |
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
