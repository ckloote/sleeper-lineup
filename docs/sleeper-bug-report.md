# Reporting the upstream mutation to Sleeper

The finding is [implementation-plan.md §12](implementation-plan.md) and its follow-ups:
Sleeper is still rewriting the completed 2025-26 season, five months on. This file is the
report itself, kept in the repo because every figure in it is reproducible from
`snapshots/` and will need restating if they reply.

> **Status: Part 1 sent 2026-09-01. Two of its claims are now withdrawn. Send Part 4.**
>
> 1. *The weeks 19-24 concentration* — withdrawn 2026-09-02. The next day's observation
>    moved 497 starter values across 24 of 25 weeks, putting only 27% of them in weeks
>    19-24. It was true of one 24-day window and is not a property of the mutation.
> 2. *The proposed mechanism* — withdrawn 2026-09-20. Part 1 offers "if lock selections
>    aren't persisted for completed seasons and each read regenerates them" as the
>    explanation. Nineteen days of daily sampling show the opposite: the correct locks
>    **are** persisted, and a wrong value reverts to the stored one at the next rewrite
>    (implementation-plan.md §12, "The locks are intact").
>
> **Parts 1 and 2 below are left exactly as sent** — rewriting a report already in
> someone's inbox would only make this file disagree with what they are holding. Part 3
> was drafted but never sent and is **superseded by Part 4**, which carries its retraction.
>
> What survives from Part 1 and is strengthened: ongoing, oscillating, not per-read, every
> value one of that player's own games, box scores and lineups stable.

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

**Parts 1-3 were measured on 2026-09-01; Part 4 on 2026-09-20**, both against the
committed snapshot archive. Nothing is inherited from the August analysis — §12's
`0/10 lineups` claim did not reproduce and has been withdrawn, which is why the
renumbering paragraph reads differently from the one in that section.

**Send Part 4.** Send Part 2 if they engage — its evidence still stands, but read its
"WHAT I RULED OUT" section against Part 4 first: the mechanism it proposes in point 1
is the one Part 4 withdraws.

---

## Part 1 — first contact (sent 2026-09-01)

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

## Part 2 — the full report (not sent)

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

## Part 3 — superseded, not sent

Drafted 2026-09-02 and overtaken on 2026-09-20 before it went out. Its retraction of the
weeks 19-24 claim is correct and is carried forward verbatim into Part 4's opening
paragraph; its closing line — "everything else in my original message stands" — is not,
since Part 1's mechanism has since been falsified. **Do not send this.** Kept only so the
record shows what was drafted when.

```text
To: support@sleeper.com
Subject: Re: Completed NBA season's results still changing months later (league 1283214955830575104)

Hi,

Correcting one thing in my message of 1 September, and adding a much larger
number.

I said 137 of 140 changed values fell in weeks 19-24. That was true of the one
24-day window I had measured, but it is not a property of the problem, and I
would not want you chasing the playoff weeks on the strength of it.

I have since started sampling daily. Between 2026-09-01 02:02 UTC and
2026-09-02 03:16 UTC — 25 hours, no games played — 497 starter values changed
across 24 of the 25 weeks. Only 132 of those were in weeks 19-24, which is 27%,
about what six weeks out of twenty-four gives you by chance. So the rewriting is
season-wide, and far faster than I could see when I was sampling by accident.

The rate is also not constant: 140 values in the preceding 24 days, then 497 in
a single day. Something either changed at your end in that window, or the
earlier period was unusually quiet.

One player as an illustration. Ivica Zubac (1697), roster 1, week 12, across
four observations of that same finished week:

  2026-08-06   54.5   (his Jan 7 game)
  2026-08-08   42.5   (Jan 5)
  2026-09-01   29.0   (Jan 10)
  2026-09-02   54.5   (Jan 7 again)

Four reads, four different games, back where it started. His stat lines and his
lineup slot never changed.

Everything else in my original message stands, and the daily series will keep
accumulating. Happy to share it.

Thanks,
[your name]
```

---

## Part 4 — the correction to send (supersedes Part 3)

Part 3 was never sent, and is now superseded. It withdrew one claim; the daily series has
since overturned Part 1's proposed *mechanism* as well. Sending both would mean two
corrections in a row, the second revising the first. Send this instead — it carries Part
3's retraction in its opening paragraph.

The substance is better than what it replaces. Part 1 guessed the locks had been lost.
They have not been: they are intact, and the wrong values revert to them. That is a far
more tractable bug for whoever picks it up, and it means the ask changes from "is this
expected?" to "what recomputes locks for a completed season?"

```text
To: support@sleeper.com
Subject: Re: Completed NBA season's results still changing months later (league 1283214955830575104)

Hi,

I've been sampling daily since I wrote on 1 September. Two corrections, and a
finding that I think makes this much more tractable than I first described it.

FIRST CORRECTION

I said 137 of 140 changed values fell in weeks 19-24. That was true of the one
24-day window I had measured and is not a property of the problem. Between
1 September 02:02 and 2 September 03:16 UTC — 25 hours, no games played — 497
starter values changed across 24 of the 25 weeks, only 27% of them in weeks
19-24. The rewriting is season-wide. Please don't chase the playoff weeks on
the strength of my first message.

SECOND CORRECTION, AND THE USEFUL PART

I suggested lock selections might not be persisted for completed seasons, so
that each read regenerates them. That is wrong, and I can now show it is
wrong. The correct locks are still stored. The wrong values revert to them.

From 163 archived responses covering all 25 weeks, 6 August to 20 September,
sampled daily since 2 September:

- Each starter has one value it keeps returning to. My oldest snapshot, taken
  6-8 August, matches that value for 97% of 1,382 starter slots. The original
  results are still recoverable, and still what you serve most of the time.

- When a value is currently wrong, the next rewrite restores it 85-88% of the
  time. When it is currently right, it survives the next rewrite 69-80% of the
  time. Something that regenerated the lock on each pass would give the same
  number from both states. A HIGHER rate of return-to-correct than
  stay-correct means something in the system knows the right answer.

- 84% of wrong values are corrected at the very next rewrite, and the rest at
  the one after. They are transient blips, not a drift away from the truth.

That reads to me like an intermittent write or cache-fill putting a
wrong-but-plausible value in front of the stored one, with a later pass
restoring it. Not data loss.

WHAT THE WRONG VALUE LOOKS LIKE

Still always one of that player's own games from that same week — 9,385 of
9,385 non-zero values I have ever recorded, no exceptions. Box scores and
lineups are unchanged throughout: across 138 follow-up observations, not one
"starters" array has ever differed.

But it is not only which game counts. A lock can appear and disappear
entirely. Kristaps Porzingis (player 1590), roster 6, week 1 — he played
exactly one game that week, worth 41.5:

  08-08  41.5        09-14  41.5
  09-02   0.0        09-16   0.0
  09-03  41.5        09-18  41.5

There is no second game to choose between. The value is simply absent on some
reads and present on others, and it comes back every time. Across the archive
I have 106 transitions from 0.0 to a real score and 104 the other way. In a
Lock-In league a 0.0 means the manager never locked that player, so these
reads are inventing and erasing manager decisions, not just reattributing
points between games.

The wrong values are also not random. A real lock lands on that player's best
game of the week 53-74% of the time, depending on how many games he played —
managers pick well. The wrong values are much flatter but still tilted toward
the better games. That is presumably why this has gone unnoticed: nothing on
the page ever looks absurd.

HOW MUCH IT MOVES

Across every response I hold, 13% of completed head-to-head matchups are
reported with the wrong winner — 101 of 791. When a team total is wrong it is
off by a median of 24.5 points, and by as much as 155. As I write, 3 of 118
matchups are wrong. One of them is week 14, rewritten this morning, which on
the pattern above I expect to correct itself on the next pass.

TIMING

Week 24 I sample twice a day. It changed on 2, 4, 6 and 8 September and not on
the 3rd, 5th, 7th or 9th — a clean 48-hour alternation. Then it changed on
four consecutive days, 14 to 17 September, and has not changed since.
Whole-season sweeps touching 20-24 weeks at once landed on 2, 3, 14, 16 and
18 September. Single weeks moved alone on the 6th, 7th, 9th and 20th, and
weeks 18 and 23 moved together on both the 7th and the 9th. So it runs in
bursts rather than on a fixed schedule.

THE QUESTION, REVISED

Not "are the locks gone" — they aren't. What I'd like to know is what
recomputes or re-serves lock selections for a completed season, and why it
sometimes yields a different game than the stored one. Something that ran on
2, 3, 14, 16 and 18 September, and that touched weeks 18 and 23 together on
the 7th and the 9th, would be the thing to look at.

I have all 163 raw responses with timestamps and can send any of them, or the
lot. Happy to answer questions.

Thanks,
[your name]
```

---

## Reproducing the figures

All of it runs off the committed archive; none of it needs the network or the database.

| claim | where it comes from |
|---|---|
| the three week-12 observations | `snapshots/matchups/2025/wk12/` — the first three files |
| the 24-day sweep | `lockin observe`, which wrote the `20260901T0202*` snapshots |
| every value is one of that player's own games | `box_scores` scored through `core.scoring.score_recorded` |
| lineups never change | `starters` arrays across all 138 follow-up observations |
| renumbering ruled out | earliest wk12 payload vs `snapshots.latest` for all 25 weeks |
| six identical reads | six calls to `SleeperClient.matchups` over ten seconds |
| reversion rates, 97% agreement with August, excursion lengths | `lockin repair --stats` |
| 13% wrong winners, 24.5-point median error | `lockin repair --stats` |
| the majority value for any given slot | `lockin repair --json`, field `consensus` |
| the timing table | `logs/observe.log` plus the `wk24` snapshot filenames |

If they ask for raw data, send the six `wk01` files first. Porzingis is in every one of
them, they are about 5 KB each, and a player with one game whose value blinks on and off
needs no explanation from me. The `wk12` files are the better follow-up if they want to
see the which-game-counts version.

## If they reply

The question worth an answer is now narrower than it was: **what recomputes or re-serves
lock selections for a completed season?** Persistence is no longer in doubt — the archive
settles it — so a "that's expected" answer would have to explain why a stored value is
intermittently not the one served, which is a harder thing to call expected.

Two outcomes are worth preparing for:

- **They can identify the job.** Then the timing table is the thing to hand over next: the
  sweep dates, the 48-hour alternation on week 24, and weeks 18 and 23 moving together
  twice. That is the shape of a scheduled task, and they can match it against their own.
- **They cannot reproduce it.** Offer the archive. 163 timestamped responses for one league
  over 45 days is probably more than they have retained themselves, since there is no
  historical endpoint.

What does **not** depend on their answer: the project can already repair itself. `lockin
repair` recovers the original values from the archive by majority vote and needs nothing
from Sleeper. That was not true when Part 1 was sent, and it is the reason the tone here
can stay unhurried.
