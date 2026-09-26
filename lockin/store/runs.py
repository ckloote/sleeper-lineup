"""Ingest runs: when the data under a digest was last complete.

The ingest writes, the digest and the advice page read. Kept apart from both so
neither has to import the other.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from lockin.store.db import now_iso


def start(conn: sqlite3.Connection, weeks: list[int], slate_through: str) -> int:
    """Record that an ingest began. Complete it with `finish`, or it stays running."""
    cur = conn.execute(
        "INSERT INTO ingest_runs (started_at, weeks, status, slate_through)"
        " VALUES (?, ?, 'running', ?)",
        (now_iso(), json.dumps(sorted(weeks)), slate_through),
    )
    return int(cur.lastrowid)


def finish(conn: sqlite3.Connection, run_id: int, *, skipped: Iterable[str] = ()) -> None:
    """Record that every step ran, except the ``skipped`` ones it was told to leave out."""
    conn.execute(
        "UPDATE ingest_runs SET status = 'complete', finished_at = ?, skipped = ? WHERE run_id = ?",
        (now_iso(), json.dumps(sorted(skipped)), run_id),
    )


LIVE_REQUIRES = frozenset({"nba"})
"""Steps a run must not have skipped to vouch for a live digest. The NBA step
fetches the statuses that say last night is final, and the schedule the rest of
the week is read from. The tipoff sweep is a backstop — the schedule carries
tipoffs — so a run without it still counts."""


def skipped(row: sqlite3.Row) -> set[str]:
    """What a run left out. Rows from before the column read as nothing."""
    return set(json.loads(row["skipped"] or "[]"))


def schedule_fetched_at(conn: sqlite3.Connection) -> str | None:
    """When the NBA schedule was last fetched in full."""
    row = conn.execute(
        "SELECT MAX(finished_at) FROM ingest_log WHERE source = 'nba' AND target LIKE 'schedule:%'"
    ).fetchone()
    return row[0] if row else None


def designations_read_at(conn: sqlite3.Connection) -> str | None:
    """When injury designations were last read, flagged or not."""
    row = conn.execute("SELECT MAX(observed_at) FROM status_captures").fetchone()
    return row[0] if row else None


def latest_covering(conn: sqlite3.Connection, week: int) -> sqlite3.Row | None:
    """Newest started run touching this week, including partial commits."""
    for row in conn.execute("SELECT * FROM ingest_runs ORDER BY run_id DESC"):
        if week in json.loads(row["weeks"]):
            return row
    return None


def stats_fetch(conn: sqlite3.Connection, run_id: int, week: int) -> sqlite3.Row | None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'ingest_stats_fetches'"
    ).fetchone():
        return None
    return conn.execute(
        "SELECT * FROM ingest_stats_fetches WHERE run_id = ? AND week = ?", (run_id, week)
    ).fetchone()
