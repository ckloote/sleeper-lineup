"""A week the commissioner zeroed is not evidence about who gets started.

Sleeper cannot start a league after the NBA's first fantasy week. The 2026-27
league drafted on 10-25, the last day of week 1, so week 1 was played with
lineups filled after most of its games, then zeroed: every team's
`custom_points` set to 0. Those lineups chose players already known to have
played — week 1's starters sat 1.2% of their games, the bench 20.2% — and fed
into the started-player correction from week 3 on, they would make every
starter look safer than he is.
"""

from datetime import timedelta
from unittest.mock import patch

import numpy as np
import pytest
from live_fixture import OPENING, SCALED, SyntheticSeason, config_for, connect, ingest

from lockin import backtest, digest


def ingest_zeroing(tmp_path, zero):
    """Weeks 1-2 ingested, with week 1's team overrides rewritten by ``zero``."""
    season = SyntheticSeason()
    season.play_through(OPENING + timedelta(days=7))
    cfg = config_for(tmp_path, season)
    real = SyntheticSeason.matchups_payload

    def zeroed(self, week):
        payload = real(self, week)
        if week == 1:
            for team in payload:
                zero(team)
        return payload

    with patch.object(SyntheticSeason, "matchups_payload", zeroed):
        ingest(season, cfg, weeks=[1, 2])
    return season, cfg


def every_team(team):
    team["custom_points"] = 0.0


def all_but_roster_1(team):
    team["custom_points"] = None if team["roster_id"] == 1 else 0.0


def nothing_scored(team):
    team["custom_points"] = team["points"] = 0.0


@pytest.mark.parametrize(
    ("zero", "voided"),
    [(every_team, {1}), (all_but_roster_1, set()), (nothing_scored, set()), (lambda t: t, set())],
    ids=["every team zeroed", "one team left", "nothing scored", "untouched"],
)
def test_a_week_is_voided_only_when_every_scored_team_is_zeroed(tmp_path, zero, voided):
    _, cfg = ingest_zeroing(tmp_path, zero)
    with connect(cfg) as conn:
        assert backtest.voided_weeks(conn) == voided


@pytest.mark.parametrize("voided", [True, False], ids=["voided", "counted"])
def test_a_voided_weeks_lineups_are_left_out_of_the_starter_correction(tmp_path, voided):
    season, cfg = ingest_zeroing(tmp_path, every_team if voided else lambda t: t)
    seen = []
    real = digest.starter_dnp_scale

    def spy(panel, source, is_starter, week, start, **kwargs):
        seen.append((panel, is_starter))
        return real(panel, source, is_starter, week, start, **kwargs)

    with connect(cfg) as conn, patch("lockin.digest.starter_dnp_scale", spy):
        ctx = digest.load_context(conn, season.season, params=SCALED)
    panel, is_starter = seen[0]
    week = np.concatenate([h.week for h in panel.histories.values()])
    # Week 1's lineups are the only ones with games behind them on this morning.
    assert is_starter[week == 1].any() != voided
    assert ctx.lineup_ids(1, 1)  # the lineup itself is still read
