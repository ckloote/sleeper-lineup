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

from collections.abc import Sequence
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
    n_paths: int = 1000
    """Default paths for a joint week simulation. Separate from ``n_draws``
    because a path costs one draw per remaining game, not one in total."""


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
    nine features and is refit a few hundred times per calibration run; the
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
        return np.array([0.25, 0.25, 0.25, 0.25, 3.0, 0.0, stage, 0.0, 0.0])

    dnp = (~prior_played).astype(np.float64)
    rest = min(int(target_day - prior_days[-1]), 6)
    load7 = float(np.count_nonzero(target_day - prior_days <= 7))

    # The trailing DNP streak. An EWMA of availability blurs the two states that
    # matter most — mid-injury and just-back — and this is what separates them.
    #
    # An earlier version carried a tenth feature, "games since last played",
    # which is the *same number* as this one by construction: identical on all
    # 15,847 rows. Re-expressing it in days made it genuinely distinct
    # (correlation 0.94) and still bought nothing — log loss 0.3299 against
    # 0.3297 without it — so it is gone rather than kept for appearances.
    played_positions = np.nonzero(prior_played)[0]
    streak = len(dnp) - 1 - int(played_positions[-1]) if len(played_positions) else len(dnp)

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
        ]
    )


def dnp_feature_matrix(
    days: np.ndarray,
    played: np.ndarray,
    target_day: int,
    fantasy_week: int,
) -> np.ndarray:
    """:func:`dnp_feature_row` over many simulated histories at once.

    ``played`` is ``(n_paths, T)`` and ``days`` is the shared ``(T,)`` schedule —
    the fixture list is deterministic, only the outcomes differ between paths.
    Returns ``(n_paths, len(DNP_FEATURE_NAMES))``.

    Kept beside the scalar version and tested against it row by row. The path
    simulator calls this once per simulated game, so a divergence between the
    two would mean the hazard used to *fit* and the hazard used to *simulate*
    were different models.
    """
    n, t = played.shape
    stage = fantasy_week / 25.0
    if t == 0:
        return np.tile([0.25, 0.25, 0.25, 0.25, 3.0, 0.0, stage, 0.0, 0.0], (n, 1))

    dnp = (~played).astype(np.float64)
    rest = min(int(target_day - days[-1]), 6)
    load7 = float(np.count_nonzero(target_day - days <= 7))
    # Trailing DNP run: in the reversed row, the first played game sits exactly
    # `streak` positions in. A path where he never played has no such position.
    streak = np.where(played.any(axis=1), np.argmax(played[:, ::-1], axis=1), t)

    out = np.empty((n, len(DNP_FEATURE_NAMES)))
    out[:, 0] = dnp @ _ewma_weights(t, 4.0)
    out[:, 1] = dnp @ _ewma_weights(t, 12.0)
    out[:, 2] = dnp @ _ewma_weights(t, 30.0)
    out[:, 3] = dnp[:, -1]
    out[:, 4] = rest
    out[:, 5] = load7
    out[:, 6] = stage
    out[:, 7] = 1.0 if rest <= 1 else 0.0
    out[:, 8] = np.log1p(np.minimum(streak, 10))
    return out


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
        hist = self.panel.history(sleeper_id).before(as_of)
        n_own = int(hist.played.sum())
        p_dnp = self._project_dnp(hist, as_of, fantasy_week)

        scores = self._draw_played_scores(
            sleeper_id, hist, as_of, rng, n_draws or self.params.n_draws
        )
        scores = np.where(rng.random(len(scores)) < p_dnp, 0.0, scores)
        scores.sort()
        return ScoreDistribution(
            samples=scores, p_dnp=float(p_dnp), basis=self._basis(n_own), n_own_games=n_own
        )

    def _basis(self, n_own: int) -> str:
        p = self.params
        if n_own < p.min_played_games:
            return "pooled"
        return "own" if n_own >= p.full_own_games else "mixed"

    def _own_weight(self, n_own: int) -> float:
        p = self.params
        if n_own < p.min_played_games:
            return 0.0
        return min(1.0, n_own / p.full_own_games) if p.full_own_games else 1.0

    def _draw_played_scores(
        self,
        sleeper_id: str,
        hist: PlayerHistory,
        as_of: int,
        rng: np.random.Generator,
        n: int,
        form: np.ndarray | None = None,
    ) -> np.ndarray:
        """``n`` scores for one game, **conditional on the player playing**.

        Shared by the marginal projection and the week-path simulator so the two
        cannot describe different players. ``form`` lets the caller supply the
        lognormal line factor instead of drawing it — the path simulator holds
        one factor fixed across a player's week, which is what makes his games
        correlated with each other without changing any single game's marginal.
        """
        p = self.params
        own = np.nonzero(hist.played)[0]
        group = int(hist.pos_group[-1]) if len(hist) else POS_FORWARD

        minutes = self._draw_minutes(hist, own, as_of, rng, n)
        use_own = rng.random(n) < self._own_weight(len(own))
        donor_comps, donor_minutes = self._draw_donors(
            hist, own, group, as_of, minutes, use_own, rng
        )
        if donor_comps is None:
            raise InsufficientHistory(
                f"player {sleeper_id} has {len(own)} played games before day {as_of}"
                f" and the league pool is empty for group {group}"
            )

        scale = minutes / np.clip(donor_minutes, p.donor_min_minutes, None)
        if p.form_sd > 0:
            # Median-preserving: widen the line without shifting its centre.
            scale = scale * (np.exp(rng.normal(0.0, p.form_sd, n)) if form is None else form)
        return score_matrix(_coerce_line(donor_comps * scale[:, None]), self.scoring)

    def project_path(
        self,
        sleeper_id: str,
        as_of: int,
        game_days: Sequence[int],
        fantasy_weeks: Sequence[int],
        *,
        rng: np.random.Generator,
        n_paths: int | None = None,
        dnp_scale: float = 1.0,
    ) -> np.ndarray:
        """Joint draws over a player's remaining games. ``(n_paths, len(game_days))``.

        ``dnp_scale`` multiplies the hazard. It exists for one specific, measured
        reason: **a lineup slot is evidence of availability.** Over held-out
        weeks the hazard predicts a 17.2% DNP rate for players their manager
        actually started, against a realised 8.5% — managers read the injury
        report before setting a lineup and the model cannot, because
        ``player_status`` is empty and ``/players/nba`` publishes only today's
        designation. Left uncorrected this understates every team total by
        around 26 points and makes banking look far safer than it is. The caller
        supplies a factor fit on **prior weeks only**; see
        ``lockin.backtest.starter_dnp_scale``.

        This is the function a week simulation must use. Drawing each game from
        :meth:`project` and treating the results as independent is wrong by a
        wide margin: availability is a persistent state, so independent draws
        understate P(he misses the whole week) by up to 28x and price the
        ride-into-a-zero disaster at roughly 2% when it happens 13.4% of the
        time. See implementation-plan.md §13.

        Two things are propagated along each path:

        - **Availability.** Each drawn outcome is fed back into the hazard's
          own feature vector before the next game is drawn, so a simulated
          absence raises the simulated chance of the next absence exactly as a
          real one would. The coefficients are fit once at ``as_of`` and never
          refit, so nothing here reaches past the cutoff.
        - **Form.** One lognormal line factor per path, held across the week. A
          player having a heavy week has it in all his games. Because each game
          still sees a draw from the same distribution, this changes the joint
          without disturbing the marginal the Phase 3 gate certified.

        Minutes shocks stay independent per game. A minutes restriction really
        does persist across a week, so this understates the correlation
        slightly — but splitting the shock into persistent and per-game parts
        adds a parameter with no held-out weeks left to fit it honestly.
        """
        n = n_paths or self.params.n_paths
        hist = self.panel.history(sleeper_id).before(as_of)
        model = self.dnp_model(as_of)
        fallback = float((~hist.played).mean()) if len(hist) else 0.25

        form = (
            np.exp(rng.normal(0.0, self.params.form_sd, n))
            if self.params.form_sd > 0
            else np.ones(n)
        )

        days = np.asarray(hist.day, dtype=np.int64)
        played = np.repeat(hist.played[None, :], n, axis=0)
        out = np.zeros((n, len(game_days)))

        for g, (day, week) in enumerate(zip(game_days, fantasy_weeks, strict=True)):
            if model is None:
                p_dnp = np.full(n, fallback)
            else:
                p_dnp = model.predict(dnp_feature_matrix(days, played, int(day), int(week)))
            if dnp_scale != 1.0:
                p_dnp = np.clip(p_dnp * dnp_scale, 0.0, 1.0)
            plays = rng.random(n) >= p_dnp
            scores = self._draw_played_scores(sleeper_id, hist, as_of, rng, n, form=form)
            out[:, g] = np.where(plays, scores, 0.0)

            days = np.append(days, int(day))
            played = np.hstack([played, plays[:, None]])
        return out

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
