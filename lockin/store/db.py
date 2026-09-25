"""SQLite access. Single writer, WAL, schema applied idempotently."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class DatabaseMissing(Exception):
    """The database named does not exist, and the caller did not ask to create one.

    Its own type because the answer depends on which season you meant, and only
    the caller knows: `lockin ingest` creates, everything else refuses. See
    `connect`.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        super().__init__(f"no database at {self.db_path}")


def now_iso() -> str:
    """UTC timestamp for `observed_at` / `ingested_at` columns, to the microsecond.

    Whole seconds were not enough. `observed_at` is what separates one poll from
    the next, and two ingests inside one second shared a stamp: the second poll
    replaced the first one's rows where they overlapped and inherited the rest,
    so a player dropped in between stayed on the roster (review finding 4). The
    same second-granularity collision is finding 12's, for digest runs.

    Mixed precision still sorts correctly, which every `MAX(observed_at)`
    relies on: a whole-second stamp and a fractional one from the same second
    differ first at `+` against `.`, and `+` sorts first.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """A connection that cannot write, for readers that must not.

    `lockin serve` handles requests from the network. Opening the database
    read-only means a bug in a request handler cannot corrupt the season, and it
    is not a promise anyone has to keep — SQLite enforces it.

    No schema is applied, deliberately: applying it is a write, and a server that
    creates tables is a server that can be made to create tables. A database too
    old to have the tables a page needs is a `lockin digest` away from having
    them, and the page says so.

    WAL means this coexists with the cron ingest writing at the same moment.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        if not Path(db_path).exists():
            raise DatabaseMissing(db_path) from exc
        raise
    conn.row_factory = sqlite3.Row
    return conn


def connect(db_path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open for writing. With ``create=False``, refuse to invent the database.

    Creating on demand is right for `lockin ingest`, which is how a season comes
    into being, and wrong for everything else. A mistyped `LOCKIN_DB` used to
    yield a valid, fully-schemed, *empty* database, so the gates reported
    `0/25 weeks ingested` — a missing ingest — when the truth was a missing
    setting. It also defeated the test suite's `db_path.exists()` skip guards,
    which then errored instead of skipping.

    `mode=rw` opens without creating, so SQLite enforces this rather than an
    `exists()` check the caller could race against a concurrent ingest.
    """
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, isolation_level=None)
    else:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, isolation_level=None)
        except sqlite3.OperationalError as exc:
            if not Path(db_path).exists():
                raise DatabaseMissing(db_path) from exc
            raise
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Wait for a busy writer rather than failing at sqlite3's 5-second default.
    # Measured: with the ingest mid-transaction, a concurrent digest raised
    # "database is locked" after exactly 5.0s — which on a Pi means no digest
    # that morning and a mailed traceback. Readers are unaffected either way;
    # WAL never blocks them. See also `checkpoint`, which keeps the ingest from
    # holding the lock for minutes in the first place.
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


# Columns added to existing tables after the first release. CREATE TABLE IF NOT
# EXISTS will not add them to a database that already exists, so they are
# applied explicitly. Additive only — nothing here drops or retypes.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "game_links": {"occurred": "INTEGER", "is_exhibition": "INTEGER", "state": "TEXT"},
    "nba_schedule": {"status": "INTEGER"},
    "box_scores": {"is_team_row": "INTEGER"},
    # Added columns are nullable even where schema.sql declares NOT NULL:
    # SQLite cannot ALTER TABLE ADD a NOT NULL column without a default. A
    # freshly created database gets the stricter definition.
    "lock_inferences": {
        "status": "TEXT",
        "locked_early": "INTEGER",
        "counted_points": "REAL",
    },
    "manager_scorecards": {
        "squandered_share": "REAL",
        "mean_stake": "REAL",
        "share_lo": "REAL",
        "share_hi": "REAL",
    },
    "roster_strength": {"availability": "REAL", "points_per_game_played": "REAL"},
    # Without it, two rosters digested on the same day interleave into one
    # undistinguishable list. Nullable, so the rows written before it survive.
    "recommendations": {
        "roster_id": "INTEGER",
        "run_id": "TEXT",
        "expires_utc": "TEXT",
        "p_clear": "REAL",
        "games_after": "INTEGER",
    },
    "digest_runs": {
        "last_ingest_at": "TEXT",
        "run_id": "TEXT",
        "state_source": "TEXT",
        "opponent_state": "TEXT",
        "poll_observed_at": "TEXT",
        "ingest_run_id": "INTEGER",
        "n_sims": "INTEGER",
        "seed": "INTEGER",
        "model": "TEXT",
        "abstained": "INTEGER",
        "schedule_at": "TEXT",
        "status_at": "TEXT",
    },
    "ingest_runs": {"skipped": "TEXT"},
}


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; schema.sql will include the column
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _rebuild_recommendations(conn: sqlite3.Connection) -> None:
    """Widen `recommendations`' primary key, which ALTER TABLE cannot do.

    Phase 6 found the placeholder key `(generated_at, week, sleeper_id)` too
    narrow: one digest legitimately writes several rows for one player — a call
    on last night's game and a standing rule for each of the next few nights —
    and under the old key those silently overwrote each other.

    Guarded on the table being **empty**. A dropped table is not recoverable, and
    this table is the only record of what was advised on a given day (§12), so it
    is rebuilt only in the case where nothing can be lost. A populated old-shape
    table is left alone and reported by `lockin reconcile` instead.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)")}
    if not columns or "for_day" in columns:
        return
    if conn.execute("SELECT COUNT(*) c FROM recommendations").fetchone()["c"]:
        return
    conn.execute("DROP TABLE recommendations")
    conn.executescript(SCHEMA_PATH.read_text())


def _schema_statement(kind: str, name: str) -> str:
    """One CREATE statement from schema.sql, for a migration that must re-issue it."""
    match = re.search(
        rf"CREATE {kind} IF NOT EXISTS {name}\b.*?;", SCHEMA_PATH.read_text(), re.DOTALL
    )
    if match is None:
        raise RuntimeError(f"schema.sql has no CREATE {kind} {name}")
    return match.group(0)


def _complete_partial_polls(conn: sqlite3.Connection) -> int:
    """Make every repair observation a whole poll. Returns rows copied forward.

    `lockin repair` used to append only the starter rows it corrected, which the
    old per-player view merged with everything else. The per-poll view would
    read such an observation as the roster's entire membership, dropping the
    bench and every starter the repair left alone — so before the view changes,
    each one is completed from the poll it corrected.

    Restricted to observations `ingest_log` records as repairs. A live poll
    that is smaller than the one before it is a roster that lost a player, and
    filling it in would resurrect him, which is the bug being fixed.
    Chronological, so a second repair of the same week copies from the first
    one's completed poll rather than from a partial one.
    """
    written = 0
    stamps = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT started_at FROM ingest_log WHERE source = 'repair' ORDER BY 1"
        )
    ]
    for stamp in stamps:
        teams = conn.execute(
            "SELECT week, roster_id FROM weekly_matchup_teams WHERE observed_at = ?", (stamp,)
        ).fetchall()
        for week, roster_id in teams:
            prior = conn.execute(
                "SELECT MAX(observed_at) FROM weekly_matchup_teams"
                " WHERE week = ? AND roster_id = ? AND observed_at < ?",
                (week, roster_id, stamp),
            ).fetchone()[0]
            if prior is None:
                continue
            # OR IGNORE keeps the corrected rows the repair did write.
            cur = conn.execute(
                "INSERT OR IGNORE INTO weekly_matchups"
                " (week, roster_id, matchup_id, sleeper_id, counted_points, is_starter,"
                "  slot_index, slot, observed_at)"
                " SELECT week, roster_id, matchup_id, sleeper_id, counted_points, is_starter,"
                "        slot_index, slot, ?"
                "   FROM weekly_matchups"
                "  WHERE week = ? AND roster_id = ? AND observed_at = ?",
                (stamp, week, roster_id, prior),
            )
            written += cur.rowcount
    return written


def _migrate_coherent_polls(conn: sqlite3.Connection) -> None:
    """Review finding 4: the latest view reads whole polls, not rows per player."""
    _complete_partial_polls(conn)
    conn.execute("DROP VIEW IF EXISTS weekly_matchups_latest")
    conn.execute(_schema_statement("VIEW", "weekly_matchups_latest"))


def _migrate_fixture_states(conn: sqlite3.Connection) -> None:
    """Review finding 1: carry `occurred` over to `state` for fixtures already here.

    Every fixture in a database from before this change is past-dated, so the
    old binary was right about them: played is final, and "nobody played" was a
    real postponement (reconcile checked each against the NBA). The next ingest
    reclassifies from full evidence anyway.
    """
    conn.execute(
        "UPDATE game_links SET state = CASE occurred WHEN 1 THEN 'final'"
        " WHEN 0 THEN 'postponed' END WHERE state IS NULL"
    )


# Data migrations, in order. Each runs once per database, inside a savepoint, and
# is recorded in `schema_migrations`. A fresh database runs them too, against
# empty tables, which is what records them as done.
_MIGRATIONS: tuple[tuple[str, Callable[[sqlite3.Connection], None]], ...] = (
    ("coherent-polls", _migrate_coherent_polls),
    ("fixture-states", _migrate_fixture_states),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    for name, migrate in _MIGRATIONS:
        if name in done:
            continue
        conn.execute("SAVEPOINT migrate")
        try:
            migrate(conn)
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, now_iso()),
            )
        except BaseException:
            conn.execute("ROLLBACK TO migrate")
            conn.execute("RELEASE migrate")
            raise
        conn.execute("RELEASE migrate")


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    _apply_added_columns(conn)
    _rebuild_recommendations(conn)
    _apply_migrations(conn)


@contextmanager
def session(db_path: Path, *, create: bool = True) -> Iterator[sqlite3.Connection]:
    """Open a connection with the schema applied, committing on clean exit.

    ``create=False`` raises `DatabaseMissing` rather than starting an empty
    season; see `connect`.
    """
    conn = connect(db_path, create=create)
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


def checkpoint(conn: sqlite3.Connection) -> None:
    """Commit what is done and start a fresh transaction.

    `session` wraps its whole body in one transaction, which for a 25-week
    ingest means holding the write lock for minutes while the network is slow.
    Anything else wanting to write during that window waits, and a `busy_timeout`
    only helps if the wait is shorter than the timeout.

    Calling this between units of work keeps the lock held for seconds instead.
    It is safe precisely because ingest is idempotent — every write is INSERT OR
    REPLACE and re-running fills any gap — so a partial commit is a re-runnable
    state rather than a corrupt one, and `lockin reconcile` is what says whether
    the result is complete.
    """
    if conn.in_transaction:
        conn.execute("COMMIT")
    conn.execute("BEGIN")


def log_ingest(
    conn: sqlite3.Connection, source: str, target: str, rows: int, started_at: str
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source, target, rows, started_at, finished_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (source, target, rows, started_at, now_iso()),
    )
