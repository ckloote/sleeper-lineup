"""Put the digest on the home board: a wall tablet that shows short text cards.

The board is a separate service on the same Pi (`home-board`). It takes a card
over HTTPS with a bearer token and shows it full-screen. This module turns a
`Digest` into one card and posts it, so the morning's calls are on the wall
without opening the phone.

**Opt-in, like the notification.** With `LOCKIN_BOARD_URL` unset, :func:`send`
reports that and does nothing.

**Never fatal, for the same reason as `notify.send`.** By the time this runs the
digest has been printed, persisted and possibly pushed. A board that is down
must not turn a working morning into a cron failure mail.

**Live advice only.** A historical replay is not an instruction (README, "Every
morning"), and a card on the kitchen wall carries no "this is a replay" context
the way the terminal does. Replays are refused here, not just in the CLI.

**One card, replaced each morning, and it expires.** The card id is fixed, so
each digest overwrites yesterday's. It also carries a TTL: if the cron stops,
the card disappears overnight instead of showing stale calls as if they were
today's.

The card is a summary. The thresholds and deadlines match the digest, but the
answers are still recorded on the advice page, and the card says so whenever
there is a LOCK call to answer.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from lockin.config import Config
from lockin.digest import Digest, _short, deadline_label
from lockin.state import SOURCE_LEDGER

CARD_ID = "lockin-digest"
TIMEOUT_SECONDS = 10
TTL_MINUTES = 18 * 60
"""Long enough to cover a 09:00 digest through the last tip of the night, short
enough that a card from a digest that stopped running is gone by morning."""

# The board's limits (home-board app.py). Exceeding one is a 422 and no card,
# so the card is fitted here rather than trusted to be small.
MAX_TITLE = 80
MAX_BODY = 600
MAX_ITEMS = 12
MAX_ITEM = 120


@dataclass(frozen=True)
class Settings:
    url: str
    """The board's card collection, e.g. https://127.0.0.1:8443/api/boards/desk."""
    token: str
    ca_file: str | None
    """The board's self-signed certificate, used as the only trust anchor."""


def settings() -> Settings | None:
    """The configured board, or None if posting is off.

    The token is read from the environment for the same reason as the ntfy
    topic: it is a secret, and cron supplies it from a file only the user can
    read. It is never printed.
    """
    url = os.environ.get("LOCKIN_BOARD_URL", "").strip().rstrip("/")
    if not url:
        return None
    token = os.environ.get("LOCKIN_BOARD_TOKEN", "").strip()
    ca_file = os.environ.get("LOCKIN_BOARD_CA", "").strip() or None
    return Settings(url=url, token=token, ca_file=ca_file)


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def card(digest: Digest) -> dict:
    """The digest as one board card. Pure; no I/O.

    Same order as `digest.render`: what expires before tonight's tip first,
    then the standing rules, then what to watch. P(win) and the banked total
    are context, so they go in the body under the title rather than first in
    the list.
    """
    day = datetime.fromordinal(digest.as_of_day).strftime("%a %-d %b")
    title = _fit(f"Lock-in · wk {digest.week} · {day}", MAX_TITLE)

    if digest.note:
        return {
            "title": title,
            "body": _fit(digest.note, MAX_BODY),
            "items": [],
            "priority": 0,
            "ttl_minutes": TTL_MINUTES,
        }

    items: list[str] = []
    calls = sorted(digest.calls, key=lambda c: (c.expires_utc is None, c.expires_utc or ""))
    for call in calls:
        verb = "LOCK" if call.lock else "pass"
        need = "ride" if not np.isfinite(call.break_even) else f"score {call.break_even:g} or more"
        when = f" · before {deadline_label(call.expires_utc)} tip" if call.expires_utc else ""
        line = f"{verb} {_short(call.name, 20)} {call.score:.1f} · {need}{when}"
        if call.said_lock_on and digest.state_source == SOURCE_LEDGER:
            said = datetime.fromisoformat(call.said_lock_on).strftime("%a")
            line += f" · {said} said LOCK, not recorded"
        items.append(line)

    for rule in sorted(digest.rules, key=lambda r: (r.night, -r.threshold)):
        when = (
            "Tonight"
            if rule.night == digest.as_of_day
            else datetime.fromordinal(rule.night).strftime("%a %-d")
        )
        chance = "" if np.isnan(rule.p_clear) else f" ({rule.p_clear:.0%})"
        items.append(f"{when}: lock {_short(rule.name, 20)} at {rule.threshold:g}+{chance}")

    for warn in digest.warnings:
        items.append(f"Watch {_short(warn.name, 20)} — {warn.short}")

    items = [_fit(i, MAX_ITEM) for i in items]
    if len(items) > MAX_ITEMS:
        hidden = len(items) - (MAX_ITEMS - 1)
        items = items[: MAX_ITEMS - 1] + [f"+{hidden} more in the digest"]

    parts = []
    if digest.p_win is not None:
        parts.append(f"P(win) {digest.p_win:.0%}")
    parts.append(f"banked {sum(digest.banked.values()):.1f} across {len(digest.banked)} of 6")
    if digest.my_total is not None and digest.opponent_total is not None:
        parts.append(f"projected {digest.my_total:.0f} v {digest.opponent_total:.0f}")
    body = " · ".join(parts)
    if any(c.lock for c in digest.calls):
        body += "\nRecord each lock on the advice page."
    if not digest.calls and not digest.rules:
        body += "\nNothing to decide today."

    return {
        "title": title,
        "body": _fit(body, MAX_BODY),
        "items": items,
        "priority": 1 if any(c.lock for c in digest.calls) else 0,
        "ttl_minutes": TTL_MINUTES,
    }


def _where(url: str) -> str:
    """Host and board for the log line. The URL holds no secret; the token is
    in a header and is never echoed."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def send(digest: Digest, cfg: Config) -> str:
    """Post the card. Returns a status line for the CLI, never raises."""
    conf = settings()
    if conf is None:
        return "disabled (set LOCKIN_BOARD_URL to enable)"
    if digest.retrospective:
        return "skipped: historical replay, not live advice"
    if not conf.token:
        return "failed: LOCKIN_BOARD_URL is set but LOCKIN_BOARD_TOKEN is empty"

    try:
        payload = json.dumps(card(digest)).encode()
        request = urllib.request.Request(
            f"{conf.url}/cards/{CARD_ID}",
            data=payload,
            method="PUT",
            headers={
                "Authorization": f"Bearer {conf.token}",
                "Content-Type": "application/json",
            },
        )
        context = ssl.create_default_context(cafile=conf.ca_file) if conf.ca_file else None
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as resp:
            if 200 <= resp.status < 300:
                return f"posted to {_where(conf.url)}"
            return f"failed: HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        # notify.send's contract, for notify.send's reason: nothing gets out.
        # urllib's HTTPError (a 401, 403, 422 or 429 from the board) lands here
        # too, and its message names the status.
        return f"failed: {type(exc).__name__}: {exc}"
