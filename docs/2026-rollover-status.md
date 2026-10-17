# 2026–27 rollover status — 2026-10-17

The host now uses league `1411432817891495936`, season `2026`, and
`data/lockin-2026.db`. The web service reads that database for advice and
`data/lockin-2025.db` for its retrospective dashboard. The cron schedule is
unchanged, including daily `observe`.

## Preserved history

- SQLite backup: `data/lockin-2025.pre-rollover-2026-10-17.db` (`integrity_check: ok`).
- Shared snapshot archive retained in place; its 430 files were also saved as
  `data/snapshots.pre-rollover-2026-10-17.tar.gz` before rollover. This includes
  untracked snapshots. Do not remove either the old database or these backups.
- After the first 2026 snapshot, all 431 files were saved again in
  `data/snapshots.pre-gitignore-2026-10-17.tar.gz`. This local copy is interim;
  schedule `scripts/backup-snapshots` to another device or host once a
  destination is chosen.
- Exact rows copied into the new schema before ingest:

| Table | Rows | First | Last |
| --- | ---: | --- | --- |
| `player_status` | 2,154 | 2026-08-08 | 2026-09-23 |
| `status_captures` | 24 | 2026-09-24T10:30:02Z | 2026-10-17T10:30:03Z |
| `player_status_events` | 1,178 | 2026-09-24T10:30:02Z | 2026-10-17T10:30:03Z |

Source and destination rows, capture IDs, date ranges, and latest designation
states matched exactly. The latest transferred capture had 149 flagged players.
Foreign-key checks passed and the destination's season-specific tables were empty
before ingest. The legacy date-only `player_status` rows remain less precise than
`player_status_events`.

## First ingest and live pages

The 2026 league points to prior league `1283214955830575104`. Scoring, six
starting slots, start week 1, playoff start week 22, and eight playoff teams
match 2025. `last_scored_leg` is currently absent while the league is
`pre_draft`; recheck it after the draft.

The first `ingest --weeks current` completed for week 1. `db_identity` names the
2026 league and season. The first new injury capture had 2,176 players seen and
146 flagged. The NBA schedule has 1,200 future games from 2026-10-20 through
2027-04-11, all with tipoff times; 43 of 43 week 1 games have tipoffs. Seven
schedule entries still lack teams. Sleeper returned an empty week 1 matchup
payload, so there are no matchup rows, box scores, or fixtures yet. The empty
payload is archived under `snapshots/matchups/2026/wk01/`. A manual `observe
--weeks 1` read the same payload and reported it unchanged.

`reconcile` failed only its week 1 matchup-coverage gate (`0/1`); its other
checks passed. `verify` passed with `0/0` scored games, so it has not yet
validated live scoring. A manual digest recorded an expected `insufficient
history` abstention. After the web-service fix in 0.1.1, HTTP `/` displays that
2026 abstention and `/dashboard` renders 2025 scorecards.

## Open launch checks

1. After Sleeper publishes matchups, recheck scoring, lineup slots, week settings,
   and `last_scored_leg`. Ingest again. Require complete week 1 lineups,
   upcoming fixtures, tipoffs, no unresolved ingest warnings, and passing
   `lockin reconcile` and `lockin verify` before relying on live advice.
2. After the next scheduled run, check `logs/.cron-guard/{observe,ingest,digest,advice}.ok`
   and the matching logs. The 2026-10-17 success stamps were written before
   the `.env` switch. Confirm the 2026 `observe` run is reflected under
   `snapshots/matchups/2026/`. The snapshot backup cron job still needs a
   destination and has not been installed.
3. Keep live advice under manual review. Once it makes calls, compare `BANKED`
   with actual locks each morning. Run `lockin shadow` after finalized weeks
   until two consecutive full clean tracking weeks pass its gate.
4. Around week 5, run `lockin calibrate --cold-start`. Keep start/sit advice off
   until its own history and validation gate exists.

The installed cron times in the host's Eastern timezone are `observe` 05:15,
`ingest` 06:30, `digest` 09:00, and `advice` 09:05 daily.
