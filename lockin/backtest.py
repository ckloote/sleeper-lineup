"""Phase 4 gate: replay the season under each stopping policy.

Every roster, every week, the *actual* lineup, only the stopping rule varying.
Holding the lineup fixed is what makes this a clean read on the decision the
engine actually makes. Letting the policy pick lineups too would confound the
stopping question with an assignment question and, worse, would compare against
lineups nobody ever fielded.

The four policies, all of them the same walk over a week under different
thresholds (see ``lockin.core.policy``):

| policy      | reads                    | mutated by Sleeper? |
|-------------|--------------------------|---------------------|
| Actual      | ``counted_points``       | **yes** — advisory  |
| Never lock  | box scores + schedule    | no                  |
| Lock first  | box scores + schedule    | no                  |
| Greedy      | box scores + projections | no                  |

Only **Actual** touches the field Sleeper rewrote (§12), so it is reported and
labelled rather than dropped, and nothing is gated on it. The other three replay
from box scores, which were byte-identical across the observed mutation.

Rollout is Phase 5 and is absent here.

**Point-in-time throughout.** A threshold for a decision taken after the game on
day *d* is computed from a path simulation cut at *d + 1* — every game through
day *d* is known, nothing after it is. The hazard coefficients come from the
same cutoff.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from lockin.core.policy import Game, WeekOutcome, continuation_value, lock_first_thresholds, replay
from lockin.core.projections import (
    EWMAProjectionSource,
    InsufficientHistory,
    ProjectionParams,
    SeasonPanel,
)
from lockin.projections import load_panel, observed_scores
from lockin.verify import Check, scoring_settings

DEFAULT_HOLDOUT_FROM = 18
"""Same contiguous holdout as Phase 3. The projection layer's hyperparameters
were chosen on weeks 1-17, and the greedy policy is built on that layer, so
scoring it on those weeks would inherit the tuning."""

NEVER_LOCK = "never_lock"
LOCK_FIRST = "lock_first"
GREEDY = "greedy"
ACTUAL = "actual"
ORACLE = "oracle"
REPLAYED_POLICIES = (NEVER_LOCK, LOCK_FIRST, GREEDY)
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


@dataclass(slots=True)
class BacktestResult:
    rows: list[RosterWeek]
    decisions: int = 0
    """Player-weeks where a real choice existed — two or more countable games."""
    skipped: int = 0

    def holdout(self, holdout_from: int) -> BacktestResult:
        return BacktestResult(
            rows=[r for r in self.rows if r.week >= holdout_from],
            decisions=self.decisions,
        )

    def points(self, policy: str) -> np.ndarray:
        return np.array([r.points[policy] for r in self.rows if policy in r.points])

    def zeroed(self, policy: str) -> int:
        return sum(r.zeroed.get(policy, 0) for r in self.rows)

    def locked(self, policy: str) -> int:
        return sum(r.locked.get(policy, 0) for r in self.rows)

    def starters(self) -> int:
        return sum(r.starters for r in self.rows)


def _player_games(panel: SeasonPanel, scores: np.ndarray, sleeper_id: str, week: int) -> list[Game]:
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


def run_backtest(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
    n_paths: int = 400,
    seed: int = 20260808,
    panel: SeasonPanel | None = None,
) -> BacktestResult:
    """Replay all ten rosters, all weeks, under every policy."""
    scoring = scoring_settings(conn)
    panel = panel or load_panel(conn, season, params=params)
    source = EWMAProjectionSource(panel, scoring, params)
    scores = observed_scores(panel, scoring)
    rng = np.random.default_rng(seed)

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

    result = BacktestResult(rows=[])
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
            games = _player_games(panel, scores, sleeper_id, week)
            if not games:
                result.skipped += 1
                continue
            if len(games) >= 2:
                result.decisions += 1

            outcomes: dict[str, WeekOutcome] = {
                NEVER_LOCK: replay(games, None),
                LOCK_FIRST: replay(games, lock_first_thresholds(games)),
                GREEDY: replay(
                    games, greedy_thresholds(source, sleeper_id, games, week, rng, n_paths)
                ),
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
    x, y = result.points(a), result.points(b)
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


def run(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
    n_paths: int = 400,
    holdout_from: int = DEFAULT_HOLDOUT_FROM,
    seed: int = 20260808,
    result: BacktestResult | None = None,
) -> tuple[list[Check], BacktestResult]:
    full = (
        result
        if result is not None
        else run_backtest(conn, season, params=params, n_paths=n_paths, seed=seed)
    )
    held = full.holdout(holdout_from)
    if not held.rows:
        raise RuntimeError(f"no roster-weeks in weeks >= {holdout_from}")
    checks = [
        check_greedy_beats_never_lock(held),
        check_greedy_beats_lock_first(held),
        check_no_foresight(held),
        check_zeroed_slots(held),
        check_lock_rate_is_selective(held),
    ]
    return checks, full
