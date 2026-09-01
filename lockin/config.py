"""Runtime configuration.

Nothing here is read by ``lockin.core`` — core takes plain values and
dataclasses. This module exists so the ingest and CLI layers have one place to
resolve the league, the season and the database path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The 2025-26 league. Sleeper labels that season "2025".
#
# This is a DEFAULT, not a constant. The league is `status: complete`, and the
# 2026-27 league will carry a different id once the commissioner rolls it over
# (see implementation-plan.md §7.3). Resolve by season rather than assuming this
# value stays current.
DEFAULT_LEAGUE_ID = "1283214955830575104"
DEFAULT_SEASON = "2025"
DEFAULT_USER_ID = "1283460931447164928"

# The timezone NBA game dates are filed under, which is not the same question
# as where the server is. See lockin/clock.py.
DEFAULT_TZ = "America/New_York"

# Weeks 1-21 are regular season, 22-24 playoffs, 25 exists in the stats feed but
# is unscored by the league (`last_scored_leg: 24`).
REGULAR_SEASON_WEEKS = range(1, 22)
PLAYOFF_WEEKS = range(22, 25)
ALL_STAT_WEEKS = range(1, 26)


@dataclass(frozen=True)
class Config:
    league_id: str
    season: str
    user_id: str
    db_path: Path
    timezone: str
    snapshot_root: Path
    """Raw matchup payloads, kept OUTSIDE the database.

    Sleeper rewrites completed seasons and publishes no history, so an
    unobserved change is unrecoverable. The database is disposable and gets
    rebuilt; snapshots must not be. See lockin/store/snapshots.py.
    """

    @classmethod
    def from_env(cls) -> Config:
        db = os.environ.get("LOCKIN_DB", "data/lockin.db")
        snaps = os.environ.get("LOCKIN_SNAPSHOTS", "snapshots")
        return cls(
            league_id=os.environ.get("LOCKIN_LEAGUE_ID", DEFAULT_LEAGUE_ID),
            season=os.environ.get("LOCKIN_SEASON", DEFAULT_SEASON),
            user_id=os.environ.get("LOCKIN_USER_ID", DEFAULT_USER_ID),
            timezone=os.environ.get("LOCKIN_TZ", DEFAULT_TZ),
            db_path=Path(db).expanduser(),
            snapshot_root=Path(snaps).expanduser(),
        )


ENV_FILE = ".env"


def load_env_file(path: Path | None = None) -> Path | None:
    """Read ``.env`` into the process environment. Returns the file, or None.

    The deployment runbook configures the Pi by writing ``LOCKIN_DB`` to
    ``/home/pi/lockin/.env`` (deployment.md §2, §3), and nothing used to read
    it: cron and systemd both start a process whose environment has never seen
    that file, so the setting silently did nothing and the gates ran against
    the default path. That failed as `0/25 weeks ingested` rather than as a
    missing database, because `store.db.connect` creates what it cannot open.

    Every command goes through ``lockin.cli.main``, which calls this before
    dispatching, so the runbook's instruction is now true wherever it is
    followed from.

    **An existing variable always wins.** Values already in the environment are
    left alone, so ``LOCKIN_NTFY_TOPIC=$(cat ~/.lockin-topic) lockin digest``
    and day-one.md's ``export LOCKIN_DB=data/lockin-2026.db`` still override
    the file. A ``.env`` that outranked an explicit export would reintroduce
    this same bug pointing the other way — next season's ingest quietly writing
    into last season's database, which `weekly_matchups` has no season column
    to keep apart.

    Resolved against the working directory, like ``data/lockin-2025.db``
    itself; the cron entries and the systemd unit both enter the project first.

    The format is ``KEY=value``, one per line, ``#`` comments and blank lines
    ignored, an ``export`` prefix allowed so lines paste between here and a
    shell, and one layer of matching quotes stripped. Nothing is expanded:
    ``$HOME`` and ``$(cat ...)`` are literal text here, and belong in the
    crontab, where a shell runs them.
    """
    env_path = Path(ENV_FILE) if path is None else path
    try:
        text = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        # Loud, not lenient. A typo here is a misconfigured host, and this file
        # exists because a setting that quietly did nothing cost a deployment.
        if not sep or not key:
            raise ValueError(f"{env_path}:{lineno}: expected KEY=value, got {raw.strip()!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return env_path
