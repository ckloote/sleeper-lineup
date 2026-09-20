# Deploying to the Pi

The last piece of Phase 6, deferred by decision rather than blocked (§8). Everything here
is executable today against the completed 2025-26 season — do it *before* October, so day
one is [day-one.md](day-one.md) and not this.

**Treat the first deployment as a test, not a formality.** Every bug found in Phase 6 —
the notification's latin-1 crash, the availability capture that never ran, the `pit_team`
false positive — was found by *running* the thing rather than reading it. The Pi is the
last unrun path.

Target: **Raspberry Pi, US Eastern.** Both matter and both are checked below.

---

## 1. Get the code and the environment there

```bash
git clone <repo> /home/pi/lockin
cd /home/pi/lockin
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --frozen
```

`--frozen` is not optional anywhere in this document. It pins the lockfile, so the Pi can
never resolve a dependency set different from the one the tests passed against.

**Verify before going further:**

```bash
uv run --frozen python -c "import numpy, scipy, pandas; print('ok')"
```

The scientific stack is the part that can fail on ARM. If it does, fix it now rather than
discovering it at 9am in November.

---

## 2. Set the timezone — twice, and for different reasons

```bash
sudo timedatectl set-timezone America/New_York   # the host
```

```bash
# /home/pi/lockin/.env
LOCKIN_TZ=America/New_York
```

`.env` is read by `lockin.config.load_env_file`, which every command runs before
dispatching. That is the point of it being in code rather than in your shell profile:
**cron and systemd do not source a profile**, so an `export` in `.bashrc` would fix the
terminal and leave both scheduled paths on the defaults. It is `KEY=value` per line, `#`
comments allowed, nothing expanded — `$(cat ...)` is literal text here and belongs in the
crontab, where a shell runs it. A line that is not `KEY=value` stops the command with the
line number rather than falling back silently.

**Anything already in the environment wins**, which is what keeps day-one.md's `export
LOCKIN_DB=data/lockin-2026.db` authoritative next season.

It is read from the *working directory*, like `data/lockin-2025.db` itself. That is why
every cron entry starts with `cd /home/pi/lockin` and the systemd unit sets
`WorkingDirectory` — those lines are load-bearing, not decoration. `.env` is gitignored: it
names this host's database, and may carry `LOCKIN_NTFY_TOPIC`, where the topic name is the
whole of the secret.

These are not redundant. The host timezone affects logs and cron scheduling; `LOCKIN_TZ`
tells the *engine* what timezone NBA game dates are filed under, and it defaults to
`America/New_York` precisely so a misconfigured host clock cannot shift a digest by a day.

**Why this is load-bearing.** 945 of the 2025-26 season's 1231 games — 77% — tip on a
different UTC date than the one they are filed under; they start 23:00-04:00 UTC and the
last finish around 06:30 UTC. The digest treats every game dated `today - 1` as complete,
so it must not run before roughly 07:00 UTC.

At 9am US Eastern that is 13:00 or 14:00 UTC depending on daylight saving — seven hours of
headroom, which is why Eastern needs no special handling. **A Pi east of UTC+2 would**, and
`lockin.clock.too_early_for` is the rule stated in code.

---

## 3. Bring last season's database across — do not re-ingest it

`snapshots/` is committed, so it arrives with the clone. **`data/` is gitignored**, so a
fresh clone has no database at all.

```bash
# from the dev machine
rsync -avP data/lockin.db pi@raspberrypi:/home/pi/lockin/data/lockin-2025.db
```

**Copy it; do not rebuild it on the Pi.** Sleeper rewrites completed seasons (§12) — 38% of
week-12 starter values changed under us between two days in August. Re-ingesting 2025-26 on
the Pi would fetch *today's* version, producing a database that disagrees with this one and
with the committed snapshots. Copying preserves the record that was actually observed;
`lockin reconcile` then confirms it still matches the archive.

Name it for its season from the start. Two seasons must never share a database:
`weekly_matchups` carries no season column, so 2026-27 rows would silently hide 2025-26
ones — see [day-one.md](day-one.md) step 2, which is where the second database appears.

```bash
# /home/pi/lockin/.env  — read by every command; see step 2
LOCKIN_DB=data/lockin-2025.db
```

**A wrong path here fails immediately, and says so.** Only `lockin ingest` creates a
database; every other command refuses:

```
Error: no database at data/lockin-2025.bd

  wrong path   check LOCKIN_DB in .env, and that you are in the project directory
  new host     copy last season across      (deployment.md step 3)
  new season   `lockin ingest` creates it   (day-one.md step 2)
```

This is deliberate, and it is the deployment's own scar tissue. `store.db.connect` used to
create whatever it was pointed at and apply the schema to it, so a mistyped or unread
`LOCKIN_DB` produced a valid, fully-schemed, *empty* database — and all five gates in step
4 then reported `0/25 weeks ingested`. Every symptom named the ingest; the fault was the
setting. `connect(create=False)` now opens with SQLite's `mode=rw`, so the refusal is
enforced by SQLite rather than by an `exists()` check that a concurrent ingest could race.

**What this does and does not buy you.**

It does **not** help next season's projections. `load_panel` filters `WHERE season = ?`, so
the 2026-27 panel ignores 2025-26 entirely. The cold start in the opening weeks — where
players have no own history and fall back to the pooled donor cohort — is real, and last
season sitting on disk does not soften it. Fixing that would mean cross-season panel
support, which does not exist and is not planned.

What it does buy is the next step.

## 4. Run the whole gate suite on the Pi

This is the deployment test. With the season present, every gate the project has can run on
the actual hardware — not a smoke test of imports, a proof that the engine produces the
same answers there.

```bash
cd /home/pi/lockin
uv run --frozen lockin reconcile
uv run --frozen lockin verify
uv run --frozen lockin locks
uv run --frozen lockin calibrate
uv run --frozen lockin backtest
```

**Pass:** five × `all gates passed`. `backtest` is the slow one — minutes of Monte Carlo —
and is the single best evidence that the Pi is a working host. If it passes there, nothing
about the digest will surprise you.

Then populate the dashboard, which reads what this stores:

```bash
uv run --frozen lockin managers
```

## 5. One live ingest, watching it

```bash
uv run --frozen lockin ingest --weeks 12
```

Watch the `status` line:

```
status      219 availability rows across 2 day(s)
```

The **day count** is the number to watch, not the row count — a capture frozen months ago
still reports thousands of rows. It should be one higher than before.

---

## 6. Prove the notification works, before you depend on it

```bash
head -c 24 /dev/urandom | base64 | tr -d '/+=' > ~/.lockin-topic
chmod 600 ~/.lockin-topic
```

The topic name **is** the secret — ntfy topics are public and unauthenticated, so anyone
who guesses it reads your lineup. Generate it, do not choose it.

Subscribe the phone to that topic in the ntfy app, then:

```bash
LOCKIN_NTFY_TOPIC=$(cat ~/.lockin-topic) \
  uv run --frozen lockin digest --date 2026-01-08 --locked 1000:46.0,1787:47.5 --notify
```

**Pass:** the last line reads `notification: sent to https://ntfy.sh/<first six chars>…`,
and it arrives on the phone with the column alignment intact.

**The topic is truncated on purpose.** This line is redirected into `logs/digest.log` by
the cron below, so printing it whole would write the secret into an unprotected file every
morning — having just generated it from `/dev/urandom` and `chmod 600`'d it. Six characters
of a 24-byte random name confirm you are looking at the right topic and leave it
unguessable. If you need the whole thing, it is in `~/.lockin-topic`, which is where it
should be.

This is worth doing by hand because the send path had never executed once until it was
tested deliberately, and testing it found a crash (§20).

---

## 7. Install the cron

```cron
30 6 * * *  cd /home/pi/lockin && scripts/cron-guard ingest /home/pi/.local/bin/uv run --frozen lockin ingest --weeks current
0  9 * * *  cd /home/pi/lockin && scripts/cron-guard digest /home/pi/.local/bin/uv run --frozen lockin digest --notify
5  9 * * *  cd /home/pi/lockin && scripts/cron-guard advice /home/pi/.local/bin/uv run --frozen lockin advice
```

`mkdir -p logs && chmod 700 logs` first. The mode matters: these logs record what the
digest decided about your lineup every morning, and the ntfy status line names the topic
they were sent to. The guard creates its own files 600 and the directory 700, but it cannot
fix a log that already exists — `chmod 600 logs/*.log` once if you set this up before the
guard existed.

### The guard, and why the redirect was not enough

`scripts/cron-guard <job-name> <command...>` appends to `logs/<job-name>.log` with a
timestamped header, and pushes an ntfy alert when the command exits nonzero. It exits with
the command's own status, so cron still sees a failure as a failure.

The first version of this section ended each line with `>> logs/x.log 2>&1` and stopped
there. On 2026-09-10 the observe and ingest jobs began failing with `Temporary failure in
name resolution` and kept failing for four days. Nothing reported it. The only symptom was
the next successful run reporting five days of upstream drift as though it were one, which
briefly looked like a finding about Sleeper rather than a finding about this Pi.

Three things follow from that, and the guard is shaped by them:

- **It is shell, not a `lockin` subcommand.** The failures worth catching include a broken
  venv, a missing `uv` and a corrupt lockfile. A Python entry point does not survive any of
  those long enough to report them.
- **A network outage suppresses its own alert.** The DNS failure that breaks the job also
  breaks the POST to ntfy, so those four days would still have been silent *while they were
  happening*. Every successful run therefore stamps `logs/.cron-guard/<job>.ok`, and a run
  that finds its stamp more than 36 hours old sends a **recovered** alert naming the gap.
  An outage is announced late rather than never. Everything local — bad lockfile, missing
  database, full disk, schema error — alerts immediately.
- **The topic comes from the file, not the cron line.** The digest line used to carry
  `LOCKIN_NTFY_TOPIC=$(cat ~/.lockin-topic)`. An ntfy topic is unauthenticated, so the name
  is the whole of the secret, and `ps` shows a command line to every user on the box. The
  guard reads `~/.lockin-topic` itself and exports it for the child, so `--notify` still
  works and the crontab no longer names it.

Two smaller consequences. `advice` now writes `logs/advice.log` rather than sharing
`logs/digest.log`, because the guard names the log after the job. And the crontab lost its
last `date` call, which is where the `%`-escaping trap of the previous version lived — the
timestamps are produced inside the script now, where `%` means nothing.

To prove it works, before you depend on it — the same rule as §6:

```bash
scripts/cron-guard alert-test sh -c 'echo pretending to fail; exit 1'
```

That should exit 1, append to `logs/alert-test.log`, and put a **lockin alert-test failed**
notification on your phone carrying the last lines of the output. Delete the log and
`logs/.cron-guard/alert-test.ok` afterwards.

In-band, the digest records `last_ingest_at` with every run for the same reason: a digest
running on data a failed ingest never refreshed is otherwise indistinguishable from a
healthy one.

### Rotation

```cron
45 4 * * *  cd /home/pi/lockin && scripts/cron-guard logrotate /usr/sbin/logrotate --state logs/.logrotate.state scripts/logrotate.conf
```

Fifth line, and the first of the day — 04:45, half an hour ahead of `observe`, so nothing
rotates a file mid-write. Nothing else truncates these logs, and the daily observe cadence
means they only grow from here.

**Not an `/etc/logrotate.d` entry.** That needs root to install and root to edit, while
everything else here runs as the unprivileged user out of the repo, and a rule living
somewhere the repo does not know about goes stale the moment the checkout moves.
`scripts/logrotate.conf` is committed, `--state` keeps its bookkeeping in `logs/`, and
running it under the guard means a broken rotation alerts like any other failed job.

The policy is deliberately generous: **monthly, 24 archives, compressed, dated names.**
`logs/observe.log` is not an ordinary service log — it is the only record of *when* Sleeper
rewrites the completed season, and the 48-hour alternation and sweep dates in
implementation-plan.md §12 were read straight out of it. None of that is reconstructable
after the fact. Two years of a file growing ~70 KB a month costs nothing worth counting.
`maxsize 5M` rotates early if something floods; four days of tracebacks put 38 KB into
`logs/ingest.log` in September and a tighter loop could do much worse.

Archives are named `observe.log-20260920.gz` rather than `observe.log.1.gz`, so a reader
can tell which window a file covers without unpacking it — and so an archived name does not
come to mean something different after the next rotation.

One trap, called out in the config because nobody would guess it: **the quotes around the
path are load-bearing.** logrotate parses a bare relative path as a keyword, so

```
logs/*.log { ... }        # error: keyword 'logs' not properly separated, found 0x2f
"logs/*.log" { ... }      # globs against the working directory, which the cron `cd` fixes
```

Check it without waiting a month:

```bash
cd /home/pi/lockin && /usr/sbin/logrotate --force --state logs/.logrotate.state scripts/logrotate.conf
```

`--force` rotates everything immediately; drop it and the command is the daily no-op, which
should exit 0 and seed `logs/.logrotate.state` with a line per log.

**`--weeks current` asks Sleeper, not the calendar.** It reads `settings.leg` from the
league payload the ingest already fetches, so it costs no extra request and cannot disagree
with the run it belongs to. It returns the week being played, plus the previous one while
that is still being scored — two weeks at most, never the whole season. The run prints what
it resolved:

```
  weeks       current -> 12
```

This replaced `--weeks $(date +\%V)`, the ISO calendar week, which was wrong every day it
ran. Fantasy weeks are 1-25 and nothing maps between them: in October that asked for week
40 and ingested nothing, and in January it asked for week 3 and re-ingested October every
morning. Nothing announced it — `--weeks` takes any integer, the availability capture runs
regardless, and the digest still finds box scores — so the season would have quietly
stopped advancing with only `lockin reconcile` to say so. It also takes the last `\%`
out of the crontab, where an unescaped `%` silently truncates the command.

Ordering matters: ingest writes what the digest reads, and `advice` re-renders the page
from what the digest wrote.

**On overlap.** The ingest now commits between weeks and the connection carries a 60-second
busy timeout, so a digest firing mid-ingest waits rather than failing. Measured before the
fix: `database is locked` after exactly 5.0 seconds, which meant no digest that morning and
a mailed traceback. Two and a half hours apart, this should never arise — the guard is for
the night a network call hangs.

---

## 8. Serve the pages

```bash
sudo tee /etc/systemd/system/lockin-serve.service >/dev/null <<'EOF'
[Unit]
Description=Lock-in pages
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/lockin
ExecStart=/home/pi/.local/bin/uv run --frozen lockin serve --quiet --dashboard-db data/lockin-2025.db
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now lockin-serve
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/
```

**Pass:** `200`.

**`ExecStart` is one line, and must stay one line.** It is long and the temptation is to wrap
it. systemd's line continuation is a *single* trailing backslash, and the heredoc above is
quoted (`<<'EOF'`), so whatever you type lands in the file verbatim — no shell escaping. A
`\\` there is an escaped backslash, not a continuation: systemd ends the directive, passes a
literal `\` to `lockin serve`, and silently drops the next line. That fails as

```
Error: Got unexpected extra argument (\)
```

with `--dashboard-db` quietly gone, which is the harder half to notice.

**Check what systemd parsed, not what you wrote**, because those are different questions:

```bash
systemctl show lockin-serve -p ExecStart | tr ';' '\n' | grep argv
```

Every flag you intended should be on that line. If a restart loop has already tripped the
rate limiter — `Start request repeated too quickly` — clear it with `sudo systemctl
reset-failed lockin-serve` before starting again, or systemd will refuse without retrying.

Adapt `User`, `WorkingDirectory` and the `uv` path to the account you deployed under; the
`/home/pi` values here are illustrative. `WorkingDirectory` is the one that must be right
regardless: it is where `.env` is read from (step 2).

Then from the phone, on the LAN: `http://<pi>:8080/`. Over Tailscale it is the same URL
with the tailnet address — the server binds all interfaces, so nothing further is needed.

A bad `--dashboard-db` fails the unit at start rather than on the first request, so
`systemctl status` tells you, instead of a phone showing a 500 in November.

`--dashboard-db` is what makes the two-database split usable. Scorecards are retrospective:
the only ones that exist during 2026-27 describe 2025-26, and they live in that season's
file. Without the flag `/dashboard` reads "No scorecards yet" for the entire season — true,
and useless. `LOCKIN_DB` still points at the current season, so tonight's advice is
unaffected.

**Do not port-forward it.** There is no authentication; the network is the boundary. And
do not substitute `python -m http.server`, which in this directory would publish
`data/lockin.db` — the entire season — with directory listing on.

---

## 9. The morning after

The checks that matter are the ones that prove the *scheduled* runs worked, not the manual
ones.

```bash
cd /home/pi/lockin
tail -20 logs/ingest.log logs/digest.log
uv run --frozen python -c "
from lockin.config import Config, load_env_file
from lockin.store.db import connect_readonly
load_env_file()
c = connect_readonly(Config.from_env().db_path)
for r in c.execute('SELECT as_of, COUNT(*) n FROM player_status GROUP BY as_of ORDER BY as_of DESC LIMIT 3'):
    print(r['as_of'], r['n'])
"
```

**This resolves `LOCKIN_DB` rather than naming a file**, and opens read-only. An earlier
version hardcoded `data/lockin.db` and used a bare `sqlite3.connect`, which on a host
following step 3 pointed at the wrong path — and, since `sqlite3.connect` creates what it
cannot find, answered with `no such table: player_status` against a 0-byte database it had
just made. The same trap as step 3, reached from outside the CLI, so `lockin`'s own guard
does not apply. `connect_readonly` cannot create, so a wrong path here says so.

**Pass:** a new `as_of` row for today. That number climbing daily is the single most
important signal in this deployment — it cannot be backfilled, and it is the prerequisite
for ever ranking start/sit decisions (§19).

Then open the page. It reports its own health:

- **Green banner** — the advice is for this morning.
- **Red "YESTERDAY's advice"** — the digest did not run. Check `logs/digest.log`.
- **Red ingest warning** — the digest ran, but on data the ingest failed to refresh. This
  is the quiet failure: without it the page would look completely normal, because the
  digest still finds box scores and still makes confident calls, just on last night's data
  minus last night.

---

## 10. Watch upstream for rewrites

Sleeper is still rewriting the completed 2025-26 season — not stat corrections, but which
game counts for a player-week (implementation-plan.md §12). It is ongoing, it oscillates,
and it moves faster than anyone guessed: 497 starter values across 24 of 25 weeks in a
single day. There is no historical endpoint, so an unobserved change is gone.

```bash
uv run --frozen lockin observe
```

**It does not open the database.** That is the point: a full `lockin ingest` would refetch
a completed season into the file that preserves it, which is what step 3 forbids. `observe`
only ever adds files under `snapshots/`, deduplicated on content, so a stable week costs
nothing.

```
  week 11     CHANGED  20 starter values  -> snapshots/matchups/2025/wk11/20260902T031645Z.json
               roster 2 player 1924  46.0 -> 15.0
               ... and 17 more
  week 25     unchanged
  observed    25 weeks, 24 changed
```

**Daily.** Weekly was the first setting and it lasted exactly one run: that run found 497
starter values moved across 24 of 25 weeks in the 25 hours since the previous sweep. A
series whose interval is longer than the thing it measures records that something changed
and loses when, which is the only question it exists to answer.

```cron
15 5 * * *  cd /home/pi/lockin && scripts/cron-guard observe /home/pi/.local/bin/uv run --frozen lockin observe
```

05:15, deliberately clear of the 06:30 ingest and the 09:00 digest; it needs no database,
so an overlap would be harmless anyway. 25 requests against a documented ceiling of 1000
a minute, and dedup means a quiet season writes nothing at all.

**The fortnight is in, and the answer is bursts.** Week 24 is sampled twice daily, by
`observe` at 09:15Z and the ingest cron at 10:30Z, so it has the finest resolution:
it changed on 2, 4, 6 and 8 September and not on the 3rd, 5th, 7th or 9th — a clean
48-hour alternation — then on four consecutive days, 14 to 17 September, then not since.
Whole-season sweeps landed on 2, 3, 14, 16 and 18 September. No period fits all of it, so
**keep it daily**: a longer interval would have merged the alternation and the four-day
run into the same undifferentiated "it changed".

**This job used to fail silently.** It died four days running, 10 to 13 September, with
`Temporary failure in name resolution`, and the only symptom was the next successful run
reporting five days of drift as though it were one. Both cron lines redirected stderr to a
log nobody reads. That is what `scripts/cron-guard` in step 7 now fixes, and the recovery
stamp is there specifically for this shape of outage: a DNS failure takes the alert down
with the job, so the run that recovers is the one that reports the gap. Step 7's rotation
rule keeps this log bounded without losing it — it is the record the §12 timing analysis was
read out of, so it rotates into dated archives rather than being truncated.

**Commit what it writes.** New snapshots are the whole product, they cannot be refetched,
and `snapshots/` is version-controlled precisely because `data/` is not. The cron cannot
commit for you — check `git status snapshots/` when you see a changed week in the log. At
daily cadence on a moving season that is up to 25 small files a day, so make it a habit
rather than an occasional sweep.

## 11. Repair the season from the archive

The archive is not only evidence. It is a better copy of the season than any single read,
and `lockin repair` puts it back.

Nineteen days of daily sampling established that Sleeper has **not** lost the lock
selections: a corrupted value reverts to the stored one at the next rewrite, 84% of the
time at the very next one, and the value a slot keeps returning to agrees with the oldest
snapshot in 97% of cases (implementation-plan.md §12, "The locks are intact"). So the
original is recoverable by counting, and needs nothing from Sleeper.

```bash
uv run --frozen lockin repair            # what it would change
uv run --frozen lockin repair --apply    # change it
```

This matters because the database was ingested on particular days, and those days were not
special. `weekly_matchups` is frozen at the first full ingest for every week except the
current one, with two partial re-ingests — and both landed while some weeks happened to be
in excursion. On this deployment that is **48 starter values across 6 weeks** holding a
value Sleeper itself no longer serves. Everything derived from them is wrong by that much:
`lock_inferences`, `manager_decisions`, the scorecards, the grades on the served dashboard.

```
  week 12     21 starter values
               roster 1 player 1809  57.5 -> 49.5  (5/8)
               roster 1 player 2133  23.0 -> 45.0  (4/8)
               ... and 19 more
  planned     48 values across 6 weeks
nothing written. re-run with --apply to restore these values.
```

`(5/8)` is the vote: five of eight observations agree on 49.5. A slot marked `TIED` has no
majority and falls back to the earliest observation — 58 slots in the 2025 archive, and in
57 of them the earliest value is one of the tied leaders anyway.

**It appends, it does not update.** The corrupted rows stay in `weekly_matchups` as
history, readers all go through `weekly_matchups_latest`, and deleting one `observed_at` is
the whole undo. Provenance lands in `ingest_log` under source `repair`:

```sql
SELECT target, rows, started_at FROM ingest_log WHERE source = 'repair';
```

After applying, re-run the two commands that cache derived results — nothing else reads
stale state:

```bash
uv run --frozen lockin managers
uv run --frozen lockin dashboard
```

**Run it after a sweep, not on a schedule.** A repair is only as good as the votes behind
it, and a week rewritten this morning has one fresh observation arguing against five or six
older ones — which is the right answer, but it means there is nothing to gain from repairing
faster than the archive grows. It is not in the cron for that reason.

`lockin repair --stats` measures the archive against itself and opens no database at all:
the reversion rates, the excursion lengths, and how many completed matchups a given read
reports backwards (12.8%, as of 2026-09-20).

## What this deployment does not include

- **Nothing runs against a live league**, because there is not one until the commissioner
  rolls 2026-27 over. Everything above is exercised against the completed season, which is
  what §7.3 always said the live paths would have to be smoke-tested against.
- **No `Click` header on the notification.** Deliberately deferred: the ntfy app already
  holds the full digest body, so a link earns its place only if you find yourself wanting
  the page while away from home. Add it with Tailscale if so.
- **`lockin managers` and `lockin backtest` are not in the cron.** They cost seconds to
  minutes of Monte Carlo and answer retrospective questions. Run them by hand when curious;
  `lockin dashboard` renders whatever they last stored.

## Resource notes, measured

```
digest        0.54 s, 61 MB peak RSS      (400 sims, on a laptop)
advice page   two SQL queries, rendered per request
database      27 MB for a full season, dominated by box scores
```

Assume the Pi is several times slower and the digest is still seconds. Performance was the
expected risk here and is not one.
