"""SQLite access. Single writer, WAL, schema applied idempotently."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    """UTC timestamp for `observed_at` / `ingested_at` columns."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# Columns added to existing tables after the first release. CREATE TABLE IF NOT
# EXISTS will not add them to a database that already exists, so they are
# applied explicitly. Additive only — nothing here drops or retypes.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "game_links": {"occurred": "INTEGER", "is_exhibition": "INTEGER"},
    "box_scores": {"is_team_row": "INTEGER"},
}


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; schema.sql will include the column
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    _apply_added_columns(conn)


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with the schema applied, committing on clean exit."""
    conn = connect(db_path)
    try:
        apply_schema(conn)
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        # Guarded: some statements (notably executescript) implicitly commit, so
        # a transaction may no longer be open. An unguarded ROLLBACK would raise
        # OperationalError here and mask the exception we are trying to report.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def log_ingest(
    conn: sqlite3.Connection, source: str, target: str, rows: int, started_at: str
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source, target, rows, started_at, finished_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (source, target, rows, started_at, now_iso()),
    )
