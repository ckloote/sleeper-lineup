"""Serve the pages over HTTP, so a phone can read them.

Two routes, rendered from the database on each request. Nothing else exists.

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

**Read-only against SQLite.** Enforced by the connection, not by care, so a bug
in a handler cannot corrupt the season. WAL means it coexists with the ingest
writing at the same moment.

**On exposure.** The default binds all interfaces, because "reachable from my
phone" is the entire reason this exists and a loopback default would be a
command that does nothing. That means the LAN, and it means Tailscale if the
interface is up — which is the intended way to reach it from outside the house.
It also means anyone who can route to the host can read your lineup. There is no
authentication here and adding a password field would only imply more safety than
it delivers; the right boundary is the network, so do not port-forward it. The
command prints what it is reachable on, every time, rather than leaving that to
be discovered.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lockin import advice as advice_mod
from lockin import dashboard as dashboard_mod
from lockin.store.db import connect_readonly

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
        return advice_mod.render(advice_mod.latest_run(conn, src.roster_id))
    finally:
        conn.close()


def _dashboard_page(src: Sources) -> str:
    conn = connect_readonly(src.dashboard_db)
    try:
        rows = dashboard_mod.load(conn)
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
