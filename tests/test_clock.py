"""What day it is, which had three different answers until 2026-08-15.

The CLI used `date.today()` (the machine's), `advice` used
`datetime.now(UTC).date()`. On a Pi in US Eastern those disagree every evening
after 7pm, so a digest generated that morning was labelled a day old the moment
the page was opened after dinner.

The right basis is neither: NBA game dates are US Eastern, and 77% of the
season's games tip on a different UTC date than the one they are filed under.
`LOCKIN_TZ` names the schedule's timezone, not the operator's.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lockin import clock
from lockin.config import Config


def test_the_default_is_the_schedules_timezone_not_the_machines():
    """`America/New_York` is a fact about the NBA, not a user preference."""
    assert clock.DEFAULT_TZ == "America/New_York"
    assert Config.from_env().timezone


def test_an_eastern_evening_is_not_yet_the_next_day(monkeypatch):
    """The bug, stated as a test.

    20:00 in New York is 01:00 UTC tomorrow. A digest generated that morning is
    hours old, not a day, and the page must not tell you to re-run it.
    """
    monkeypatch.delenv("LOCKIN_TZ", raising=False)
    evening = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)  # 2026-11-02 20:00 EST
    assert evening.date().isoformat() == "2026-11-03"
    assert evening.astimezone(clock.zone()).date().isoformat() == "2026-11-02"


def test_the_timezone_is_configurable_but_not_the_machines_business(monkeypatch):
    """A Pi that moves keeps working; a misconfigured clock cannot shift a day."""
    monkeypatch.setenv("LOCKIN_TZ", "Europe/London")
    assert str(clock.zone()) == "Europe/London"
    monkeypatch.setenv("LOCKIN_TZ", "America/New_York")
    assert str(clock.zone()) == "America/New_York"


@pytest.mark.parametrize(
    ("hour_utc", "expected"),
    [
        (3, True),  # late games still running
        (6, True),  # the last of them finishing
        (7, False),  # the cutoff
        (14, False),  # 9am US Eastern, the intended cron slot
    ],
)
def test_too_early_for_guards_an_unfinished_slate(hour_utc, expected):
    """Reading a half-played night as final is a leak from the other direction.

    Latest tipoffs are around 04:00 UTC; a digest before ~07:00 UTC would treat
    in-progress box scores as complete.
    """
    moment = datetime(2026, 11, 3, hour_utc, 0, tzinfo=UTC)
    assert clock.too_early_for("2026-11-03", moment) is expected


def test_a_past_date_is_never_too_early():
    """The as-of and backtest case: those slates finished months ago."""
    assert clock.too_early_for("2026-01-08") is False


def test_nine_am_eastern_clears_the_cutoff_in_both_daylight_regimes():
    """The season spans a DST change, so check the tighter side too.

    9am EDT is 13:00 UTC, 9am EST is 14:00 UTC. Both are comfortably past the
    07:00 cutoff — which is the reason a US Eastern Pi needs no special handling
    and somewhere east of UTC+2 would.
    """
    for hour_utc in (13, 14):
        moment = datetime(2026, 11, 3, hour_utc, 0, tzinfo=UTC)
        assert clock.too_early_for("2026-11-03", moment) is False
