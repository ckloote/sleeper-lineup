"""Which league and season a database belongs to, enforced rather than documented.

One file per season is the design (day-one.md step 2), and until 2026-09-23 the
only thing holding it was a sentence in that checklist. `weekly_matchups` and
every derived table carry no season column, so a missed `LOCKIN_DB` change at
rollover would ingest 2026-27 into last season's file without an error — and
`weekly_matchups_latest` would then prefer the new season's rows over the old
ones, silently. A mismatched `LOCKIN_LEAGUE_ID` or `LOCKIN_SEASON` would do the
same thing more quietly still: one league's rosters joined to another season's
box scores.

So a database records its identity the first time it is ingested into, and
every command compares the configuration against it before doing anything.
`lockin ingest` also compares both against the league payload Sleeper returns,
and does so **before its first write** — a refusal leaves the file, and the
snapshot archive, exactly as they were.

A full multi-season schema would make mixing impossible rather than refused.
It is not needed: the refusal is enough to make the one-file-per-season design
safe, and it costs one row.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from lockin.store.db import now_iso


class IdentityMismatch(Exception):
    """The database, the configuration and the league do not agree."""


@dataclass(frozen=True, slots=True)
class Identity:
    league_id: str
    season: str


def read(conn: sqlite3.Connection) -> Identity | None:
    row = conn.execute("SELECT league_id, season FROM db_identity").fetchone()
    return Identity(row["league_id"], row["season"]) if row else None


def _legacy(conn: sqlite3.Connection) -> Identity | None:
    """What a database made before this table existed must belong to.

    Adopted only when it is unambiguous: exactly one (league, season) in
    `league_settings`. Two means the mixing this module exists to prevent has
    already happened, and the honest answer is to refuse rather than to pick.
    """
    rows = conn.execute("SELECT DISTINCT league_id, season FROM league_settings").fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        seen = ", ".join(f"league {r['league_id']} season {r['season']}" for r in rows)
        raise IdentityMismatch(
            f"this database holds league settings for several seasons ({seen}), so it"
            " cannot say which one it is. It may already mix two seasons; rebuild it"
            " from `lockin ingest` into a new file rather than trust it."
        )
    return Identity(rows[0]["league_id"], str(rows[0]["season"]))


def _mismatch(have: Identity, want: Identity, db_path: Path | None) -> IdentityMismatch:
    where = f" ({db_path})" if db_path else ""
    return IdentityMismatch(
        f"this database{where} belongs to league {have.league_id} season {have.season},\n"
        f"but the configuration asks for league {want.league_id} season {want.season}.\n\n"
        "Each season lives in its own database file (day-one.md step 2), and writing\n"
        "one season into another's file mixes them without an error.\n\n"
        f"  to start {want.season}   export LOCKIN_DB=data/lockin-{want.season}.db\n"
        f"                  and run `lockin ingest`, which creates it\n"
        f"  to read {have.season}    set LOCKIN_LEAGUE_ID={have.league_id} and"
        f" LOCKIN_SEASON={have.season}"
    )


def check(
    conn: sqlite3.Connection,
    league_id: str,
    season: str,
    *,
    claim: bool = False,
    db_path: Path | None = None,
) -> Identity | None:
    """Refuse a configuration that does not match this database.

    ``claim`` is for `lockin ingest`, the one command that may bring a season
    into being: an empty database takes the identity it is given. Every other
    command asks a question about a season that must already be here, so an
    empty database returns None and the command fails later, on its own terms.

    A database from before this table existed adopts its identity from
    `league_settings` when that is unambiguous, which is how the 2025-26 file
    acquires one without a migration step anybody has to remember.
    """
    want = Identity(league_id, str(season))
    have = read(conn)
    if have is None:
        have = _legacy(conn)
        if have is None:
            if not claim:
                return None
            have = want
        elif have != want:
            raise _mismatch(have, want, db_path)
        conn.execute(
            "INSERT INTO db_identity (singleton, league_id, season, created_at)"
            " VALUES (1, ?, ?, ?)",
            (have.league_id, have.season, now_iso()),
        )
    if have != want:
        raise _mismatch(have, want, db_path)
    return have


def check_payload(league: dict, league_id: str, season: str) -> None:
    """Refuse a league payload that is not the league and season configured.

    Checked before anything is written. The case it catches is the rollover: a
    new `LOCKIN_LEAGUE_ID` with last season's `LOCKIN_SEASON` still set, or the
    reverse, which would file one league's rosters under another season's
    box scores.
    """
    got_id, got_season = str(league.get("league_id")), str(league.get("season"))
    if got_id != league_id:
        raise IdentityMismatch(
            f"asked Sleeper for league {league_id} and it returned league {got_id}"
        )
    if got_season != str(season):
        raise IdentityMismatch(
            f"league {league_id} is Sleeper's {got_season} league, but LOCKIN_SEASON is"
            f" {season}.\n\nSet LOCKIN_SEASON={got_season}, or LOCKIN_LEAGUE_ID to the"
            f" {season} league (day-one.md step 1 finds it)."
        )


def league_payload(conn: sqlite3.Connection) -> dict:
    """This database's own league payload.

    Replaces `SELECT ... FROM league_settings LIMIT 1`, which in a database
    holding two seasons would return whichever row SQLite happened to find
    first — and with it that season's scoring.
    """
    ident = read(conn) or _legacy(conn)
    if ident is None:
        raise RuntimeError("no league settings ingested; run `lockin ingest`")
    row = conn.execute(
        "SELECT payload_json FROM league_settings WHERE league_id = ? AND season = ?",
        (ident.league_id, ident.season),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"no league settings for league {ident.league_id} season {ident.season};"
            " run `lockin ingest`"
        )
    return json.loads(row["payload_json"])
