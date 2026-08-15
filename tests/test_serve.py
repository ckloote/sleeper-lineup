"""The HTTP server, which is the only part of this project that listens.

Everything else is a command you run. This accepts requests from the network, so
the tests are about what it refuses as much as what it returns.

Two properties carry the design:

`test_a_new_digest_shows_up_without_restarting` proves the pages are rendered
per request rather than served from disk. That is what makes a served page
incapable of being older than the data behind it — the staleness failure this
area has produced twice already.

The `test_no_path_reaches_the_filesystem` family proves the thing that motivated
writing a server at all. `python -m http.server` pointed at this project would
publish `data/lockin.db` — the entire season — with directory listing on. This
one holds no document root, so there is no path handling to get wrong.
"""

from __future__ import annotations

import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from lockin import digest as digest_mod
from lockin import serve
from lockin.config import Config
from lockin.store.db import apply_schema, connect_readonly, session

cfg = Config.from_env()
pytestmark = pytest.mark.skipif(
    not cfg.db_path.exists(), reason=f"no database at {cfg.db_path}; run `lockin ingest`"
)

AS_OF = "2026-01-08"
BANKED = {"1000": 46.0, "1787": 47.5}


@pytest.fixture(scope="module")
def report():
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    try:
        ctx = digest_mod.load_context(conn, cfg.season)
        roster_id = digest_mod.roster_for_user(conn, cfg.user_id)
        return digest_mod.build(ctx, roster_id, AS_OF, n_sims=150, n_paths=150, locked=dict(BANKED))
    finally:
        conn.close()


@pytest.fixture
def served(tmp_path, report) -> Iterator[tuple[str, object]]:
    """A real server on a real ephemeral port, torn down afterwards.

    Port 0 lets the OS choose, so parallel runs cannot collide — a fixed port
    would make this test suite fail for reasons unrelated to the code.
    """
    db = tmp_path / "serve.db"
    with session(db) as conn:
        digest_mod.persist(conn, report, state_supplied=True)
        conn.executemany(
            "INSERT OR REPLACE INTO players"
            " (sleeper_id, full_name, positions, updated_at) VALUES (?, ?, '[]', 't')",
            list(report.names.items()),
        )

    httpd = serve.build_server(db, report.roster_id, host="127.0.0.1", port=0, quiet=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, db
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def fetch(url: str) -> tuple[int, str, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.status, exc.read().decode(), dict(exc.headers)


# ------------------------------------------------------------------- the routes


def test_the_advice_page_is_the_root(served):
    base, _ = served
    status, body, headers = fetch(f"{base}/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "What to do" in body


def test_the_dashboard_is_its_own_route(served):
    base, _ = served
    status, body, _ = fetch(f"{base}/dashboard")
    assert status == 200
    # Empty scorecards in a scratch database still render an instruction.
    assert "lockin managers" in body or "Who decided well" in body


def test_a_trailing_slash_is_the_same_page(served):
    base, _ = served
    assert fetch(f"{base}/dashboard/")[0] == 200


def test_a_query_string_is_ignored_rather_than_confusing_the_router(served):
    base, _ = served
    status, body, _ = fetch(f"{base}/?refresh=1")
    assert status == 200
    assert "What to do" in body


def test_an_unknown_route_offers_the_two_that_exist(served):
    base, _ = served
    status, body, _ = fetch(f"{base}/nope")
    assert status == 404
    assert 'href="/"' in body and 'href="/dashboard"' in body


def test_pages_are_not_cached(served):
    """A cached copy of a stale digest is exactly what this must not serve."""
    base, _ = served
    _, _, headers = fetch(f"{base}/")
    assert headers["Cache-Control"] == "no-store"


# --------------------------------------------------------- nothing reaches disk


@pytest.mark.parametrize(
    "path",
    [
        "/data/lockin.db",
        "/../data/lockin.db",
        "/..%2f..%2fdata/lockin.db",
        "/advice.html",
        "/dashboard.html",
        "/lockin/config.py",
        "/README.md",
        "/.git/config",
    ],
)
def test_no_path_reaches_the_filesystem(served, path):
    """There is no document root, so every one of these is simply unrouted.

    `python -m http.server` in this project's directory would return the
    database for the first of these, which is why this server exists.
    """
    status, body, _ = fetch(f"{served[0]}{path}")
    assert status == 404
    assert "SQLite" not in body
    assert "\x00" not in body


# ------------------------------------------------------------------- freshness


def test_a_new_digest_shows_up_without_restarting(served, report):
    """Rendered per request. The property that makes a served page unstaleable.

    Serving `advice.html` from disk would need someone to remember to
    regenerate it; rendering on each request removes the step and therefore the
    failure. `advice` is a reader — two queries — so this costs nothing.
    """
    base, db = served
    before = fetch(f"{base}/")[1]
    assert "54%" in before or "%" in before

    with session(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO digest_runs"
            " (generated_at, roster_id, as_of, week, p_win, state_supplied)"
            " VALUES ('2099-01-01T00:00:00+00:00', ?, ?, ?, 0.07, 1)",
            (report.roster_id, AS_OF, report.week),
        )

    after = fetch(f"{base}/")[1]
    assert after != before
    assert "7%" in after


# ---------------------------------------------------------------- read-only db


def test_the_server_connection_cannot_write(tmp_path, report):
    """Enforced by SQLite, not by the handlers being careful."""
    db = tmp_path / "ro.db"
    with session(db) as conn:
        digest_mod.persist(conn, report, state_supplied=True)

    conn = connect_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM recommendations")
    finally:
        conn.close()


def test_a_render_failure_returns_no_traceback(served, monkeypatch):
    """A stack trace in an HTTP response describes the filesystem to a stranger."""
    base, _ = served

    def explode(db_path, roster_id):
        raise RuntimeError("/home/secret/path blew up")

    monkeypatch.setitem(serve.ROUTES, "/", explode)
    status, body, _ = fetch(f"{base}/")
    assert status == 500
    assert "secret" not in body
    assert "Traceback" not in body
    assert "Failed to render" in body


# ------------------------------------------------------------------ addresses


def test_reachable_addresses_always_includes_loopback():
    urls = serve.reachable_addresses(8080)
    assert "http://127.0.0.1:8080" in urls
    assert len(set(urls)) == len(urls), "duplicates would be noise at startup"
