"""Phase 4 and 5 gates: replay the season under each stopping policy.

Every roster, every week, the *actual* lineup, only the stopping rule varying.
Holding the lineup fixed is what makes this a clean read on the decision the
engine actually makes. Letting the policy pick lineups too would confound the
stopping question with an assignment question and, worse, would compare against
lineups nobody ever fielded.

The policies, all of them the same walk over a week under different thresholds
(see ``lockin.core.policy``):

| policy      | objective   | reads                     | mutated by Sleeper? |
|-------------|-------------|---------------------------|---------------------|
| Actual      | —           | ``counted_points``        | **yes** — advisory  |
| Never lock  | —           | box scores + schedule     | no                  |
| Lock first  | —           | box scores + schedule     | no                  |
| Greedy      | points      | box scores + projections  | no                  |
| Rollout     | P(win)      | the above + an opponent   | no                  |

Only **Actual** touches the field Sleeper rewrote (§12), so it is reported and
labelled rather than dropped, and nothing is gated on it. The rest replay from
box scores, which were byte-identical across the observed mutation.

Phase 4 asks whether greedy beats never-lock **on points**. Phase 5 asks whether
rollout beats greedy **on wins**, which is the only currency the league pays out
in — and which per §7.1 needs all ten rosters replayed to be measurable at all.

**Point-in-time throughout.** A threshold for a decision taken after the game on
day *d* is computed from a path simulation cut at *d + 1* — every game through
day *d* is known, nothing after it is. The hazard coefficients, and the
started-player correction in :func:`starter_dnp_scale`, come from the same
cutoff.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import statistics
import zlib
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from lockin.core.policy import Game, WeekOutcome, continuation_value, lock_first_thresholds, replay
from lockin.core.projections import (
    EWMAProjectionSource,
    InsufficientHistory,
    ProjectionParams,
    SeasonPanel,
)
from lockin.projections import load_panel, observed_scores
from lockin.rollout import SimulationCache, replay_week
from lockin.store.db import connect_readonly
from lockin.verify import Check, scoring_settings

DEFAULT_HOLDOUT_FROM = 18

DEFAULT_PATHS = 2000
"""Simulated paths per decision, for greedy's thresholds and rollout's P(win) alike.

400 until 2026-09-24. Across 40 seeds at 400, rollout beat greedy by 5.3 wins
and the Phase 5 gate passed in 14 of 40; at 2,000 it beat greedy by 8.8 and
passed in 10 of 12, giving up fewer points. Greedy won 117 or 118 in every run:
the noise was rollout's own, deciding near-tied lock/pass calls on two noisy
estimates (implementation-plan.md §15, §21 W8)."""

DEFAULT_SEED = 20260808

DEFAULT_SEEDS = 5
"""Independent replays the Phase 5 gate is judged over. One replay is one draw of
the policy's own Monte Carlo, and at 400 paths that draw decided the gate."""
"""Same contiguous holdout as Phase 3. The projection layer's hyperparameters
were chosen on weeks 1-17, and the greedy policy is built on that layer, so
scoring it on those weeks would inherit the tuning."""

NEVER_LOCK = "never_lock"
LOCK_FIRST = "lock_first"
GREEDY = "greedy"
ROLLOUT = "rollout"
ACTUAL = "actual"
ORACLE = "oracle"
REPLAYED_POLICIES = (NEVER_LOCK, LOCK_FIRST, GREEDY, ROLLOUT)
ALL_POLICIES = (*REPLAYED_POLICIES, ORACLE)

MAX_ORACLE_SHARE = 0.90
"""How much of perfect foresight's headroom a legitimate policy may capture.

The architecture doc's §12 warning — "if it claims more, suspect leakage" —
needs a number to be enforceable, and this is it. For iid draws the *optimal*
stopping rule captures 70.7% / 74.4% / 76.9% of the gap between never-lock and
best-game for 2 / 3 / 4 games. Nothing without foresight gets far past that, so
a policy above 90% is reading the future, not deciding well."""


@dataclass(slots=True)
class RosterWeek:
    """One team's week under every policy."""

    week: int
    roster_id: int
    matchup_id: int | None
    points: dict[str, float] = field(default_factory=dict)
    zeroed: dict[str, int] = field(default_factory=dict)
    locked: dict[str, int] = field(default_factory=dict)
    actual_points: float | None = None
    starters: int = 0
    rollout_decisions: int = 0


@dataclass(slots=True)
class BacktestResult:
    rows: list[RosterWeek]
    decisions: int = 0
    """Player-weeks where a real choice existed — two or more countable games."""
    skipped: int = 0
    seed: int | None = None
    n_paths: int | None = None

    def holdout(self, holdout_from: int) -> BacktestResult:
        return BacktestResult(
            rows=[r for r in self.rows if r.week >= holdout_from],
            decisions=self.decisions,
            seed=self.seed,
            n_paths=self.n_paths,
        )

    def points(self, policy: str) -> np.ndarray:
        return np.array([r.points[policy] for r in self.rows if policy in r.points])

    def paired_points(self, a: str, b: str) -> tuple[np.ndarray, np.ndarray]:
        """Both policies' points over the roster-weeks where **both** ran.

        Rollout needs an opponent, so it is absent from weeks 23-24's eliminated
        teams and from unscored week 25. Comparing its mean against a policy
        that ran everywhere would compare different sets of weeks and quietly
        favour whichever set was easier.
        """
        rows = [r for r in self.rows if a in r.points and b in r.points]
        return (
            np.array([r.points[a] for r in rows]),
            np.array([r.points[b] for r in rows]),
        )

    def zeroed(self, policy: str) -> int:
        return sum(r.zeroed.get(policy, 0) for r in self.rows)

    def locked(self, policy: str) -> int:
        return sum(r.locked.get(policy, 0) for r in self.rows)

    def starters(self) -> int:
        return sum(r.starters for r in self.rows)


def player_games(panel: SeasonPanel, scores: np.ndarray, sleeper_id: str, week: int) -> list[Game]:
    """A player's countable games in one fantasy week, in date order.

    Exhibitions and postponed fixtures are already excluded from the panel, so
    the All-Star Game cannot masquerade as somebody's final game of the week and
    a fixture that never happened cannot zero anyone.
    """
    hist = panel.histories.get(sleeper_id)
    if hist is None:
        return []
    base = panel.offsets[sleeper_id]
    idx = np.nonzero(hist.week == week)[0]
    return [
        Game(
            index=int(k),
            day=int(hist.day[k]),
            played=bool(hist.played[k]),
            score=float(scores[base + k]),
        )
        for k in idx
    ]


def starter_dnp_scale(
    panel: SeasonPanel,
    source: EWMAProjectionSource,
    is_starter: np.ndarray,
    week: int,
    week_start_day: int,
    *,
    min_games: int = 300,
    bounds: tuple[float, float] = (0.2, 2.0),
) -> float:
    """How much the hazard over-predicts absence for players who get started.

    A lineup slot is evidence. Managers read the injury report before setting a
    lineup; the model cannot, because ``player_status`` is empty for the whole
    season and ``/players/nba`` carries only today's designation. Measured over
    held-out weeks the hazard predicts a **17.2%** DNP rate for started players
    against a realised **8.5%**, while for benched players it is close (36.3%
    against 39.1%). Uncorrected, that understates a six-man team total by about
    26 points, which makes an opponent look beatable and banking look safe — and
    it is why the first rollout build scored *worse* than the policy it was
    supposed to improve on.

    The correction is the realised-over-predicted DNP ratio among started
    player-games in **strictly earlier weeks**, so it never sees the week it is
    applied to. It is a stand-in for the injury feed the live engine will
    actually have, not a free parameter: there is nothing to tune, and the
    quantity it estimates is directly observable.
    """
    model = source.dnp_model(week_start_day)
    mask = is_starter & (panel.day < week_start_day)
    if model is None or int(mask.sum()) < min_games:
        return 1.0
    predicted = float(model.predict(panel.dnp_features[mask]).sum())
    if predicted <= 0:
        return 1.0
    realised = float(panel.dnp_target[mask].sum())
    return float(np.clip(realised / predicted, *bounds))


def greedy_thresholds(
    source: EWMAProjectionSource,
    sleeper_id: str,
    games: list[Game],
    week: int,
    rng: np.random.Generator,
    n_paths: int,
) -> dict[int, float]:
    """The score each played game must beat for banking it to be correct.

    One path simulation per decision point, cut the day after that game. The
    threshold is the expected value of riding on and stopping optimally, which
    is exactly what tonight's score is competing against.
    """
    thresholds: dict[int, float] = {}
    for position, game in enumerate(games):
        if not game.played:
            continue  # nothing to bank
        remaining = games[position + 1 :]
        if not remaining:
            continue  # last game: the decision is moot, it counts either way
        try:
            paths = source.project_path(
                sleeper_id,
                game.day + 1,
                [g.day for g in remaining],
                [week] * len(remaining),
                rng=rng,
                n_paths=n_paths,
            )
        except InsufficientHistory:
            continue  # no basis to bank on; ride
        thresholds[game.index] = continuation_value(paths)
    return thresholds


GREEDY_STREAM, ROLLOUT_STREAM = 1, 2


def stream(seed: int, kind: int, week: int, key: int | str) -> np.random.Generator:
    """The random draws for one piece of the replay, independent of every other.

    The replay used to draw everything from one generator, in order. Any change
    to any input — a revised box score in week 3 — then shifted every draw after
    it, which is a reseed of the whole season. The Phase 5 gate went from z=+2.06
    to +1.15 that way with no code change (implementation-plan.md §15). Keyed
    streams keep a change inside the roster-week it touches.
    """
    part = zlib.crc32(key.encode()) if isinstance(key, str) else key
    return np.random.default_rng([seed, kind, week, part])


def run_backtest(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
    n_paths: int = DEFAULT_PATHS,
    n_sims: int | None = None,
    seed: int = DEFAULT_SEED,
    panel: SeasonPanel | None = None,
) -> BacktestResult:
    """Replay all ten rosters, all weeks, under every policy.

    ``n_sims``, rollout's paths per P(win) estimate, defaults to ``n_paths``.
    They used to default separately, so ``--paths`` sharpened greedy's
    thresholds and left rollout's decisions exactly as noisy as before.
    """
    n_sims = n_paths if n_sims is None else n_sims
    scoring = scoring_settings(conn)
    panel = panel or load_panel(conn, season, params=params)
    source = EWMAProjectionSource(panel, scoring, params)
    scores = observed_scores(panel, scoring)
    panel_weeks = np.concatenate([h.week for h in panel.histories.values()])

    lineups: dict[tuple[int, int], list[str]] = defaultdict(list)
    matchups: dict[tuple[int, int], int | None] = {}
    for row in conn.execute(
        """
        SELECT week, roster_id, matchup_id, sleeper_id
          FROM weekly_matchups_latest
         WHERE is_starter = 1
         ORDER BY week, roster_id, slot_index
        """
    ):
        key = (row["week"], row["roster_id"])
        lineups[key].append(row["sleeper_id"])
        matchups[key] = row["matchup_id"]

    actuals = {
        (row["week"], row["roster_id"]): row["points"]
        for row in conn.execute("SELECT week, roster_id, points FROM weekly_matchup_teams_latest")
    }

    # Opponent lookup: a matchup has exactly two rosters.
    opponents: dict[tuple[int, int], int] = {}
    by_matchup: dict[tuple[int, int], list[int]] = defaultdict(list)
    for (week, roster_id), matchup_id in matchups.items():
        if matchup_id is not None:
            by_matchup[(week, matchup_id)].append(roster_id)
    for (week, _), members in by_matchup.items():
        if len(members) == 2:
            opponents[(week, members[0])] = members[1]
            opponents[(week, members[1])] = members[0]

    games_cache: dict[tuple[str, int], list[Game]] = {}

    def games_for(sleeper_id: str, week: int) -> list[Game]:
        key = (sleeper_id, week)
        if key not in games_cache:
            games_cache[key] = player_games(panel, scores, sleeper_id, week)
        return games_cache[key]

    threshold_cache: dict[tuple[str, int], dict[int, float]] = {}

    def thresholds_for(sleeper_id: str, week: int) -> dict[int, float]:
        key = (sleeper_id, week)
        if key not in threshold_cache:
            threshold_cache[key] = greedy_thresholds(
                source,
                sleeper_id,
                games_for(sleeper_id, week),
                week,
                stream(seed, GREEDY_STREAM, week, sleeper_id),
                n_paths,
            )
        return threshold_cache[key]

    def lineup_for(week: int, roster_id: int) -> dict[str, list[Game]]:
        return {
            pid: games_for(pid, week)
            for pid in lineups.get((week, roster_id), [])
            if games_for(pid, week)
        }

    # A lineup slot is evidence of availability the model cannot otherwise see.
    # Fit the correction per week from strictly earlier weeks.
    starter_rows = np.zeros(len(panel.day), dtype=bool)
    for (week, _), starters in lineups.items():
        for pid in starters:
            hist = panel.histories.get(pid)
            if hist is None:
                continue
            base = panel.offsets[pid]
            starter_rows[base + np.nonzero(hist.week == week)[0]] = True

    week_start = {
        week: int(panel.day[panel_weeks == week].min())
        for week in np.unique(panel_weeks)
        if (panel_weeks == week).any()
    }
    dnp_scale = {
        week: starter_dnp_scale(panel, source, starter_rows, week, day)
        for week, day in week_start.items()
    }

    cache = SimulationCache(
        source=source, n_sims=n_sims, dnp_scale=dnp_scale, ride_unprojectable=True
    )

    result = BacktestResult(rows=[], seed=seed, n_paths=n_paths)
    for (week, roster_id), starters in sorted(lineups.items()):
        entry = RosterWeek(
            week=week,
            roster_id=roster_id,
            matchup_id=matchups[(week, roster_id)],
            actual_points=actuals.get((week, roster_id)),
            starters=len(starters),
        )
        totals = dict.fromkeys(ALL_POLICIES, 0.0)
        zeros = dict.fromkeys(ALL_POLICIES, 0)
        locks = dict.fromkeys(REPLAYED_POLICIES, 0)

        for sleeper_id in starters:
            games = games_for(sleeper_id, week)
            if not games:
                result.skipped += 1
                continue
            if len(games) >= 2:
                result.decisions += 1

            outcomes: dict[str, WeekOutcome] = {
                NEVER_LOCK: replay(games, None),
                LOCK_FIRST: replay(games, lock_first_thresholds(games)),
                GREEDY: replay(games, thresholds_for(sleeper_id, week)),
            }
            for name, outcome in outcomes.items():
                totals[name] += outcome.counted
                zeros[name] += int(outcome.zeroed)
                locks[name] += int(outcome.locked_index is not None)

            # Perfect foresight. Not a policy — a ceiling, so that a suspiciously
            # good result can be told apart from an impossible one.
            playable = [g.score for g in games if g.played]
            best = max(playable) if playable else 0.0
            totals[ORACLE] += best
            zeros[ORACLE] += int(best == 0.0)

        # Rollout needs the whole week walked at once with shared state, and it
        # needs an opponent, so it cannot join the per-player loop above.
        opponent_id = opponents.get((week, roster_id))
        mine = lineup_for(week, roster_id)
        if opponent_id is not None and mine:
            theirs = lineup_for(week, opponent_id)
            outcome = replay_week(
                mine,
                theirs,
                {pid: thresholds_for(pid, week) for pid in theirs},
                week,
                cache,
                stream(seed, ROLLOUT_STREAM, week, roster_id),
            )
            totals[ROLLOUT] = outcome.total
            zeros[ROLLOUT] = sum(1 for v in outcome.counted.values() if v == 0.0)
            locks[ROLLOUT] = len(outcome.locked)
            entry.rollout_decisions = outcome.decisions
        else:
            # Weeks 23-24 drop eliminated teams and week 25 is unscored, so
            # there is no opponent and no win to play for. Left absent rather
            # than filled with the greedy value, which would quietly pad the
            # comparison with roster-weeks rollout never actually played.
            del totals[ROLLOUT]
            del zeros[ROLLOUT]
            del locks[ROLLOUT]

        entry.points = totals
        entry.zeroed = zeros
        entry.locked = locks
        result.rows.append(entry)
    return result


# --------------------------------------------------------------------- checks


def _paired(result: BacktestResult, a: str, b: str) -> tuple[float, float, float]:
    """Mean difference a − b over the same roster-weeks, and its paired t.

    Paired because the same ten rosters play the same schedules under both
    policies; the week-to-week variance is enormous and shared, and an unpaired
    comparison would drown a real effect in it.
    """
    x, y = result.paired_points(a, b)
    if len(x) == 0:
        return 0.0, float("inf"), 0.0
    diff = x - y
    mean = float(diff.mean())
    se = float(diff.std(ddof=1) / np.sqrt(len(diff))) if len(diff) > 1 else float("inf")
    return mean, se, (mean / se if se else 0.0)


def check_greedy_beats_never_lock(result: BacktestResult) -> Check:
    """The Phase 4 exit criterion."""
    mean, se, t = _paired(result, GREEDY, NEVER_LOCK)
    n = len(result.points(GREEDY))
    offenders = []
    if mean <= 0:
        offenders.append(f"greedy scores {mean:+.2f} points per roster-week against never-lock")
    elif t < 2.0:
        offenders.append(f"gain of {mean:+.2f} is not distinguishable from noise (t={t:.2f})")
    return Check(
        name="greedy threshold beats never-lock on points, out of sample",
        passed=not offenders,
        detail=f"{mean:+.2f} points per roster-week (se {se:.2f}, t={t:.2f}) over {n} roster-weeks",
        offenders=offenders,
    )


def check_greedy_beats_lock_first(result: BacktestResult) -> Check:
    """Banking early is not the same as banking well.

    Lock-first is the policy that never rides, and it is a real strategy rather
    than a straw man — it removes all zero risk. If the threshold cannot beat
    it, the engine is not choosing *when* to bank, it is just banking.
    """
    mean, se, t = _paired(result, GREEDY, LOCK_FIRST)
    offenders = []
    if mean <= 0:
        offenders.append(f"greedy scores {mean:+.2f} points per roster-week against lock-first")
    return Check(
        name="greedy threshold beats lock-first on points",
        passed=not offenders,
        detail=f"{mean:+.2f} points per roster-week (se {se:.2f}, t={t:.2f})",
        offenders=offenders,
    )


def check_zeroed_slots(result: BacktestResult) -> Check:
    """How often a starter slot counted nothing, by policy.

    Architecture doc §12 asks for this alongside points. It is the failure that
    loses weeks outright rather than narrowly, and it is the one a manager
    actually notices.
    """
    starters = result.starters()
    parts = [
        f"{name}: {result.zeroed(name)} ({result.zeroed(name) / starters:.1%})"
        for name in REPLAYED_POLICIES
    ]
    never, greedy = result.zeroed(NEVER_LOCK), result.zeroed(GREEDY)
    offenders = []
    if greedy > never:
        offenders.append(f"greedy zeroes more slots than never-lock ({greedy} vs {never})")
    return Check(
        name="greedy zeroes no more starter slots than never-lock",
        passed=not offenders,
        detail=f"{starters} starter-weeks; " + "; ".join(parts),
        offenders=offenders,
    )


def check_lock_rate_is_selective(result: BacktestResult) -> Check:
    """A threshold policy that always fires, or never does, is not a policy.

    Both degenerate ends would still pass a points comparison by accident —
    always-lock is lock-first wearing a different name, never-lock is the
    baseline — so the gate says so explicitly.
    """
    starters = result.starters()
    rate = result.locked(GREEDY) / starters if starters else 0.0
    offenders = []
    if not 0.05 < rate < 0.95:
        offenders.append(
            f"greedy locks {rate:.1%} of starter-weeks; it has collapsed to a constant"
        )
    return Check(
        name="greedy actually chooses when to bank",
        passed=not offenders,
        detail=f"locks {result.locked(GREEDY)}/{starters} starter-weeks ({rate:.1%});"
        f" lock-first {result.locked(LOCK_FIRST) / starters:.1%}",
        offenders=offenders,
    )


def check_no_foresight(result: BacktestResult) -> Check:
    """The leakage guard: greedy must stay well short of perfect foresight.

    Architecture doc §12 says an honest backtest shows a modest gain and that a
    large one should be treated as leakage. The gain here is *not* modest —
    never-lock is a genuinely bad policy in this format, zeroing about one
    starter slot in seven — so the warning needs a sharper form than "is the
    number big".

    Perfect foresight is the right yardstick. It banks each player's best game,
    which is unattainable without knowing the future, and anything approaching
    it is reading ahead rather than choosing well.
    """
    never = result.points(NEVER_LOCK).mean()
    greedy = result.points(GREEDY).mean()
    oracle = result.points(ORACLE).mean()
    headroom = oracle - never
    share = (greedy - never) / headroom if headroom > 0 else 0.0

    offenders = []
    if greedy >= oracle:
        offenders.append(f"greedy ({greedy:.2f}) matches or beats perfect foresight ({oracle:.2f})")
    elif share > MAX_ORACLE_SHARE:
        offenders.append(
            f"greedy captures {share:.1%} of foresight's headroom, above the"
            f" {MAX_ORACLE_SHARE:.0%} ceiling — suspect leakage"
        )
    return Check(
        name="greedy does not approach perfect foresight",
        passed=not offenders,
        detail=(
            f"never-lock {never:.2f} < greedy {greedy:.2f} < oracle {oracle:.2f};"
            f" greedy captures {share:.1%} of the headroom"
            f" (optimal stopping on iid draws captures ~75%)"
        ),
        offenders=offenders,
    )


def head_to_head(
    result: BacktestResult, policy: str, against: str, opponent: str = GREEDY
) -> np.ndarray:
    """Paired win indicators: ``(n_team_weeks, 2)`` for ``policy`` and ``against``.

    Both are played against the *same* opponent policy on the *same* team-weeks,
    so the comparison is paired and the enormous week-to-week variance cancels.
    Raw win counts would not resolve an effect this size — which is the whole
    substance of implementation-plan.md §7.1.
    """
    return head_to_head_by_matchup(result, policy, against, opponent)[0]


def head_to_head_by_matchup(
    result: BacktestResult, policy: str, against: str, opponent: str = GREEDY
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """`head_to_head`, with the matchup each row came from.

    Every matchup contributes two rows, one per side, and they are not
    independent: both sides' `against` column is the same greedy-v-greedy game
    read from opposite ends (review 2026-09-23). `clustered_mcnemar` needs to
    know which rows belong together.
    """
    by_matchup: dict[tuple[int, int], list[RosterWeek]] = defaultdict(list)
    for row in result.rows:
        if row.matchup_id is not None:
            by_matchup[(row.week, row.matchup_id)].append(row)

    pairs, clusters = [], []
    for key, entries in by_matchup.items():
        if len(entries) != 2:
            continue
        for me, them in (entries, entries[::-1]):
            if policy in me.points and against in me.points and opponent in them.points:
                pairs.append(
                    (
                        me.points[policy] > them.points[opponent],
                        me.points[against] > them.points[opponent],
                    )
                )
                clusters.append(key)
    return np.array(pairs, dtype=bool).reshape(-1, 2), clusters


def mcnemar(pairs: np.ndarray) -> tuple[int, int, float]:
    """Discordant counts and a z for a paired binary comparison.

    Only the team-weeks where the two policies disagree carry information; the
    rest are ties whichever way they went. ``b`` is where the first policy wins
    and the second does not, ``c`` the reverse.
    """
    if len(pairs) == 0:
        return 0, 0, 0.0
    b = int((pairs[:, 0] & ~pairs[:, 1]).sum())
    c = int((~pairs[:, 0] & pairs[:, 1]).sum())
    z = (b - c) / np.sqrt(b + c) if b + c else 0.0
    return b, c, float(z)


def clustered_mcnemar(pairs: np.ndarray, clusters: list) -> float:
    """McNemar's z with each matchup's two rows taken as one unit.

    The ordinary statistic is Σd / sqrt(Σd²) over rows, d = +1, -1 or 0 for
    each discordance. Summing d within a matchup first and squaring the sums
    lets the two sides' correlation — whichever sign it has — reach the
    variance, and reduces to the ordinary z when every matchup has one row.
    """
    if len(pairs) == 0:
        return 0.0
    d = pairs[:, 0].astype(int) - pairs[:, 1].astype(int)
    per: dict = defaultdict(int)
    for key, value in zip(clusters, d, strict=True):
        per[key] += int(value)
    sums = np.array(list(per.values()), dtype=float)
    denom = np.sqrt((sums**2).sum())
    return float(sums.sum() / denom) if denom else 0.0


Replays = BacktestResult | Sequence[BacktestResult]
"""One replay of the season, or several under different seeds."""


def _replays(result: Replays) -> list[BacktestResult]:
    return [result] if isinstance(result, BacktestResult) else list(result)


def check_rollout_beats_greedy_on_wins(result: Replays) -> Check:
    """The Phase 5 exit criterion, restated to be measurable (§7.1).

    The architecture doc asks for "rollout beats greedy on wins in held-out
    weeks". Taken literally that is one roster's five or six matchups, which a
    modest effect cannot clear — the gate would be decided by coin flips. §7.1's
    adopted fix is to replay **all ten rosters**, turning 21 matchups into 105
    and making a paired test possible at all.

    Those 236 team-weeks are 118 matchups seen from both sides, and the two
    sides are not independent (review 2026-09-23). The z is also computed with
    each matchup as one unit, and the gate needs **both** to clear 1.64: the
    correction must not become a way to pass.

    **Judged over several replays** (2026-09-24). One replay is one draw of the
    policies' own Monte Carlo, and at 400 paths that draw decided the gate: it
    passed in 14 of 40 seeds on identical data. Over several, rollout must out-win
    greedy in *every* replay, and the *median* z must clear 1.64 both ways.
    """
    runs = []
    for one in _replays(result):
        pairs, clusters = head_to_head_by_matchup(one, ROLLOUT, GREEDY)
        b, c, z = mcnemar(pairs)
        runs.append(
            {
                "seed": one.seed,
                "rollout": int(pairs[:, 0].sum()),
                "greedy": int(pairs[:, 1].sum()),
                "b": b,
                "c": c,
                "z": z,
                "z_matchup": clustered_mcnemar(pairs, clusters),
                "rows": len(pairs),
                "matchups": len(set(clusters)),
            }
        )
    z_mid = statistics.median(r["z"] for r in runs)
    zm_mid = statistics.median(r["z_matchup"] for r in runs)
    behind = [r for r in runs if r["rollout"] <= r["greedy"]]

    offenders = []
    if len(runs) == 1:
        (only,) = runs
        if behind:
            offenders.append(f"rollout wins {only['rollout']}, greedy wins {only['greedy']}")
        elif min(z_mid, zm_mid) < 1.64:
            offenders.append(
                f"rollout leads {only['rollout']}-{only['greedy']} but the paired test is"
                f" inconclusive (z={z_mid:.2f}, by matchup {zm_mid:.2f}; both need 1.64)"
            )
        detail = (
            f"{only['rows']} team-weeks in {only['matchups']} matchups: rollout"
            f" {only['rollout']} wins, greedy {only['greedy']}; flipped +{only['b']}/-{only['c']},"
            f" McNemar z={only['z']:+.2f}, by matchup z={only['z_matchup']:+.2f}"
        )
    else:
        if behind:
            offenders.append(
                f"rollout does not out-win greedy in {len(behind)} of {len(runs)} replays"
                f" (seeds {', '.join(str(r['seed']) for r in behind)})"
            )
        elif min(z_mid, zm_mid) < 1.64:
            offenders.append(
                f"rollout leads in every replay but the median paired test is inconclusive"
                f" (z={z_mid:.2f}, by matchup {zm_mid:.2f}; both need 1.64)"
            )
        leads = [r["rollout"] - r["greedy"] for r in runs]
        zs = [r["z"] for r in runs]
        detail = (
            f"{len(runs)} replays of {runs[0]['rows']} team-weeks in {runs[0]['matchups']}"
            f" matchups: rollout out-wins greedy by {min(leads):+d} to {max(leads):+d}"
            f" (mean {statistics.mean(leads):+.1f}); median z={z_mid:+.2f}"
            f" (range {min(zs):+.2f} to {max(zs):+.2f}), by matchup {zm_mid:+.2f}"
        )
    return Check(
        name="rollout beats greedy on wins, all ten rosters, paired",
        passed=not offenders,
        detail=detail,
        offenders=offenders,
    )


def check_rollout_holdout_direction(result: Replays, holdout_from: int) -> Check:
    """The held-out block on its own — directional, and honest about power.

    §7.1 predicted this exact situation: a contiguous holdout leaves too few
    matchups to resolve a modest effect. It is checked for *direction* rather
    than significance, and the achievable power is printed so nobody reads a
    passing z as evidence it was not. Over several replays the direction is the
    mean one: four discordant pairs each is too few for any single replay to
    decide it.
    """
    runs = []
    for one in _replays(result):
        pairs = head_to_head(one.holdout(holdout_from), ROLLOUT, GREEDY)
        b, c, z = mcnemar(pairs)
        runs.append((int(pairs[:, 0].sum()), int(pairs[:, 1].sum()), b, c, z, len(pairs)))
    leads = [r[0] - r[1] for r in runs]
    offenders = []
    if len(runs) == 1:
        wins_rollout, wins_greedy, b, c, z, n = runs[0]
        if wins_rollout < wins_greedy:
            offenders.append(f"rollout loses on held-out wins: {wins_rollout} vs {wins_greedy}")
        detail = (
            f"{n} team-weeks: rollout {wins_rollout}, greedy {wins_greedy};"
            f" flipped +{b}/-{c}, z={z:+.2f}"
            f" — only {b + c} discordant pairs, too few to resolve significance (§7.1)"
        )
    else:
        if statistics.mean(leads) < 0:
            offenders.append(
                f"rollout loses on held-out wins on average: {statistics.mean(leads):+.2f}"
                f" across {len(runs)} replays"
            )
        discordant = statistics.mean(r[2] + r[3] for r in runs)
        detail = (
            f"{len(runs)} replays of {runs[0][5]} team-weeks: rollout − greedy"
            f" {min(leads):+d} to {max(leads):+d} (mean {statistics.mean(leads):+.2f}),"
            f" behind in {sum(x < 0 for x in leads)}"
            f" — about {discordant:.0f} discordant pairs each, too few to resolve"
            f" significance (§7.1)"
        )
    return Check(
        name=f"rollout does not lose on wins in held-out weeks {holdout_from}+",
        passed=not offenders,
        detail=detail,
        offenders=offenders,
    )


def check_rollout_trades_points_for_wins(result: Replays) -> Check:
    """Rollout should give up points. That is the objective working, not failing.

    Architecture doc §4: maximise P(win), not expected points. The two agree
    early in the week and diverge at the end, where the correct play is to take
    variance when behind and bank when ahead — neither of which a
    points-maximiser will do. A rollout that matched greedy on points would be
    evidence it was ignoring the opponent.

    The band is one-sided in spirit: giving up a little is expected, giving up a
    lot means the win-probability estimate is wrong rather than sharp. Over
    several replays it is the mean cost that is judged.
    """
    per = [_paired(one, ROLLOUT, GREEDY) for one in _replays(result)]
    mean = float(statistics.mean(p[0] for p in per))
    offenders = []
    if mean < -25.0:
        offenders.append(
            f"rollout sacrifices {-mean:.1f} points per roster-week; that is too much"
            f" to be explained by the objective and suggests a mispriced opponent"
        )
    if len(per) == 1:
        _, se, t = per[0]
        detail = f"{mean:+.2f} points per roster-week against greedy (se {se:.2f}, t={t:.2f})"
    else:
        means = [p[0] for p in per]
        detail = (
            f"{mean:+.2f} points per roster-week against greedy, mean of {len(per)} replays"
            f" (range {min(means):+.2f} to {max(means):+.2f})"
        )
    return Check(
        name="rollout trades points for win probability, as the objective intends",
        passed=not offenders,
        detail=detail,
        offenders=offenders,
    )


def wins_flipped(
    result: BacktestResult, policy: str, baseline: str = NEVER_LOCK
) -> tuple[int, int]:
    """Head-to-head record from adopting `policy` while the opponent does not.

    Points are the intermediate variable; the league pays out wins. This is the
    unilateral question — what adopting the policy buys against a field that
    has not — rather than a symmetric comparison, which is close to zero-sum by
    construction.
    """
    by_matchup: dict[tuple[int, int], list[RosterWeek]] = defaultdict(list)
    for row in result.rows:
        if row.matchup_id is not None:
            by_matchup[(row.week, row.matchup_id)].append(row)

    wins = games = 0
    for entries in by_matchup.values():
        if len(entries) != 2:
            continue
        for me, them in (entries, entries[::-1]):
            if policy not in me.points or baseline not in them.points:
                continue
            games += 1
            wins += int(me.points[policy] > them.points[baseline])
    return wins, games


def _replay(job: tuple[str, str, int, int, ProjectionParams | None]) -> BacktestResult:
    """One seed's replay, in a worker process with its own read-only connection."""
    db_path, season, seed, n_paths, params = job
    conn = connect_readonly(Path(db_path))
    try:
        return run_backtest(conn, season, params=params, n_paths=n_paths, seed=seed)
    finally:
        conn.close()


def run_seeds(
    db_path: Path,
    season: str,
    seeds: Sequence[int],
    *,
    n_paths: int = DEFAULT_PATHS,
    params: ProjectionParams | None = None,
    workers: int | None = None,
) -> list[BacktestResult]:
    """Replay the season once per seed, in parallel. Results come back in seed order.

    Replays are independent and CPU-bound, so each runs in its own process: five
    at 2,000 paths take about as long as two on the Pi's four cores. Workers are
    spawned rather than forked, so none inherits a connection or a thread.
    """
    jobs = [(str(db_path), season, s, n_paths, params) for s in seeds]
    workers = min(len(jobs), workers or multiprocessing.cpu_count())
    if workers <= 1:
        return [_replay(job) for job in jobs]
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        return list(pool.map(_replay, jobs))


def run(
    conn: sqlite3.Connection | None,
    season: str,
    *,
    params: ProjectionParams | None = None,
    n_paths: int = DEFAULT_PATHS,
    holdout_from: int = DEFAULT_HOLDOUT_FROM,
    seed: int = DEFAULT_SEED,
    result: BacktestResult | None = None,
    extra: Sequence[BacktestResult] = (),
) -> tuple[list[Check], BacktestResult]:
    """Every gate. Returns the checks and the first replay.

    The points and zeroing gates are read from the first replay: greedy, never-lock
    and lock-first barely move with the seed, and neither does their margin. The
    rollout gates are judged over ``result`` and every ``extra`` replay together.
    """
    full = (
        result
        if result is not None
        else run_backtest(conn, season, params=params, n_paths=n_paths, seed=seed)
    )
    replays = [full, *extra]
    held = full.holdout(holdout_from)
    if not held.rows:
        raise RuntimeError(f"no roster-weeks in weeks >= {holdout_from}")
    checks = [
        check_greedy_beats_never_lock(held),
        check_greedy_beats_lock_first(held),
        check_no_foresight(held),
        check_zeroed_slots(held),
        check_lock_rate_is_selective(held),
        # Phase 5. Pooled over all ten rosters per §7.1, because a contiguous
        # holdout on its own cannot resolve an effect this size.
        check_rollout_beats_greedy_on_wins(replays),
        check_rollout_holdout_direction(replays, holdout_from),
        check_rollout_trades_points_for_wins(replays),
    ]
    return checks, full
