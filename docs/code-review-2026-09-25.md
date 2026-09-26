# Follow-up review — 2026-09-25

**Resolution status:** all five P2 findings below are implemented in the working tree.
The original findings and reproductions are retained for audit; see the implementation
and verification update at the end. Deployment is outside this change.

Scope: changes from `66a1d99` through `45d6c2f`, particularly ingest, live slate assembly, lock inference, digest/persistence, advice, shadow evaluation, and their tests. Reproductions used temporary synthetic databases; no live requests, notifications, or production database changes were made. The original review added only this document; the resolution below adds code and tests.

## Findings

### 1. P2 — A detected calendar disagreement does not stop live advice

Locations: `lockin/digest.py:728`, `lockin/ingest/run.py:186`.

`calendar.disagreement()` detects when Sleeper's current week contradicts the inferred Monday-to-Sunday calendar. Its only application caller prints an ingest warning. The live digest checks unfinished fixtures and ingest freshness but never checks this disagreement, despite the ingest comment saying that the digest refuses to advise on it.

Reproduction: start with the synthetic advising morning, 2026-10-28, and change the league payload's `settings.leg` from 2 to 4. The calendar helper reports the discrepancy. A live `morning()` still returns week 2, `abstained=False`, four calls, ten standing rules, and no explanatory note.

If the next season's fantasy-week structure differs from the inferred calendar, the system can issue recommendations for the wrong matchup and wrong stopping horizon. Invoke the calendar check as a live abstention condition before producing recommendations, and test the mismatch through `morning()`, not just the helper.

### 2. P2 — A missing tipoff leaves an actionable call without an expiry

Locations: `lockin/digest.py:824`, `lockin/advice.py:65`.

Expiry is enforced only if `expires` is truthy. The schedule permits null tipoffs, and an ingest without the optional tipoff sweep still qualifies for live readiness. With no timestamp, the digest emits the call and the reader's `Item.expired()` always returns false. This silently treats an unknown deadline as an open window.

Reproduction: the synthetic morning produces a LOCK for player `1002`, expiring at `2026-10-28T23:30:00Z`. Clear that team's schedule tipoffs and rerun live at 23:59 UTC. The same player's LOCK is still emitted with `expires_utc=None`, after the fixture's original tip time.

Require a verified deadline for actionable live calls, or suppress the affected call with an explicit unknown-deadline reason. A same-day game with unknown tipoff must not remain bankable indefinitely. Cover both the digest and persisted advice rendering.

### 3. P2 — Freshness still measures run completion rather than when stats were fetched

Location: `lockin/digest.py:503`.

The completed-run record fixes the original “players refresh makes stats fresh” case. However, `stale_ingest()` accepts a run solely because its **finish** is after the 07:00 UTC completion boundary. Stats are fetched earlier in the run, before matchups and NBA work. A run that fetches partial scores before the boundary and completes after it can therefore certify those scores as fresh. The source-specific timestamps now shown on the page do not close this admission-check gap.

Reproduction: on the synthetic advising database, set the complete run's start to `2026-10-27T23:00:00+00:00`, its finish to `2026-10-28T10:30:00+00:00`, and its stats log completion to the previous night's 23:01. `stale_ingest(conn, 2, known_through)` returns `None`, meaning acceptable. This reproduces the freshness decision; it does not claim those timestamp edits themselves create incorrect scores.

Check the relevant week's successful stats-fetch boundary, tied to the ingest run. Conservatively requiring the run to start after the slate boundary would also prevent this case, though it may refuse some usable runs. A later matchup observation or explicit `--locked` state must not substitute for fresh box scores.

### 4. P2 — The new shadow gate can pass with almost no daily inference coverage

Locations: `lockin/shadow.py:125`, `lockin/shadow.py:329`, `lockin/shadow.py:404`.

Every persisted run satisfies morning coverage, including abstentions and runs using supplied state. `_state()` skips runs that did not infer state, while `WeekSummary.clean` requires only one positive aggregate `state_checked` count for the entire week. Consequently, one inferred morning can validate a week whose other six mornings could not read lock state.

Reproduction against a temporary synthetic database with finalized weeks 2 and 3: persist an inferred run on each Monday and a “lock state unknown” abstention on each remaining morning. `shadow.build(...).gate()` returns `(True, 'weeks 2-3 clean')`. Twelve of fourteen runs abstained; the weeks had only five and six player-state checks respectively.

This matters because the day-one instructions use this gate to decide when the daily manual cross-check may stop. Track usable inference coverage per required roster/morning, distinguish “no decision needed” from “could not infer,” and do not count an abstention or supplied-state run as a successful test of automatic inference. Test the gate with real persisted runs spanning two weeks.

### 5. P2 — The corrected inclusive threshold is still displayed and measured with different semantics

Locations: `lockin/digest.py:390`, `lockin/digest.py:1009`, `lockin/advice.py:487`.

`lock_threshold()` now correctly returns the **minimum score worth banking**, on the half-point grid. But `clearing_chance()` still counts only draws strictly greater than it, and both user-facing renderers round the threshold to a whole point under “lock if he clears” wording. The mathematical fix therefore has not reached the rule the user applies.

Reproduction: continuation samples `[11, 11]` against opponent samples `[11, 11]` yield a threshold of 11.5; `evaluate_lock(..., lock_value=11.5)` says LOCK. Both renderers print 12. With all projected draws equal to 11.5, the reported clearing chance is 0%, although every draw meets the minimum worthwhile score.

Use one contract throughout: for example, display `11.5 or more` and calculate the chance using `>=`. If whole-number rules are intentional, quantize the actual rule first and calculate its probability from that same rule; do not silently round the model's decision boundary.

## Status of the previous review

The original failures around future games disappearing, assumed execution of own LOCK calls, cold-start zero projections, stale poll membership, season identity, injury-status clearing, live-week repair, day-one gates, and recommendation-row mixing have substantive fixes and regression coverage. Warnings now reach persistence and the page; pinned assignments and impossible lock evidence have additional validation; synthetic fixtures exercise the live lifecycle. The uncertainty changes use week blocks and matchup clustering.

The remaining findings qualify that progress: timing/deadline handling and freshness are incomplete (previous findings 7 and 9), threshold/action semantics still disagree at the presentation boundary, and the independent calendar guard is not enforced. The shadow workflow introduces a new false-positive readiness gate. Passing historical tests does not establish the unobserved upstream behavior of next season's live lock inference; the documented shadow check remains necessary once its coverage requirement is corrected.

## Validation

- Full suite in the sandbox: **629 passed, 1 skipped, 2 failed, 35 errors**. All 37 failures/errors were local socket permission failures in the cron/HTTP suites.
- Reran cron, HTTP server, dashboard, and advice suites with socket access: **93 passed**, covering all 37 restricted cases plus 56 already-passing cases. Combined unique suite result: **666 passed, 1 skipped**.
- Ruff: **all checks passed**.
- Targeted synthetic reproductions confirmed the five findings above. They were run outside the application tree using temporary databases; no implementation fixes were made during this review.

The original review required these guards and the shadow coverage criterion to be fixed
before closing the findings. The implementation below addresses that requirement.


## Implementation and verification update

All five findings are resolved:

1. Live `morning()` checks calendar disagreement before loading projection context;
   direct digest construction also enforces the guard. CLI explanations use the same
   entrypoint. The saved abstention names both weeks and the date. Monday tolerance,
   ingest capture, and historical replay remain available.
2. Live calls require a valid timezone-aware next tipoff. Unknown deadlines suppress
   only the affected player's call and persist a warning. Exact tipoff closes the window.
   Advice distinguishes open, closed, and unknown deadlines; replay is labelled historical.
3. `ingest_stats_fetches` records each week's request start and successful storage completion
   against its ingest run, before the checkpoint. Freshness checks the request start against
   07:00 UTC and rejects the newest covering run if incomplete or missing required NBA work.
   Selected provenance is carried through persistence; the advice footer shows request time.
   Additive migrations leave legacy evidence absent and require ingest to run again.
4. Shadow coverage is checked per roster and calendar morning across the latest two
   finalized tracking weeks, including entirely missing weeks. Partial first weeks,
   abstentions, supplied state, and uncheckable evidence cannot qualify. Successful reruns
   restore coverage without erasing discrepancies. Only a fresh complete poll with an
   explicitly null matchup permits a recorded exemption; completeness is captured at ingest.
5. Clearing chance uses `>=`; notifications, advice, explanations, and new persisted
   rationales preserve half-point thresholds and say “score X or more.” Numeric JSON and
   existing historical records are preserved. Infinite thresholds still produce no rule.

Synthetic regressions in `tests/test_review_p2.py` exercise persisted calendar abstention,
malformed/missing deadlines, exact expiry, request-boundary timing, missing evidence,
partial ingests, provenance selection, read-only legacy schemas, no-matchup exemptions,
retrospective display, and daily shadow histories. They reproduce the twelve-abstention
failure, rerun recovery, retained discrepancies, missing roster-weeks, and the exact 11.5
LOCK/display/100% clearing-probability case.

Final verification:

- Full suite with local socket access: **693 passed, 1 skipped** (105.68 seconds).
- Focused final advice and P2 regression rerun: **65 passed**; this includes the explicit
  11.5 threshold assertion and selected-fetch freshness fixtures.
- Ruff and `git diff --check`: **passed**.
- Initial sandbox execution could not open the cron/HTTP test sockets; the full suite
  above was rerun with the required access.

Code and documentation are ready for review. No deployment was performed. These synthetic
checks close the implementation findings; the new season must still accumulate the two
full weeks of daily inference evidence required by the corrected shadow gate.
