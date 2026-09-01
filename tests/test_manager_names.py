"""Display names, stored rather than fetched per render.

`lockin serve` holds a read-only connection and must not make network calls
inside a request handler, so the served dashboard could only ever label rows
"roster 3" while `lockin dashboard --names` showed real names. The names live
only on Sleeper's /league/{id}/users endpoint and no table carried them.

Now `lockin ingest` stores them and every reader goes to the table.
"""

from __future__ import annotations

import sqlite3

import pytest

from lockin import dashboard as dashboard_mod
from lockin.ingest.sleeper import ingest_users
from lockin.store.db import checkpoint, connect_readonly, session


class FakeClient:
    def __init__(self, users, rosters):
        self._by_url = {"users": users, "rosters": rosters}
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return self._by_url["users" if url.endswith("/users") else "rosters"]


USERS = [
    {"user_id": "u1", "display_name": "kloote"},
    {"user_id": "u2", "display_name": "rival"},
    {"user_id": "u9", "display_name": "commissioner-no-roster"},
]
ROSTERS = [
    {"owner_id": "u1", "roster_id": 1},
    {"owner_id": "u2", "roster_id": 2},
    {"owner_id": None, "roster_id": 3},
]


@pytest.fixture
def conn(tmp_path):
    with session(tmp_path / "t.db", create=True) as c:
        yield c


def test_names_are_stored_against_their_roster(conn):
    n = ingest_users(conn, FakeClient(USERS, ROSTERS), "L1")

    assert n == 2
    assert dashboard_mod.labels(conn) == {1: "kloote", 2: "rival"}


def test_a_member_without_a_roster_is_skipped(conn):
    """Leagues carry commissioners and co-owners who own no roster."""
    ingest_users(conn, FakeClient(USERS, ROSTERS), "L1")

    rows = conn.execute("SELECT roster_id FROM league_users ORDER BY roster_id").fetchall()
    assert [r["roster_id"] for r in rows] == [1, 2]


def test_a_rename_replaces_rather_than_accumulates(conn):
    """A display name is a live attribute, always refetchable — keep the newest."""
    ingest_users(conn, FakeClient(USERS, ROSTERS), "L1")
    renamed = [{"user_id": "u1", "display_name": "kloote-2"}, USERS[1]]
    ingest_users(conn, FakeClient(renamed, ROSTERS), "L1")

    assert dashboard_mod.labels(conn) == {1: "kloote-2", 2: "rival"}
    assert conn.execute("SELECT COUNT(*) c FROM league_users").fetchone()["c"] == 2


def test_labels_ignore_league_id(conn):
    """`--dashboard-db` points at last season, whose league id differs.

    Filtering on the current league would silently return nothing, and the page
    would fall back to roster numbers for the whole season. One database holds
    one season, so every row here is the right answer.
    """
    ingest_users(conn, FakeClient(USERS, ROSTERS), "last-seasons-league-id")

    assert dashboard_mod.labels(conn) == {1: "kloote", 2: "rival"}


def test_a_database_without_the_table_falls_back_quietly(tmp_path):
    """`connect_readonly` applies no schema; an older season file has no names."""
    old = tmp_path / "old.db"
    with session(old, create=True) as c:
        c.execute("SELECT 1")
    with sqlite3.connect(old) as raw:
        raw.execute("DROP TABLE league_users")

    ro = connect_readonly(old)
    try:
        assert dashboard_mod.labels(ro) == {}
    finally:
        ro.close()


def test_a_blank_name_does_not_become_a_label(conn):
    """Better a roster number than an empty cell where a name should be."""
    ingest_users(
        conn,
        FakeClient([{"user_id": "u1", "display_name": ""}, USERS[1]], ROSTERS),
        "L1",
    )

    assert dashboard_mod.labels(conn) == {2: "rival"}


def test_the_served_page_uses_them(conn, monkeypatch):
    """The whole point: no network call, and not a roster number in sight."""
    from lockin import serve

    ingest_users(conn, FakeClient(USERS, ROSTERS), "L1")
    for roster_id, regret in ((1, 1.5), (2, 2.5)):
        _scorecard(conn, roster_id, regret)
    checkpoint(conn)  # the page opens its own connection; it must see committed rows

    src = serve.Sources(advice_db=conn_path(conn), dashboard_db=conn_path(conn), roster_id=1)
    html = serve._dashboard_page(src)

    # The name leads; the roster id stays as a subordinate label, by design.
    assert "<td class=who>kloote<span class=rid>roster 1</span>" in html
    assert "<td class=who>rival<span class=rid>roster 2</span>" in html


def conn_path(conn) -> object:
    from pathlib import Path

    return Path(conn.execute("PRAGMA database_list").fetchone()["file"])


def _scorecard(conn, roster_id: int, regret: float) -> None:
    """A row satisfying every NOT NULL in `manager_scorecards`."""
    conn.execute(
        "INSERT INTO manager_scorecards (roster_id, decisions, squandered_share,"
        " mean_stake, mean_regret, right_rate, regret_lo, regret_hi, divergent,"
        " divergent_right_rate, upside_share, upside_decisions, rode_to_zero,"
        " computed_at) VALUES (?, 10, 0.1, 0.2, ?, 0.5, ?, ?, 3, 0.5, 0.4, 4, 1,"
        " '2026-09-01T00:00:00Z')",
        (roster_id, regret, regret - 0.5, regret + 0.5),
    )
