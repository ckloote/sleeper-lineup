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
# /home/pi/lockin/.env  (or wherever your cron reads environment from)
LOCKIN_TZ=America/New_York
```

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

## 3. First run, by hand, watching it

Do not install the cron yet.

```bash
cd /home/pi/lockin
uv run --frozen lockin ingest --weeks 12        # one week, to prove the path
uv run --frozen lockin reconcile
uv run --frozen lockin verify
```

**Pass:** both gates report `all gates passed`. `verify` is the one that matters — it
reproduces every nonzero counted score from box scores, so it catches a scoring-settings
change or a broken ingest immediately.

Watch the ingest's `status` line:

```
status      219 availability rows across 2 day(s)
```

The **day count** is the number to watch, not the row count. A capture frozen months ago
still reports thousands of rows.

---

## 4. Prove the notification works, before you depend on it

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

**Pass:** the last line reads `notification: sent to https://ntfy.sh/<topic>`, and it
arrives on the phone with the column alignment intact.

This is worth doing by hand because the send path had never executed once until it was
tested deliberately, and testing it found a crash (§20).

---

## 5. Install the cron

```cron
30 6 * * *  cd /home/pi/lockin && /home/pi/.local/bin/uv run --frozen lockin ingest --weeks $(date +\%V) >> logs/ingest.log 2>&1
0  9 * * *  cd /home/pi/lockin && LOCKIN_NTFY_TOPIC=$(cat ~/.lockin-topic) /home/pi/.local/bin/uv run --frozen lockin digest --notify >> logs/digest.log 2>&1
5  9 * * *  cd /home/pi/lockin && /home/pi/.local/bin/uv run --frozen lockin advice >> logs/digest.log 2>&1
```

`mkdir -p logs` first, and add a logrotate rule — nothing here truncates them.

Ordering matters: ingest writes what the digest reads, and `advice` re-renders the page
from what the digest wrote.

**On overlap.** The ingest now commits between weeks and the connection carries a 60-second
busy timeout, so a digest firing mid-ingest waits rather than failing. Measured before the
fix: `database is locked` after exactly 5.0 seconds, which meant no digest that morning and
a mailed traceback. Two and a half hours apart, this should never arise — the guard is for
the night a network call hangs.

---

## 6. Serve the pages

```bash
sudo tee /etc/systemd/system/lockin-serve.service >/dev/null <<'EOF'
[Unit]
Description=Lock-in pages
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/lockin
ExecStart=/home/pi/.local/bin/uv run --frozen lockin serve --quiet
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now lockin-serve
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/
```

**Pass:** `200`.

Then from the phone, on the LAN: `http://<pi>:8080/`. Over Tailscale it is the same URL
with the tailnet address — the server binds all interfaces, so nothing further is needed.

**Do not port-forward it.** There is no authentication; the network is the boundary. And
do not substitute `python -m http.server`, which in this directory would publish
`data/lockin.db` — the entire season — with directory listing on.

---

## 7. The morning after

The checks that matter are the ones that prove the *scheduled* runs worked, not the manual
ones.

```bash
cd /home/pi/lockin
tail -20 logs/ingest.log logs/digest.log
uv run --frozen python -c "
import os, sqlite3
c = sqlite3.connect('data/lockin.db'); c.row_factory = sqlite3.Row
for r in c.execute('SELECT as_of, COUNT(*) n FROM player_status GROUP BY as_of ORDER BY as_of DESC LIMIT 3'):
    print(r['as_of'], r['n'])
"
```

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
