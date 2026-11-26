"""The home-board card: what it says, and that posting it can't hurt the digest.

Like the notification, this leaves the machine, so the success path is tested
against a stubbed transport rather than only the failure paths (see
test_notify.py for why that matters). No network is needed.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from lockin import board
from lockin.config import Config
from lockin.digest import Digest, LockCall, StandingRule, Warning

cfg = Config.from_env()
TONIGHT = datetime(2026, 10, 28).toordinal()


def a_digest(**overrides) -> Digest:
    values = dict(
        as_of="2026-10-28",
        week=2,
        roster_id=1,
        opponent_roster_id=2,
        known_through=TONIGHT - 1,
        banked={"1005": 36.0},
        p_win=0.61,
        my_total=260.0,
        opponent_total=250.0,
        margin={"q10": -30.0, "q50": 10.0, "q90": 50.0},
        state_source="ledger",
    )
    values.update(overrides)
    return Digest(**values)


def a_call(pid: str, lock: bool = True, **overrides) -> LockCall:
    values = dict(
        sleeper_id=pid,
        name=f"Player {pid}",
        day=TONIGHT - 1,
        score=41.0,
        lock=lock,
        p_win_lock=0.6,
        p_win_pass=0.5,
        break_even=38.0,
        expires_utc="2026-10-28T23:30:00Z",
    )
    values.update(overrides)
    return LockCall(**values)


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def posted(monkeypatch):
    """Capture the request instead of sending it."""
    captured: dict = {}

    def fake_urlopen(request, timeout=None, context=None):
        captured["request"] = request
        captured["timeout"] = timeout
        captured["context"] = context
        if "raise" in captured:
            raise captured["raise"]
        return FakeResponse(captured.get("status", 200))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LOCKIN_BOARD_URL", "https://127.0.0.1:8443/api/boards/desk/")
    monkeypatch.setenv("LOCKIN_BOARD_TOKEN", "s3cret-token")
    monkeypatch.delenv("LOCKIN_BOARD_CA", raising=False)
    return captured


# ------------------------------------------------------------------ opt in/out


def test_off_unless_a_url_is_configured(monkeypatch):
    monkeypatch.delenv("LOCKIN_BOARD_URL", raising=False)
    assert board.settings() is None
    assert board.send(a_digest(), cfg).startswith("disabled")


def test_a_replay_is_never_posted(posted):
    """A card on the wall has no 'historical replay' banner to warn anyone."""
    status = board.send(a_digest(retrospective=True, calls=[a_call("1")]), cfg)
    assert status.startswith("skipped")
    assert "request" not in posted


def test_a_url_without_a_token_fails_without_sending(posted, monkeypatch):
    monkeypatch.setenv("LOCKIN_BOARD_TOKEN", "")
    assert board.send(a_digest(), cfg).startswith("failed")
    assert "request" not in posted


# ------------------------------------------------------------------- transport


def test_the_card_is_put_to_one_fixed_id(posted):
    status = board.send(a_digest(calls=[a_call("1")]), cfg)
    request = posted["request"]
    assert request.get_method() == "PUT"
    assert request.full_url == "https://127.0.0.1:8443/api/boards/desk/cards/lockin-digest"
    assert request.get_header("Authorization") == "Bearer s3cret-token"
    assert json.loads(request.data)["title"].startswith("Lock-in · wk 2")
    assert status == "posted to 127.0.0.1:8443/api/boards/desk"
    assert "s3cret" not in status


def test_a_configured_certificate_is_the_trust_anchor(posted, monkeypatch, tmp_path):
    """The board's certificate is self-signed; without the CA file it can't verify."""
    import ssl

    seen = {}
    monkeypatch.setenv("LOCKIN_BOARD_CA", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(
        ssl, "create_default_context", lambda cafile=None: seen.setdefault("cafile", cafile)
    )
    board.send(a_digest(), cfg)
    assert seen["cafile"] == str(tmp_path / "cert.pem")
    assert posted["context"] == str(tmp_path / "cert.pem")


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO()),
        urllib.error.URLError("connection refused"),
        TimeoutError("timed out"),
        ValueError("anything at all"),
    ],
)
def test_nothing_gets_out(posted, error):
    """A board that is down must not fail the digest's cron run."""
    posted["raise"] = error
    status = board.send(a_digest(calls=[a_call("1")]), cfg)
    assert status.startswith("failed:")
    assert "s3cret" not in status


def test_a_non_2xx_status_is_a_failure(posted):
    posted["status"] = 500
    assert board.send(a_digest(), cfg) == "failed: HTTP 500"


# ------------------------------------------------------------------------ card


def test_calls_come_first_and_a_lock_raises_priority():
    card = board.card(
        a_digest(
            calls=[a_call("1", lock=False, name="Pass Guy"), a_call("2", name="Lock Guy")],
            rules=[StandingRule("3", "Rule Guy", TONIGHT + 2, 44.5, 0.31, 0, 2)],
            warnings=[Warning("4", "Hurt Guy", "q", "questionable tonight", "Q tonight")],
        )
    )
    assert card["priority"] == 1
    assert card["items"][0].startswith("pass Pass Guy 41.0 · score 38 or more · before ")
    assert card["items"][1].startswith("LOCK Lock Guy")
    assert card["items"][2].endswith("lock Rule Guy at 44.5+ (31%)")
    assert card["items"][3] == "Watch Hurt Guy — Q tonight"
    assert "P(win) 61%" in card["body"]
    assert "Record each lock on the advice page." in card["body"]
    assert card["ttl_minutes"] == board.TTL_MINUTES


def test_no_lock_call_means_normal_priority_and_no_record_line():
    card = board.card(a_digest(calls=[a_call("1", lock=False)]))
    assert card["priority"] == 0
    assert "advice page" not in card["body"]


def test_an_unrecorded_earlier_lock_is_called_out():
    card = board.card(a_digest(calls=[a_call("1", said_lock_on="2026-10-27T13:00:00+00:00")]))
    assert "said LOCK, not recorded" in card["items"][0]


def test_tonights_rule_says_tonight():
    card = board.card(a_digest(rules=[StandingRule("3", "R", TONIGHT, 30.5, float("nan"), 0, 1)]))
    assert card["items"] == ["Tonight: lock R at 30.5+"]


def test_an_abstaining_digest_shows_why_and_nothing_else():
    card = board.card(a_digest(note="insufficient history: 212 of 400 player-games"))
    assert card["body"] == "insufficient history: 212 of 400 player-games"
    assert card["items"] == []
    assert card["priority"] == 0


def test_the_card_fits_the_boards_limits():
    """The board rejects an oversized card outright (422), so it has to fit."""
    long_name = "Giannis Antetokounmpo-Extremely-Long-Hyphenated Surname Here"
    rules = [StandingRule(str(i), long_name, TONIGHT + 1, 40.0 + i, 0.5, 0, 1) for i in range(20)]
    card = board.card(a_digest(rules=rules, note=None, calls=[a_call("x", name=long_name)]))
    assert len(card["title"]) <= board.MAX_TITLE
    assert len(card["body"]) <= board.MAX_BODY
    assert len(card["items"]) == board.MAX_ITEMS
    assert all(len(i) <= board.MAX_ITEM for i in card["items"])
    assert card["items"][-1] == "+10 more in the digest"

    long_note = board.card(a_digest(note="x" * 2000))
    assert len(long_note["body"]) == board.MAX_BODY
