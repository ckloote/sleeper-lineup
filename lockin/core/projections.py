"""The projection layer: a distribution over a player's score in one game.

Pure. No network, no database, no clock, and no implicit randomness — every
draw comes from a ``numpy.random.Generator`` the caller supplies, so a backtest
can be replayed exactly.

What it produces is a **sample of correlated component stat lines**, not a mean
(architecture doc §9). The mean is useless here: the whole reason passing on a
game is ever correct is the right tail, and a point estimate has no tail.

The model is a three-stage decomposition of one player-game:

    P(DNP) ─────────────► zero-inflation gate
                              │
    minutes level (EWMA) ─────┼──► component draws ──► score_matrix()
      × empirical shock       │      (whole donor lines, so covariance
    minutes bucket ───────────┘       between components is preserved)

1. **DNP gate.** A ridge-penalised logistic hazard over the player's own recent
   availability, days of rest, trailing load and season stage. Refit from
   scratch at every ``as_of``, so it never sees the future. It matters more than
   its size suggests: a quarter of rostered player-games are DNPs, and an
   unlocked starter who does not play counts 0.0.

2. **Minutes.** A recency-weighted EWMA *level*, multiplied by a shock drawn
   from the empirical distribution of (actual ÷ trailing EWMA) ratios pooled
   across the league. The shock is empirical rather than Gaussian on purpose:
   real minutes shocks are left-skewed — foul trouble, blowouts and in-game
   knocks have no counterpart on the upside — and a symmetric shock leaves the
   bottom of the distribution too thin.

3. **Components.** Whole stat lines are resampled from games in the same
   minutes bucket and role, then scaled to the drawn minutes. Resampling the
   line as a unit is what preserves the covariance between points, attempts and
   the rest; drawing components independently would produce lines no basketball
   player has ever recorded, and would badly misprice the double-double cliff.
   A lognormal *form* factor widens the result, because a player's own three
   dozen games underestimate how good his best game can be — the empirical
   bootstrap cannot exceed its own sample maximum, and the right tail is
   precisely what the policy is being asked to reason about.

**Leakage rule, enforced rather than documented.** ``as_of`` is a required
positional argument of :meth:`ProjectionSource.project`, and the implementation
truncates history itself rather than trusting the caller to have done it. Every
quantity — the hazard coefficients, the shock library, the donor pool, the
minutes level — is refit from rows strictly before ``as_of``. Days are integer
ordinals, and the cutoff is by day rather than by timestamp, which is very
slightly conservative: an evening game cannot learn from an afternoon one. That
is the correct direction to err.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from lockin.core.scoring import COMPONENT_INDEX, COMPONENT_ORDER, score_matrix

N_COMPONENTS = len(COMPONENT_ORDER)

# Position groups, as integer codes so core never handles league vocabulary.
POS_GUARD, POS_FORWARD, POS_CENTRE = 0, 1, 2

DNP_FEATURE_NAMES = (
    "dnp_ewma_4",
    "dnp_ewma_12",
    "dnp_ewma_30",
    "dnp_previous",
    "rest_days",
    "games_last_7d",
    "season_stage",
    "back_to_back",
    "log_dnp_streak",
    "log_games_since_played",
)


class InsufficientHistory(ValueError):
    """Too little point-in-time history to project this player at all."""


@dataclass(frozen=True, slots=True)
class ProjectionParams:
    """Everything tunable, in one place.

    The defaults were selected on fantasy weeks 1-17 and the Phase 3 gate is
    evaluated on 18-25, so these numbers are not fitted to the weeks they are
    judged on. See implementation-plan.md §13.
    """

    minutes_halflife: float = 14.0
    """Half-life in *games* for the minutes EWMA and the donor weighting."""
    form_sd: float = 0.11
    """Lognormal spread on the whole line. Widens the tail beyond what a
    player's own finite history can express."""
    dnp_ridge: float = 1.0
    minutes_buckets: tuple[float, ...] = (0.0, 12.0, 20.0, 26.0, 32.0, 38.0, 60.0)
    min_bucket_donors: int = 3
    """Below this, widen from the bucket to the player's whole played history."""
    min_played_games: int = 5
    """Hard floor. Below this there is no own-history projection to make."""
    full_own_games: int = 12
    """At or above this, donors come entirely from the player himself. Below it
    the draw is mixed with the league pool in proportion."""
    shrink_games: float = 3.0
    """Pseudo-games of cohort mean minutes mixed into a thin EWMA level."""
    min_minutes: float = 2.0
    max_minutes: float = 48.0
    donor_min_minutes: float = 4.0
    """Floor on a donor's minutes before dividing by it. A 90-second cameo
    scaled up to 30 minutes is a rate estimate with no information in it."""
    pool_day_halflife: float = 30.0
    """Half-life in *days* for recency weighting of league-pool donors."""
    min_shock_samples: int = 200
    n_draws: int = 1000


DEFAULT_PARAMS = ProjectionParams()


# --------------------------------------------------------------- distribution


@dataclass(frozen=True, slots=True)
class ScoreDistribution:
    """A Monte Carlo sample of one player's fantasy score in one game."""

    samples: np.ndarray
    """Ascending. Includes the zero-inflation atom, so ``mean()`` is already
    DNP-weighted and needs no further adjustment."""
    p_dnp: float
    basis: str
    """``own`` | ``mixed`` | ``pooled`` — what the component draws came from."""
    n_own_games: int

    def quantile(self, q: float | np.ndarray) -> float | np.ndarray:
        return np.quantile(self.samples, q)

    @property
    def mean(self) -> float:
        return float(self.samples.mean())

    def prob_above(self, threshold: float) -> float:
        """P(score > threshold). The quantity a lock threshold is defined by."""
        return float((self.samples > threshold).mean())

    def pit(self, observed: float, u: float) -> float:
        """Randomised probability integral transform of an observed score.

        ``u`` must be a draw from U(0,1). The randomisation is not cosmetic:
        this distribution has an atom at zero worth about a quarter of its mass
        and lives on a half-point grid, so the ordinary PIT is not uniform even
        under a perfect model and would make a correct model look broken.
        """
        n = len(self.samples)
        lo = float(np.searchsorted(self.samples, observed, side="left")) / n
        hi = float(np.searchsorted(self.samples, observed, side="right")) / n
        return lo + u * (hi - lo)

    def crps(self, observed: float) -> float:
        """Continuous ranked probability score, lower better.

        Proper, so it cannot be gamed by hedging, and it scores calibration and
        sharpness together — which is what stops a uselessly wide distribution
        from passing the calibration gate on its own.
        """
        n = len(self.samples)
        k = np.arange(1, n + 1)
        return float(
            np.abs(self.samples - observed).mean()
            - (2.0 / n**2) * np.sum(self.samples * (k - (n + 1) / 2.0))
        )


# ------------------------------------------------------------------- logistic


@dataclass(frozen=True, slots=True)
class LogisticModel:
    coef: np.ndarray
    """Intercept first. The intercept is never penalised."""

    def predict(self, X: np.ndarray) -> np.ndarray:
        z = np.hstack([np.ones((len(X), 1)), X]) @ self.coef
        return 1.0 / (1.0 + np.exp(-z))


def fit_logistic(
    X: np.ndarray, y: np.ndarray, *, ridge: float = 1.0, max_iter: int = 60, tol: float = 1e-9
) -> LogisticModel:
    """Newton-IRLS with an L2 penalty on the slopes.

    Hand-rolled rather than pulled from a fitting library because the model has
    ten features and is refit a few hundred times per calibration run; the
    dependency would buy nothing and cost reproducibility.
    """
    Xd = np.hstack([np.ones((len(X), 1)), X])
    beta = np.zeros(Xd.shape[1])
    penalty = np.eye(Xd.shape[1]) * ridge
    penalty[0, 0] = 0.0
    for _ in range(max_iter):
        p = 1.0 / (1.0 + np.exp(-(Xd @ beta)))
        w = np.clip(p * (1.0 - p), 1e-6, None)
        hessian = Xd.T @ (Xd * w[:, None]) + penalty
        gradient = Xd.T @ (y - p) - penalty @ beta
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:  # pragma: no cover - separable data only
            step = np.linalg.lstsq(hessian, gradient, rcond=None)[0]
        beta = beta + step
        if np.abs(step).max() < tol:
            break
    return LogisticModel(coef=beta)


# --------------------------------------------------------------------- panel


def _ewma_weights(n: int, halflife: float) -> np.ndarray:
    """Exponentially decaying weights, most recent last, summing to 1."""
    w = 0.5 ** (np.arange(n)[::-1] / halflife)
    return w / w.sum()


def dnp_feature_row(
    prior_days: np.ndarray,
    prior_played: np.ndarray,
    target_day: int,
    fantasy_week: int,
) -> np.ndarray:
    """Hazard features for one game, from strictly prior games only.

    Shared by the fitting path and the prediction path so the two cannot drift
    apart — the classic way a hazard model ends up scoring rubbish in
    production while looking fine in training.
    """
    stage = fantasy_week / 25.0
    if len(prior_days) == 0:
        # No history: a neutral prior. The intercept and stage still apply.
        return np.array([0.25, 0.25, 0.25, 0.25, 3.0, 0.0, stage, 0.0, 0.0, 0.0])

    dnp = (~prior_played).astype(np.float64)
    rest = min(int(target_day - prior_days[-1]), 6)
    load7 = float(np.count_nonzero(target_day - prior_days <= 7))

    # An EWMA of availability blurs the two states that matter most: mid-injury
    # and just-back. The streak and the games since the last absence separate
    # them, and they are what carries the hazard's discrimination.
    streak = int(np.argmin(dnp[::-1])) if dnp[-1] else 0
    if streak == 0 and dnp[-1]:
        streak = len(dnp)
    played_positions = np.nonzero(prior_played)[0]
    since = len(dnp) - 1 - played_positions[-1] if len(played_positions) else len(dnp)

    return np.array(
        [
            float(dnp @ _ewma_weights(len(dnp), 4.0)),
            float(dnp @ _ewma_weights(len(dnp), 12.0)),
            float(dnp @ _ewma_weights(len(dnp), 30.0)),
            float(dnp[-1]),
            float(rest),
            load7,
            stage,
            1.0 if rest <= 1 else 0.0,
            float(np.log1p(min(streak, 10))),
            float(np.log1p(min(since, 10))),
        ]
    )


@dataclass(frozen=True, slots=True)
class PlayerHistory:
    """One player's scheduled games, in date order.

    Rows exist for games he sat out (``played`` False, zero minutes), which is
    what makes the DNP hazard estimable at all.
    """

    sleeper_id: str
    day: np.ndarray
    week: np.ndarray
    played: np.ndarray
    minutes: np.ndarray
    components: np.ndarray
    pos_group: np.ndarray

    def __len__(self) -> int:
        return len(self.day)

    def before(self, as_of: int) -> PlayerHistory:
        """The prefix strictly before ``as_of``. The only way in to history."""
        cut = int(np.searchsorted(self.day, as_of, side="left"))
        return PlayerHistory(
            sleeper_id=self.sleeper_id,
            day=self.day[:cut],
            week=self.week[:cut],
            played=self.played[:cut],
            minutes=self.minutes[:cut],
            components=self.components[:cut],
            pos_group=self.pos_group[:cut],
        )


@dataclass(slots=True)
class SeasonPanel:
    """Every player's history, flattened once so point-in-time slicing is cheap.

    Construction precomputes two things that are functions of a row's own past
    and therefore leakage-free by construction: the hazard feature matrix, and
    the minutes-shock ratio (actual ÷ trailing EWMA) that seeds the shock
    library.
    """

    histories: dict[str, PlayerHistory]
    params: ProjectionParams = DEFAULT_PARAMS

    day: np.ndarray = field(init=False)
    played: np.ndarray = field(init=False)
    minutes: np.ndarray = field(init=False)
    components: np.ndarray = field(init=False)
    pos_group: np.ndarray = field(init=False)
    dnp_features: np.ndarray = field(init=False)
    dnp_target: np.ndarray = field(init=False)
    shock_ratio: np.ndarray = field(init=False)
    offsets: dict[str, int] = field(init=False)
    """Where each player's history starts in the flat arrays. Published rather
    than left for callers to recompute, so there is one definition of the
    row order and it cannot drift from the one used to build these arrays."""
    _bucket_rows: dict[tuple[int, int], np.ndarray] = field(init=False)
    _bucket_days: dict[tuple[int, int], np.ndarray] = field(init=False)

    def __post_init__(self) -> None:
        if not self.histories:
            raise ValueError("panel needs at least one player history")

        days, played, minutes, comps, groups = [], [], [], [], []
        feats, targets, shocks = [], [], []
        hl = self.params.minutes_halflife
        self.offsets, cursor = {}, 0

        for sleeper_id, hist in self.histories.items():
            self.offsets[sleeper_id] = cursor
            cursor += len(hist)
            days.append(hist.day)
            played.append(hist.played)
            minutes.append(hist.minutes)
            comps.append(hist.components)
            groups.append(hist.pos_group)
            targets.append(~hist.played)
            for k in range(len(hist)):
                feats.append(
                    dnp_feature_row(
                        hist.day[:k], hist.played[:k], int(hist.day[k]), int(hist.week[k])
                    )
                )
                prior_played = np.nonzero(hist.played[:k])[0]
                if not hist.played[k] or len(prior_played) < self.params.min_played_games:
                    shocks.append(np.nan)
                    continue
                level = float(hist.minutes[prior_played] @ _ewma_weights(len(prior_played), hl))
                shocks.append(hist.minutes[k] / level if level > 5.0 else np.nan)

        self.day = np.concatenate(days)
        self.played = np.concatenate(played)
        self.minutes = np.concatenate(minutes)
        self.components = np.concatenate(comps)
        self.pos_group = np.concatenate(groups)
        self.dnp_features = np.array(feats)
        self.dnp_target = np.concatenate(targets).astype(np.float64)
        self.shock_ratio = np.array(shocks)

        # Played rows indexed by (minutes bucket, position group) and sorted by
        # day, so an `as_of` cut is a searchsorted prefix rather than a scan.
        buckets = self.bucket_of(self.minutes)
        self._bucket_rows, self._bucket_days = {}, {}
        n_buckets = len(self.params.minutes_buckets) - 1
        for b in range(n_buckets):
            for g in (POS_GUARD, POS_FORWARD, POS_CENTRE):
                rows = np.nonzero(self.played & (buckets == b) & (self.pos_group == g))[0]
                rows = rows[np.argsort(self.day[rows], kind="stable")]
                self._bucket_rows[(b, g)] = rows
                self._bucket_days[(b, g)] = self.day[rows]

    def bucket_of(self, minutes: np.ndarray) -> np.ndarray:
        edges = np.asarray(self.params.minutes_buckets)
        return np.clip(np.digitize(minutes, edges) - 1, 0, len(edges) - 2)

    def history(self, sleeper_id: str) -> PlayerHistory:
        try:
            return self.histories[sleeper_id]
        except KeyError:
            raise InsufficientHistory(f"no history for player {sleeper_id}") from None

    def pool_rows(self, bucket: int, group: int, as_of: int) -> np.ndarray:
        """League-pool donor rows in a (bucket, group), strictly before as_of."""
        rows = self._bucket_rows[(bucket, group)]
        cut = int(np.searchsorted(self._bucket_days[(bucket, group)], as_of, side="left"))
        return rows[:cut]


# ------------------------------------------------------------------ protocol


class ProjectionSource(Protocol):
    """Anything that can produce a score distribution for a player-game.

    ``as_of`` is positional and required. That is deliberate: the architecture
    doc calls the leakage rule non-negotiable, and an optional cutoff is a
    cutoff that eventually gets left out. An implementation backed by a live
    external feed — DARKO or similar, which publish no point-in-time history —
    must refuse to answer for a past ``as_of`` rather than answer with today's
    numbers.
    """

    def project(
        self,
        sleeper_id: str,
        as_of: int,
        *,
        fantasy_week: int,
        rng: np.random.Generator,
        n_draws: int | None = None,
    ) -> ScoreDistribution: ...


# -------------------------------------------------------------- EWMA source


class EWMAProjectionSource:
    """v1 projection source: trailing EWMA, reconstructible point-in-time.

    Everything it uses comes from box scores, which is what makes the backtest
    honest — see the leakage rule in the module docstring.
    """

    def __init__(
        self,
        panel: SeasonPanel,
        scoring: dict[str, float],
        params: ProjectionParams | None = None,
    ) -> None:
        self.panel = panel
        self.scoring = scoring
        self.params = params or panel.params
        self._dnp_cache: dict[int, LogisticModel | None] = {}
        self._shock_cache: dict[int, np.ndarray] = {}
        self._level_cache: dict[int, float] = {}

    # -- point-in-time fits, cached by cutoff ------------------------------

    def dnp_model(self, as_of: int) -> LogisticModel | None:
        """Hazard coefficients fit on every row strictly before ``as_of``."""
        if as_of not in self._dnp_cache:
            mask = self.panel.day < as_of
            if int(mask.sum()) < 500:
                self._dnp_cache[as_of] = None
            else:
                self._dnp_cache[as_of] = fit_logistic(
                    self.panel.dnp_features[mask],
                    self.panel.dnp_target[mask],
                    ridge=self.params.dnp_ridge,
                )
        return self._dnp_cache[as_of]

    def shock_library(self, as_of: int) -> np.ndarray:
        """Observed (minutes ÷ trailing EWMA) ratios from before ``as_of``."""
        if as_of not in self._shock_cache:
            ratios = self.panel.shock_ratio
            usable = ratios[(self.panel.day < as_of) & ~np.isnan(ratios)]
            if len(usable) < self.params.min_shock_samples:
                usable = np.ones(1)
            self._shock_cache[as_of] = usable
        return self._shock_cache[as_of]

    def _cohort_minutes(self, as_of: int) -> float:
        if as_of not in self._level_cache:
            mask = self.panel.played & (self.panel.day < as_of)
            self._level_cache[as_of] = (
                float(self.panel.minutes[mask].mean()) if mask.any() else 20.0
            )
        return self._level_cache[as_of]

    # -- the projection itself ---------------------------------------------

    def project(
        self,
        sleeper_id: str,
        as_of: int,
        *,
        fantasy_week: int,
        rng: np.random.Generator,
        n_draws: int | None = None,
    ) -> ScoreDistribution:
        p = self.params
        n = n_draws or p.n_draws
        hist = self.panel.history(sleeper_id).before(as_of)
        own = np.nonzero(hist.played)[0]
        n_own = len(own)

        group = int(hist.pos_group[-1]) if len(hist) else POS_FORWARD
        p_dnp = self._project_dnp(hist, as_of, fantasy_week)
        minutes = self._draw_minutes(hist, own, as_of, rng, n)

        own_weight = min(1.0, n_own / p.full_own_games) if p.full_own_games else 1.0
        if n_own < p.min_played_games:
            own_weight = 0.0
        use_own = rng.random(n) < own_weight
        if own_weight >= 1.0:
            basis = "own"
        elif own_weight <= 0.0:
            basis = "pooled"
        else:
            basis = "mixed"

        donor_comps, donor_minutes = self._draw_donors(
            hist, own, group, as_of, minutes, use_own, rng
        )
        if donor_comps is None:
            raise InsufficientHistory(
                f"player {sleeper_id} has {n_own} played games before day {as_of}"
                f" and the league pool is empty for group {group}"
            )

        scale = minutes / np.clip(donor_minutes, p.donor_min_minutes, None)
        if p.form_sd > 0:
            # Median-preserving: widen the line without shifting its centre.
            scale = scale * np.exp(rng.normal(0.0, p.form_sd, n))

        lines = _coerce_line(donor_comps * scale[:, None])
        scores = score_matrix(lines, self.scoring)
        scores = np.where(rng.random(n) < p_dnp, 0.0, scores)
        scores.sort()
        return ScoreDistribution(samples=scores, p_dnp=float(p_dnp), basis=basis, n_own_games=n_own)

    def _project_dnp(self, hist: PlayerHistory, as_of: int, fantasy_week: int) -> float:
        model = self.dnp_model(as_of)
        if model is None:
            # Too early in the season to fit anything; fall back to the
            # player's own rate, and to the league's if he has no history.
            return float((~hist.played).mean()) if len(hist) else 0.25
        row = dnp_feature_row(hist.day, hist.played, as_of, fantasy_week)
        return float(model.predict(row[None, :])[0])

    def _draw_minutes(
        self,
        hist: PlayerHistory,
        own: np.ndarray,
        as_of: int,
        rng: np.random.Generator,
        n: int,
    ) -> np.ndarray:
        p = self.params
        cohort = self._cohort_minutes(as_of)
        if len(own):
            weights = _ewma_weights(len(own), p.minutes_halflife)
            level = float(hist.minutes[own] @ weights)
        else:
            level = cohort
        # Shrink a thin EWMA toward the cohort mean rather than trusting three
        # games to establish a role.
        level = (len(own) * level + p.shrink_games * cohort) / (len(own) + p.shrink_games)
        shocks = rng.choice(self.shock_library(as_of), size=n)
        return np.clip(level * shocks, p.min_minutes, p.max_minutes)

    def _draw_donors(
        self,
        hist: PlayerHistory,
        own: np.ndarray,
        group: int,
        as_of: int,
        minutes: np.ndarray,
        use_own: np.ndarray,
        rng: np.random.Generator,
    ) -> tuple[np.ndarray | None, np.ndarray | None]:
        """Resample whole component lines, conditioned on minutes bucket and role."""
        p = self.params
        n = len(minutes)
        target_bucket = self.panel.bucket_of(minutes)
        own_bucket = self.panel.bucket_of(hist.minutes[own]) if len(own) else np.empty(0, int)
        own_weights = _ewma_weights(len(own), p.minutes_halflife) if len(own) else None

        comps = np.empty((n, N_COMPONENTS))
        donor_minutes = np.empty(n)
        filled = np.zeros(n, dtype=bool)

        for bucket in range(len(p.minutes_buckets) - 1):
            in_bucket = target_bucket == bucket
            if not in_bucket.any():
                continue

            sel = in_bucket & use_own
            if sel.any() and len(own):
                cand = np.nonzero(own_bucket == bucket)[0]
                if len(cand) < p.min_bucket_donors:
                    cand = np.arange(len(own))  # widen rather than refuse
                w = own_weights[cand] / own_weights[cand].sum()
                pick = rng.choice(cand, size=int(sel.sum()), p=w)
                rows = own[pick]
                comps[sel] = hist.components[rows]
                donor_minutes[sel] = hist.minutes[rows]
                filled[sel] = True

            sel = in_bucket & ~use_own
            if sel.any():
                rows = self.panel.pool_rows(bucket, group, as_of)
                if len(rows) < p.min_bucket_donors:
                    rows = np.concatenate(
                        [
                            self.panel.pool_rows(b, group, as_of)
                            for b in range(len(p.minutes_buckets) - 1)
                        ]
                    )
                if len(rows) == 0:
                    continue
                age = as_of - self.panel.day[rows]
                w = 0.5 ** (age / p.pool_day_halflife)
                w = w / w.sum()
                pick = rng.choice(rows, size=int(sel.sum()), p=w)
                comps[sel] = self.panel.components[pick]
                donor_minutes[sel] = self.panel.minutes[pick]
                filled[sel] = True

        if not filled.all():
            if not filled.any():
                return None, None
            # A bucket with neither own games nor pool coverage: reuse what we
            # did fill rather than dropping draws, which would bias the sample.
            donors = np.nonzero(filled)[0]
            take = rng.choice(donors, size=int((~filled).sum()))
            comps[~filled] = comps[take]
            donor_minutes[~filled] = donor_minutes[take]
        return comps, donor_minutes


def _coerce_line(lines: np.ndarray) -> np.ndarray:
    """Round a scaled donor line back onto the integer counting-stat lattice.

    Scaling a real stat line by a minutes ratio produces fractional makes and
    attempts. Rounding restores the discreteness that the double-double
    threshold actually lives on — at the 10/10 boundary the difference between
    9.6 and 10 points is ten fantasy points, so smoothing it away would
    systematically misprice exactly the games worth locking.

    Rounding independently can put makes above attempts, so the shooting
    identities are re-imposed afterwards.
    """
    out = np.maximum(np.rint(lines), 0.0)
    i = COMPONENT_INDEX
    out[:, i["fgm"]] = np.minimum(out[:, i["fgm"]], out[:, i["fga"]])
    out[:, i["ftm"]] = np.minimum(out[:, i["ftm"]], out[:, i["fta"]])
    out[:, i["tpm"]] = np.minimum(out[:, i["tpm"]], out[:, i["fgm"]])
    out[:, i["tpa"]] = np.maximum(np.minimum(out[:, i["tpa"]], out[:, i["fga"]]), out[:, i["tpm"]])
    return out
