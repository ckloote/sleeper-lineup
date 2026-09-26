# Follow-up review — 2026-09-26

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
