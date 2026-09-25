# Day one of the 2026-27 season

Everything the project deferred because there was no live league to run against.
Scattered across implementation-plan.md §7.3, §7.5, §15, §17 and §20; assembled here
because these are due on one morning and a checklist read on that morning is worth more
than four cross-references.

Ordered. Steps 1-3 must happen before the first ingest, and step 2 is destructive if
skipped.

---

## 0. Before the season opens

**Deploy to the Pi.** ✅ Done 2026-09-20 — see deployment.md, which is the runbook that
came out of doing it. Every cron line runs under `scripts/cron-guard`, so a failure pushes
a notification instead of dying into a log nobody reads, and `logs/` rotates rather than
growing forever.

It was worth doing in advance for exactly the reason given here — day one is the wrong
time to discover that cron cannot find `uv` — and deploying early is also what surfaced the
LeagueGameFinder problem in step 5 below, a month before it would have bitten.

Verify the deployment by running a gate, not by running the digest:

```bash
uv run --frozen lockin verify     # exits nonzero on failure
```

**Back up `data/lockin-2025.db` before the Pi first runs the code from the 2026-09-23
review.** ✅ Done 2026-09-23: `data/lockin-2025.pre-review.db`, two minutes before the
first command to open the file migrated it (implementation-plan.md §21, "Deployed
2026-09-24"). Keep the copy. The same backup, under a new name, comes before deploying
any later change that migrates data.

The migration adopted the file's league identity, completed the partial `lockin repair`
polls so the new per-poll lineup view reads them whole, and classified fixture states. All
three are additive and were checked on a copy: `verify`, `reconcile` and `locks` were
identical before and after, and `weekly_matchups_latest` matched row for row. But the file
is the only copy of the season's derived tables, and a cron job applies a migration
unattended.

```bash
uv run python -c "
import sqlite3
src=sqlite3.connect('file:data/lockin-2025.db?mode=ro', uri=True)
dst=sqlite3.connect('data/lockin-2025.pre-review.db')
src.backup(dst); dst.close(); print('backed up')
"
```

(The Pi has no `sqlite3` command-line tool; this is SQLite's own backup API, which also
copies whatever is still in the WAL.)

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

`lockin ingest` is the one command that creates a database, which is what makes this step
work on a path that does not exist yet. Every other command refuses a missing file, so if
you set `LOCKIN_DB` and reach for `lockin digest` first, it will tell you to ingest.

```bash
export LOCKIN_DB=data/lockin-2026.db
export LOCKIN_LEAGUE_ID=<the id from step 1>
export LOCKIN_SEASON=2026
```

These outrank `.env`, so they hold for this shell even though deployment.md §3 left
`LOCKIN_DB=data/lockin-2025.db` in the file. Once you are satisfied, edit `.env` to match —
otherwise the cron and the systemd unit, which have only `.env`, keep last season's path.

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

> **Since 2026-09-23 this is enforced, not just advised.** A database records the league
> and season it was first ingested for (`db_identity`), and every command compares the
> configuration against it. Skip this step and `lockin ingest` refuses before writing
> anything — `this database (...) belongs to league 1283214955830575104 season 2025` — and
> names the `export` to run. The 2025-26 file adopts its identity automatically the first
> time any command opens it. `lockin observe` makes the same check against the league
> payload, so the archive cannot file 2026-27 payloads under 2025 either.

**Point `lockin serve --dashboard-db` at the old file.** Scorecards are retrospective, so
the only ones that exist now describe 2025-26 and live in `data/lockin-2025.db`. Without it
`/dashboard` reads "No scorecards yet" until this season is over.

**Keep `LOCKIN_SNAPSHOTS` pointing at the same directory.** Snapshot paths are already
season-scoped (`snapshots/<kind>/<season>/wkNN/`), so the seasons cannot collide, and the
2025-26 archive is the only defence against §12's rewriting. Do not start a fresh one.

---

## 3. Confirm the week structure before trusting `--weeks`

`config.ALL_STAT_WEEKS` hardcodes `range(1, 26)` — 25 weeks, from 2025-26. The league
publishes the real numbers, and a different playoff format would move them. Read them from
Sleeper — the new database does not exist until step 4, so there is nothing local to read
yet:

```bash
uv run python -c "
import json, os, urllib.request
url=f'https://api.sleeper.app/v1/league/{os.environ[\"LOCKIN_LEAGUE_ID\"]}'
with urllib.request.urlopen(url, timeout=20) as r: s=json.load(r)['settings']
print({k:s.get(k) for k in ('start_week','playoff_week_start','last_scored_leg','playoff_teams')})
"
```

2025-26 gave `start_week 1, playoff_week_start 22, last_scored_leg 24, playoff_teams 8`.
If those have moved, `ALL_STAT_WEEKS`, `REGULAR_SEASON_WEEKS` and `PLAYOFF_WEEKS` in
`lockin/config.py` need updating. `last_scored_week()` already reads the setting rather
than a constant, so `lockin managers` is safe either way; the ingest default is not.

Every `lockin ingest` also prints the same numbers on its `structure` line, and a
`WARNING` beneath it when they disagree with `lockin/config.py` — so a format change made
mid-season is caught by the cron log too, not only by this step.

Early in the season, ingest one week at a time — `--weeks 1` — rather than sweeping 25 that
do not exist yet.

---

## 4. First ingest, then the gates that still apply

```bash
uv run lockin ingest --weeks 1
uv run lockin reconcile
uv run lockin verify
```

The ingest reports the schedule as it goes, and the second number is the one to read:

```
  schedule    1200 NBA games, 1200 not yet played
```

**A season in progress that reports `0 not yet played` means the schedule source has
reverted to a results feed** — the failure this line exists to make visible, and the one
that went unnoticed for the project's whole life because it had only ever run against a
finished season. A trailing `N without teams yet` is normal: the NBA Cup bracket is on the
calendar before its teams are known, and those fixtures land on a later run.

This also writes the first `player_status` rows of the season (step 6). That happens on
every ingest and cannot be skipped — it used to sit behind a `--full` flag, which is exactly
how a season of it nearly went uncaptured.

**Pass:** both gates report all gates passed. `verify` is the one that matters — it
reproduces every nonzero counted score from box scores, so it catches a scoring-settings
change the moment it appears. **If the commissioner changed scoring, `verify` fails and
everything downstream is wrong until the settings are re-read.** That is the intended
behaviour, not a bug to work around.

`reconcile` can pass on day one now, and for the right reasons. It used to demand all 25
weeks and read "nothing played yet" as a 0% link rate, so the gate this step prescribes
could not pass on the morning it was written for (review finding 11). While the league is
in progress it checks the weeks that should exist by now, links only games that were
played ("no fixtures played yet" is a pass), fails on any fixture whose evidence disagrees
(`unknown`: due or NBA-final with no stat lines), and requires tipoff times for the current
week — the deadline every call is printed against. `tests/test_day_one.py` runs this
step against a fresh synthetic season.

`calibrate`, `backtest` and `locks` need most of a season and will not pass in week 1.
Do not run them as gates until there is enough history.

**Around week 5, run `uv run lockin calibrate --cold-start`.** The digest abstains until 400
player-games have been played league-wide, a threshold chosen on 2025-26's first month
(implementation-plan.md §21). That is a consistency check on the season it was chosen from;
2026-27's first month is the out-of-sample test of it. If the gate fails, the threshold is
too low and the first week's advice was not calibrated — raise `min_pool_rows`.

---

## 5. §7.5 — a cross-check now, not a dependency

This used to be **the one unverified assumption the digest depended on**: that Sleeper
publishes stat rows for games not yet played. It no longer matters. Since 2026-09-23 the
digest reads every game still to come from the NBA schedule (`lockin/slate.py`), joined to
each player's team and to a Monday-to-Sunday week calendar that held for all 25 weeks of
2025-26. Sleeper's forward rows, if they exist, are compared against it, and a
disagreement is printed rather than silently preferred.

It is still worth knowing which way it went. On a day with games scheduled, **before tip**:

```bash
uv run python -c "
from lockin import clock
from lockin.config import Config, load_env_file
from lockin.store.db import connect_readonly
load_env_file()
cfg=Config.from_env()
c=connect_readonly(cfg.db_path)
today=clock.today_iso(cfg.timezone)
r=c.execute('SELECT COUNT(*) n, SUM(played) p FROM box_scores WHERE game_date=?',(today,)).fetchone()
s=c.execute('SELECT state, COUNT(*) FROM game_links WHERE game_date>=? GROUP BY state',(today,)).fetchall()
print(f'{today}: {r[\"n\"]} Sleeper rows ahead, {r[\"p\"] or 0} played; fixture states {[tuple(x) for x in s]}')
"
```

Either answer is fine. What must **not** appear is `postponed` against games that are
simply in the future — that was review finding 1, and it removed every upcoming fixture
from the digest. And the schedule must reach April:

```bash
uv run python -c "
from lockin.config import Config, load_env_file
from lockin.store.db import connect_readonly
load_env_file(); c=connect_readonly(Config.from_env().db_path)
r=c.execute('SELECT COUNT(*) n, MIN(game_date) a, MAX(game_date) b FROM nba_schedule').fetchone()
print(f'{r[\"n\"]} fixtures, {r[\"a\"]} .. {r[\"b\"]}')
"
```

**Pass:** a range ending in April of next year, not yesterday.

---

## 6. Confirm the two accumulating records are actually accumulating

Neither can be backfilled. Both are worthless if the cron is silently failing, and a cron
that silently fails looks exactly like a quiet season.

**Availability designations** (§17 — the prerequisite for ever ranking start/sit):

```bash
uv run python -c "
from lockin.config import Config, load_env_file
from lockin.store.db import connect_readonly
load_env_file()
c=connect_readonly(Config.from_env().db_path)
for r in c.execute('SELECT substr(observed_at,1,10) day, COUNT(*) n, MAX(flagged) f FROM status_captures GROUP BY day ORDER BY day DESC LIMIT 7'):
    print(r['day'], r['n'], 'capture(s),', r['f'], 'flagged')
"
```

**Pass:** one row per calendar day, each with a plausible flagged count (110 designations on
2026-08-08). A missing day is a missing day forever. Every ingest now captures this
unconditionally, and prints the day count as it goes, so the check is that the number is
**one higher than yesterday** — not merely nonzero.

> Since 2026-09-23 the capture is timestamped and records changes, including a designation
> being *cleared* (`player_status_events`), and every read is logged in `status_captures`
> whether or not anyone was flagged. The old date-keyed `player_status` table kept a
> morning's Out all day, let an afternoon update overwrite what was known before tip, and
> could not tell a healthy day from a missed one (review finding 8). It is kept for the
> days it already holds and no longer written.

**Matchup poll history** (§10/§15 — what live opponent-lock inference needs):

```bash
uv run python -c "
from lockin.config import Config, load_env_file
from lockin.store.db import connect_readonly
load_env_file()
c=connect_readonly(Config.from_env().db_path)
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

## 7. First digest — and the first week, which it will decline to advise on

```bash
uv run lockin digest
```

**On opening morning the pass is an abstention.** With no games played it says
`insufficient history`, and it keeps saying so — with a count — until 400 player-games
have been played league-wide, which in 2025-26 was the Monday of week 2. It used to report
both projected totals as 0.0, P(win) 50% and every threshold 0.0, which is what this step's
old "P(win) near 50%" pass criterion was unknowingly accepting (review finding 3).

From then on it advises, reading what you have banked from the morning's matchup poll
(`lockin/state.py`). It prints where the state came from: `read from the <time> poll`.
`--locked` still overrides it whenever you give it.

**Run in shadow until `lockin shadow` passes its gate**, before trusting the cron without
`--locked`. The rule the poll reading applies — a locked player's counted score freezes, so
an earlier lock shows once he has played again — is the architecture doc's §10 reading, and
it has never been observed live.

Until the gate passes, check it by eye each morning, because the report can only judge a
week once Sleeper has scored it. Compare the `BANKED` list with the locks you actually made.
They should agree on every player whose next game has been played since he was locked. Last
night's locks are not in it, by design: last night's games are the calls. A disagreement
means the reading is wrong. Pass `--locked` and fix `lockin/state.py` before relying on it.

Each Monday, once the week is scored:

```bash
uv run lockin shadow
```

For every finalized week it reports:
- whether each call was followed, overridden, or moot because he had already banked;
- every morning whose `BANKED` list disagreed with the locks the final scores reveal;
- P(win) against results;
- calls that changed between runs.

**Gate:** two consecutive weeks marked `clean`. That means a live run every morning, no
`BANKED` disagreement, and no call that changed on the same inputs (a digest is seeded, so
that would be a bug). Once it passes, `--locked` is only an override and the daily check
stops. Calibration is printed but not gated. Look at it again around week 6, when about
thirty mornings have accumulated.

A live run also declines when last night's games are not final yet, when the last
*complete* ingest finished before they did — a cron that died half-way no longer vouches
for the data (review findings 7 and 9) — or when the last ingest ran with `--skip-nba`.
Each says so in the notification.

Then render the page, which is how a missed notification stays readable:

```bash
uv run lockin advice
```

**Pass:** a green banner saying the advice is for this morning, and each call showing the
tip it must be acted on before. A red banner means the digest did not run today — check
`logs/digest.log` before trusting anything on the page. A greyed call is one whose tip has
passed. The footer's `Inputs:` line gives the age of the box scores, the lineup poll, the NBA
schedule and the designations. All four should be from this morning's ingest.

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
