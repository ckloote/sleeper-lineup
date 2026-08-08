"""The projection layer, unit level.

The season-wide proof that this produces honest uncertainty lives in
`lockin calibrate`. These tests pin the pieces that calibration cannot see: that
the vectorised scorer agrees with the definition, that the point-in-time cutoff
actually cuts, and that the same seed gives the same answer twice.
"""

from __future__ import annotations

import numpy as np
import pytest

from lockin.core.projections import (
    DNP_FEATURE_NAMES,
    POS_FORWARD,
    POS_GUARD,
    EWMAProjectionSource,
    InsufficientHistory,
    PlayerHistory,
    ProjectionParams,
    ScoreDistribution,
    SeasonPanel,
    _coerce_line,
    dnp_feature_matrix,
    dnp_feature_row,
    fit_logistic,
)
from lockin.core.scoring import (
    COMPONENT_INDEX,
    COMPONENT_ORDER,
    StatLine,
    UnscorableStat,
    score_line,
    score_matrix,
)

SCORING = {
    "pts": 1.0,
    "ast": 1.0,
    "oreb": 1.5,
    "dreb": 1.0,
    "reb": 0.0,
    "stl": 2.0,
    "blk": 2.0,
    "to": -1.0,
    "fgm": 0.5,
    "fgmi": -1.0,
    "ftm": 1.0,
    "ftmi": -1.0,
    "tpm": 2.0,
    "dd": 10.0,
    "td": 20.0,
    "tf": -3.0,
    "ff": -2.0,
}


# --------------------------------------------------------- vectorised scoring


def _random_lines(rng, n=400):
    """Component lines skewed toward the 10/10 boundary on purpose.

    Uniform noise would almost never straddle a double-double threshold, which
    is exactly where the two scoring paths could disagree.
    """
    lines = np.zeros((n, len(COMPONENT_ORDER)))
    for name, hi in (
        ("pts", 40),
        ("ast", 14),
        ("oreb", 8),
        ("dreb", 14),
        ("stl", 12),
        ("blk", 12),
        ("tov", 7),
        ("fga", 30),
        ("fta", 14),
        ("tpa", 14),
        ("tech", 2),
        ("flagrant", 1),
    ):
        lines[:, COMPONENT_INDEX[name]] = rng.integers(0, hi, n)
    for made, att in (("fgm", "fga"), ("ftm", "fta"), ("tpm", "tpa")):
        lines[:, COMPONENT_INDEX[made]] = rng.integers(0, 1 + lines[:, COMPONENT_INDEX[att]])
    return lines


def test_score_matrix_agrees_with_score_line():
    """The vectorised path is an optimisation, never a second opinion."""
    rng = np.random.default_rng(11)
    lines = _random_lines(rng)
    got = score_matrix(lines, SCORING)
    want = [
        score_line(StatLine(**dict(zip(COMPONENT_ORDER, row, strict=True))), SCORING)
        for row in lines
    ]
    assert got == pytest.approx(want)


def test_score_matrix_covers_the_double_double_boundary():
    """A test that never crosses 10 would not be testing anything."""
    rng = np.random.default_rng(3)
    lines = _random_lines(rng)
    doubles = sum(
        1
        for row in lines
        if score_line(StatLine(**dict(zip(COMPONENT_ORDER, row, strict=True))), SCORING) > 0
        and StatLine(**dict(zip(COMPONENT_ORDER, row, strict=True))).pts >= 10
    )
    assert doubles > 50


def test_score_matrix_refuses_a_stat_it_cannot_represent():
    with pytest.raises(UnscorableStat):
        score_matrix(np.zeros((2, len(COMPONENT_ORDER))), {"bonus_pt_40p": 5.0})


def test_score_matrix_rejects_the_wrong_shape():
    with pytest.raises(ValueError):
        score_matrix(np.zeros((2, 3)), SCORING)


def test_coerce_line_restores_the_shooting_identities():
    """Rounding a scaled line independently can put makes above attempts."""
    raw = np.zeros((1, len(COMPONENT_ORDER)))
    raw[0, COMPONENT_INDEX["fgm"]] = 7.6
    raw[0, COMPONENT_INDEX["fga"]] = 7.4
    raw[0, COMPONENT_INDEX["ftm"]] = 3.9
    raw[0, COMPONENT_INDEX["fta"]] = 3.4
    raw[0, COMPONENT_INDEX["tpm"]] = 6.8
    raw[0, COMPONENT_INDEX["tpa"]] = 2.1
    raw[0, COMPONENT_INDEX["pts"]] = -1.0
    out = _coerce_line(raw)
    assert (out >= 0).all()
    assert out[0, COMPONENT_INDEX["fgm"]] <= out[0, COMPONENT_INDEX["fga"]]
    assert out[0, COMPONENT_INDEX["ftm"]] <= out[0, COMPONENT_INDEX["fta"]]
    assert out[0, COMPONENT_INDEX["tpm"]] <= out[0, COMPONENT_INDEX["fgm"]]
    assert out[0, COMPONENT_INDEX["tpa"]] >= out[0, COMPONENT_INDEX["tpm"]]


# ------------------------------------------------------------- distributions


def test_crps_matches_the_brute_force_definition():
    rng = np.random.default_rng(5)
    samples = np.sort(rng.normal(30, 12, 200))
    dist = ScoreDistribution(samples=samples, p_dnp=0.0, basis="own", n_own_games=40)
    y = 26.5
    brute = np.abs(samples - y).mean() - 0.5 * np.abs(samples[:, None] - samples[None, :]).mean()
    assert dist.crps(y) == pytest.approx(brute, rel=1e-9)


def test_crps_is_minimised_by_the_truth():
    """Proper scoring rule: a biased forecast must score worse."""
    rng = np.random.default_rng(6)
    truth = rng.normal(30, 10, 4000)
    y = 30.0
    honest = ScoreDistribution(np.sort(truth), 0.0, "own", 40)
    biased = ScoreDistribution(np.sort(truth + 15), 0.0, "own", 40)
    assert honest.crps(y) < biased.crps(y)


def test_randomised_pit_is_uniform_under_a_correct_model():
    """Including the zero atom, which is where the plain PIT breaks down.

    The predictive distribution really is the data-generating one here, so any
    departure from uniformity is a bug in the PIT, not in a model.
    """
    rng = np.random.default_rng(9)
    grid = np.arange(0, 80, 0.5)

    def draw(n):
        out = rng.choice(grid, size=n)
        return np.where(rng.random(n) < 0.25, 0.0, out)

    pits = []
    for _ in range(3000):
        dist = ScoreDistribution(np.sort(draw(500)), 0.25, "own", 40)
        pits.append(dist.pit(float(draw(1)[0]), float(rng.random())))
    counts, _ = np.histogram(pits, bins=10, range=(0.0, 1.0))
    chi2 = ((counts - 300.0) ** 2 / 300.0).sum()
    assert chi2 < 27.9, f"PIT not uniform under a correct model: {counts}"


# ------------------------------------------------------------------ logistic


def test_fit_logistic_recovers_known_coefficients():
    rng = np.random.default_rng(2)
    X = rng.normal(0, 1, (20000, 2))
    p = 1.0 / (1.0 + np.exp(-(-0.7 + 1.5 * X[:, 0] - 0.9 * X[:, 1])))
    y = (rng.random(len(p)) < p).astype(float)
    model = fit_logistic(X, y, ridge=1e-6)
    assert model.coef == pytest.approx([-0.7, 1.5, -0.9], abs=0.06)


def test_fit_logistic_penalty_shrinks_slopes_but_not_the_intercept():
    rng = np.random.default_rng(4)
    X = rng.normal(0, 1, (500, 2))
    y = (rng.random(500) < 0.3).astype(float)
    loose = fit_logistic(X, y, ridge=1e-6)
    tight = fit_logistic(X, y, ridge=1e4)
    assert np.abs(tight.coef[1:]).max() < np.abs(loose.coef[1:]).max()
    assert tight.coef[0] == pytest.approx(np.log(0.3 / 0.7), abs=0.25)


# -------------------------------------------------------------- DNP features


STREAK = DNP_FEATURE_NAMES.index("log_dnp_streak")
B2B = DNP_FEATURE_NAMES.index("back_to_back")


def test_dnp_features_read_the_injury_streak():
    days = np.array([10, 12, 14, 16])
    played = np.array([True, False, False, False])
    assert dnp_feature_row(days, played, 18, 12)[STREAK] == pytest.approx(np.log1p(3))


def test_dnp_features_reset_after_a_return():
    days = np.array([10, 12, 14, 16])
    played = np.array([False, False, False, True])
    assert dnp_feature_row(days, played, 18, 12)[STREAK] == pytest.approx(0.0)


def test_dnp_features_handle_an_entirely_absent_player():
    days = np.array([10, 12, 14])
    played = np.array([False, False, False])
    assert dnp_feature_row(days, played, 16, 12)[STREAK] == pytest.approx(np.log1p(3))


def test_dnp_features_flag_a_back_to_back():
    days = np.array([10, 12])
    played = np.array([True, True])
    assert dnp_feature_row(days, played, 13, 5)[B2B] == 1.0
    assert dnp_feature_row(days, played, 15, 5)[B2B] == 0.0


def test_dnp_features_survive_an_empty_history():
    row = dnp_feature_row(np.array([]), np.array([], dtype=bool), 10, 5)
    assert len(row) == len(DNP_FEATURE_NAMES)
    assert np.isfinite(row).all()


def test_dnp_feature_names_match_what_the_row_produces():
    """The names are documentation, and documentation that can drift will."""
    row = dnp_feature_row(np.array([10, 12]), np.array([True, False]), 14, 8)
    assert len(row) == len(DNP_FEATURE_NAMES)


def test_no_two_hazard_features_are_the_same_number():
    """A regression test for a real one.

    `log_games_since_played` was byte-identical to `log_dnp_streak` on all
    15,847 rows — the trailing DNP streak and the count of games since the last
    appearance are the same quantity by construction. The model carried nine
    features under ten names, and the ridge penalty split one coefficient
    between two columns rather than anything failing loudly.
    """
    rng = np.random.default_rng(17)
    rows = []
    for _ in range(300):
        n = int(rng.integers(1, 25))
        days = 739000 + np.cumsum(rng.integers(1, 4, n))
        played = rng.random(n) > 0.3
        rows.append(dnp_feature_row(days, played, int(days[-1]) + 2, int(rng.integers(1, 26))))
    F = np.array(rows)
    for i in range(F.shape[1]):
        for j in range(i + 1, F.shape[1]):
            assert not np.array_equal(F[:, i], F[:, j]), (
                f"{DNP_FEATURE_NAMES[i]} and {DNP_FEATURE_NAMES[j]} are the same feature"
            )


# ------------------------------------------------------------------- panel


def _availability(rng, n_games, stay_out=0.75, fall_out=0.08):
    """Absences as a two-state Markov chain, not independent coin flips.

    Real absences come in spells — 76% of games after a DNP are also DNPs — and
    a fixture built on iid flips gives the hazard no persistence to learn, so a
    path simulator tested against it would look broken while being correct.
    Stationary out-rate here is 0.08 / (0.08 + 0.25) ≈ 0.24, near the real 26%.
    """
    out = np.zeros(n_games, dtype=bool)
    absent = False
    for i in range(n_games):
        absent = rng.random() < (stay_out if absent else fall_out)
        out[i] = absent
    return ~out


def _synthetic_panel(n_players=12, n_games=40, seed=1, params=None):
    """A small league whose players differ in minutes, so buckets are populated."""
    rng = np.random.default_rng(seed)
    histories = {}
    for p in range(n_players):
        level = 12.0 + 2.2 * p
        minutes = np.clip(rng.normal(level, 4.0, n_games), 0.0, 44.0)
        played = _availability(rng, n_games)
        minutes = np.where(played, minutes, 0.0)
        comps = np.zeros((n_games, len(COMPONENT_ORDER)))
        comps[:, COMPONENT_INDEX["pts"]] = np.rint(minutes * 0.6)
        comps[:, COMPONENT_INDEX["ast"]] = np.rint(minutes * 0.12)
        comps[:, COMPONENT_INDEX["dreb"]] = np.rint(minutes * 0.18)
        comps[:, COMPONENT_INDEX["oreb"]] = np.rint(minutes * 0.05)
        comps[:, COMPONENT_INDEX["fga"]] = np.rint(minutes * 0.45)
        comps[:, COMPONENT_INDEX["fgm"]] = np.rint(minutes * 0.22)
        comps[~played] = 0.0
        days = 739000 + np.arange(n_games) * 2
        histories[f"p{p}"] = PlayerHistory(
            sleeper_id=f"p{p}",
            day=days,
            week=1 + np.arange(n_games) // 3,
            played=played,
            minutes=minutes,
            components=comps,
            pos_group=np.full(n_games, POS_GUARD if p % 2 else POS_FORWARD),
        )
    return SeasonPanel(histories=histories, params=params or ProjectionParams())


def test_history_before_truncates_strictly():
    panel = _synthetic_panel()
    hist = panel.histories["p3"]
    cut = int(hist.day[10])
    before = hist.before(cut)
    assert len(before) == 10
    assert (before.day < cut).all()


def test_projection_ignores_the_future():
    """The leakage rule, at unit level: delete the future, get the same answer.

    `lockin calibrate` runs this against the real season; here it is cheap
    enough to run on every commit.
    """
    panel = _synthetic_panel()
    as_of = int(panel.histories["p5"].day[30])
    truncated = SeasonPanel(
        histories={k: h.before(as_of) for k, h in panel.histories.items()},
        params=panel.params,
    )
    full_src = EWMAProjectionSource(panel, SCORING)
    cut_src = EWMAProjectionSource(truncated, SCORING)
    kwargs = dict(fantasy_week=11, n_draws=300)
    a = full_src.project("p5", as_of, rng=np.random.default_rng(0), **kwargs)
    b = cut_src.project("p5", as_of, rng=np.random.default_rng(0), **kwargs)
    assert np.array_equal(a.samples, b.samples)
    assert a.p_dnp == b.p_dnp


def test_projection_is_reproducible_from_a_seed():
    panel = _synthetic_panel()
    src = EWMAProjectionSource(panel, SCORING)
    as_of = int(panel.histories["p2"].day[25])
    kwargs = dict(fantasy_week=9, n_draws=200)
    a = src.project("p2", as_of, rng=np.random.default_rng(7), **kwargs)
    b = src.project("p2", as_of, rng=np.random.default_rng(7), **kwargs)
    assert np.array_equal(a.samples, b.samples)


def test_projection_uses_the_pool_when_a_player_is_new():
    """A player with no history still gets a distribution, flagged as such."""
    panel = _synthetic_panel()
    newcomer = PlayerHistory(
        sleeper_id="rookie",
        day=np.array([739000, 739002]),
        week=np.array([1, 1]),
        played=np.array([True, True]),
        minutes=np.array([18.0, 20.0]),
        components=np.zeros((2, len(COMPONENT_ORDER))),
        pos_group=np.array([POS_GUARD, POS_GUARD]),
    )
    panel = SeasonPanel(histories={**panel.histories, "rookie": newcomer}, params=panel.params)
    src = EWMAProjectionSource(panel, SCORING)
    dist = src.project("rookie", 739050, fantasy_week=20, rng=np.random.default_rng(1), n_draws=200)
    assert dist.basis == "pooled"
    assert dist.n_own_games == 2
    assert dist.samples.max() > 0


def test_projection_refuses_an_unknown_player():
    src = EWMAProjectionSource(_synthetic_panel(), SCORING)
    with pytest.raises(InsufficientHistory):
        src.project("nobody", 739050, fantasy_week=3, rng=np.random.default_rng(1))


def test_samples_are_sorted_and_carry_the_dnp_atom():
    panel = _synthetic_panel()
    src = EWMAProjectionSource(panel, SCORING)
    as_of = int(panel.histories["p9"].day[35])
    dist = src.project("p9", as_of, fantasy_week=12, rng=np.random.default_rng(2), n_draws=2000)
    assert (np.diff(dist.samples) >= 0).all()
    assert 0.0 < dist.p_dnp < 1.0
    assert (dist.samples == 0.0).mean() > 0.05


def test_more_minutes_projects_a_higher_distribution():
    """Sanity that the minutes layer drives the level at all."""
    panel = _synthetic_panel()
    src = EWMAProjectionSource(panel, SCORING)
    as_of = int(panel.histories["p0"].day[35])
    kwargs = dict(fantasy_week=12, rng=np.random.default_rng(3), n_draws=2000)
    low = src.project("p0", as_of, **dict(kwargs, rng=np.random.default_rng(3)))
    high = src.project("p11", as_of, **dict(kwargs, rng=np.random.default_rng(3)))
    assert high.quantile(0.75) > low.quantile(0.75)


# ------------------------------------------------------------- path simulation


def test_vectorised_hazard_features_match_the_scalar_version():
    """The fitting path and the simulating path must be the same model.

    `dnp_feature_row` builds the training matrix; `dnp_feature_matrix` drives
    every simulated game. If they drifted, the hazard would be fit on one
    parameterisation and applied on another, and nothing else would notice.
    """
    rng = np.random.default_rng(31)
    for _ in range(150):
        n = int(rng.integers(1, 30))
        days = 739000 + np.cumsum(rng.integers(1, 4, n))
        played = rng.random(n) > 0.3
        target, wk = int(days[-1]) + 2, int(rng.integers(1, 26))
        want = dnp_feature_row(days, played, target, wk)
        got = dnp_feature_matrix(days, played[None, :], target, wk)[0]
        assert np.allclose(want, got)


def test_path_simulation_shape_and_determinism():
    panel = _synthetic_panel()
    src = EWMAProjectionSource(panel, SCORING)
    as_of = int(panel.histories["p6"].day[30])
    args = ("p6", as_of, [as_of + 1, as_of + 3, as_of + 5], [11, 11, 11])
    a = src.project_path(*args, rng=np.random.default_rng(4), n_paths=200)
    b = src.project_path(*args, rng=np.random.default_rng(4), n_paths=200)
    assert a.shape == (200, 3)
    assert np.array_equal(a, b)


def test_path_simulation_ignores_the_future():
    panel = _synthetic_panel()
    as_of = int(panel.histories["p6"].day[30])
    truncated = SeasonPanel(
        histories={k: h.before(as_of) for k, h in panel.histories.items()}, params=panel.params
    )
    args = ("p6", as_of, [as_of + 1, as_of + 3], [11, 11])
    a = EWMAProjectionSource(panel, SCORING).project_path(
        *args, rng=np.random.default_rng(5), n_paths=150
    )
    b = EWMAProjectionSource(truncated, SCORING).project_path(
        *args, rng=np.random.default_rng(5), n_paths=150
    )
    assert np.array_equal(a, b)


def test_paths_make_absences_cluster_within_a_week():
    """The whole reason this function exists (implementation-plan.md §13).

    A player who misses Monday is far more likely to miss Wednesday too. Drawing
    each game independently loses that, and with it the risk of riding a week
    into a zero.
    """
    panel = _synthetic_panel(n_players=14, n_games=60, seed=9)
    src = EWMAProjectionSource(panel, SCORING)
    rng = np.random.default_rng(6)

    # Averaged over players: a single player caught mid-absence has a high
    # marginal rate, which compresses the ratio and makes the test flaky.
    joint_total = independent_total = 0.0
    for player, hist in panel.histories.items():
        as_of = int(hist.day[45])
        paths = src.project_path(
            player,
            as_of,
            [as_of + 1, as_of + 3, as_of + 5],
            [15, 15, 15],
            rng=rng,
            n_paths=2000,
        )
        missed = paths == 0.0
        joint_total += float(missed.all(axis=1).mean())
        independent_total += float(np.prod(missed.mean(axis=0)))

    n = len(panel.histories)
    joint, independent = joint_total / n, independent_total / n
    assert joint > 1.5 * independent, f"joint {joint:.4f} vs independent {independent:.4f}"


def test_a_path_game_has_the_same_marginal_as_a_direct_projection():
    """Adding joint structure must not move any single game's distribution.

    Phase 3's gate certifies the marginal. If the path simulator quietly shifted
    it, the calibration evidence would no longer describe what the policy uses.
    """
    panel = _synthetic_panel(n_players=14, n_games=60, seed=2)
    src = EWMAProjectionSource(panel, SCORING)
    hist = panel.histories["p10"]
    as_of = int(hist.day[45])
    direct = src.project("p10", as_of, fantasy_week=15, rng=np.random.default_rng(1), n_draws=8000)
    paths = src.project_path(
        "p10", as_of, [as_of], [15], rng=np.random.default_rng(2), n_paths=8000
    )[:, 0]
    assert paths.mean() == pytest.approx(direct.mean, rel=0.08)
    assert float((paths == 0).mean()) == pytest.approx(
        float((direct.samples == 0).mean()), abs=0.03
    )
