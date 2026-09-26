# Follow-up review — 2026-09-26

**Resolution status:** all three findings are fixed on branch `review-0926-fixes`, one
commit each, with regressions shaped like the reproductions below. The findings are kept
as written for audit; the update at the end records the fixes and how they were checked.
Deployment is outside this change.

Reviewed HEAD `815e7cc`, focusing on the changes since `45d6c2f` and the live
consumers around them. The five previous findings have substantive fixes and
passing regression coverage. Three remaining issues are reproduced below.
Application code was not changed. Reproductions used temporary synthetic
databases, without live API requests or external notifications.

## 1. P2 — An explicitly incomplete poll still produces actionable live advice

Locations: `lockin/state.py:105-109`, `lockin/digest.py:299-308`.

Ingest now records `weekly_matchup_teams.poll_complete`, but ordinary live
inference never checks it. `load_context()` constructs the lineup from the
incomplete latest poll, and `infer_state()` checks only those remaining players.
A missing starting slot therefore disappears from both the simulation and the
state checks. The completeness guard currently protects only the no-matchup
exemption.

Reproduction: use `SyntheticSeason`, play through 2026-10-27, and wrap
`matchups_payload()` to remove the final entry from roster 1's `starters` array
while retaining its players, points and matchup ID. Ingest weeks 1 and 2, then
call live `morning()` for 2026-10-28 at 13:00 UTC with `SCALED` projection
parameters and 30 simulations. The stored poll has `poll_complete=0`, but the
digest has `abstained=False`, `state_source='inferred'`, four calls and P(win)
0.30. Its lineup contains only players 1001–1005, losing the sixth starter.

Require complete, coherent lineup evidence for both teams before live advice.
Merely selecting an older complete state poll is insufficient if the lineup is
still loaded from the newer incomplete poll. Cover the ordinary matchup path,
including incomplete opponent membership and valid explicit empty slots.

## 2. P2 — One checkable player per morning can still qualify an entire shadow week

Locations: `lockin/shadow.py:365-371`, `lockin/shadow.py:465-470`.

The new daily coverage check uses `any()` over final player evidence. Meanwhile,
`_state()` silently skips players whose final truth is unknown. Thus daily
inferred runs plus one checkable starter qualify a week even when the remaining
starters cannot be verified. The report records neither missing coverage nor
discrepancies for those players, yet authorizes ending the daily manual check.

Reproduction: use the two-week persisted history shape from
`tests/test_review_p2.py`, covering every morning of weeks 2 and 3. Leave player
1001's final counted points intact and set the other roster-1 players' final
counted points to NULL. Persist an incorrect banked score of 999 for player 1002
in each inferred run. `shadow.build(...).gate()` returns
`(True, 'weeks 2-3 clean')`; each week reports seven state checks, zero misses,
and no uncheckable mornings. Only one of six starters was checked each day;
the deliberately incorrect banked value was skipped along with its missing
final evidence.

Track verification coverage for the relevant players as well as the mornings.
Missing or ambiguous final evidence must remain visibly unverified and prevent
the gate from claiming a fully checked week unless a justified exemption
applies. A mixture of checkable and uncheckable players needs its own regression;
the existing test makes every player uncheckable and does not catch this case.

## 3. P3 — Historical replays without calls lose their historical label on the page

Location: `lockin/advice.py:477-482` (inside `if calls`); standing rules are
rendered independently at `lockin/advice.py:507-525`.

The retrospective warning is conditional on having LOCK/PASS calls. A replay
with only standing rules renders ordinary instructions such as “Tonight” and
“Lock him at this score or more,” without saying it bypassed the live guards.
This is a normal Monday shape, because there are no earlier games in the week
to generate calls. The text digest correctly labels the same run historical.

Reproduction: on the synthetic database played through 2026-10-27, replay
2026-10-26 with `live=False`, `SCALED` parameters and 30 simulations, persist it,
and render `advice.latest_run()`. The digest contains zero calls and ten rules;
the page contains the lock instructions but no “Historical replay” warning.
An old-date staleness banner can still appear, but it does not communicate the
replay's different validation semantics.

Render the historical label at run level, regardless of whether the run has
calls, rules, or only a note. Test a rules-only replay as well as a replay with
calls.

## Validation

- Full suite in the sandbox: **667 passed, 1 skipped, 2 failed, 35 errors**.
  All failures/errors were local socket permission failures in the cron and
  HTTP suites.
- Reran `tests/test_cron_guard.py` and `tests/test_serve.py` with local socket
  access: **39 passed**, including all 37 restricted cases and two cases that
  already passed. Combined unique result: **704 passed, 1 skipped**.
- Ruff and `git diff --check`: passed.
- Temporary synthetic reproductions confirmed all three findings above.

The passing suite verifies the implemented regressions; it does not cover the
mixed-evidence shadow case or reject incomplete ordinary lineup polls. Those
two correctness gaps should be closed before relying on the automatic gate.


## Implementation and verification update

All three findings are resolved:

1. **Incomplete polls (`e88a1f2`).** A live digest now requires a whole poll for both
   teams: the polls their lineups are read from, not an older whole poll picked for the
   lock state. `load_context` records each roster-week's `poll_complete` from
   `weekly_matchup_teams_latest`, which selects the same latest poll as
   `weekly_matchups_latest`. `partial_lineup` joins the live guards ahead of the "no
   starters" branch and abstains with a note naming the roster. A NULL (legacy) poll does
   not vouch for itself; an explicit empty slot counts as accounted for. The no-matchup
   check's own completeness term read the same poll after the new guard, so it was
   dropped. Replays are unchanged.
2. **Shadow coverage (`7765a53`).** `_state` no longer skips a starter whose final score
   cannot settle a morning's reading. It records the reading as unverified, once per
   morning and player, with the reason: no final score, a score nothing explains, a final
   zero despite a played game, a tie with the final game, or tied games on either side of
   the morning. A week with any unverified reading is not clean, and the report lists
   them in a section of their own. One justified exemption: before a starter's second
   game no lock can show, so "nothing banked" is the right reading whatever the final
   score says. That turns such early mornings into real checks; for readable truths the
   answer is unchanged. On the 2025-26 season, 18 of 250 roster-weeks had a starter
   whose final score cannot be read, so two fully verifiable weeks in a row stay likely.
3. **Replay label (`e5bd7fa`).** The page carries a run-level "Historical replay" banner
   under the age banner, above the win probability, whether the run has calls, rules, or
   only a note. A replay's rules are dated, never "Tonight", and described as what that
   morning would have said. The text digest prints its label before any note, which
   covers the compact notification.

Regressions in `tests/test_review_p2.py` and `tests/test_shadow.py`, each seen failing on
the unfixed code first:

- Your final starter missing, and the opponent's, both abstain, with no calls, rules or
  P(win). A NULL completeness abstains too. An explicit empty slot in an ordinary matchup
  still advises from the inferred state, and a replay of the incomplete case still runs.
- The reproduction above, where 1001 is the only readable starter and 1002 reads as 999
  every morning: the gate fails. 1002–1005 are unverified after their second games, and
  the 999 is a miss on 1002's first morning. 1006's second week-3 game is the week's
  last night, so none of his readings depend on a final score. With 1002 alone
  unreadable, the week is not clean, the other starters are still checked, and a rerun
  adds no duplicate entries.
- The shared history's seed has a natural tie: 1003 scored 11.5 on 10-28 and in his final
  game on 10-30. Week 2 is flagged for 10-29 to 11-01 and week 3 stays clean. The
  fixture's clean baseline now sits him out of 10-28, so the tests that need a passing
  gate still have one.
- A rules-only replay and a note-only replay carry the banner, above the state and
  without "Tonight". So does the replayed Monday 2026-10-26 above: no calls, standing
  rules, persisted and rendered. A live run of the same season has no banner.

Final verification, from the branch worktree, with `LOCKIN_DB` pointed at a copy of the
season file:

- Full suite: **715 passed, 1 skipped**, cron and HTTP suites included (local sockets
  were available).
- Ruff check, Ruff format check, and `git diff --check`: **passed**.

`docs/day-one.md` describes the new live abstention and the per-starter shadow gate. No
schema change or data migration. The page change needs a `lockin-serve` restart to show.
