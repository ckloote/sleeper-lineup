# Changelog

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
