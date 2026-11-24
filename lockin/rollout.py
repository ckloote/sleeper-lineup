"""Replaying a roster-week under the rollout policy.

The database-facing half of Phase 5. ``lockin.core.winprob`` decides a single
lock given samples; this assembles the samples, walks the week in order, and
keeps the state the decisions depend on.

Why a week cannot be replayed player by player, as Phase 4 did: with a points
objective each starter's stopping problem is separable, because points add. With
a **win-probability** objective they are not. Whether to bank a 40 depends on
what the rest of the team has already banked and on what the opponent is likely
to finish with, so the week has to be walked as one chronological process with
shared state.

Decisions are taken **once a day**, at the end of the day, over every starter who
played that day and still has games left. That is not a simplification of the
mechanic — it is the product. The tool exists to be read once a day, and a policy
that assumed intra-day check-ins would be measuring something nobody will do.

**Two dates, not one.** The backtest only ever asks about the end of a day that
has finished, where "what is known" and "what is past banking" are the same
instant. Phase 6's digest is read in the *morning*, and there they come apart:
tonight is unobserved but still bankable, and §7.2 requires the nights between
now and a forward threshold be assumed idle. Every simulation entry point
therefore takes ``known_through`` (observed through here) and ``act_from`` (first
night a lock may be taken) rather than a single ``after_day``. Where they
coincide the behaviour is exactly the Phase 5 behaviour, which is pinned by
``tests/test_digest.py::test_backtest_cutoffs_are_unchanged``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from lockin.core.eligibility import InvalidPin, NoValidLineup, _slot_key, assign_slots
from lockin.core.policy import Game, replay
from lockin.core.projections import EWMAProjectionSource, InsufficientHistory
from lockin.core.winprob import (
    RolloutDecision,
    apply_base_policy,
    base_policy_thresholds,
    evaluate_lock,
    lock_threshold,
)


@dataclass(slots=True)
class SimulationCache:
    """Path draws and base-policy thresholds, keyed by (player, cutoff).

    A decision day asks about a dozen players at the same cutoff, and every
    starter deciding that day asks about the same dozen. Without this the same
    simulation is run six times over.
    """

    source: EWMAProjectionSource
    n_sims: int
    dnp_scale: dict[int, float] = field(default_factory=dict)
    """Per-week hazard correction for started players, fit on prior weeks only.
    See ``lockin.backtest.starter_dnp_scale`` for why it is needed."""
    contributions: dict[tuple[str, int, int], np.ndarray] = field(default_factory=dict)
    bench_rows: dict[tuple[str, int, int, bool], np.ndarray | None] = field(default_factory=dict)
    """`bench_contribution`'s results. Kept apart from ``contributions``: the
    same player and cutoffs mean a different thing on the bench."""
    thresholds: dict[tuple[str, int], dict[int, float]] = field(default_factory=dict)
    misses: int = 0
    ride_unprojectable: bool = False
    """What to do with a player who cannot be projected but has games left.

    False, the default: raise `InsufficientHistory`. The alternative was to
    assume he rides to his final game and count that game's recorded score —
    which in a completed season is a known number, and in a live one is a game
    not yet played and so a certain 0.0. On opening day that turned every
    projection into zero and every P(win) into 50% (review finding 3). The
    digest therefore refuses up front instead (`lockin.digest.build`).

    The backtest and the manager evaluation replay a finished season and keep
    the old behaviour, explicitly. For the handful of early-season players it
    touches it reads the final game's real score — hindsight, but hindsight
    about a decision nobody could make, not about one being graded."""

    def contribution(
        self,
        sleeper_id: str,
        games: Sequence[Game],
        week: int,
        rng: np.random.Generator,
        *,
        known_through: int,
        act_from: int | None = None,
    ) -> np.ndarray:
        """What this player goes on to count from here. ``(n_sims,)``.

        Two dates, and keeping them apart is the point of the signature:

        ``known_through``
            The last day whose games are **observed**. Everything after it is
            simulated, conditioned on the history up to it.
        ``act_from``
            The first night a lock may be taken. Games before it are ridden
            through: they cannot be banked, and under the one-game-counts rule
            an unbanked game is forfeited. Defaults to ``known_through + 1`` —
            act at the first opportunity, which is the Phase 4-5 base policy.

        In the backtest the two always coincide, because decisions are taken at
        the end of a day: the day being decided is both the last day observed
        and the last day forfeited, and passing ``after_day`` for both was
        therefore correct. A **morning** digest breaks that. Tonight's games
        have not tipped, so ``known_through`` is yesterday, while the night a
        standing rule applies to may be tonight or later — and §7.2 requires
        the nights in between be assumed idle. Collapsing the two would read a
        score out of a game that has not been played.
        """
        act_from = known_through + 1 if act_from is None else act_from
        key = (sleeper_id, known_through, act_from)
        cached = self.contributions.get(key)
        if cached is not None:
            return cached

        remaining = [g for g in games if g.day > known_through]
        if not remaining:
            # Nothing left to decide: an unlocked player counts his final game,
            # which has already happened and may well be a 0.0.
            last = games[-1]
            value = np.full(self.n_sims, last.score if last.played else 0.0)
        else:
            try:
                paths = self.source.project_path(
                    sleeper_id,
                    known_through + 1,
                    [g.day for g in remaining],
                    [week] * len(remaining),
                    rng=rng,
                    n_paths=self.n_sims,
                    dnp_scale=self.dnp_scale.get(week, 1.0),
                )
            except InsufficientHistory:
                if not self.ride_unprojectable:
                    raise
                # Retrospective only: see `ride_unprojectable`.
                last = games[-1]
                value = np.full(self.n_sims, last.score if last.played else 0.0)
            else:
                # An idle night is a threshold no score can clear. Masking them
                # leaves the rest of the base policy untouched: each surviving
                # threshold is a continuation value over strictly later columns,
                # which the mask does not reach.
                thresholds = [
                    float("inf") if game.day < act_from else threshold
                    for game, threshold in zip(
                        remaining, base_policy_thresholds(paths), strict=True
                    )
                ]
                value = apply_base_policy(paths, thresholds)
                self.misses += 1
        self.contributions[key] = value
        return value

    def bench_contribution(
        self,
        sleeper_id: str,
        games: Sequence[Game],
        week: int,
        rng: np.random.Generator,
        *,
        known_through: int,
        start: int,
        regular: bool = False,
    ) -> np.ndarray | None:
        """What a **bench** player would count if moved into a slot from ``start``.

        Only his games on or after ``start`` can count: a player has to be in a
        starting slot when his game tips (§7.6), so the games he sits through
        on the bench are gone. They are still simulated, because what happens
        in them shapes his availability for the rest. From ``start`` on he is
        played under the same base policy as a starter, every game bankable.

        Two differences from :meth:`contribution`, both conservative:

        - **No starter hazard correction, unless he is a regular.** ``dnp_scale``
          halves the DNP hazard because a manager who started someone had read
          the injury report. Nobody has vouched for a bench player, so he gets
          the raw hazard. Overrating his availability would make passing look
          safer than it is, which is how a slot ends the week on 0.0. A
          ``regular`` (`lockin.rotation`) is someone the manager starts on his
          game nights, so he gets the starter correction.
        - **No fallback.** A bench player with nothing left, or who cannot be
          projected, returns None and is simply not an option. It is never a
          reason to stop advising.
        """
        key = (sleeper_id, known_through, start, regular)
        if key in self.bench_rows:
            return self.bench_rows[key]
        # Every game after the cutoff is simulated, including the ones he sits
        # through before ``start``: they cannot count, but rest, back-to-backs
        # and DNP state carry from them into the games that can.
        ahead = [g for g in games if g.day > known_through]
        value: np.ndarray | None = None
        if any(g.day >= start for g in ahead):
            try:
                paths = self.source.project_path(
                    sleeper_id,
                    known_through + 1,
                    [g.day for g in ahead],
                    [week] * len(ahead),
                    rng=rng,
                    n_paths=self.n_sims,
                    dnp_scale=self.dnp_scale.get(week, 1.0) if regular else 1.0,
                )
            except InsufficientHistory:
                value = None
            else:
                # As in `contribution`: a game before ``start`` is a threshold no
                # score clears, and the last game is always one he can count.
                thresholds = [
                    float("inf") if game.day < start else threshold
                    for game, threshold in zip(ahead, base_policy_thresholds(paths), strict=True)
                ]
                value = apply_base_policy(paths, thresholds)
        self.bench_rows[key] = value
        return value


@dataclass(slots=True)
class Bench:
    """Who else could fill a slot that has not been locked yet.

    The pass branch of a lock call used to be the player's own continuation
    and nothing else. That leaves out a choice the manager really has: pass,
    then bench him and start someone who plays later in the week. That choice
    is always worth at least as much as riding him, and leaving it out made
    every pass look too weak, so the engine locked too readily. Locking also
    *uses up* the slot, which was being priced as free.

    With a `Bench`, each branch fields the best legal lineup for its open slots
    (exactly, by `assign_slots`), from the unlocked starters and these players,
    each choice made on **mean** continuation. That is a decision available at
    decision time, which is what keeps this a rollout base policy; choosing per
    simulated path would choose with hindsight and inflate passing.

    ``slots`` are the starting slots in poll order and ``slot_of`` the slot key
    each current starter holds (`lockin.core.eligibility._slot_key`). Locked
    players are pinned there, because a banked score stands only while he stays
    in his slot.
    """

    slots: tuple[str, ...]
    slot_of: dict[str, str]
    games: dict[str, list[Game]]
    """Bench candidates' countable games this week. Callers leave out anyone
    who cannot be moved in (IR, designated out)."""
    positions: dict[str, tuple[str, ...]]
    """Starters' and bench players' eligible positions."""
    regulars: frozenset[str] = frozenset()
    """Bench players the manager starts on his game nights (`lockin.rotation`),
    priced with the starter hazard correction."""

    @classmethod
    def from_lineup(
        cls,
        slots: Sequence[str],
        starters: Sequence[str],
        games: dict[str, list[Game]],
        positions: dict[str, Sequence[str]],
        regulars: Sequence[str] = (),
    ) -> Bench:
        """``starters`` in the same order as ``slots`` — the poll's slot order."""
        if len(slots) != len(starters):
            raise ValueError(f"{len(starters)} starters for {len(slots)} slots")
        return cls(
            slots=tuple(slots),
            slot_of={pid: _slot_key(slots, i) for i, pid in enumerate(starters)},
            games={pid: list(g) for pid, g in games.items() if pid not in starters},
            positions={pid: tuple(pos) for pid, pos in positions.items()},
            regulars=frozenset(regulars),
        )

    def copy(self) -> Bench:
        return Bench(
            self.slots, dict(self.slot_of), dict(self.games), dict(self.positions), self.regulars
        )


def bench_rows(
    cache: SimulationCache,
    bench: Bench,
    lineup: dict[str, list[Game]],
    week: int,
    rng: np.random.Generator,
    *,
    known_through: int,
    start: int,
) -> dict[str, np.ndarray]:
    """Each usable bench player's row, for a swap taking effect from ``start``."""
    out = {}
    for pid, games in bench.games.items():
        if pid in lineup:
            continue
        row = cache.bench_contribution(
            pid,
            games,
            week,
            rng,
            known_through=known_through,
            start=start,
            regular=pid in bench.regulars,
        )
        if row is not None:
            out[pid] = row
    return out


def _field(
    bench: Bench, pool: dict[str, np.ndarray], locked: Sequence[str], pin: str | None
) -> dict[str, str] | None:
    """Best legal lineup by mean, or None when the data cannot support one."""
    pinned = [*locked, *([pin] if pin is not None else [])]
    if any(p not in bench.slot_of for p in pinned):
        return None
    candidates = [p for p in pool if p != pin]
    try:
        return assign_slots(
            list(bench.slots),
            candidates,
            bench.positions,
            {p: float(pool[p].mean()) for p in candidates},
            locked={bench.slot_of[p]: p for p in pinned},
        )
    except (NoValidLineup, InvalidPin):
        return None


@dataclass(frozen=True, slots=True)
class Branches:
    """The two lineups a lock call compares, once bench players are options."""

    passing: np.ndarray
    """Everything the open slots count if he is not banked. ``(n_sims,)``."""
    others_if_locked: np.ndarray | None
    """The other open slots, if he is. None when no one is deciding."""
    fielded: dict[str, str]
    """The pass branch's lineup, ``{slot key: player}``."""
    brought_in: tuple[str, ...]
    """Bench players the pass branch uses and the lock branch does not: the swap
    that passing pays for."""


def with_bench(
    bench: Bench,
    rows: dict[str, np.ndarray],
    extra: dict[str, np.ndarray],
    locked: Sequence[str],
    deciding: str | None,
    n_sims: int,
) -> Branches | None:
    """Field both branches. None means fall back to starters only.

    ``rows`` are the unlocked starters' continuations, ``extra`` the bench's.
    Falling back is always safe: it is exactly the pre-bench behaviour, and it
    is taken when there is no usable bench player or the positions on file
    cannot make a legal lineup.
    """
    if not extra:
        return None
    pool = {**rows, **extra}
    locked = list(locked)
    passing = _field(bench, pool, locked, None)
    if passing is None:
        return None
    zero = np.zeros(n_sims)
    total = sum((pool[p] for p in passing.values() if p not in locked), zero)
    if deciding is None:
        return Branches(total, None, passing, ())
    banking = _field(bench, pool, locked, deciding)
    if banking is None:
        return None
    others = sum((pool[p] for p in banking.values() if p not in locked and p != deciding), zero)
    used = set(banking.values())
    brought = tuple(sorted(p for p in passing.values() if p in extra and p not in used))
    return Branches(total, others, passing, brought)


@dataclass(slots=True)
class WeekResult:
    total: float
    counted: dict[str, float]
    locked: dict[str, float]
    decisions: int = 0
    """Lock/pass calls actually faced — the denominator for a lock rate."""


def decision_days(lineup: dict[str, list[Game]]) -> list[int]:
    return sorted({g.day for games in lineup.values() for g in games if g.played})


def opponent_totals(
    lineup: dict[str, list[Game]],
    greedy_thresholds: dict[str, dict[int, float]],
    week: int,
    known_through: int,
    cache: SimulationCache,
    rng: np.random.Generator,
    *,
    known: dict[str, float] | None = None,
) -> np.ndarray:
    """Simulated final totals for the opposing team. ``(n_sims,)``.

    The opponent is assumed to play the Phase 4 base policy. For games that have
    already happened the assumption is *resolved*, not sampled: their thresholds
    are known, so whether the policy would have banked is determined, and a
    banked player is a constant from then on. Only the genuinely unresolved part
    is simulated.

    §7.2's idle-nights assumption is deliberately **not** applied here. It is a
    statement about the nights *you* might miss, and imposing it on the opponent
    would lower his simulated total — pessimism about yourself and optimism
    about him, which is not conservatism, just a thumb on the scale.

    ``known`` is the opponent's state read from a poll (`lockin.state`): every
    closed-window lock, as a constant, and — by omission — every closed window
    he did *not* lock. Given it, the policy stands in only where the poll
    cannot see: last night's game, whose window is still open. Without it
    (replays, or no poll yet) the policy stands in for his whole week so far,
    which is the stand-in the digest says it is using.
    """
    total = np.zeros(cache.n_sims)
    for sleeper_id, games in lineup.items():
        if known is not None:
            if sleeper_id in known:
                total += known[sleeper_id]  # banked, observed
                continue
            seen = [g for g in games if g.day <= known_through]
            last = seen[-1] if seen else None
            still_open = (
                last is not None and last.played and any(g.day > known_through for g in games)
            )
            limit = greedy_thresholds.get(sleeper_id, {}).get(last.index) if last else None
            if still_open and limit is not None and last.score > limit:
                total += last.score  # the stand-in, for the one window he may yet use
            else:
                total += cache.contribution(
                    sleeper_id, games, week, rng, known_through=known_through
                )
            continue
        past = [g for g in games if g.day <= known_through]
        outcome = replay(past, greedy_thresholds.get(sleeper_id, {})) if past else None
        if outcome is not None and outcome.locked_index is not None:
            total += outcome.counted  # already banked; a constant from here
        else:
            total += cache.contribution(sleeper_id, games, week, rng, known_through=known_through)
    return total


def _window_closes(games: list[Game], day: int, through_day: int | None) -> bool:
    """Has he a later game — and, when walking only to a date, has it tipped by then?"""
    after = [g.day for g in games if g.day > day]
    if not after:
        return False
    return through_day is None or min(after) <= through_day


def walk_locks(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    cache: SimulationCache,
    rng: np.random.Generator,
    *,
    through_day: int | None = None,
    bench: Bench | None = None,
    fielded: dict[str, list[Game]] | None = None,
) -> tuple[dict[str, float], int]:
    """Take every lock the rollout policy would take, day by day.

    Returns the banked scores and how many calls were faced. ``through_day``
    stops the walk after that day, which is how a replay reconstructs the state
    a week is *currently* in without also asserting how it ends. Reading the
    whole week and then discarding the tail would be the same computation only
    if nothing downstream saw the discarded part, and that is exactly the kind
    of thing that stops being true after one edit.

    **With ``through_day``, only closed windows are walked**: a game whose next
    game is still ahead is the call the digest is about to present, and deciding
    it here would bank the very action the digest exists to deliver (review
    finding 2). Without it — the backtest's full replay — every window is.

    ``bench`` prices each call's pass branch with the bench in play (`Bench`).
    ``fielded``, given with it, is the lineup the walk actually plays: at the
    end of each day the walk **makes** the swaps its pass branches assumed,
    mutating it, so a replay scores the week it decided rather than the polled
    one. Without ``fielded`` the polled lineup is never changed.
    """
    locked: dict[str, float] = {}
    decisions = 0
    live = lineup if fielded is None else fielded

    days = decision_days(lineup)
    if bench is not None:
        days = sorted(
            set(days) | {g.day for games in bench.games.values() for g in games if g.played}
        )
    for day in days:
        if through_day is not None and day > through_day:
            break
        # Highest score first: the strongest lock candidate is judged against a
        # state nothing else has disturbed yet. Any fixed order is defensible —
        # rollout is a heuristic — but an arbitrary one would be harder to read.
        todays = sorted(
            (
                (next(g.score for g in games if g.day == day and g.played), pid)
                for pid, games in live.items()
                if pid not in locked
                and any(g.day == day and g.played for g in games)
                and _window_closes(games, day, through_day)
            ),
            reverse=True,
        )
        if not todays:
            if bench is not None and fielded is not None:
                _make_swaps(bench, fielded, locked, week, cache, rng, day)
            continue

        opponent = opponent_totals(opponent_lineup, opponent_thresholds, week, day, cache, rng)

        for score, sleeper_id in todays:
            unlocked = [pid for pid in live if pid not in locked]
            rows = {
                pid: cache.contribution(pid, live[pid], week, rng, known_through=day)
                for pid in unlocked
            }
            plan = None
            if bench is not None:
                extra = bench_rows(cache, bench, live, week, rng, known_through=day, start=day + 1)
                plan = with_bench(bench, rows, extra, list(locked), sleeper_id, cache.n_sims)
            if plan is None:
                decision = evaluate_lock(
                    banked=sum(locked.values()),
                    contributions=np.vstack([rows[pid] for pid in unlocked]),
                    player=unlocked.index(sleeper_id),
                    lock_value=score,
                    opponent=opponent,
                )
            else:
                decision = evaluate_lock(
                    banked=sum(locked.values()),
                    contributions=plan.passing[None, :],
                    player=0,
                    lock_value=score,
                    opponent=opponent,
                    others_if_locked=plan.others_if_locked,
                )
            decisions += 1
            if decision.lock:
                locked[sleeper_id] = score

        if bench is not None and fielded is not None:
            _make_swaps(bench, fielded, locked, week, cache, rng, day)

    return locked, decisions


def _make_swaps(
    bench: Bench,
    fielded: dict[str, list[Game]],
    locked: dict[str, float],
    week: int,
    cache: SimulationCache,
    rng: np.random.Generator,
    day: int,
) -> None:
    """End of ``day``: field the lineup the pass branch assumes, from tomorrow.

    A starter left out is benched, and whatever he had not banked is gone; he
    does not return to the pool, which keeps a replay from flipping two
    near-equal players back and forth. A player brought in counts only the
    games after ``day``.
    """
    unlocked = [pid for pid in fielded if pid not in locked]
    rows = {
        pid: cache.contribution(pid, fielded[pid], week, rng, known_through=day) for pid in unlocked
    }
    extra = bench_rows(cache, bench, fielded, week, rng, known_through=day, start=day + 1)
    plan = with_bench(bench, rows, extra, list(locked), None, cache.n_sims)
    if plan is None:
        return
    chosen = set(plan.fielded.values())
    if all(pid in fielded for pid in chosen):
        return
    for pid in unlocked:
        if pid not in chosen:
            del fielded[pid]
    for pid in chosen:
        if pid not in fielded:
            fielded[pid] = [g for g in bench.games.pop(pid) if g.day > day]
    bench.slot_of = {pid: key for key, pid in plan.fielded.items()}


def replay_week(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    cache: SimulationCache,
    rng: np.random.Generator,
    *,
    bench: Bench | None = None,
) -> WeekResult:
    """Walk one roster-week under the rollout policy and return what it scored.

    With ``bench`` the walk also makes the swaps it priced, and the week is
    scored on the lineup it ended with.
    """
    fielded = dict(lineup) if bench is not None else None
    locked, decisions = walk_locks(
        lineup,
        opponent_lineup,
        opponent_thresholds,
        week,
        cache,
        rng,
        bench=bench.copy() if bench is not None else None,
        fielded=fielded,
    )
    counted = dict(locked)
    for sleeper_id, games in (lineup if fielded is None else fielded).items():
        if sleeper_id not in counted:
            last = games[-1]
            counted[sleeper_id] = last.score if last.played else 0.0
    return WeekResult(
        total=float(sum(counted.values())),
        counted=counted,
        locked=dict(locked),
        decisions=decisions,
    )


def standing_thresholds(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    night: int,
    locked: dict[str, float],
    cache: SimulationCache,
    rng: np.random.Generator,
    *,
    known_through: int,
    opponent_known: dict[str, float] | None = None,
    bench: Bench | None = None,
) -> dict[str, float]:
    """ "Lock him if he clears X on ``night``", for every starter playing then.

    The first-class output of §11 — what makes a missed check-in survivable —
    and the reason :meth:`SimulationCache.contribution` carries two dates.

    ``known_through`` is the last day whose games are observed. For a digest
    read in the morning it is *yesterday*, so tonight is simulated like any
    other future night. Passing ``night`` for both, as the end-of-day backtest
    can, would resolve tonight's games out of scores that do not exist yet.

    Per §7.2, nights strictly between ``known_through`` and ``night`` are
    assumed **idle**: a threshold for Thursday that quietly assumed you acted on
    Tuesday's is wrong exactly when you needed it. From ``night`` onwards the
    base policy resumes, because a night you are reading a threshold for is a
    night you are checking in.

    The asymmetry that makes this correct: every other starter is priced with
    ``act_from=night``, since his game that night is still live and bankable.
    The player being asked about is priced with ``act_from=night + 1``, because
    the threshold *is* the question of whether to bank that game — the pass
    branch has to be his continuation from the following day, not a value that
    silently re-banks the score under test.
    """
    opponent = opponent_totals(
        opponent_lineup, opponent_thresholds, week, known_through, cache, rng, known=opponent_known
    )
    unlocked = [pid for pid in lineup if pid not in locked]
    if not unlocked:
        return {}
    banked = sum(locked.values())
    # Teammates: their `night` games are live, so the base policy may bank them.
    teammate = {
        pid: cache.contribution(
            pid, lineup[pid], week, rng, known_through=known_through, act_from=night
        )
        for pid in unlocked
    }
    out: dict[str, float] = {}
    for pid in unlocked:
        games = lineup[pid]
        if not any(g.day == night for g in games) or not any(g.day > night for g in games):
            continue  # not playing that night, or nothing left to ride for
        if bench is not None:
            # He has to be in his slot when he plays ``night``, so a bench player
            # can only take it over from the night after — for teammates too,
            # which is conservative: one fixed start keeps the solve exact.
            rows = {
                other: cache.contribution(
                    other,
                    lineup[other],
                    week,
                    rng,
                    known_through=known_through,
                    act_from=night + 1,
                )
                if other == pid
                else teammate[other]
                for other in unlocked
            }
            extra = bench_rows(
                cache, bench, lineup, week, rng, known_through=known_through, start=night + 1
            )
            plan = with_bench(bench, rows, extra, list(locked), pid, cache.n_sims)
            if plan is not None:
                out[pid] = lock_threshold(
                    banked=banked,
                    contributions=plan.passing[None, :],
                    player=0,
                    opponent=opponent,
                    others_if_locked=plan.others_if_locked,
                )
                continue
        contributions = np.vstack(
            [
                cache.contribution(
                    other,
                    lineup[other],
                    week,
                    rng,
                    known_through=known_through,
                    act_from=night + 1,
                )
                if other == pid
                else teammate[other]
                for other in unlocked
            ]
        )
        out[pid] = lock_threshold(
            banked=banked,
            contributions=contributions,
            player=unlocked.index(pid),
            opponent=opponent,
        )
    return out


def decision_for(
    lineup: dict[str, list[Game]],
    opponent_lineup: dict[str, list[Game]],
    opponent_thresholds: dict[str, dict[int, float]],
    week: int,
    day: int,
    sleeper_id: str,
    score: float,
    locked: dict[str, float],
    cache: SimulationCache,
    rng: np.random.Generator,
    *,
    opponent_known: dict[str, float] | None = None,
    bench: Bench | None = None,
    brought_in: list[str] | None = None,
) -> tuple[RolloutDecision, float]:
    """One lock/pass call, and the break-even it was taken against.

    ``day`` is the last day observed. In the backtest it is the night the game
    was played; in a morning digest it is yesterday, whichever night the game
    being decided was — a player who played Monday and next plays Thursday can
    still bank Monday's score on Wednesday, and is decided with everything known
    by Wednesday (review finding 6). Either way everything through ``day`` is
    observed, and the candidate's pass branch is his continuation after it.

    The break-even comes from the same contributions as the call, so the two
    cannot disagree: LOCK exactly when the score is at or above it.

    With ``bench``, the bench players the pass branch would bring in because he
    was not banked are appended to ``brought_in``, so the call can name them.
    """
    opponent = opponent_totals(
        opponent_lineup, opponent_thresholds, week, day, cache, rng, known=opponent_known
    )
    unlocked = [pid for pid in lineup if pid not in locked]
    rows = {
        pid: cache.contribution(pid, lineup[pid], week, rng, known_through=day) for pid in unlocked
    }
    if bench is not None:
        extra = bench_rows(cache, bench, lineup, week, rng, known_through=day, start=day + 1)
        plan = with_bench(bench, rows, extra, list(locked), sleeper_id, cache.n_sims)
        if plan is not None:
            if brought_in is not None:
                brought_in.extend(plan.brought_in)
            args = dict(
                banked=sum(locked.values()),
                contributions=plan.passing[None, :],
                player=0,
                opponent=opponent,
                others_if_locked=plan.others_if_locked,
            )
            return evaluate_lock(**args, lock_value=score), lock_threshold(**args)
    contributions = np.vstack([rows[pid] for pid in unlocked])
    args = dict(
        banked=sum(locked.values()),
        contributions=contributions,
        player=unlocked.index(sleeper_id),
        opponent=opponent,
    )
    return evaluate_lock(**args, lock_value=score), lock_threshold(**args)
