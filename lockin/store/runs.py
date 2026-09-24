"""Ingest runs: when the data under a digest was last complete.

The ingest writes, the digest and the advice page read. Kept apart from both so
neither has to import the other.
"""

from __future__ import annotations

import json
import sqlite3

from lockin.store.db import now_iso


def start(conn: sqlite3.Connection, weeks: list[int], slate_through: str) -> int:
    """Record that an ingest began. Complete it with `finish`, or it stays running."""
    cur = conn.execute(
        "INSERT INTO ingest_runs (started_at, weeks, status, slate_through)"
        " VALUES (?, ?, 'running', ?)",
        (now_iso(), json.dumps(sorted(weeks)), slate_through),
    )
    return int(cur.lastrowid)


def finish(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        "UPDATE ingest_runs SET status = 'complete', finished_at = ? WHERE run_id = ?",
        (now_iso(), run_id),
    )


def latest_complete(conn: sqlite3.Connection, week: int | None = None) -> sqlite3.Row | None:
    """The newest ingest that finished every step — and covered ``week``, if given."""
    for row in conn.execute(
        "SELECT run_id, started_at, finished_at, weeks FROM ingest_runs"
        " WHERE status = 'complete' ORDER BY finished_at DESC"
    ):
        if week is None or week in json.loads(row["weeks"]):
            return row
    return None


def any_recorded(conn: sqlite3.Connection) -> bool:
    """Has this database ever been ingested by code that records runs?"""
    return conn.execute("SELECT EXISTS (SELECT 1 FROM ingest_runs)").fetchone()[0] == 1
