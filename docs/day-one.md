# Day one of the 2026-27 season

Everything the project deferred because there was no live league to run against.
Scattered across implementation-plan.md §7.3, §7.5, §15, §17 and §20; assembled here
because these are due on one morning and a checklist read on that morning is worth more
than four cross-references.

Ordered. Steps 1-3 must happen before the first ingest, and step 2 is destructive if
skipped.

---

## 0. Before the season opens

**Deploy to the Pi.** The only piece of Phase 6 not done, deferred by decision rather than
blocked (§8). The crontab entries are in the README; `uv sync --frozen` reproducibility was
Phase 0's exit criterion, so this is execution rather than design. Doing it in advance
matters because day one is the wrong time to discover that cron cannot find `uv`.

Verify the deployment by running a gate, not by running the digest:

```bash
uv run --frozen lockin verify     # exits nonzero on failure
```

---

## 1. Find the new league id

The commissioner has to roll the league over first. Until then this returns `[]`, which is
what it has returned every time it has been checked (most recently 2026-08-15).

```bash
uv run python -c "
import json, urllib.request
url='https://api.sleeper.app/v1/user/1283460931447164928/leagues/nba/2026'
with urllib.request.urlopen(url, timeout=20) as r: d=json.load(r)
for x in d: print(x['league_id'], x['name'], x['status'], 'prev=', x.get('previous_league_id'))
"
```

**Pass:** one league, with `previous_league_id` equal to `1283214955830575104`. That field
is the lineage check — it confirms this is the same league rolled forward and not a new one
somebody else made, which matters because keeper rosters carry over and a wrong id would
replay strangers.

> **The plan overstates what the code does here.** §7.3 says config "resolves the league by
> walking the user's leagues, following `previous_league_id` to confirm lineage". It does
> not — `Config.from_env` reads environment variables with 2025-26 defaults, and nothing
> walks anything. Setting them by hand is the whole mechanism. Worth knowing before you
> trust the default.

---

## 2. Use a new database — this one is destructive if skipped

**Set `LOCKIN_DB` to a new file.** Do not ingest 2026-27 into `data/lockin.db`.

```bash
export LOCKIN_DB=data/lockin-2026.db
export LOCKIN_LEAGUE_ID=<the id from step 1>
export LOCKIN_SEASON=2026
```

`box_scores`, `nba_schedule` and `league_settings` carry a season and would coexist safely.
**`weekly_matchups` and `weekly_matchup_teams` do not**, and they are the tables every
reader goes through — `weekly_matchups_latest` resolves ties on `MAX(observed_at)`, so a
second season silently *wins* and the first becomes invisible:

```
week 12, roster 4, player 2126, ingested twice
  2026-01-08  counted 54.0  starter        <- 2025-26
  2027-01-08  counted 11.0  bench          <- 2026-27
weekly_matchups_latest returns 1 row: the 2026-27 one
```

Nothing errors. The projection panel would then be built from 2025-26 box scores filtered
to players on 2026-27 rosters, and every derived table — `lock_inferences`,
`manager_scorecards`, `roster_strength`, `recommendations` — is season-blind and would mix
the two. The backtest would still run. It would just be meaningless.

A separate file per season is the intended shape anyway: the database is disposable and
rebuilt from the API, which is why the archive that is *not* disposable lives outside it.

**Point `lockin serve --dashboard-db` at the old file.** Scorecards are retrospective, so
the only ones that exist now describe 2025-26 and live in `data/lockin-2025.db`. Without it
`/dashboard` reads "No scorecards yet" until this season is over.

**Keep `LOCKIN_SNAPSHOTS` pointing at the same directory.** Snapshot paths are already
season-scoped (`snapshots/<kind>/<season>/wkNN/`), so the seasons cannot collide, and the
2025-26 archive is the only defence against §12's rewriting. Do not start a fresh one.

---

## 3. Confirm the week structure before trusting `--weeks`

`config.ALL_STAT_WEEKS` hardcodes `range(1, 26)` — 25 weeks, from 2025-26. The league
publishes the real numbers, and a different playoff format would move them:

```bash
uv run python -c "
import json, sqlite3, os
c=sqlite3.connect(os.environ['LOCKIN_DB']); c.row_factory=sqlite3.Row
s=json.loads(c.execute('SELECT payload_json FROM league_settings LIMIT 1').fetchone()['payload_json'])['settings']
print({k:s[k] for k in ('start_week','playoff_week_start','last_scored_leg','playoff_teams')})
"
```

2025-26 gave `start_week 1, playoff_week_start 22, last_scored_leg 24, playoff_teams 8`.
If those have moved, `ALL_STAT_WEEKS`, `REGULAR_SEASON_WEEKS` and `PLAYOFF_WEEKS` in
`lockin/config.py` need updating. `last_scored_week()` already reads the setting rather
than a constant, so `lockin managers` is safe either way; the ingest default is not.

Early in the season, ingest one week at a time — `--weeks 1` — rather than sweeping 25 that
do not exist yet.

---

## 4. First ingest, then the gates that still apply

```bash
uv run lockin ingest --weeks 1
uv run lockin reconcile
uv run lockin verify
```

This also writes the first `player_status` rows of the season (step 6). That happens on
every ingest and cannot be skipped — it used to sit behind a `--full` flag, which is exactly
how a season of it nearly went uncaptured.

**Pass:** both gates report all gates passed. `verify` is the one that matters — it
reproduces every nonzero counted score from box scores, so it catches a scoring-settings
change the moment it appears. **If the commissioner changed scoring, `verify` fails and
everything downstream is wrong until the settings are re-read.** That is the intended
behaviour, not a bug to work around.

`calibrate`, `backtest` and `locks` need most of a season and will not pass in week 1.
Do not run them as gates until there is enough history; the projection layer falls back to
the pooled donor cohort for players without their own games, which is the right behaviour
but not something to gate on.

---

## 5. §7.5 — confirm Sleeper publishes rows for games not yet played

**The one unverified assumption the digest actually depends on.** The evidence from
2025-26 is encouraging — rows exist for games players sat out, so the feed appears to track
the team schedule rather than participation — but that was only ever observable in
retrospect, and retrospectively every game has been played.

Run this on a day with games scheduled, **before tip**:

```bash
uv run python -c "
import os, sqlite3, datetime as dt
c=sqlite3.connect(os.environ['LOCKIN_DB']); c.row_factory=sqlite3.Row
today=dt.date.today().isoformat()
r=c.execute('SELECT COUNT(*) n, SUM(played) p FROM box_scores WHERE game_date=?',(today,)).fetchone()
print(f'{today}: {r[\"n\"]} rows, {r[\"p\"] or 0} marked played')
"
```

**Pass:** a nonzero row count with zero (or few) marked played. That is the forward-looking
feed working.

**Fail — zero rows:** tonight's slate must come from the NBA schedule instead. The fallback
is already ingested (`nba_schedule`, populated by `lockin ingest`), so the work is rerouting
`lockin/digest.py`'s `lineup_as_of` to build `Game` days from `nba_schedule` joined through
`game_links` rather than from the box-score panel. Cheap, but it has never been exercised —
budget an afternoon, not five minutes.

---

## 6. Confirm the two accumulating records are actually accumulating

Neither can be backfilled. Both are worthless if the cron is silently failing, and a cron
that silently fails looks exactly like a quiet season.

**Availability designations** (§17 — the prerequisite for ever ranking start/sit):

```bash
uv run python -c "
import os, sqlite3
c=sqlite3.connect(os.environ['LOCKIN_DB']); c.row_factory=sqlite3.Row
for r in c.execute('SELECT as_of, COUNT(*) n FROM player_status GROUP BY as_of ORDER BY as_of DESC LIMIT 7'):
    print(r['as_of'], r['n'])
"
```

**Pass:** one row per calendar day, each with a plausible count (110 designations on
2026-08-08). A missing day is a missing day forever. Every ingest now captures this
unconditionally, and prints the day count as it goes, so the check is that the number is
**one higher than yesterday** — not merely nonzero.

**Matchup poll history** (§10/§15 — what live opponent-lock inference needs):

```bash
uv run python -c "
import os, sqlite3
c=sqlite3.connect(os.environ['LOCKIN_DB']); c.row_factory=sqlite3.Row
for r in c.execute('SELECT week, COUNT(DISTINCT observed_at) polls FROM weekly_matchups GROUP BY week ORDER BY week DESC LIMIT 5'):
    print('week', r['week'], r['polls'], 'observations')
"
```

**Pass:** the current week's poll count climbing daily. One observation per week means the
append-only design is doing nothing, and the opponent model stays on the base-policy
stand-in indefinitely.

> A daily poll is the minimum and is what the README's cron does. §10's inference resolves a
> lock one game *later* than it happened; polling more often than daily narrows that lag but
> does not remove it. Decide whether that is worth more cron entries after seeing a week of
> real data — not before.

---

## 7. First digest

```bash
uv run lockin digest --locked ""
```

The explicit empty `--locked` asserts that nothing is banked yet, which early in week 1 is
both true and useful — it skips the reconstruction, which is the noisiest thing the digest
does (§20).

**Pass:** it names a real opponent, gives a P(win) near 50%, and prints standing rules for
tonight. Early-season thresholds will be wide and the projections will lean on the pooled
donor cohort; that is expected and self-correcting as own-history accumulates.

Once a week of real state exists, pass `--locked` with what you actually banked. Live, that
is simply known, and supplying it is strictly better than having it inferred.

Then render the page, which is how a missed notification stays readable:

```bash
uv run lockin advice
```

**Pass:** a green banner saying the advice is for this morning. A red one means the digest
did not run today — check `logs/digest.log` before trusting anything on the page.

If `lockin-serve` is running on the Pi, the same page is at `http://<pi>:8080/` and is
rendered fresh on each request, so it cannot lag behind the digest.

---

## 8. What stays switched off

**Start/sit advice.** Held until it has its own gate — roughly week 10, when ~100
roster-weeks of lineup decisions with genuine point-in-time availability exist to build one
against (§19). Shipping it in week 1 on the strength of a backtest that could not have
tested it is the one way this project would ship something unvalidated.

The digest emits no `START`/`SIT` row today, so this requires no action — only the
discipline not to add one because the availability feed has finally started working. It
working is the *precondition* for building the gate, not a substitute for it.
