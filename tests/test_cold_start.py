"""No information is not certainty of nothing (review finding 3).

On the morning of opening day there are no games to project from. The digest
used to catch the projection layer's refusal and substitute the final game's
score — which live has not been played, so 0.0 — and report both projected
totals as 0.0, P(win) 50% and every standing threshold 0.0. That satisfied the
day-one checklist's "near 50%" and said nothing. These pin the replacement: it
abstains, says why, and resumes only once the cold-start gate says the
projections mean something.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import numpy as np
import pytest
from click.testing import CliRunner
from live_fixture import OPENING, SyntheticSeason, config_for, connect, ingest

from lockin import calibrate, cli
from lockin import digest as digest_mod
from lockin.core.policy import Game
from lockin.core.projections import InsufficientHistory, ProjectionParams
from lockin.rollout import SimulationCache
from lockin.store.db import apply_schema

# ----------------------------------------------------- the recorded season


@pytest.fixture(scope="module")
def season(season_db):
    conn = sqlite3.connect(season_db)
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    yield conn, digest_mod.load_context(conn, "2025")
    conn.close()


def test_opening_day_abstains_instead_of_projecting_zero(season):
    """The review's reproduction: roster 4, 2025-10-21, nothing banked."""
    _, ctx = season
    report = digest_mod.build(ctx, 4, "2025-10-21", n_sims=200, n_paths=200, locked={})

    assert report.abstained
    assert report.note.startswith("insufficient history")
    assert report.p_win is None and report.my_total is None
    assert not report.rules and not report.calls
    assert "insufficient history" in digest_mod.render(report)


def test_advice_resumes_once_the_pool_is_calibrated(season):
    """2025-10-28: past the 400 rows the cold-start gate certified."""
    _, ctx = season
    assert ctx.panel.pool_size(ctx.panel.day.min() + 7) >= 400

    report = digest_mod.build(ctx, 4, "2025-10-28", n_sims=200, n_paths=200, locked={})

    assert not report.abstained
    assert report.my_total > 50 and report.opponent_total > 50
    assert report.p_win not in (None, 0.5)


def test_the_cold_start_gate_passes_on_the_season_it_was_chosen_on(season):
    conn, _ = season
    sample, pool = calibrate.evaluate_cold_start(conn, "2025", n_draws=300)
    gate, advisory = calibrate.cold_start_checks(sample, pool)
    assert gate.passed, gate.detail
    assert advisory.passed


# ------------------------------------------------------ the unfinished season


def test_a_fresh_database_on_opening_morning_says_why_there_is_no_advice(tmp_path, monkeypatch):
    """Not a traceback from the cron, and not a 50% coin flip: a sentence."""
    synthetic = SyntheticSeason()
    cfg = config_for(tmp_path, synthetic)
    ingest(synthetic, cfg, weeks=[1])
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "LOCKIN_DB": str(cfg.db_path),
        "LOCKIN_LEAGUE_ID": cfg.league_id,
        "LOCKIN_SEASON": cfg.season,
        "LOCKIN_USER_ID": "u1",
    }.items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(cli.main, ["digest", "--date", OPENING.isoformat(), "--locked", ""])

    assert result.exit_code == 0, result.output
    assert "insufficient history" in result.output
    assert "P(win)" not in result.output
    with connect(cfg) as conn:
        run = conn.execute("SELECT note, p_win FROM digest_runs").fetchone()
    assert run["note"].startswith("insufficient history") and run["p_win"] is None


def test_a_player_with_no_game_yet_is_projected_from_the_pool(tmp_path):
    """Mid-season, a starter whose first game is still ahead. Not a certain zero."""
    synthetic = SyntheticSeason()
    synthetic.play_through(OPENING + timedelta(days=8))
    newcomer = synthetic.drop_add(1, synthetic.rosters[1][0], joined=synthetic.today)
    cfg = config_for(tmp_path, synthetic)
    ingest(synthetic, cfg, weeks=[1, 2])

    with connect(cfg) as conn:
        ctx = digest_mod.load_context(conn, cfg.season, params=ProjectionParams(min_pool_rows=50))
        assert newcomer in ctx.panel.unplayed
        report = digest_mod.build(
            ctx, 1, synthetic.today.isoformat(), n_sims=100, n_paths=100, locked={}
        )

        dist = ctx.source.project(
            newcomer,
            report.as_of_day,
            fantasy_week=report.week,
            rng=np.random.default_rng(0),
            n_draws=2000,
        )

    assert not report.abstained, report.note
    assert newcomer in report.names
    assert dist.basis == "pooled" and dist.n_own_games == 0
    assert dist.mean > 5.0, "projected from the pool by role, not a certain zero"


def test_the_pool_threshold_is_what_holds_advice_back(tmp_path):
    synthetic = SyntheticSeason()
    synthetic.play_through(OPENING + timedelta(days=8))
    cfg = config_for(tmp_path, synthetic)
    ingest(synthetic, cfg, weeks=[1, 2])

    with connect(cfg) as conn:
        ctx = digest_mod.load_context(conn, cfg.season)  # the real 400
        report = digest_mod.build(ctx, 1, synthetic.today.isoformat(), n_sims=100, n_paths=100)

    assert report.abstained
    assert "projections are not calibrated until 400" in report.note


# ---------------------------------------------------------------- the seam


def test_an_unprojectable_player_with_games_left_is_an_error_not_a_zero():
    class Refuses:
        def project_path(self, *args, **kwargs):
            raise InsufficientHistory("nothing to go on")

    games = [Game(0, 10, True, 30.0), Game(1, 12, False, 0.0)]
    with pytest.raises(InsufficientHistory):
        SimulationCache(source=Refuses(), n_sims=4).contribution(
            "x", games, 1, np.random.default_rng(0), known_through=10
        )
    rode = SimulationCache(source=Refuses(), n_sims=4, ride_unprojectable=True).contribution(
        "x", games, 1, np.random.default_rng(0), known_through=10
    )
    assert rode.tolist() == [0.0] * 4, "retrospective callers opt in, and know what it means"
