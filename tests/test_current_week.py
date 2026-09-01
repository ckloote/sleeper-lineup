"""Which week to ingest, asked of Sleeper rather than of the calendar.

deployment.md step 7 ran the nightly ingest as `--weeks $(date +\\%V)` — the ISO
calendar week. Fantasy weeks are 1-25 and nothing maps between them, so the job
was wrong every single day it ran, in two different ways:

    October     ISO 40-44   no such fantasy week; nothing ingested
    January     ISO 1-5     a real week, but October's — re-ingested each morning

Neither is loud. `--weeks` takes any integer, the daily availability capture runs
regardless of it, and the digest still finds box scores. The season would simply
have stopped advancing, and `lockin reconcile` would have been the only thing
that ever said so.

Sleeper publishes the answer in the league payload it already fetches.
"""

from __future__ import annotations

import pytest

from lockin.cli import _parse_weeks
from lockin.ingest.sleeper import current_weeks


def league(**settings) -> dict:
    return {"settings": settings}


def test_the_steady_state_is_one_week():
    """Mid-week, `leg` and `last_scored_leg` agree and there is nothing else to do."""
    assert current_weeks(league(leg=12, last_scored_leg=12)) == [12]


def test_a_week_boundary_returns_both():
    """`leg` rolls before the week just gone is finished being scored.

    Refetching only `leg` would leave week 12 frozen at whatever Monday's run
    saw, which for a Sunday night slate is most of a week of games.
    """
    assert current_weeks(league(leg=13, last_scored_leg=12)) == [12, 13]


def test_it_is_bounded_at_two_weeks():
    """This runs unattended every morning; its cost must not track the calendar."""
    for leg in range(1, 26):
        for scored in range(0, 26):
            assert len(current_weeks(league(leg=leg, last_scored_leg=scored))) <= 2


def test_weeks_outside_the_scored_range_are_dropped():
    """`last_scored_leg: 0` before the first week is settled, and 25 is unscored."""
    assert current_weeks(league(leg=1, last_scored_leg=0)) == [1]


def test_a_leg_outside_the_season_is_an_error_not_a_guess():
    """Preseason. Silently ingesting the wrong week is what this replaced."""
    with pytest.raises(ValueError, match="outside the 1-25"):
        current_weeks(league(leg=40, last_scored_leg=40))


def test_a_missing_leg_says_to_pass_weeks_explicitly():
    with pytest.raises(ValueError, match="--weeks"):
        current_weeks(league(last_scored_leg=12))
    with pytest.raises(ValueError, match="--weeks"):
        current_weeks({})


def test_a_complete_season_reports_its_last_week():
    """2025-26 as recorded: `leg` and `last_scored_leg` both stop at 24."""
    assert current_weeks(league(leg=24, last_scored_leg=24)) == [24]


def test_the_sentinel_is_not_parsed_as_a_number():
    """`_parse_weeks` cannot answer this one; `ingest` resolves it from the league."""
    with pytest.raises(ValueError, match="resolved from the league"):
        _parse_weeks("current")


def test_literal_week_lists_are_unchanged():
    assert _parse_weeks("12") == [12]
    assert _parse_weeks("12,13") == [12, 13]
    assert _parse_weeks("1-25") == list(range(1, 26))
    assert _parse_weeks(None) == list(range(1, 26))


def test_garbage_is_a_usage_error_not_a_traceback():
    """It runs from cron, which mails tracebacks."""
    import click

    with pytest.raises(click.BadParameter, match="is not a week list"):
        _parse_weeks("curent")
