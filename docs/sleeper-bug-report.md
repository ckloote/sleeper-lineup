# Reporting the upstream mutation to Sleeper

The finding is [implementation-plan.md §12](implementation-plan.md) and its two follow-ups:
Sleeper is still rewriting the completed 2025-26 season, five months on, and it is worst in
weeks 19-24 where the league was decided. This file is the report itself, kept in the repo
because every figure in it is reproducible from `snapshots/` and will need restating if
they reply.

**Where it goes: `support@sleeper.com`.** That is the only channel Sleeper offers for this.
Their [contact page](https://support.sleeper.com/en/articles/8017487-general-contact-information)
lists email and a mailing address; [complaints and suggestions](https://support.sleeper.com/en/articles/8017538-customer-help-and-complaints)
says to email. There is no in-app path, no chat widget, and — checked directly — no
developer channel of any kind at [docs.sleeper.com](https://docs.sleeper.com/): no Discord,
forum, GitHub or feedback link. [@SleeperSupport](https://x.com/SleeperSupport) on X is the
realistic escalation if email goes nowhere.

Worth knowing when framing it: the API is documented as free for non-commercial use,
read-only, with no stated guarantee of accuracy or historical availability. They have not
promised that past results are stable. Hence the ask below is "is this expected?" rather
than "this is broken".

**Every figure here was measured on 2026-09-01** against the committed snapshot archive.
Nothing is inherited from the August analysis — §12's `0/10 lineups` claim did not
reproduce and has been withdrawn, which is why the renumbering paragraph reads differently
from the one in that section.

Send **Part 1**. Send Part 2 if they engage.

---

## Part 1 — first contact

```text
To: support@sleeper.com
Subject: Completed NBA season's results still changing months later (league 1283214955830575104)

Hi,

I archive raw API responses for my league, and I can show that results from a
completed season are still being rewritten five months after it ended.

  League:   1283214955830575104  (NBA, Lock-In, season 2025, status "complete")
  Endpoint: GET https://api.sleeper.app/v1/league/{league_id}/matchups/{week}
  Fields:   players_points / starters_points

Between 2026-08-08 and 2026-09-01, with no games played in between, 140 starter
scores changed across the season. 137 of them are in weeks 19-24 — the last
three regular-season weeks and all three playoff weeks. Week 23 alone moved 32
of its 60 starter values.

The box scores are unchanged, and so are the lineups. What moves is which of a
player's games is reported as counting for that week. One roster's week-12 total
went 221.5 -> 289.0 -> 201.5 across three observations; another went 341.5 ->
313.0 and back to exactly 341.5. So this isn't a correction settling on a right
answer.

My question: is this expected for Lock-In leagues? If lock selections aren't
persisted for completed seasons and each read regenerates them, that would
explain everything I'm seeing — and would be worth documenting, since it means
historical results are not stable.

I have timestamped raw JSON for all of it, plus a fuller write-up with the
explanations I've already ruled out. Happy to send either.

Thanks,
[your name]
```

---

## Part 2 — the full report

```text
To: support@sleeper.com
Subject: Completed NBA season's matchup results keep changing months later (league 1283214955830575104)

Hi,

I'm reporting a data-integrity issue, not asking for a fix to my own league — I
suspect nothing can be restored at this point. I've been archiving raw API
responses for a completed season and can show that finished results are still
being rewritten five months after the season ended.

WHAT'S HAPPENING

For a completed NBA Lock-In league, the per-player scores returned for past weeks
change over time. The underlying box scores do not. What changes is which game's
score is reported as counting for a player that week.

  League:   1283214955830575104  (NBA, Lock-In, season 2025, status "complete")
  Endpoint: GET https://api.sleeper.app/v1/league/{league_id}/matchups/{week}
  Fields:   players_points / starters_points

EVIDENCE

I have raw JSON responses saved with timestamps. Week 12, three observations:

  observed (UTC)      team mean    sd    roster 7   roster 9
  2026-08-06 01:46        285.9  39.0       221.5      341.5
  2026-08-08 02:22        292.9  23.7       289.0      313.0
  2026-09-01 00:58        282.0  45.0       201.5      341.5

Two things to note. Roster 7's week-12 total moved by 67.5 points and then by
87.5 back the other way. Roster 9 returned to exactly its original 341.5 — so
this isn't a one-time correction settling on a right answer; values move and
come back.

On 2026-09-01 I re-fetched all 25 weeks and compared against responses saved on
2026-08-08 — a 24-day gap, with no games played in between:

  week 10:  1 of 60 starter values changed
  week 13:  1
  week 14:  0  (payload differed, no starter value did)
  week 16:  1
  week 19: 18
  week 20: 23
  week 21: 23
  week 22: 28   <- playoffs
  week 23: 32   <- playoffs
  week 24: 13   <- playoffs
  weeks 1-9, 11, 15, 17, 18, 25: unchanged

  140 starter values changed in 24 days. 137 of them are in weeks 19-24 — the
  last three regular-season weeks and all three playoff weeks. Week 23 moved
  more than half its starters.

The concentration in the weeks that decided the league is the part I'd most
want someone to look at.

WHAT IS NOT CHANGING

The box scores are stable. Every changed value is one of that same player's own
game scores from that same week. Example — Ivica Zubac (player 1697), week 12,
who played four games:

  Jan 5  42.5   Jan 7  54.5   Jan 9  12.5   Jan 10  29.0

The three observations report 54.5, then 42.5, then 29.0 — his Jan 7, Jan 5 and
Jan 10 games. His stat lines, his roster slot and his starter status never
changed. So this looks like the selection of which game counts, not a stat
correction rippling through.

The lineups are stable too. Across the 11 weeks for which I hold more than one
observation, the "starters" arrays are byte-identical every time — not one
roster changed in any week. Only the points attached to those unchanged players
move.

WHAT I RULED OUT

- Week renumbering, i.e. the same data being re-labelled under a different week.
  I compared my earliest week-12 response against today's response for all 25
  weeks. Week 12 is uniquely the match: 43 of 60 starter values agree, and all
  10 lineups are identical. The best any other week manages is 2 of 46 values,
  and week 12's own lineups are unchanged throughout. Same week, same rosters.
- A mechanical fallback such as "best game" or "last game played". Neither rule
  fits: across the 55 week-12 values that moved, "best game" accounts for 21-31
  of them and "last game" for 15-31, depending on which observation you check.
- Per-request non-determinism. Six fetches of the same endpoint over ten seconds
  returned byte-identical payloads. So this isn't a flaky read path; something
  is writing a new value periodically.

WHY IT MATTERS

There's no historical endpoint, so once a value changes the previous one is
unrecoverable unless someone happened to save it. For anyone doing season
review, record-keeping or analysis against past results, the same query returns
different answers depending on the day it was run — and for this league the
playoff weeks are the least stable of all.

WHAT WOULD HELP

1. Is this known, and is it expected behaviour for Lock-In leagues specifically?
   If lock selections aren't persisted for completed seasons and each read
   regenerates them, that would explain everything I'm seeing — and would be
   worth documenting, since it means historical results are not stable by
   design.
2. If it isn't expected, the weeks 19-24 concentration seems like the strongest
   clue.

I have the raw timestamped JSON for all of the above and am happy to send any of
it. Happy to answer questions.

Thanks,
[your name]
```

---

## Reproducing the figures

All of it runs off the committed archive; none of it needs the network or the database.

| claim | where it comes from |
|---|---|
| the three week-12 observations | `snapshots/matchups/2025/wk12/` — three files |
| the 24-day sweep | `lockin observe`, which wrote the `20260901T0202*` snapshots |
| every changed value is one of that player's own games | `box_scores` scored through `core.scoring.score_recorded` |
| lineups never change | `starters` arrays across all 11 multi-observation weeks |
| renumbering ruled out | earliest wk12 payload vs `snapshots.latest` for all 25 weeks |
| six identical reads | six calls to `SleeperClient.matchups` over ten seconds |

If they ask for raw data, send the three `wk12` files first — they carry the argument on
their own and are about 5 KB each.

## If they reply

The most useful outcome is a yes/no on whether lock state is persisted for completed
seasons. A "yes, expected" closes §12's open mechanism question and means the archive is
the only route to a stable record — which is what `lockin observe` already assumes. A "no,
that's a bug" makes the weeks 19-24 concentration the thing to hand over next.

Either way the project's decision does not change: today's data stays canonical, `Actual`
stays ungated, and the four box-score policies are unaffected.
