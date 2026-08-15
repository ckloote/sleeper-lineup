"""Push the digest to a phone.

ntfy, per §8's decision: no account, no API key, no SDK — a topic name is the
whole configuration, and publishing is an HTTP POST. That matters for a job that
runs from cron on a Raspberry Pi, where the failure mode to design against is a
credential that expired eight months ago and a digest nobody noticed stopping.

**Opt-in.** With no topic configured :func:`send` reports that and does nothing.
A digest that silently posted to a guessable public topic would be publishing
the user's lineup to anyone who subscribed to it, and ntfy topics are public by
default — the topic name *is* the secret.

**Never fatal.** A failed notification must not fail the digest. The text has
already been printed and written to `recommendations` by the time this runs, so
an exception here would lose nothing but would make cron send a failure mail
every morning, which is how a working system gets switched off.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

from lockin.config import Config

DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT_SECONDS = 10


def topic() -> str | None:
    """The configured ntfy topic, or None if notifications are off.

    Read from the environment rather than stored, because it is a shared secret:
    anyone who knows the topic can read the digest, and anyone who guesses it can
    write to it. Cron supplies it from a file only the user can read.
    """
    value = os.environ.get("LOCKIN_NTFY_TOPIC", "").strip()
    return value or None


def send(body: str, cfg: Config, *, title: str = "Lock-in digest") -> str:
    """Publish the digest. Returns a human-readable outcome, never raises.

    The return value is a status line for the CLI to print, not a boolean:
    "disabled", "sent" and "failed: ..." are three different things the user
    needs to be able to tell apart at a glance in a cron log.
    """
    name = topic()
    if name is None:
        return "disabled (set LOCKIN_NTFY_TOPIC to enable)"

    server = os.environ.get("LOCKIN_NTFY_SERVER", DEFAULT_SERVER).rstrip("/")
    request = urllib.request.Request(
        f"{server}/{name}",
        data=body.encode(),
        method="POST",
        headers={
            "Title": title,
            # Markdown would render the threshold tables as prose. Plain text
            # keeps the column alignment the digest was laid out for.
            "Content-Type": "text/plain; charset=utf-8",
            "Priority": "default",
            "Tags": "basketball",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if 200 <= response.status < 300:
                return f"sent to {server}/{name}"
            return f"failed: HTTP {response.status}"
    except urllib.error.URLError as exc:
        return f"failed: {exc.reason}"
    except OSError as exc:  # DNS, TLS, socket — all non-fatal here
        return f"failed: {exc}"
