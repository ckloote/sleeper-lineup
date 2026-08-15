"""The push notification, which is the digest's actual delivery mechanism.

Everything else in this project produces a number; this is the only part that
has to leave the machine. Until 2026-08-15 it was tested only on its two failure
paths — "disabled" and "failed" — so the success path had never executed once,
and the first time it would have run for real was on a Raspberry Pi at 9am.

Testing it properly found a bug that the negative tests could not have: a
non-ASCII title raised `UnicodeEncodeError` out of `send`, breaking the "never
fatal" contract in the one function whose entire job is to protect a cron run.
`UnicodeEncodeError` is a `ValueError`, and the guard caught `URLError` and
`OSError`.

The unit tests here stub the transport so CI needs no network. The live
round-trip against ntfy.sh is opt-in — set `LOCKIN_LIVE_NTFY=1` — because a test
that silently depends on a third-party service is a test that fails for reasons
that have nothing to do with the code.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from email.header import decode_header

import pytest

from lockin import notify
from lockin.config import Config

cfg = Config.from_env()

BODY = "LAST NIGHT (Wed) — do this now\n  margin −54 / +4\n  assumes idle (§7.2)"
"""The characters the real digest actually contains. An em dash, a minus sign
and a section sign — none of them latin-1-safe in a header, all of them fine in
a UTF-8 body, which is exactly the distinction that broke."""


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch):
    """Capture the request instead of transmitting it."""
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(captured.get("status", 200))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LOCKIN_NTFY_TOPIC", "unit-test-topic")
    monkeypatch.delenv("LOCKIN_NTFY_SERVER", raising=False)
    return captured


# ------------------------------------------------------------------ opt in/out


def test_notifications_are_off_unless_a_topic_is_configured(monkeypatch):
    """An ntfy topic is public. Defaulting one on would publish the lineup."""
    monkeypatch.delenv("LOCKIN_NTFY_TOPIC", raising=False)
    assert notify.topic() is None
    assert notify.send("body", cfg).startswith("disabled")


def test_a_blank_topic_counts_as_off(monkeypatch):
    """`LOCKIN_NTFY_TOPIC=` in a cron file must not post to `https://ntfy.sh/`."""
    monkeypatch.setenv("LOCKIN_NTFY_TOPIC", "   ")
    assert notify.topic() is None
    assert notify.send("body", cfg).startswith("disabled")


# --------------------------------------------------------------- success path


def test_a_2xx_reports_sent_and_says_where(sent):
    result = notify.send(BODY, cfg)
    assert result == "sent to https://ntfy.sh/unit-test-topic"


def test_the_request_is_shaped_the_way_ntfy_expects(sent):
    notify.send(BODY, cfg)
    request = sent["request"]
    assert request.full_url == "https://ntfy.sh/unit-test-topic"
    assert request.get_method() == "POST"
    assert request.data == BODY.encode("utf-8")
    assert request.get_header("Content-type") == "text/plain; charset=utf-8"
    assert sent["timeout"] == notify.TIMEOUT_SECONDS


def test_the_body_carries_utf_8_not_a_mangled_approximation(sent):
    """Verified against the real service: the body survives verbatim."""
    notify.send(BODY, cfg)
    assert sent["request"].data.decode("utf-8") == BODY


def test_a_self_hosted_server_is_honoured_without_a_double_slash(monkeypatch, sent):
    monkeypatch.setenv("LOCKIN_NTFY_SERVER", "http://pi.local:8080/")
    assert notify.send(BODY, cfg) == "sent to http://pi.local:8080/unit-test-topic"
    assert sent["request"].full_url == "http://pi.local:8080/unit-test-topic"


@pytest.mark.parametrize("status", [301, 400, 403, 500])
def test_a_non_2xx_is_reported_as_a_failure(sent, status):
    sent["status"] = status
    assert notify.send(BODY, cfg) == f"failed: HTTP {status}"


# ------------------------------------------------------- the header encoding bug


def test_a_non_ascii_title_does_not_raise(sent):
    """The bug. `http.client` encodes headers as latin-1 and an em dash is not.

    Before the fix this raised `UnicodeEncodeError` — a `ValueError`, so it slid
    past guards that named `URLError` and `OSError` — and took the digest down
    with it.
    """
    result = notify.send(BODY, cfg, title="Lock-in — digest §20")
    assert result.startswith("sent")


def test_a_non_ascii_title_arrives_intact_via_rfc_2047(sent):
    """Encoded, not stripped. ntfy decodes it back; verified against ntfy.sh."""
    title = "Lock-in — digest §20"
    notify.send(BODY, cfg, title=title)
    header = sent["request"].get_header("Title")
    assert header.isascii(), "the whole point is that the wire value is ASCII"
    decoded, charset = decode_header(header)[0]
    assert decoded.decode(charset) == title


def test_an_ascii_title_is_left_alone(sent):
    """The common case stays legible in a header dump rather than base64."""
    notify.send(BODY, cfg, title="Lock-in digest")
    assert sent["request"].get_header("Title") == "Lock-in digest"


# ------------------------------------------------------------- the contract


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("no route to host"),
        OSError("socket blew up"),
        UnicodeEncodeError("latin-1", "—", 0, 1, "not in range"),
        RuntimeError("something nobody predicted"),
        TimeoutError("too slow"),
    ],
)
def test_nothing_escapes_send(monkeypatch, error):
    """The module's contract, tested against the kinds of failure that exist.

    Enumerating exception types is what caused the bug this file documents, so
    the test enumerates instead and the implementation does not.
    """
    monkeypatch.setenv("LOCKIN_NTFY_TOPIC", "unit-test-topic")

    def boom(request, timeout=None):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    result = notify.send(BODY, cfg)
    assert result.startswith("failed: ")
    assert type(error).__name__ in result


def test_a_failure_names_the_exception_type(monkeypatch):
    """A cron log reading "failed: gaierror" is diagnosable; "failed:" is not."""
    monkeypatch.setenv("LOCKIN_NTFY_TOPIC", "unit-test-topic")
    monkeypatch.setenv("LOCKIN_NTFY_SERVER", "http://127.0.0.1:1")
    result = notify.send(BODY, cfg)
    assert result.startswith("failed: URLError")


# ------------------------------------------------------------------- live path


@pytest.mark.skipif(
    os.environ.get("LOCKIN_LIVE_NTFY") != "1",
    reason="set LOCKIN_LIVE_NTFY=1 to round-trip against ntfy.sh",
)
def test_live_round_trip_against_ntfy():
    """Publish to a random throwaway topic and read it back off the server.

    The only test here that proves delivery rather than intent. Opt-in, because
    it needs the network and a third party; random topic, because ntfy topics
    are public and this must not collide with anything real.
    """
    topic = "lockin-test-" + secrets.token_hex(8)
    os.environ["LOCKIN_NTFY_TOPIC"] = topic
    title = "Lock-in — test §20"
    try:
        assert notify.send(BODY, cfg, title=title).startswith("sent")
        time.sleep(2)
        url = f"https://ntfy.sh/{topic}/json?poll=1"
        with urllib.request.urlopen(url, timeout=30) as response:
            lines = [
                json.loads(line) for line in response.read().decode().splitlines() if line.strip()
            ]
    finally:
        os.environ.pop("LOCKIN_NTFY_TOPIC", None)

    messages = [m for m in lines if m.get("event") == "message"]
    assert len(messages) == 1
    assert messages[0]["message"] == BODY
    assert messages[0]["title"] == title
