# Changelog

## Unreleased

- **Bench-aware lock/pass, off by default.** A lock call's "pass" used to be
  priced as the same player riding out his own remaining games. That misses a
  real option: pass, then bench him and start someone who plays later. Leaving
  it out made passing look too weak, so the digest locked mid-level nights that
  a stronger bench player would have beaten. With `LOCKIN_BENCH_AWARE=1` (or
  `lockin digest --bench-aware`), each branch of a call fields the best legal
  lineup from the unlocked starters and the bench, chosen on expected value.
  Locking pins the player to his slot. Passing keeps that slot open for a bench
  player. A call that turns on this says "pass: start …" and, when the
  re-solved lineup sends someone to the bench, "and bench …", then
  "lock lineup sits …" for whom the modeled lock lineup leaves out. That player is
  not always the one deciding, so `explain` lists the slot moves. When the
  projection assumes a bench player starts, WATCH says "assumed to start".
- Bench players designated Out, IR or Suspended are left out. They get the raw
  DNP hazard, not the starter correction. A bench player who can't be
  projected is skipped, and the call falls back to the old pricing rather than
  abstaining.
- `lockin backtest --bench-aware` replays a `rollout+bench` policy on the same
  draws as rollout. It makes the swaps it priced, then reports points, zeroed
  slots, locks and paired wins. The existing gates are unchanged.
- **It fails that gate on 2025-26 and stays off.** Over 3 seeds it lost 12–13
  points a week, zeroed 64–76 slots against 46–49, and flipped about 8 matchups
  to wins and 30 to losses (z ≈ −3.5). Bench player-weeks are projected about
  21% above what they score; starters run about 9% below. Even an in-sample 21%
  haircut still lost. A retry needs a bench value correction fit on strictly
  earlier weeks.
- **Only rotation regulars are bench candidates** (`lockin/rotation.py`). That
  backtest's bench was each week's final poll, which holds mostly players
  pulled for a reason. Bench regulars there played 55% of their games and
  scored 0.54× projection. A player who sits only on nights he has no game was
  barely in it. A regular is someone this roster started on at least 60% of
  his game nights (at least 3). That is read from the first poll after each
  slate and before the next day's first tip. He must also have played his last
  game. Regulars get the starter DNP correction. Everyone else on the bench is
  left out.
- **Bench shadow, on by default.** With bench-aware calls off, each digest also
  builds the bench-aware digest for the same morning and records it in two new
  tables, `bench_shadow_runs` and `bench_shadow`, keyed to the live run. Calls
  and the notification are unchanged. `digest.log` gets a `bench shadow:` line
  naming the regulars and any call that would flip. A failed shadow is
  recorded and never stops the digest. `LOCKIN_BENCH_SHADOW=0` turns it off.
- Review fixes (PR #3):
  - A swap-in's games before the swap night are simulated for his availability,
    rather than dropped, and are never counted.
  - The `--bench-aware` backtest replays on an isolated cache, so it can no
    longer change rollout's numbers or the existing gates.
  - A bench player whose game has already tipped can be moved in only from
    the day after. A live rerun used to price a promotion into a game already
    under way.
- The two new tables are created by `CREATE TABLE IF NOT EXISTS` on the next
  run, with no migration. With bench-aware off, the digest's calls and numbers
  are unchanged; the model id gains "; bench-aware" when it is on.

## 0.1.4 — 2026-10-29

- A lock shows in the matchup poll only once the player has played again. So a
  player with a night off is called on the same game two mornings running, and
  a lock made after the first call is invisible on the second. On 10-29 the
  digest repeated Wednesday's LOCK calls on Sabonis and Coward, and counted
  both as unlocked in its banked total, P(win) and thresholds. A repeated call
  now says "Wed said LOCK; ignore if you locked". The full digest adds a
  footnote naming who the totals count as unlocked, and the advice page marks
  the row, the heading and the banked line. The digest does not assume the
  earlier call was taken, so a missed check-in still gets the call. Only live
  runs from earlier mornings count. With `--locked` there is no reminder.
- No schema change or migration. The page computes the mark from the runs it
  already stores, so restarting `lockin-serve` marks the 10-29 run's calls too.
- A page test stamped a replay with the wall clock. From 2026-10-28 13:00 UTC
  that replay outranked the synthetic live run the test then read back, so the
  test failed. The replay now has a fixed stamp.
- Validated with 744 passing tests (1 skipped), Ruff checks, and the 10-29
  digest and page rerun from a copy of the 2026 database: same numbers, both
  calls marked.

## 0.1.3 — 2026-10-26

- Sleeper kept `start_week=1`. The league drafted on the last day of week 1 and
  the commissioner zeroed that week, setting every team's `custom_points` to 0.
  A week zeroed that way is now detected from its final polls, and its lineups,
  filled after its games, are left out of the started-player DNP correction:
  week 1's starters sat 1.2% of their games, against 8.5% for real starters in
  2025-26. One shared mask now serves the digest, the backtest and the manager
  scorecards. 2025-26 has no such week, so its replays are unchanged.
- The shadow gate owes a morning only a run, as on Monday, when no starter has
  yet played twice and so no lock can show. A cold-start abstention on the
  Tuesday of week 2 no longer keeps that week from being clean.
- Deploy before 2026-11-02: week 3's digest is the first that would have fit
  the correction with week 1 included. No schema change or migration.
- Validated with 735 passing tests (1 skipped), Ruff checks, and 0.1.3 wheel and
  source distribution builds.

## 0.1.2 — 2026-10-24

- Honor Sleeper's configured `start_week` for scheduled ingest, reconciliation,
  completed-week coverage, and current-week tipoff checks. This supports leagues
  that draft after NBA week 1 and begin play in week 2.
- The league still needs `start_week=2` set in Sleeper after its draft settings
  are available; verify the returned setting before relying on live advice.
- Validated with 728 passing tests (1 skipped), Ruff checks, and 0.1.2 wheel and
  source distribution builds.

## 0.1.1 — 2026-10-17

- Pass `--dashboard-db` through the CLI to the web server, so the 2026 advice
  page can display 2025 manager scorecards on `/dashboard`.
- Validated with 724 passing tests (1 skipped), Ruff checks, a 0.1.1 wheel and
  source distribution, and both live HTTP routes after service restart.
- Ignore the raw snapshot archive in Git and add a checksum-verified, append-only
  rsync backup script for a separately prepared destination.

## 0.1.0 — 2026-09-26

First tagged release of the Sleeper NBA Lock-In assistant. Ready for supervised
live use; validation in a live season remains pending. Version 1.0 is reserved
for proven live operation.

### Included

- Daily lock/pass recommendations and inclusive half-point score thresholds.
- Schedule, scoring, availability and matchup ingestion with archived raw polls.
- Live banked-state inference, explicit overrides and saved recommendation provenance.
- Abstention on insufficient history, stale or incomplete inputs, calendar
  disagreement and unreadable lock state; actionable calls require known deadlines.
- Browser advice, optional push notifications and guarded daily jobs.
- A shadow report requiring two full consecutive clean weeks of daily inference,
  complete final polls and verifiable player readings before manual checks can stop.
- Historical replay, calibration, policy backtesting and manager analysis.
- A concise setup and season workflow in the README.

### Operating limits

- All locks are made manually in Sleeper. Start/sit recommendations are not included.
- Advice initially abstains until at least 400 player-games exist and other guards pass.
- Confirm the new league, season configuration, schedule and scoring at rollover.
- Cross-check inferred banked state daily until the live shadow gate passes.
- Recheck cold-start calibration around week 5. Historical and synthetic validation
  does not establish future competitive performance.

### Validation

- 723 tests passed, 1 skipped, including cron and HTTP tests with local socket access.
- Ruff lint, formatting and whitespace checks passed.
- Both final review reproductions were rerun and confirmed fixed.

The `v0.1.0` tag fixes this release's contents. Subsequent changes require a new
version; see [the release procedure](docs/releases.md).
