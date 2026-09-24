# Project review — 2026-09-23

The historical scoring and replay foundation has substantial validation. The project is **not ready for unattended live recommendations**. Several paths assume a completed season even when called by the live digest. Fixing the schedule feed alone does not close that gap.

Scope: application modules, core models, ingestion, persistence, CLI, HTTP readers, notification/deployment scripts, tests, and implementation/day-one documentation. No application code was changed. Reproductions used read-only connections to the season database, in-memory copies, or synthetic in-memory databases; no live API requests or notifications were made.

## Validation

- Initial suite: 494 passed, 1 skipped, 2 failed, 35 errors. All 37 failures/errors were denied local sockets in the sandbox.
- Rerunning the affected HTTP/cron suites outside the sandbox: 39 passed, including their two tests that already passed. Combined result: **531 passing tests, 1 skipped**.
- Ruff: all checks passed.
- Historical `verify`: 26,665/26,665 component-derived bonuses and scores agree; 2,478/2,478 nonzero counted scores reproduce.
- Historical `reconcile`: all checks pass, including 1,231/1,231 played fixture links and 1,500/1,500 starter-week coverage. Upstream drift is correctly reported as advisory.
- These results validate the recorded season. They do not demonstrate opening-day or live-state correctness. In particular, digest tests load a completed season and blank future scores after ingest; they do not simulate ingesting an unfinished season.

## Findings, ordered by priority

### 1. P1 — Upcoming games are classified as postponed and removed

Locations: `lockin/ingest/sleeper.py:504`, especially line 520; `lockin/projections.py:122`; `lockin/reconcile.py:123`.

`refresh_game_occurrence()` sets `occurred=0` whenever no player has stats. This is equally true of an upcoming game, an incomplete feed, and a postponed game. `load_panel()` then excludes it. Consequently, even if Sleeper supplies every future player-game row, the normal ingest removes all of them from the digest's schedule. Reconciliation also regards a future fixture linked to the NBA schedule as a contradictory postponement.

Reproduction: in an in-memory copy of the season, mark games from 2026-01-08 onward unplayed, then run the occurrence refresh. All 681 future fixtures become excluded; the resulting projection panel has **zero future rows**.

Fix: distinguish scheduled, in-progress, final, postponed, cancelled, and unknown states. Use NBA status/timing and completeness evidence. Construct the live slate from the schedule independently of historical box-score observations, and join it to player/team membership and fantasy-week boundaries. Do not require `game_links` to exist for a fixture that has no Sleeper rows yet. `resolve_week()` also needs a calendar source independent of future box scores.

### 2. P1 — The morning digest assumes its pending LOCK calls already happened

Locations: `lockin/digest.py:518`, `:527`, `:563`.

With no `--locked`, `walk_locks(... through_day=known_through)` replays through yesterday, adds yesterday's recommended locks to `banked`, and then skips those players while emitting the calls the user must act on this morning. It predicts state instead of observing it, and consumes the very actions the digest should deliver.

Reproduction: roster 4 on 2026-01-07 at the default simulation count reports two banked players and **no calls**. With an explicit empty banked state, it emits a LOCK for player 1787's 47.5. On 2026-01-10 the same pattern suppresses player 1648's 48.5 LOCK.

Fix: separate confirmed banked state from proposed actions. Read confirmed state from a lock ledger/poll inference, or require explicit state for actionable live output. At minimum, historical reconstruction must stop before the decision window being presented. Do not treat a previously printed recommendation as evidence of execution.

### 3. P1 — Opening-day projections silently become certain zeroes

Locations: `lockin/projections.py:120`; `lockin/core/projections.py:593`; `lockin/rollout.py:121`.

The panel is restricted to the current season. Before its first game there are neither own-player donors nor pooled donors. Projection raises `InsufficientHistory`; the simulation catches this and substitutes the final game's score. In a correctly masked digest that future score is zero. This turns absence of information into certainty of scoring nothing.

Reproduction: build roster 4's digest as of 2025-10-21 with `locked={}`. Both projected totals are **0.0**, P(win) is **50%**, and all seven standing thresholds are **0.0**. This superficially satisfies the day-one checklist's “near 50%” check while providing useless advice. If finding 1 removes all rows, opening day fails even earlier in `load_panel()`.

Fix: explicitly import a prior-season projection prior without mixing season-specific matchup tables, or abstain with “insufficient history.” Add a calibration gate for the cold-start regime; the existing calibration defaults exclude players with fewer than eight prior played games. Test fresh databases and opening-week days, not only January replays.

### 4. P1 — “Latest” lineups retain players absent from later polls

Locations: `lockin/store/schema.sql:237`, especially line 244; `lockin/ingest/sleeper.py:336`; `lockin/digest.py:284`.

`weekly_matchups_latest` chooses the latest observation **per player**, not the players belonging to the latest roster snapshot. If an earlier starter disappears from a subsequent response after a drop/trade, their old starter row remains current alongside their replacement. The digest can then simulate more than the configured number of starters. Sparse/empty `players_points` also means the ingest records no player rows even when `starters` lists a valid lineup.

Reproduction: insert one poll with `old` in slot 0, then a later poll with only `new` in slot 0. The latest view returns **both starters**.

Fix: store complete roster poll membership with an observation ID, including removals/empty snapshots, and query one coherent poll. Enumerate players/starters independently of score availability. Preserve repair overrides separately, or adapt repairs to write complete snapshots; simply changing the view would otherwise discard untouched rows from partial repairs.

### 5. P1 — Season/league isolation is documented but not enforced

Locations: `lockin/cli.py:186`; `lockin/ingest/sleeper.py:83`; `lockin/store/schema.sql:196`; `lockin/verify.py:44`.

The ingest accepts the configured league and season independently and writes to any existing database without checking its identity. Matchups and derived tables have no season/league key; settings queries use `LIMIT 1`. A missed database-path change at rollover silently mixes seasons, and a mismatched league ID/season mixes one league's roster with another season's stats. This is already described in `docs/day-one.md`, but a manual instruction is the only barrier to data corruption.

Fix: add immutable database identity metadata and verify it, the requested season, and the API league payload before any writes or snapshot saves. Reject a mismatch with a concrete new-database instruction. A complete multiseason schema migration is unnecessary to make the intended one-file-per-season design safe.

### 6. P2 — Calls are selected by the team's latest game night, not each player's open window

Location: `lockin/digest.py:544`.

The digest chooses one `last_night` across the whole lineup. A player who played Monday and next plays Thursday still has a bankable score on Wednesday, but is omitted if a teammate played Tuesday. The player's next tipoff defines the deadline; the team's latest played date does not. Evaluating a delayed call at the original game day also misses newer opponent information.

Fix: identify each unlocked player's most recent completed game and whether its lock window is still open. Evaluate it using all information available this morning. Show individual expiry times. Add a staggered-schedule test, including a DNP between two played games.

### 7. P2 — The timing guard is never invoked and tipoffs never reach advice

Locations: `lockin/clock.py:66`; `lockin/cli.py:844`; `lockin/digest.py:447`.

`too_early_for()` is tested but has no application caller. Running a digest shortly after midnight can treat partial previous-night scores as final. Neither the digest nor its reader consults `tipoff_utc`, so an afternoon rerun can present a morning call after the player's next game has tipped. A same-day green banner is not proof an action remains available.

Fix: enforce completion checks for live runs, retain unrestricted historical replay, and expire individual calls at tipoff. Prefer actual game status over a fixed completion-hour assumption. This also closes the plan's currently unimplemented deadline-display requirement.

### 8. P2 — Availability capture preserves cleared injuries and loses observation timing

Location: `lockin/ingest/sleeper.py:189`.

Only nonempty designations are inserted, keyed by player/date. A second same-day fetch that clears an injury inserts nothing, leaving the earlier Out row intact. A nonempty update overwrites the morning observation, so a later start/sit backtest cannot distinguish what was known before a decision from what arrived after it. An entirely healthy capture day is indistinguishable from a missed capture in the coverage counter.

Reproduction: record `Out`, then record `None` for the same player/date. The stored designation remains **Out**.

Fix: store timestamped complete observations or status-change events with explicit healthy/cleared values and capture-run metadata. Read the latest status strictly before each decision. Fix this before the season: missing timestamps cannot be recovered later.

### 9. P2 — Ingest freshness can certify stale stats as fresh

Locations: `lockin/digest.py:747`; `lockin/cli.py:228`.

`last_ingest_at()` returns the latest successful Sleeper suboperation, including a player refresh or an unrelated matchup observation. It does not identify a completed ingest for this week. Between-week commits mean an ingest can persist fresh log entries and then fail before the required data is ready. `observe` can also refresh this timestamp without fetching stats.

Reproduction: yesterday's `stats:week=1` log plus today's `players` log yields today's freshness timestamp despite no refreshed scores.

Fix: record ingest-run start/completion, covered weeks, source-specific success, and data-through boundaries. Advice should report the freshness of its actual stats, schedule, lineup, and status inputs. Decide explicitly when stale inputs require abstention. Partial committed ingests must not advertise full readiness.

### 10. P2 — Historical repair would overwrite legitimate live-week evolution

Locations: `lockin/repair.py:81`, `:144`; `lockin/cli.py:336`.

Consensus counts all archived observations, with no completed-week or post-finalization filter. Once live polling begins, early zeros and interim scores are ordinary observations, not corruption. Their modal value need not be the final counted score. Even a repair run after the week ends still includes those in-progress snapshots.

Reproduction: observations `[0, 0, 0, 50]` yield a confident 0 consensus that would replace a correct final 50.

Fix: restrict mutation-repair evidence to observations after a recorded finalization boundary, refuse open weeks, and retain live polls separately. Report plurality/tie uncertainty rather than describing every recovered value as a proven original.

### 11. P2 — The prescribed day-one reconciliation gate cannot pass

Locations: `lockin/reconcile.py:19`, `:31`, `:46`; `docs/day-one.md`, steps 3–4.

The checklist says to ingest week 1 and require both reconciliation and verification to pass. Reconciliation unconditionally requires all 25 weeks of stats and matchups. It also treats zero played fixtures as a failed link rate. Separately, step 3 reads league settings from the new database before step 4 creates it.

Fix: distinguish historical-completeness gates from live-readiness gates, parameterized by expected elapsed/requested weeks. Fetch settings before inspecting the new database. A fresh-database rehearsal should run the documented commands in order and check meaningful readiness, not merely an exit code or a 50% probability.

### 12. P2 — Recommendation persistence is not reliably append-only

Locations: `lockin/digest.py:777`, `:780`; `lockin/store/schema.sql:423`, `:456`.

Run timestamps resolve only to seconds, and both run and recommendation inserts use `OR REPLACE`. Two runs for the same roster within a second overwrite the run header while retaining any old recommendation rows not overwritten by the second run. The reader can display a mixture of both runs, contradicting the audit-history contract. Recommendation identity also omits roster ID.

Fix: assign a unique run ID, use it as the recommendation foreign key, and insert immutable rows. Persist model version, parameters, confirmed banked state, and input observation IDs so a recommendation has enough provenance to audit.

## Additional correctness and modeling improvements

- **Persist warnings.** `Digest.warnings` is rendered in text but never stored by `persist`; the phone's advice page loses the final-game DNP warning shown in the notification. Store structured warnings, clearing probability, and per-player banked scores with the run.
- **Align threshold and action semantics.** `lock_threshold()` returns the maximum deficit when passing already wins every simulation. A score above that threshold is advertised as LOCK even though `evaluate_lock()` returns PASS on equal probabilities. Reproduction: continuation 20, opponent 10, candidate score 11 gives threshold 10 and a PASS decision. Define whether indifferent decisions prefer lock/pass, and calculate thresholds on the same tie-aware empirical distribution and scoring grid.
- **Account for correlated observations in uncertainty estimates.** Manager bootstrap intervals resample individual decisions, although decisions from the same player/week and matchup share state and simulation error. Resample week/matchup blocks and test rank stability across seeds before interpreting close rankings. The all-roster win comparison likewise contains dependent views of the same matchups; report that dependence when interpreting significance.
- **Validate eligibility before implementing start/sit.** The permissive slot mappings and player-specific exceptions were inferred from historical assignments paired with live position metadata. Those observations can establish compatibility with the sample, but do not establish all allowed assignments for next season. The plan already documents the metadata drift. Validate current eligibility directly and reject invalid/duplicate pinned assignments in `assign_slots()` before exposing lineup recommendations.
- **Avoid silently resolving impossible lock evidence.** `infer_lock()` labels single-game and no-game weeks resolved without validating the counted value. A separate scoring gate may catch nonzero mismatches, but `locks` alone can still claim confidence in inconsistent input. Return an unresolved status when the observation cannot be explained.
- **Keep the opponent stand-in explicit.** `opponent_totals()` still assumes greedy behavior for past locks; collecting poll history does not automatically make it use that history. Implement and test the live inference consumer before describing the opponent state as observed.
- **Make tests portable and representative.** Several major suites skip when a gitignored database is absent; digest fixtures also apply schema to the configured database. Add a compact, versioned synthetic live fixture and use temporary database copies. A clean checkout should exercise rollover, unfinished games, removals, status clearing, source failure, and action expiry without production data.
- **Update status prose.** The plan header says phases 0–5 complete and phase 6 remains, later sections say phase 6 complete, and later deployment notes supersede older “not deployed” passages. Code comments still describe some live attributes as point-in-time and some feeds as historical results. Maintain one current status/gate table, with the older narrative clearly historical.

## Recommended next implementation sequence

1. **Before live capture:** database identity guard; timestamped status and complete roster snapshots; post-finalization-only repair. These protect information that cannot be reconstructed later.
2. **Before actionable digest delivery:** schedule/status separation; independent fantasy-week resolution; explicit cold-start prior or abstention; confirmed own/opponent state; per-player open lock windows and expiry; source-specific readiness checks.
3. **Rehearse the lifecycle:** fresh season → first tip → first morning → DNP → roster change → missed/partial ingest → week rollover → postponement. Verify rendered advice and push content, not just successful command execution.
4. **Run live in shadow mode:** capture recommendations and actual actions, evaluate calibration and decision stability prospectively, then enable unattended actionable notifications once the live gates pass.
5. **Revisit start/sit after sufficient valid data:** the week-10 reminder is an earliest review point, not evidence of model readiness. Require timestamped availability, actual lineup history, validated eligibility, and its own prospective/held-out gate.

Useful additions after these fixes: a phone-friendly “confirmed locked” ledger; a data-health page showing per-source completeness; automatic league-lineage discovery with explicit identity checks; a recommendation audit/history view clearly marked historical; prior-season warm-start projections; and lock-deadline reminders that disappear after confirmation or expiry.

The retrospective dashboard and historical analysis can continue to be used with their existing model/data caveats. The next milestone should be **live-state correctness**, before expanding the recommendation feature set.
