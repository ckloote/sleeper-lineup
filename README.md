# sleeper-lineup

A daily lock/pass assistant for Sleeper NBA Lock-In leagues. It recommends whether
to bank a completed game's score and gives score thresholds for future nights.
You choose your starters and make every lock yourself in Sleeper, then record it
on the advice page. This tool does not change your team or provide start/sit advice.

**Release: 0.3.0.** Ready for supervised live use; 1.0 is reserved for proven live
operation. See [release notes](CHANGELOG.md) and the [version policy](docs/releases.md).

## Start here

Requires `uv`, Python 3.12, and network access to Sleeper and the NBA feeds.

```bash
git clone git@github.com:ckloote/sleeper-lineup.git
cd sleeper-lineup
uv python install 3.12
uv sync --frozen
```

Once the commissioner rolls the league over, confirm the new league's
`previous_league_id` points to last season's league. Check its scoring and fantasy
week structure using the [opening-day checklist](docs/day-one.md).

Create `.env` in the project directory, replacing the placeholders:

```dotenv
LOCKIN_LEAGUE_ID=YOUR_NEW_LEAGUE_ID
LOCKIN_USER_ID=YOUR_SLEEPER_USER_ID
LOCKIN_SEASON=2026
LOCKIN_DB=data/lockin-2026.db
LOCKIN_SNAPSHOTS=snapshots
LOCKIN_TZ=America/New_York
```

Sleeper labels 2026-27 as `2026`. **Use a separate database for every season** and
keep the existing snapshots directory. Update `.env`, not just your shell: cron
and the web service need the same configuration. Exported variables override
`.env`. Run commands from the project directory.

First ingest, then check the data:

```bash
uv run --frozen lockin ingest --weeks current
uv run --frozen lockin reconcile
uv run --frozen lockin verify
uv run --frozen lockin digest
```

Both checks should pass. Confirm the schedule contains upcoming games and current
week tipoffs. An opening-day digest that says `insufficient history` is expected.
Do not skip the NBA or tipoff ingest steps for live use.

To view saved advice in a browser:

```bash
uv run --frozen lockin serve --dashboard-db data/lockin-2025.db
```

Open `http://<host>:8080/`. The optional `--dashboard-db` keeps last season's
manager dashboard available at `/dashboard`; omit it if you have no previous
season database. `serve` displays saved runs and does not generate new advice.
Its one write is your answers to the calls (**Locked** / **Passed**, see
[Recording your locks](#recording-your-locks)). A standalone HTML file from
`uv run --frozen lockin advice` shows the calls but cannot record answers.

For cron, notifications, a persistent web service and log rotation, follow the
[deployment guide](docs/deployment.md). Schedule ingest before the morning digest,
using **`--weeks current`**, never the ISO calendar week. The stats request must
start after 07:00 UTC and the previous night's games must be final. The runbook
uses 06:30 ingest and 09:00 digest in the host's Eastern timezone.

## Workflow through the season

| Stage | What to do | What changes |
|---|---|---|
| Opening days | Ingest and save a digest every morning. Confirm injury captures and matchup polls accumulate daily. | The digest abstains until at least 400 player-games exist league-wide and its other data checks pass. |
| Advice begins | Read the calls, and answer each LOCK call on the served page (**Locked** or **Passed**) once you have decided. | Advice becomes available. Banked state comes from your answers. |
| Each finalized week | Run `uv run --frozen lockin shadow`, normally on Monday after ingest. | The report checks each morning's calls and banked state against your recorded answers and the box scores. |
| Two full consecutive clean weeks | Confirm the shadow report explicitly says its gate passes. | You can stop the daily manual `BANKED` cross-check. Keep the daily jobs, your answers and the weekly shadow review. |
| Around week 5 | Run `uv run --frozen lockin calibrate --cold-start`. | Checks whether the initial 400-player-game threshold was adequate for this season. A failure needs investigation and adjustment before trusting early-season calibration. |
| Around week 10 or later | Review whether enough availability history exists to develop and validate start/sit advice. | Nothing switches on automatically. Start/sit needs its own implementation and validation gate. |
| Season end | Preserve the database and snapshots; run retrospective analysis if wanted. | Keep this season separate when configuring the next one. |

### Every morning

The scheduled jobs should perform this sequence; these are also the manual commands:

```bash
uv run --frozen lockin ingest --weeks current
uv run --frozen lockin digest
uv run --frozen lockin advice
```

Add `--notify` to `digest` once notifications are configured. The web service reads
the latest saved digest directly, so static `advice` generation is optional when
using it. Keep digest persistence enabled: the shadow report needs the saved runs.

Read the advice date, input freshness and warnings before acting. Make any chosen
locks in Sleeper **before the displayed next tipoff**, and record each answer on
the page. A threshold of `11.5 or more`
includes 11.5. Future rules assume you make no intervening locks; rerun the digest
when you need advice reflecting updated inputs or state.

If the digest abstains, follow its explanation and check `logs/ingest.log` and
`logs/digest.log`. Old advice, expired calls and historical replays are not current
instructions. Missing or ambiguous data should be resolved before relying on a call.

### Recording your locks

Sleeper's matchup poll cannot show a lock, so the digest banks what you record:
tap **Locked** or **Passed** beside each call on the served advice page
(`lockin-serve`), and **Undo** to correct one. A LOCK call you have not answered
counts as not locked, and its line says so. A PASS call needs no answer: doing
nothing is passing.

- A call that left the page unanswered is listed under **Not answered from earlier
  this week**, so a lock you forgot to record can still be recorded.
- Only an answer recorded after a digest ran answers that digest's call. If you
  passed on Tuesday and Wednesday says LOCK on the same game, Wednesday's call is
  open again, with Tuesday's answer shown beside it.
- Answers are taken only for the current week's live calls. A historical replay
  shows the answers as they stood that morning and takes none.

`--locked` still overrides the ledger, and `LOCKIN_LEDGER=0` goes back to reading
the poll.

### The shadow gate

The shadow gate requires the latest two finalized tracking weeks to be full,
consecutive and clean. In a ledger week, a LOCK call with no answer, or a morning
whose banked list disagrees with your answers and the box scores, keeps it closed
(listed under `UNANSWERED` and `LEDGER STATE`). In a week read from the poll,
missing runs, failed inference, incomplete final polls, unverifiable readings or
discrepancies keep it closed. Monday needs a run but can abstain before the
league rolls over. See the
[exact gate rules](docs/day-one.md#7-first-digest--and-the-first-week-which-it-will-decline-to-advise-on).

### Before the ledger: reading the poll

Compare `BANKED` with the locks you actually made that should now be visible:
locks become inferable after the player's next game, so last night's newly locked
scores need not appear yet. If the reading disagrees, use an explicit override
and investigate before relying on automatic inference:

```bash
uv run --frozen lockin digest --locked '2126:42.5,1970:31'
```

Supply the complete banked state using Sleeper player IDs; `--locked ''` explicitly
means nothing is banked. Overrides do not bypass freshness or lineup checks, and
supplied-state runs do not qualify as successful automatic inference for the
shadow gate.

### Bench-aware calls (experimental, off by default)

By default a call compares banking tonight's score with riding the same player
to the end of the week. With `LOCKIN_BENCH_AWARE=1` in `.env`, or
`lockin digest --bench-aware` for one run, passing also counts benching him and
starting a better bench player who plays later. A call that depends on this
says `pass: start <player>`, then `and bench <player>` for whoever the lineup
sends to the bench, and `lock lineup sits <player>` for the difference between
the two lineups the model chose. That isn't always the player being decided, so check
`lockin explain <player> --bench-aware` for the slot moves. `WATCH … assumed to start` means the
projection and P(win) assume a lineup change only you can make. Bench players
designated Out, IR or Suspended are left out.

Check it before turning it on:

```bash
uv run --frozen lockin backtest --bench-aware
```

Enable it only if `rollout+bench` wins no fewer matchups than rollout and zeroes
no more slots.
On 2025-26 it does not: it loses about 12 points a week and more matchups,
because projections overrate bench players by about a fifth. Leave it off.

Bench candidates are now only rotation regulars: players you start on most
nights they have a game (`lockin/rotation.py`). Last season's polls are too
sparse to test that, so each digest records what the bench-aware engine would
have said, without sending it (`bench_shadow_runs`, `bench_shadow`, and a
`bench shadow:` line in `logs/digest.log`). `LOCKIN_BENCH_SHADOW=0` turns that
off.

### After the season

Keep backups of the database and raw `snapshots/`: Sleeper can rewrite completed
results, and saved digest runs preserve what was actually advised. Snapshots are
ignored by Git and need a verified copy on another device or host. See the
[deployment guide](docs/deployment.md) for backup, observation, and repair procedures.

With sufficient season history, `lockin calibrate`, `lockin backtest`, and
`lockin managers --sims 2000` provide retrospective checks and analysis. These are
not opening-week readiness checks.

## Further reference

- [Opening-day checklist](docs/day-one.md): rollover, first ingest and live validation.
- [Deployment guide](docs/deployment.md): daily jobs, notifications, service and backups.
- [Implementation record](docs/implementation-plan.md): model evidence and limitations.
- [Architecture](docs/sleeper-lockin-engine-architecture.md): design details.

For development checks:

```bash
uv run --frozen pytest
uv run --frozen ruff check lockin tests
uv run --frozen ruff format --check lockin tests
```
