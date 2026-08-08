"""Phase 3 gate: does the projection layer's uncertainty mean anything?

The exit criterion is calibration, not accuracy: *predicted quantiles must match
realised frequencies out of sample, checked specifically in the right tail.*
That emphasis is the whole point. The engine's job is to decide whether tonight's
score is good enough to bank or whether the remaining games are likely to beat
it, and that judgement is made entirely in the upper tail. A projection with a
perfect mean and a 20% understatement of P(score > 55) would recommend banking
far too eagerly and no accuracy metric would notice.

Method
------

For every evaluated player-game the model produces a predictive distribution
from data strictly before that game, and we record the **randomised probability
integral transform** of what actually happened. If the model is calibrated the
PIT values are uniform on [0,1], whatever the underlying distributions look
like — so one uniformity check covers every player and every week at once.

Randomisation is required rather than optional. This predictive distribution has
an atom at zero holding roughly a quarter of its mass, and the rest sits on a
half-point lattice. The textbook PIT is not uniform for a discrete distribution
even when the model is perfect, so using it would report a correct model as
broken. The randomised version restores uniformity exactly.

Tail checks then fall out directly: P(PIT > 0.90) should be 0.10, P(PIT > 0.99)
should be 0.01, and each is a binomial proportion with an honest standard error.

Two guards against passing for the wrong reason
-----------------------------------------------

*Sharpness.* A distribution wide enough to cover anything calibrates trivially
and decides nothing. So the model must also beat two dumber predictors on CRPS —
a proper score that rewards calibration and sharpness together.

*Leakage.* Calibration this good is also what leakage looks like. The gate
rebuilds the panel truncated at a cutoff and asserts the projection is bit-identical
to the one built from the full season, which is the only way to be sure the
``as_of`` discipline actually holds end to end rather than in intention.

Weeks 1-17 are where the model's few hyperparameters were chosen; weeks 18-25
are held out and are what the gate is scored on. The split is contiguous, per
architecture doc §12.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np

from lockin.core.projections import (
    EWMAProjectionSource,
    InsufficientHistory,
    ProjectionParams,
    SeasonPanel,
)
from lockin.core.scoring import score_matrix
from lockin.projections import load_panel
from lockin.verify import Check, scoring_settings

DEFAULT_HOLDOUT_FROM = 18
"""First held-out fantasy week. Weeks below this tuned the model."""

MIN_PRIOR_GAMES = 15
MIN_PRIOR_PLAYED = 8
"""Burn-in. A projection from four games is not wrong so much as uninformed, and
including those player-weeks would measure the burn-in rather than the model."""

TAIL_LEVELS = (0.90, 0.95, 0.99)
CENTRAL_LEVELS = (0.25, 0.50, 0.75)
LEFT_TAIL_LEVELS = (0.05, 0.10)
MAX_ABS_Z = 3.0
"""Binomial z tolerance. At holdout size this is roughly ±0.4pp on the 99th
percentile — tight enough to catch a tail error that would change a lock call,
loose enough not to fail on Monte Carlo noise."""

PLAYOFF_WEEKS = (22, 23, 24)
MIN_PLAYOFF_SAMPLE = 400


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    """One row per evaluated player-game."""

    week: np.ndarray
    day: np.ndarray
    observed: np.ndarray
    played: np.ndarray
    pit: np.ndarray
    p_dnp: np.ndarray
    crps_model: np.ndarray
    crps_own: np.ndarray
    """CRPS of the player's own empirical history — the obvious predictor."""
    crps_pool: np.ndarray
    """CRPS of the league-wide empirical distribution — the naive predictor."""
    basis: np.ndarray
    skipped: int = 0

    def holdout(self, holdout_from: int) -> CalibrationSample:
        return self._select(self.week >= holdout_from)

    def weeks(self, weeks: tuple[int, ...]) -> CalibrationSample:
        return self._select(np.isin(self.week, weeks))

    def _select(self, mask: np.ndarray) -> CalibrationSample:
        return CalibrationSample(
            week=self.week[mask],
            day=self.day[mask],
            observed=self.observed[mask],
            played=self.played[mask],
            pit=self.pit[mask],
            p_dnp=self.p_dnp[mask],
            crps_model=self.crps_model[mask],
            crps_own=self.crps_own[mask],
            crps_pool=self.crps_pool[mask],
            basis=self.basis[mask],
        )

    def __len__(self) -> int:
        return len(self.pit)


# ------------------------------------------------------------------ baselines


@dataclass(slots=True)
class _EmpiricalCRPS:
    """CRPS against a fixed empirical sample, in O(log n) per query.

    Sorting the reference set once per cutoff and answering from a cumulative
    sum turns the baselines from the slowest thing in the run into a rounding
    error. ``mean|X - y|`` splits at ``y`` into the mass below and the mass
    above, both of which the prefix sums already know.
    """

    values: np.ndarray = field(default_factory=lambda: np.zeros(0))
    _cumsum: np.ndarray = field(init=False)
    _spread: float = field(init=False)

    def __post_init__(self) -> None:
        self.values = np.sort(self.values)
        n = len(self.values)
        self._cumsum = np.concatenate([[0.0], np.cumsum(self.values)])
        if n:
            k = np.arange(1, n + 1)
            self._spread = float((2.0 / n**2) * np.sum(self.values * (k - (n + 1) / 2.0)))
        else:
            self._spread = 0.0

    def __call__(self, observed: float) -> float:
        n = len(self.values)
        if n == 0:
            return float("nan")
        k = int(np.searchsorted(self.values, observed, side="left"))
        below = observed * k - self._cumsum[k]
        above = (self._cumsum[n] - self._cumsum[k]) - observed * (n - k)
        return (below + above) / n - self._spread


# ----------------------------------------------------------------- evaluation


def _observed_scores(panel: SeasonPanel, scoring: dict[str, float]) -> np.ndarray:
    """What every panel row actually scored — 0.0 for a game not played.

    A scheduled game the player sat out is a real 0.0 for an unlocked starter,
    not a missing value, and that is exactly the outcome the projection has to
    get right.
    """
    scores = score_matrix(panel.components, scoring)
    return np.where(panel.played, scores, 0.0)


def evaluate(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
    n_draws: int = 1000,
    seed: int = 20260808,
    min_prior_games: int = MIN_PRIOR_GAMES,
    min_prior_played: int = MIN_PRIOR_PLAYED,
    panel: SeasonPanel | None = None,
) -> CalibrationSample:
    """Project every eligible player-game from its own past, and score the result."""
    scoring = scoring_settings(conn)
    panel = panel or load_panel(conn, season, params=params)
    source = EWMAProjectionSource(panel, scoring, params)
    rng = np.random.default_rng(seed)

    all_scores = _observed_scores(panel, scoring)
    weeks, days, observed, played, pits = [], [], [], [], []
    p_dnps, crps_m, crps_o, crps_p, bases = [], [], [], [], []
    pool_cache: dict[int, _EmpiricalCRPS] = {}
    skipped = 0

    for sleeper_id, hist in panel.histories.items():
        base = panel.offsets[sleeper_id]
        for k in range(len(hist)):
            if k < min_prior_games or int(hist.played[:k].sum()) < min_prior_played:
                continue
            as_of = int(hist.day[k])
            try:
                dist = source.project(
                    sleeper_id,
                    as_of,
                    fantasy_week=int(hist.week[k]),
                    rng=rng,
                    n_draws=n_draws,
                )
            except InsufficientHistory:
                skipped += 1
                continue

            y = float(all_scores[base + k])
            if as_of not in pool_cache:
                pool_cache[as_of] = _EmpiricalCRPS(all_scores[panel.day < as_of])
            own = _EmpiricalCRPS(all_scores[base : base + k])

            weeks.append(int(hist.week[k]))
            days.append(as_of)
            observed.append(y)
            played.append(bool(hist.played[k]))
            pits.append(dist.pit(y, float(rng.random())))
            p_dnps.append(dist.p_dnp)
            crps_m.append(dist.crps(y))
            crps_o.append(own(y))
            crps_p.append(pool_cache[as_of](y))
            bases.append(dist.basis)

    return CalibrationSample(
        week=np.array(weeks),
        day=np.array(days),
        observed=np.array(observed),
        played=np.array(played, dtype=bool),
        pit=np.array(pits),
        p_dnp=np.array(p_dnps),
        crps_model=np.array(crps_m),
        crps_own=np.array(crps_o),
        crps_pool=np.array(crps_p),
        basis=np.array(bases),
        skipped=skipped,
    )


# --------------------------------------------------------------------- checks


def _z(sample: CalibrationSample, level: float) -> tuple[float, float]:
    """Realised P(PIT > level) and its binomial z against the nominal 1-level."""
    n = len(sample)
    realised = float((sample.pit > level).mean())
    se = np.sqrt(level * (1.0 - level) / n)
    return realised, (realised - (1.0 - level)) / se


def check_tail_calibration(sample: CalibrationSample) -> Check:
    """The Phase 3 exit criterion, in the tail that drives lock decisions."""
    offenders, parts = [], []
    for level in TAIL_LEVELS:
        realised, z = _z(sample, level)
        parts.append(f"q{level:.2f}: {realised:.4f} vs {1 - level:.3f} (z={z:+.2f})")
        if abs(z) > MAX_ABS_Z:
            offenders.append(
                f"P(score > predicted q{level:.2f}) = {realised:.4f},"
                f" expected {1 - level:.3f} (z={z:+.2f})"
            )
    return Check(
        name="right-tail quantiles match realised frequencies, out of sample",
        passed=not offenders,
        detail="; ".join(parts),
        offenders=offenders,
    )


def check_left_tail(sample: CalibrationSample) -> Check:
    """The known bias, reported every run rather than left to be rediscovered.

    Advisory. The bottom decile carries about 11.3% of the mass instead of 10%:
    players bust slightly more often than the model says. Roughly three quarters
    of the excess is DNP outcomes — the hazard is mildly under-confident in its
    0.10-0.20 band, where a player is a genuine game-time decision rather than
    clearly in or clearly out.

    It does not fail the run, because the Phase 3 criterion is the right tail
    and that passes cleanly. It is reported because the direction matters: too
    little bust probability means too much value assigned to *passing* on a
    game, so the engine's residual bias is toward riding rather than banking.
    Anything downstream that looks suspiciously reluctant to lock should suspect
    this first. See implementation-plan.md §13.
    """
    parts, offenders = [], []
    for level in LEFT_TAIL_LEVELS:
        realised, z = _z(sample, level)
        parts.append(f"q{level:.2f}: {realised:.4f} vs {1 - level:.3f} (z={z:+.2f})")
        if abs(z) > MAX_ABS_Z:
            offenders.append(
                f"P(score > predicted q{level:.2f}) = {realised:.4f},"
                f" expected {1 - level:.3f} (z={z:+.2f})"
            )
    zero_share = float((~sample.played & (sample.pit < 0.10)).sum()) / max(
        int((sample.pit < 0.10).sum()), 1
    )
    return Check(
        name="left-tail calibration (advisory)",
        passed=True,
        detail=f"{'; '.join(parts)}; {zero_share:.0%} of sub-decile mass is DNP outcomes",
        offenders=offenders,
    )


def check_central_calibration(sample: CalibrationSample) -> Check:
    offenders, parts = [], []
    for level in CENTRAL_LEVELS:
        realised, z = _z(sample, level)
        parts.append(f"q{level:.2f}: {realised:.4f} vs {1 - level:.3f} (z={z:+.2f})")
        if abs(z) > MAX_ABS_Z:
            offenders.append(
                f"P(score > predicted q{level:.2f}) = {realised:.4f},"
                f" expected {1 - level:.3f} (z={z:+.2f})"
            )
    return Check(
        name="central quantiles match realised frequencies, out of sample",
        passed=not offenders,
        detail="; ".join(parts),
        offenders=offenders,
    )


def check_sharpness(sample: CalibrationSample) -> Check:
    """Calibrated is not enough — the distribution has to be worth having.

    Both baselines are calibrated-ish and useless: the league-wide empirical
    distribution knows nothing about who is playing, and the player's own
    history knows nothing about his last fortnight. Beating them is the minimum
    evidence that the modelling earns its complexity.
    """
    model, own, pool = (
        float(sample.crps_model.mean()),
        float(np.nanmean(sample.crps_own)),
        float(np.nanmean(sample.crps_pool)),
    )
    offenders = []
    if model >= own:
        offenders.append(f"CRPS {model:.3f} does not beat the player's own history ({own:.3f})")
    if model >= pool:
        offenders.append(f"CRPS {model:.3f} does not beat the league marginal ({pool:.3f})")
    return Check(
        name="projection is sharper than the naive predictors",
        passed=not offenders,
        detail=(
            f"CRPS model={model:.3f} own-history={own:.3f} ({1 - model / own:+.1%})"
            f" league={pool:.3f} ({1 - model / pool:+.1%})"
        ),
        offenders=offenders,
    )


def check_dnp_hazard(sample: CalibrationSample) -> Check:
    """The zero-inflation gate, checked on its own.

    It is folded into the PIT already, but it is worth isolating: a quarter of
    player-games are DNPs, and starting a player who does not play is the single
    most expensive mistake available in this format.
    """
    realised = float((~sample.played).mean())
    predicted = float(sample.p_dnp.mean())
    n = len(sample)
    se = np.sqrt(max(realised * (1 - realised), 1e-9) / n)
    z = (predicted - realised) / se

    y = (~sample.played).astype(float)
    p = np.clip(sample.p_dnp, 1e-6, 1 - 1e-6)
    logloss = float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())
    base_rate = np.clip(realised, 1e-6, 1 - 1e-6)
    base_logloss = float(-(y * np.log(base_rate) + (1 - y) * np.log(1 - base_rate)).mean())

    offenders = []
    if abs(z) > MAX_ABS_Z:
        offenders.append(
            f"predicted DNP rate {predicted:.4f} vs realised {realised:.4f} (z={z:+.2f})"
        )
    if logloss >= base_logloss:
        offenders.append(
            f"log loss {logloss:.4f} no better than the base rate ({base_logloss:.4f})"
        )
    return Check(
        name="DNP hazard is calibrated and informative",
        passed=not offenders,
        detail=(
            f"predicted {predicted:.4f} realised {realised:.4f} (z={z:+.2f});"
            f" log loss {logloss:.4f} vs base rate {base_logloss:.4f}"
        ),
        offenders=offenders,
    )


def check_playoff_calibration(sample: CalibrationSample) -> Check:
    """Weeks 22-24 specifically, where the decisions are worth the most.

    Architecture doc §9 warns that rest behaviour shifts late in the season and
    that a model fit on November will be miscalibrated exactly when it counts.
    The hazard is refit at every cutoff and carries a season-stage feature, so
    it should adapt — this is the check that says whether it does.
    """
    if len(sample) < MIN_PLAYOFF_SAMPLE:
        return Check(
            name="calibration holds through the fantasy playoffs",
            passed=True,
            detail=f"only {len(sample)} playoff player-games evaluated; check not meaningful",
        )
    offenders, parts = [], []
    for level in TAIL_LEVELS:
        realised, z = _z(sample, level)
        parts.append(f"q{level:.2f}: {realised:.4f} (z={z:+.2f})")
        if abs(z) > MAX_ABS_Z:
            offenders.append(f"weeks 22-24 P(score > q{level:.2f}) = {realised:.4f} (z={z:+.2f})")
    return Check(
        name="calibration holds through the fantasy playoffs",
        passed=not offenders,
        detail=f"n={len(sample)}; " + "; ".join(parts),
        offenders=offenders,
    )


def check_no_leakage(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
    cutoffs: int = 3,
    per_cutoff: int = 12,
    seed: int = 4242,
) -> Check:
    """Rebuild the panel truncated at a cutoff; the projection must not move.

    The leakage rule is the one the architecture doc calls non-negotiable, and
    it is unfalsifiable by inspection — a model that reads the future looks
    like a model that is simply good. So this deletes the future outright and
    checks the answer is bit-identical.
    """
    full = load_panel(conn, season, params=params)
    scoring = scoring_settings(conn)
    days = np.unique(full.day)
    picks = days[np.linspace(len(days) // 3, len(days) - 2, cutoffs).astype(int)]

    offenders = []
    compared = 0
    for as_of in picks:
        as_of = int(as_of)
        truncated = SeasonPanel(
            histories={
                pid: hist.before(as_of)
                for pid, hist in full.histories.items()
                if len(hist.before(as_of))
            },
            params=full.params,
        )
        a = EWMAProjectionSource(full, scoring, params)
        b = EWMAProjectionSource(truncated, scoring, params)
        # Only players still playing at the cutoff: one whose season already
        # ended has no week to project, and asking for one would index past the
        # end of his history.
        eligible = [
            pid
            for pid, hist in full.histories.items()
            if int(hist.before(as_of).played.sum()) >= MIN_PRIOR_PLAYED
            and int(np.searchsorted(hist.day, as_of)) < len(hist)
        ]
        for pid in sorted(eligible)[:per_cutoff]:
            hist = full.histories[pid]
            week = int(hist.week[int(np.searchsorted(hist.day, as_of))])
            args = dict(fantasy_week=week, n_draws=200)
            left = a.project(pid, as_of, rng=np.random.default_rng(seed), **args)
            right = b.project(pid, as_of, rng=np.random.default_rng(seed), **args)
            compared += 1
            if not np.array_equal(left.samples, right.samples) or left.p_dnp != right.p_dnp:
                offenders.append(
                    f"player {pid} at day {as_of}: projection changed when the future"
                    f" was deleted (p_dnp {left.p_dnp:.6f} vs {right.p_dnp:.6f})"
                )
    return Check(
        name="projections use no data at or after as_of",
        passed=not offenders,
        detail=f"{compared - len(offenders)}/{compared} projections identical"
        f" with the future removed, across {len(picks)} cutoffs",
        offenders=offenders[:20],
    )


def pit_histogram(sample: CalibrationSample, bins: int = 10) -> list[float]:
    counts, _ = np.histogram(sample.pit, bins=bins, range=(0.0, 1.0))
    return [float(c) / len(sample) for c in counts]


def run(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
    n_draws: int = 1000,
    holdout_from: int = DEFAULT_HOLDOUT_FROM,
    seed: int = 20260808,
    sample: CalibrationSample | None = None,
) -> tuple[list[Check], CalibrationSample]:
    """Run the Phase 3 gates. Returns the checks and the full evaluation sample."""
    full = (
        sample
        if sample is not None
        else evaluate(conn, season, params=params, n_draws=n_draws, seed=seed)
    )
    held = full.holdout(holdout_from)
    if len(held) == 0:
        raise RuntimeError(f"no evaluated player-games in weeks >= {holdout_from}")
    checks = [
        check_tail_calibration(held),
        check_central_calibration(held),
        check_left_tail(held),
        check_playoff_calibration(held.weeks(PLAYOFF_WEEKS)),
        check_dnp_hazard(held),
        check_sharpness(held),
        check_no_leakage(conn, season, params=params),
    ]
    return checks, full
