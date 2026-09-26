# Follow-up review of the September 26 fixes

**Status:** two outstanding P2 findings. No implementation changes were made.

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
