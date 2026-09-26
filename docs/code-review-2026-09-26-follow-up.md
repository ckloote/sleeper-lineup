# Follow-up review of the September 26 fixes

**Resolution status:** both findings are fixed on branch `review-0926-followup-fixes`, one
commit each, with regressions shaped like the reproductions below. The findings are kept
as written for audit; the update at the end records the fixes and how they were checked.
Deployment is outside this change.

**Status as reviewed:** two outstanding P2 findings. No implementation changes were made.

Reviewed HEAD `bb34153`, focusing on the fixes since `815e7cc` and their
surrounding validation paths. The original three reproductions are addressed,
including the run-level historical replay label. Two nearby correctness gaps
remain. Reproductions used temporary synthetic databases, without live API
requests, external notifications, or production database changes.

## 1. P2 — Partial final polls can still pass the shadow gate

Locations: `lockin/shadow.py:215` (`truths`), `lockin/shadow.py:515-518`
(`_state`).

The new per-player verification handles missing counted scores for players
present in the final lineup, but derives the set of starters to check from
`weekly_matchups_latest`. If the latest final poll omits a starter, that player
disappears from the truth map and from verification unless a persisted run
explicitly banked him. The shadow evaluator does not require the final poll's
`poll_complete` flag to be true.

Reproduction: build the two-week `persisted_history` fixture from
`tests/test_review_p2.py`. Its baseline gate returns
`(True, 'weeks 2-3 clean')`. In the temporary database, set `is_starter=0` in
`weekly_matchups` for roster 1's players other than 1001, and set roster 1's
`weekly_matchup_teams.poll_complete=0`. This models the stored membership of
a partial final poll retaining only one starter, with the other players still
on the roster. Rebuild the shadow report. The gate still returns
`(True, 'weeks 2-3 clean')`; each week has seven state checks and zero
unverified readings.

Consequently, missing final membership can still authorize ending the daily
manual cross-check after checking only one player per morning. Require complete
final membership before qualifying a week, and report incomplete final evidence
as unverified. Cover missing starter membership separately from NULL counted
scores, including a case with no persisted banked entry for the omitted player.

## 2. P2 — Duplicate starters bypass the new completeness guard

Locations: `lockin/ingest/sleeper.py:374` (`poll_complete`),
`lockin/digest.py:565-567` (`partial_lineup`).

`poll_complete` checks the starters array's length and whether named players
belong to the roster, but does not check that the named players are unique.
Ingestion constructs `slot_of` keyed by player ID, so duplicate entries
collapse into one player. The live completeness guard trusts the resulting
`poll_complete=1`, even though the loaded lineup has lost a starting slot.

Reproduction: call `ingest_with_starters` from `tests/test_review_p2.py` for
roster 1 with `lambda s: [*s[:-1], s[0]]`, replacing the sixth starter with the
first. Ingest records `poll_complete=1` for week 2. Calling the fixture's live
`morning` helper returns `abstained=False`, no explanatory note, four calls,
and P(win) approximately 0.1667, using only five distinct starters.

Require unique nonempty starter IDs when certifying poll completeness, while
continuing to allow repeated explicit empty slots (`"0"`). Add regressions for
duplicate starters on both the user's and opponent's roster and require live
advice to abstain.

## Validation

- Focused suites: `tests/test_review_p2.py`, `tests/test_shadow.py`, and
  `tests/test_advice.py`: **105 passed**.
- `ruff check lockin tests`: passed.
- `git diff --check`: passed.
- Temporary synthetic reproductions confirmed both findings above.
- The full suite was not rerun for this follow-up review.

The passing tests verify the existing fixes but do not cover these two cases.


## Implementation and verification update

Both findings are resolved:

1. **Partial final polls (`f0b89c1`).** `shadow.build` reads each tracked week's final
   `poll_complete` from `weekly_matchup_teams_latest`, the same poll `truths` takes its
   starters from. A week whose final poll is not whole is not clean. Its week line in the
   report reads `incomplete final poll: a starting slot is unaccounted for, so its starter
   is unverified`. As in the live digest, NULL (a legacy poll) does not vouch for itself.
   The starters the poll does list are still checked, and their misses and unverified
   readings are still reported. `lockin repair` wrote its new final poll with a NULL flag
   although it copies the corrected poll's whole membership. Under the new check that
   would have kept a repaired week out of the gate for good, so the repair now carries the
   flag over.
2. **Duplicate starters (`0ef0445`).** `poll_complete` also requires each named starter
   to be distinct. Explicit empty slots are not named players, so they may still repeat.
   The ingest loop is unchanged. A poll with a repeated starter is now recorded as
   incomplete, and live advice, the no-matchup exemption and the shadow gate all refuse
   it.

Regressions, each seen failing on the unfixed code first:

- `tests/test_review_p2.py`, finding 1:
  - The reproduction above. Roster 1's final polls keep only 1001 as a starter, are
    marked incomplete, and nothing is banked for the dropped starters. The gate returns
    `(False, 'week(s) 2, 3 not clean')`, both weeks carry the flag, and there are no
    misses or unverified readings: the flag alone blocks them.
  - Week 3's final poll marked incomplete, or NULL, with every starter present. Week 3
    alone is not clean, and week 2 still is.
  - These are separate from the NULL-counted-score tests of the 09-26 review.
- `tests/test_repair.py`: the repaired team row keeps `poll_complete = 1`.
- `tests/test_review_p2.py`, finding 2:
  - The live test for a missing starter also runs the duplicate `[*s[:-1], s[0]]`, for
    your roster and the opponent's. Both abstain with no calls, rules or P(win), and a
    replay still runs.
  - The completeness unit test adds a repeated starter (not whole) and two empty slots
    (whole).
  - A matchup with two empty slots still advises.

Real data: all 2,280 team polls in the 2025-26 matchup archive, including the last one of
each of the 25 weeks (250 final polls), are whole under the new rule. None names a
starter twice. Neither fix changes the reading of a stored poll, and neither leaves the
gate unreachable on a season like the last one.

Final verification, from the branch worktree, with `LOCKIN_DB` pointed at a copy of the
season file:

- Full suite: **723 passed, 1 skipped**. That is the previous 715 plus the eight new
  cases, with the cron and HTTP suites included (local sockets were available).
- Ruff check, Ruff format check, and `git diff --check`: **passed**.

`docs/day-one.md` describes the stricter completeness rule and the final-poll
requirement of the shadow gate. There is no schema change or data migration. Ingest
applies the new rule at its next run. The page is unchanged, so `lockin-serve` needs no
restart.
