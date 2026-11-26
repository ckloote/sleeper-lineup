"""What the manager did with each call: the lock ledger.

Sleeper's matchup poll cannot say what is banked. Confirmed against the app for
week 5 of 2026-27:

- Mid-week, a locked player's counted value tracks his latest game as if he
  were not locked (Allen: locked 43 on 11-16; the polls read 33, then 18.5).
- A value stuck on an earlier game means he was out of a starting slot when the
  later games tipped, not a lock (Towns, Coward).
- A completed week keeps changing. Two days after week 5 ended the poll read
  Holiday at 30 while the app's total implies 40, and in weeks 2-4 most starter
  values moved after the week was over.

So `lockin.state`'s reading of the poll is wrong exactly when it matters, and the
manager is the only reliable source. He answers each call on the advice page
(Locked / Passed), and the digest reads banked state from here.

**The game, not a score.** A row names a player's game. The score is that game's
box score, so a mistyped number cannot get in, and a stat correction flows
through.

**Append-only.** The newest row per (week, roster, player, game) is the answer.
An UNDO withdraws it, and a correction is just another row, so nothing that was
said is ever lost.

**Only the past.** Every reader takes ``before``, the moment it describes, and
ignores rows recorded after it, so a replay of a past morning reads what was
known that morning.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from lockin import calendar, clock
from lockin.core.policy import Game
from lockin.projections import date_of
from lockin.store.db import now_iso

LOCK, PASS, UNDO = "LOCK", "PASS", "UNDO"
DECISIONS = (LOCK, PASS, UNDO)
SOURCES = ("page", "cli")


class NotACall(ValueError):
    """An answer to something that is not an open call of this week."""


@dataclass(frozen=True, slots=True)
class Answer:
    sleeper_id: str
    game_day: int
    decision: str
    """LOCK or PASS. A withdrawn answer is no answer and is not returned."""
    recorded_at: str
    run_id: str | None = None


@dataclass(slots=True)
class LedgerState:
    banked: dict[str, float] = field(default_factory=dict)
    """Score of each locked game on or before the morning, from its box score."""
    answers: dict[tuple[str, int], Answer] = field(default_factory=dict)
    """Every standing answer, keyed (sleeper_id, game_day)."""
    problems: list[tuple[str, str, bool]] = field(default_factory=list)
    """(sleeper_id, what is wrong, whether a lock of his was still counted) for
    each recorded lock the digest could not take at face value."""


def has_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'lock_ledger'").fetchone()
        is not None
    )


def record(
    conn: sqlite3.Connection,
    *,
    week: int,
    roster_id: int,
    sleeper_id: str,
    game_day: int,
    decision: str,
    source: str,
    run_id: str | None = None,
    recorded_at: str | None = None,
) -> int:
    """Append one answer. Returns its entry id."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, not {source!r}")
    cur = conn.execute(
        "INSERT INTO lock_ledger"
        " (week, roster_id, sleeper_id, game_day, decision, recorded_at, run_id, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            week,
            roster_id,
            sleeper_id,
            game_day,
            decision,
            recorded_at or now_iso(),
            run_id,
            source,
        ),
    )
    return int(cur.lastrowid)


def answers(
    conn: sqlite3.Connection, week: int, roster_id: int, *, before: str | None = None
) -> dict[tuple[str, int], Answer]:
    """The standing answer for each (player, game) this week, as of ``before``.

    The newest row wins. An UNDO leaves no answer. Rows recorded at or after
    ``before`` are not read; None reads everything.
    """
    if not has_table(conn):
        return {}
    sql = (
        "SELECT sleeper_id, game_day, decision, recorded_at, run_id FROM lock_ledger"
        " WHERE week = ? AND roster_id = ?"
    )
    args: list = [week, roster_id]
    if before is not None:
        sql += " AND recorded_at < ?"
        args.append(before)
    sql += " ORDER BY recorded_at, entry_id"
    out: dict[tuple[str, int], Answer] = {}
    for sid, day, decision, at, run_id in conn.execute(sql, args):
        key = (sid, day)
        if decision == UNDO:
            out.pop(key, None)
        else:
            out[key] = Answer(sid, day, decision, at, run_id)
    return out


def in_use(conn: sqlite3.Connection, roster_id: int, *, before: str | None = None) -> bool:
    """Whether this roster has ever answered a call, as of ``before``.

    A replay of a morning before the ledger began has nothing to read, and
    reading nothing as "nothing locked" would be a claim, not a reading.
    """
    if not has_table(conn):
        return False
    sql = "SELECT 1 FROM lock_ledger WHERE roster_id = ?"
    args: list = [roster_id]
    if before is not None:
        sql += " AND recorded_at < ?"
        args.append(before)
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


def began(conn: sqlite3.Connection, roster_id: int) -> str | None:
    """The first morning a live digest of this roster read banked state from the ledger.

    A replay of that morning or a later one must read the ledger too, even
    before anything was recorded: the live run advised on an empty ledger, and
    falling back to the poll would replay a different state.
    """
    try:
        row = conn.execute(
            "SELECT MIN(as_of) FROM digest_runs WHERE roster_id = ? AND state_source = 'ledger'"
            " AND COALESCE(retrospective, 0) = 0",
            (roster_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # a database from before runs had a state source
    return row[0] if row else None


def live_morning(conn: sqlite3.Connection, roster_id: int, as_of: str) -> sqlite3.Row | None:
    """The live run a replay of ``as_of`` reproduces: the latest that reached its
    state step that morning.

    One run answers both of a replay's questions, which source it read and as of
    when. Taking the cutoff from any later run, an abstention included, let a
    replay of the 09:00 advice count a tap from after it.
    """
    try:
        return conn.execute(
            "SELECT state_source, COALESCE(ledger_read_at, generated_at) AS read_at"
            "  FROM digest_runs WHERE roster_id = ? AND as_of = ?"
            "   AND COALESCE(retrospective, 0) = 0 AND NOT COALESCE(abstained, 0)"
            "   AND state_source IS NOT NULL ORDER BY generated_at DESC LIMIT 1",
            (roster_id, as_of),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # a database from before these columns


def replay_moment(conn: sqlite3.Connection, roster_id: int, as_of: str) -> str:
    """The moment a replay of ``as_of`` reads the ledger as of: its live run's.

    That run's ledger cutoff, so the replay sees exactly the answers it saw;
    with no such run, when that morning's digest was due.
    """
    row = live_morning(conn, roster_id, as_of)
    return row["read_at"] if row is not None and row["read_at"] else clock.scheduled_run(as_of)


def live_source(conn: sqlite3.Connection, roster_id: int, as_of: str) -> str | None:
    """The state source this morning's live digest saved, if one reached that step."""
    row = live_morning(conn, roster_id, as_of)
    return row["state_source"] if row is not None else None


def replays_ledger(conn: sqlite3.Connection, roster_id: int, as_of: str, *, before: str) -> bool:
    """Whether a replay of ``as_of`` reads the ledger, as that morning did.

    The live run's saved state source decides it: a morning that ran on the poll
    (``LOCKIN_LEDGER=0``) replays on the poll, though the ledger was on before
    and after it. With no saved morning, the ledger applies from the first
    morning a live run read it, or once something had been recorded.
    """
    saved = live_source(conn, roster_id, as_of)
    if saved is not None:
        return saved == "ledger"
    first = began(conn, roster_id)
    return (first is not None and first <= as_of) or in_use(conn, roster_id, before=before)


def state(
    conn: sqlite3.Connection,
    week: int,
    roster_id: int,
    lineup: dict[str, list[Game]],
    known_through: int,
    *,
    before: str | None,
) -> LedgerState:
    """What is banked on the morning after ``known_through``, by the ledger.

    A lock counts once its game is on or before ``known_through``: the score is
    that game's, from ``lineup``. A lock on a player who is not in ``lineup``, or
    on a game he does not have, is reported in ``problems`` and not banked. Two
    locks for one player in one week cannot happen in Sleeper; the later game
    counts, and the report names both games and the one counted, so the manager
    can undo the wrong one.
    """
    out = LedgerState(answers=answers(conn, week, roster_id, before=before))
    locks: dict[str, list[int]] = {}
    for (sid, day), ans in out.answers.items():
        if ans.decision == LOCK and day <= known_through:
            locks.setdefault(sid, []).append(day)
    for sid, days in sorted(locks.items()):
        games = {g.day: g for g in lineup.get(sid, [])}
        if not games:
            out.problems.append((sid, "recorded as locked, but not in this week's lineup", False))
            continue
        day = max(days)
        game = games.get(day)
        if game is None:
            out.problems.append((sid, "recorded as locked on a game he does not have", False))
            continue
        out.banked[sid] = score = game.score if game.played else 0.0
        if len(days) > 1:
            shown = " and ".join(date_of(d) for d in sorted(days))
            out.problems.append(
                (
                    sid,
                    f"recorded as locked on {shown}; counting {date_of(day)} ({score:g})",
                    True,
                )
            )
    return out


def unanswered(
    calls: dict[tuple[str, int], str],
    ledger: dict[tuple[str, int], Answer],
    called_at: dict[tuple[str, int], str] | None = None,
) -> dict[tuple[str, int], str]:
    """The earlier LOCK calls nobody has answered, from ``runs.earlier_lock_calls``.

    ``called_at`` is each game's latest earlier LOCK call's ledger cutoff
    (`runs.earlier_lock_cutoffs`). A PASS recorded before it answered an older
    call: Tuesday's PASS does not answer Wednesday's LOCK.
    """
    called_at = called_at or {}

    def answers(key: tuple[str, int]) -> bool:
        found = ledger.get(key)
        if found is None:
            return False
        at = called_at.get(key)
        return not (
            found.decision == PASS
            and at is not None
            and clock.utc(found.recorded_at) < clock.utc(at)
        )

    return {key: first for key, first in calls.items() if not answers(key)}


def open_call(
    conn: sqlite3.Connection,
    roster_id: int,
    run_id: str,
    sleeper_id: str,
    game_day: int,
    *,
    season: str | None,
    today: str,
) -> int:
    """The week of a call that may be answered, or raise `NotACall`.

    It must be a LOCK or PASS call of a **live** run for this roster, in the week
    of the roster's latest live run, and ``today`` must not be past that week's
    Sunday. A replay was never advice, and a week that is over has nothing left
    to decide. The calendar is checked as well as the runs: after the weekly
    rollover, Sunday's run stays the latest until a digest succeeds, and its page
    would otherwise keep taking edits to a finished week. A season whose calendar
    cannot be read takes no answers at all.

    This is the only thing the page's write route trusts, so it is checked
    against the database, never the form.
    """
    row = conn.execute(
        """
        SELECT r.week
          FROM recommendations c
          JOIN digest_runs r ON r.run_id = c.run_id
         WHERE c.run_id = ? AND c.sleeper_id = ? AND c.for_day = ?
           AND c.action IN ('LOCK', 'PASS')
           AND r.roster_id = ? AND COALESCE(r.retrospective, 0) = 0
        """,
        (run_id, sleeper_id, game_day, roster_id),
    ).fetchone()
    if row is None:
        raise NotACall("not a call of a live digest for this roster")
    latest = conn.execute(
        "SELECT week FROM digest_runs WHERE roster_id = ? AND COALESCE(retrospective, 0) = 0"
        " ORDER BY generated_at DESC LIMIT 1",
        (roster_id,),
    ).fetchone()
    if latest is None or latest[0] != row[0]:
        raise NotACall("that call's week is over")
    opening = calendar.opening_night(conn, season) if season else None
    if opening is None:
        raise NotACall("the season's calendar is unknown, so its weeks cannot be checked")
    _, sunday = calendar.week_bounds(int(row[0]), opening)
    if date.fromisoformat(today) > sunday:
        raise NotACall("that call's week is over")
    return int(row[0])
