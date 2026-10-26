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
starting slots, playoff start week 22, and eight playoff teams match 2025.
On 2026-10-24, Sleeper still reports `pre_draft`, `start_week=1`, and `leg=1`.
The league plans to draft on 2026-10-25 and begin scoring in week 2. Set
`start_week=2` in Sleeper if available; the 0.1.2 week gates honor that setting.
(It was not available; week 1 was zeroed instead. See the 2026-10-26 section.)
`last_scored_leg` is absent while the league is `pre_draft`; recheck it after
the draft.

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

## Draft, week 1 zeroed, and week 2 — 2026-10-26

Sleeper would not start the league after week 1. It drafted on 2026-10-25, the
last day of week 1, and kept `start_week=1`. Week 1 was played anyway, with
lineups filled after most of its games. The commissioner then zeroed it: every
team's `custom_points` is 0.0, and the player scores are untouched. On 2026-10-26
Sleeper reports `in_season`, `start_week=1`, `leg=2` and `last_scored_leg=1`.
Week 2, starting Monday 10-26, is the first that counts.

The 10-25 and 10-26 `observe` and `ingest` runs failed: `api.sleeper.app` did
not resolve, and neither did ntfy, so no alert arrived. Name resolution was
back by 23:00Z on 10-26, and a manual `cron-guard ingest` at 23:03Z recovered.
It recorded 10 drafted rosters (124 players), week 1 final with 1,055 played
player-games, week 2's matchups, and 49 of 49 week 2 tipoffs. Its alert reported
the recovery.

- `reconcile` passed every gate, including week 1 and week 2 matchup coverage.
- `verify` passed: 110 of 110 nonzero week 1 counted scores were reproduced from
  box scores, so week 1 validates live scoring despite the zeroing. Nothing in
  lockin reads `custom_points` as a score.
- A dry-run live digest abstained on the cold start: 317 rostered player-games
  against 400. Monday night has 73 rostered players scheduled and Tuesday 34,
  so Tuesday's digest abstains too and the first advice is expected Wednesday
  10-28. Roster 4's six starters all play Monday and none plays Tuesday, so
  Wednesday's calls still reach every one of Monday's games before their next
  tips.

Two consequences needed code (0.1.3). Week 1's lineups were filled after its
games: its starters sat 1.2% of their 169 games against 20.2% for the bench, and
8.5% for 2025-26's real starters. Fed into the started-player correction from
week 3 on, they would have made every starter look safer than he is, so a week
whose teams' `custom_points` are all zero is now left out of it. And Tuesday's
cold-start abstention would have counted as a failed inference in the shadow
gate, so week 2 could not be clean. A morning before any starter has played
twice now needs only a run, as Monday does.

## Open launch checks

1. ~~After Sleeper publishes matchups, recheck scoring, lineup slots, week settings,
   and `last_scored_leg`. Ingest again. Require complete week 1 lineups,
   upcoming fixtures, tipoffs, no unresolved ingest warnings, and passing
   `lockin reconcile` and `lockin verify` before relying on live advice.~~
   Done 2026-10-26; see above.
2. After the next scheduled run, check `logs/.cron-guard/{observe,ingest,digest,advice}.ok`
   and the matching logs. The 10-25 and 10-26 `observe` runs failed on the outage
   and have not run since: check that 10-27's lands under
   `snapshots/matchups/2026/`. The snapshot backup cron job still needs a
   destination and has not been installed.
3. Deploy 0.1.3 before Monday 11-02. Week 3's digest is the first whose
   started-player correction has enough starter games to use week 1.
4. Keep live advice under manual review. Once it makes calls, compare `BANKED`
   with actual locks each morning. Run `lockin shadow` after finalized weeks
   until two consecutive full clean tracking weeks pass its gate. Week 1 will
   show as not clean, since every run in it abstained; the gate reads only the
   latest two weeks, so the earliest it can pass is after week 3.
5. Around week 5, run `lockin calibrate --cold-start`. Keep start/sit advice off
   until its own history and validation gate exists.

The installed cron times in the host's Eastern timezone are `observe` 05:15,
`ingest` 06:30, `digest` 09:00, and `advice` 09:05 daily.
