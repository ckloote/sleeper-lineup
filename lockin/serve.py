"""Serve the pages over HTTP, so a phone can read them.

Two routes, rendered from the database on each request, and one route that
writes: the advice page's Locked / Passed buttons (`POST /ledger`).

**It never touches the filesystem**, which is the point of writing this rather
than reaching for `python -m http.server`. Pointed at this project's directory
that would publish `data/lockin.db` — the whole season, 27MB — plus `snapshots/`
and the source, with directory listing on. A server that cannot open a file
cannot be talked into opening the wrong one, so this one holds no document root
and has no path handling to get wrong.

**Rendered per request, not served from disk.** `lockin.advice` is a reader: two
SQL queries and some string formatting, no Monte Carlo. Regenerating on every
request is therefore free, and it removes the failure this whole area keeps
producing — a page that is quietly older than the data behind it. The cron still
writes `advice.html` for anyone who wants a file, but the served copy cannot go
stale relative to the last digest.

**Read-only against SQLite, except one insert.** The pages render on a
connection SQLite opens read-only, so a bug in a renderer cannot corrupt the
season. The ledger route is the only writer, and all it can do is append one
answer to `lock_ledger` (`lockin.ledger`): it never creates a table, and it
records an answer only to a call the database says this roster's live digest
made this week. WAL means both coexist with the ingest writing at the same
moment.

The ledger exists because Sleeper's matchup poll cannot show a lock, so the
manager's own answer is the only record of what is banked, and the page is where
he answers.

**On exposure.** The default binds all interfaces, because "reachable from my
phone" is the entire reason this exists and a loopback default would be a
command that does nothing. That means the LAN, and it means Tailscale if the
interface is up — which is the intended way to reach it from outside the house.
It also means anyone who can route to the host can read your lineup, and record
an answer to one of this week's calls. There is no authentication here and adding
a password field would only imply more safety than it delivers; the right
boundary is the network, so do not port-forward it. What the write route does
check is what a hostile *page* could exploit from your own phone's browser: a
form posted from another site is refused by its Origin, and the body is small,
form-encoded, and validated against the database before anything is written. The
command prints what it is reachable on, every time, rather than leaving that to
be discovered.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from lockin import advice as advice_mod
from lockin import dashboard as dashboard_mod
from lockin import ledger as ledger_mod
from lockin.store.db import connect, connect_readonly

PORT = 8080

NOT_FOUND = """<!doctype html>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Lock-in</title>
<style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:34rem;
margin:3rem auto;padding:0 1rem;color-scheme:light dark}a{display:block;margin:.4rem 0}</style>
<h1>Lock-in</h1>
<p>Two pages:</p>
<a href="/">What to do &mdash; tonight's calls and standing rules</a>
<a href="/dashboard">Who decided well &mdash; last season</a>
"""


@dataclass(frozen=True, slots=True)
class Sources:
    """Where each page reads from. Two databases, on purpose.

    Seasons cannot share a database — `weekly_matchups` has no season column, so
    the newer one silently hides the older (day-one.md step 2). But manager
    scorecards are *retrospective*: they describe a season that has finished, so
    in-season the only ones worth showing come from last year's database while
    tonight's advice comes from this year's.

    Without this the served dashboard reads "No scorecards yet" for an entire
    season, which is true and useless.
    """

    advice_db: Path
    dashboard_db: Path
    roster_id: int


def _advice_page(src: Sources) -> str:
    conn = connect_readonly(src.advice_db)
    try:
        return advice_mod.render(advice_mod.latest_run(conn, src.roster_id), interactive=True)
    finally:
        conn.close()


MAX_BODY = 1024
"""Bytes. Four short form fields; anything bigger is not from the page."""

FIELDS = ("run_id", "sleeper_id", "game_day", "decision")


class Refused(Exception):
    """A request the ledger route will not act on, with the status to send."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def same_origin(headers, host: str | None) -> bool:
    """Whether a POST came from a page this server served.

    Browsers send `Origin` on a form POST, and `Sec-Fetch-Site` where they
    support it. A form on another site, opened in the same phone browser that
    can reach this host, sends its own origin and is refused. With neither
    header (a script, not a browser) the `Referer` must match instead.
    """
    if not host:
        return False
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = headers.get("Origin") or headers.get("Referer")
    if not origin or origin == "null":
        return False
    return urlsplit(origin).netloc == host


def parse_answer(body: bytes) -> dict[str, str]:
    try:
        form = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(400, "malformed form") from exc
    if set(form) != set(FIELDS) or any(len(v) != 1 for v in form.values()):
        raise Refused(400, "expected exactly run_id, sleeper_id, game_day and decision")
    out = {k: v[0] for k, v in form.items()}
    if out["decision"] not in ledger_mod.DECISIONS:
        raise Refused(400, "decision must be LOCK, PASS or UNDO")
    if not out["game_day"].isdigit():
        raise Refused(400, "game_day must be a day number")
    return out


def record_answer(src: Sources, answer: dict[str, str]) -> None:
    """Append one answer, if the database says it answers an open call."""
    conn = connect(src.advice_db, create=False)
    try:
        if not ledger_mod.has_table(conn):
            # Creating it here would make this a server that creates tables.
            raise Refused(503, "no lock_ledger table yet; run `lockin digest` once")
        try:
            week = ledger_mod.open_call(
                conn, src.roster_id, answer["run_id"], answer["sleeper_id"], int(answer["game_day"])
            )
        except ledger_mod.NotACall as exc:
            raise Refused(400, str(exc)) from None
        ledger_mod.record(
            conn,
            week=week,
            roster_id=src.roster_id,
            sleeper_id=answer["sleeper_id"],
            game_day=int(answer["game_day"]),
            decision=answer["decision"],
            run_id=answer["run_id"],
            source="page",
        )
    finally:
        conn.close()


def _dashboard_page(src: Sources) -> str:
    conn = connect_readonly(src.dashboard_db)
    try:
        rows = dashboard_mod.load(conn, dashboard_mod.labels(conn))
        return dashboard_mod.render(rows, stamp=dashboard_mod.computed_at(conn))
    finally:
        conn.close()


ROUTES: dict[str, Callable[[Sources], str]] = {
    "/": _advice_page,
    "/dashboard": _dashboard_page,
}


class Handler(BaseHTTPRequestHandler):
    """Route-table only. No path is ever turned into a filename."""

    sources: Sources
    quiet: bool = False

    server_version = "lockin"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        render = ROUTES.get(route)
        if render is None:
            self._respond(404, NOT_FOUND)
            return
        try:
            body = render(self.sources)
        except Exception as exc:  # noqa: BLE001
            # A traceback in an HTTP response tells whoever is reading it the
            # filesystem layout. Log it locally, return something bland.
            self.log_error("%s rendering %s: %s", type(exc).__name__, route, exc)
            self._respond(500, "<!doctype html><meta charset=utf-8><p>Failed to render.")
            return
        self._respond(200, body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route != advice_mod.LEDGER_ROUTE:
            self._respond(404, NOT_FOUND)
            return
        try:
            self._answer()
        except Refused as exc:
            self.log_error("ledger refused (%d): %s", exc.status, exc)
            self._respond(exc.status, f"<!doctype html><meta charset=utf-8><p>Not recorded: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self.log_error("%s recording an answer: %s", type(exc).__name__, exc)
            self._respond(500, "<!doctype html><meta charset=utf-8><p>Failed to record.")
            return
        # Post/redirect/get: back to the page, so a refresh cannot post twice.
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _answer(self) -> None:
        if not same_origin(self.headers, self.headers.get("Host")):
            raise Refused(403, "not posted from this server's page")
        kind = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if kind != "application/x-www-form-urlencoded":
            raise Refused(415, "expected a form")
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise Refused(411, "Content-Length required") from None
        if length < 0 or length > MAX_BODY:
            raise Refused(413, "too large")
        record_answer(self.sources, parse_answer(self.rfile.read(length)))

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # The whole point is freshness; a cached copy of a stale digest is the
        # failure this page exists to prevent.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        if not self.quiet:
            super().log_message(fmt, *args)


def build_server(
    db_path: Path,
    roster_id: int,
    *,
    dashboard_db: Path | None = None,
    host: str = "0.0.0.0",
    port: int = PORT,
    quiet: bool = False,
) -> ThreadingHTTPServer:
    """A server bound and ready, not yet running.

    Returned rather than started so the caller decides — the CLI calls
    `serve_forever`, the tests run it in a thread and shut it down.

    ``dashboard_db`` defaults to ``db_path``, which is right out of season and
    wrong during one: see :class:`Sources`.
    """
    sources = Sources(
        advice_db=db_path,
        dashboard_db=dashboard_db or db_path,
        roster_id=roster_id,
    )
    bound = type("BoundHandler", (Handler,), {"sources": sources, "quiet": quiet})
    return ThreadingHTTPServer((host, port), bound)


def reachable_addresses(port: int) -> list[str]:
    """URLs this host is likely reachable on, for printing at startup.

    Best effort and explicitly so: enumerating interfaces portably needs a
    dependency this project will not take for one line of output. A Tailscale
    address shows up here when the interface is up, which is the case worth
    seeing.
    """
    urls = [f"http://127.0.0.1:{port}"]
    try:
        hostname = socket.gethostname()
        _, _, addresses = socket.gethostbyname_ex(hostname)
        urls += [f"http://{a}:{port}" for a in addresses if not a.startswith("127.")]
        urls.append(f"http://{hostname}:{port}")
    except OSError:
        pass
    return list(dict.fromkeys(urls))
