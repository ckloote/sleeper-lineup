"""The Phase 3 gate's own machinery.

The gate itself runs against the real season through `lockin calibrate`, which
is where the Phase 3 exit criterion is actually closed. What is tested here is
that the gate would *notice* — a check that cannot fail is not a gate, and a
calibration harness that always reports "uniform" is the most comfortable way to
ship a broken projection layer.
"""

from __future__ import annotations

import numpy as np
import pytest

from lockin import calibrate
from lockin.calibrate import CalibrationSample, _EmpiricalCRPS
from lockin.core.projections import POS_CENTRE, POS_FORWARD, POS_GUARD
from lockin.projections import position_group


def make_sample(pit, *, played=None, p_dnp=None, crps_model=None, weeks=None):
    n = len(pit)
    pit = np.asarray(pit, dtype=float)
    return CalibrationSample(
        week=np.asarray(weeks if weeks is not None else np.full(n, 20)),
        day=np.arange(n),
        observed=np.zeros(n),
        played=np.asarray(played if played is not None else np.ones(n, dtype=bool)),
        pit=pit,
        p_dnp=np.asarray(p_dnp if p_dnp is not None else np.full(n, 0.25)),
        crps_model=np.asarray(crps_model if crps_model is not None else np.full(n, 8.0)),
        crps_own=np.full(n, 10.0),
        crps_pool=np.full(n, 11.0),
        basis=np.full(n, "own"),
    )


def uniform_pit(n=6000, seed=0):
    return np.random.default_rng(seed).random(n)


# ------------------------------------------------------------- CRPS baseline


def test_empirical_crps_matches_brute_force():
    rng = np.random.default_rng(1)
    values = rng.normal(25, 15, 500)
    scorer = _EmpiricalCRPS(values)
    s = np.sort(values)
    for y in (-4.0, 0.0, 12.5, 25.0, 61.0):
        brute = np.abs(s - y).mean() - 0.5 * np.abs(s[:, None] - s[None, :]).mean()
        assert scorer(y) == pytest.approx(brute, rel=1e-9, abs=1e-9)


def test_empirical_crps_is_nan_without_history():
    assert np.isnan(_EmpiricalCRPS(np.zeros(0))(10.0))


# ------------------------------------------------------------ tail detection


def test_tail_check_passes_on_a_calibrated_sample():
    assert calibrate.check_tail_calibration(make_sample(uniform_pit())).passed


def test_tail_check_catches_a_thin_right_tail():
    """The failure the exit criterion exists to catch.

    A model whose extremes are too tight overstates how safe banking is, and
    nothing about its mean or its central quantiles would look wrong.
    """
    pit = uniform_pit()
    heavy = np.concatenate([pit, np.random.default_rng(2).uniform(0.99, 1.0, 200)])
    check = calibrate.check_tail_calibration(make_sample(heavy))
    assert not check.passed
    assert any("q0.99" in o for o in check.offenders)


def test_central_check_catches_a_shifted_median():
    pit = np.random.default_rng(3).beta(1.4, 1.0, 6000)
    assert not calibrate.check_central_calibration(make_sample(pit)).passed


def test_left_tail_check_is_advisory_but_reports_the_number():
    """Never fails the run; never silent either."""
    pit = np.concatenate([uniform_pit(), np.random.default_rng(4).uniform(0, 0.05, 400)])
    check = calibrate.check_left_tail(make_sample(pit))
    assert check.passed
    assert check.offenders, "an advisory check that reports nothing is just silence"
    assert "q0.10" in check.detail


# --------------------------------------------------------- sharpness and DNP


def test_sharpness_check_fails_a_wide_useless_distribution():
    check = calibrate.check_sharpness(make_sample(uniform_pit(), crps_model=np.full(6000, 10.5)))
    assert not check.passed
    assert any("own history" in o for o in check.offenders)


def test_sharpness_check_passes_a_sharper_model():
    assert calibrate.check_sharpness(make_sample(uniform_pit())).passed


def test_dnp_check_fails_an_uninformative_hazard():
    """A constant prediction can be perfectly unbiased and still worthless."""
    rng = np.random.default_rng(5)
    played = rng.random(4000) > 0.25
    check = calibrate.check_dnp_hazard(
        make_sample(uniform_pit(4000), played=played, p_dnp=np.full(4000, 0.25))
    )
    assert not check.passed
    assert any("log loss" in o for o in check.offenders)


def test_dnp_check_fails_a_hazard_that_is_sharp_but_systematically_low():
    """Discrimination does not excuse bias.

    Understating how often players sit is the expensive direction — it inflates
    the value of passing on a game, so the engine rides when it should bank.
    """
    rng = np.random.default_rng(6)
    truth = rng.beta(2, 6, 6000)
    played = rng.random(6000) > truth
    check = calibrate.check_dnp_hazard(
        make_sample(uniform_pit(6000), played=played, p_dnp=truth * 0.45)
    )
    assert not check.passed
    assert any("predicted DNP rate" in o for o in check.offenders)


def test_dnp_check_passes_a_calibrated_informative_hazard():
    rng = np.random.default_rng(7)
    p = rng.beta(2, 6, 6000)
    played = rng.random(6000) > p
    assert calibrate.check_dnp_hazard(make_sample(uniform_pit(6000), played=played, p_dnp=p)).passed


def test_playoff_check_reports_rather_than_asserts_on_a_thin_sample():
    check = calibrate.check_playoff_calibration(make_sample(uniform_pit(50)))
    assert check.passed
    assert "not meaningful" in check.detail


# ------------------------------------------------------------------ plumbing


def test_pit_histogram_is_a_distribution():
    h = calibrate.pit_histogram(make_sample(uniform_pit()))
    assert len(h) == 10
    assert sum(h) == pytest.approx(1.0)


def test_holdout_selects_the_contiguous_block():
    weeks = np.repeat(np.arange(1, 26), 4)
    sample = make_sample(uniform_pit(100), weeks=weeks)
    held = sample.holdout(18)
    assert set(held.week.tolist()) == set(range(18, 26))
    assert len(held) == 32


def test_position_group_prefers_centre():
    assert position_group(["C"]) == POS_CENTRE
    assert position_group(["PF", "C"]) == POS_CENTRE
    assert position_group(["PG", "SG"]) == POS_GUARD
    assert position_group(["SF", "PF"]) == POS_FORWARD
    assert position_group([]) == POS_FORWARD
