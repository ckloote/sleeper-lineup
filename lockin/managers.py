"""Scoring the managers, not the teams.

Ranks the ten managers on the quality of their lock decisions, holding roster
talent constant. Two metrics, and the difference between them is the substance
of this module.

**Points capture** — of the upside a decision could have won, how much did they
take?

    share = (counted − riding to the end) / (their best game − riding to the end)

Zero means they might as well not have decided; one means perfect foresight.
Weeks with one game, or where riding was already optimal, contribute nothing to
either half and drop out on their own, so this measures only decisions that
could be got wrong.

**Win-probability regret** — was each decision the right side of the *bet*? This
is the better metric and it is the one the ranking is sorted on, because points
capture systematically punishes correct play:

> A manager 40 points down on Sunday should *decline* to bank a safe 45 and ride
> a boom-or-bust game instead, because banking it still loses. That is the
> right call and a points metric scores it as a blunder.

Measured over the real season, the two objectives disagree on **10.9%** of
decisions, and 190 of those 250 are exactly that case — pass where points says
bank, at a mean win probability of 22%. So the objection is real, it is common
enough to reorder the table, and correcting for it moves four managers by two or
more places.

One nuance that cuts the other way: mean regret on divergent decisions (0.755%)
is *lower* than on concordant ones (1.329%). Divergence arises precisely when a
matchup is already lopsided, so the marginal win probability at stake is small.
High-leverage decisions are real but individually cheaper than ordinary ones.

Three limits, all of which belong in any write-up of the output:

1. **It reads the field Sleeper rewrote** (§12). This ranks how the current data
   makes each manager's decisions *look*, not a certified record of what they
   did.
2. **Model error is charged to the manager.** A call scored wrong may reflect
   injury news the projection cannot see — the same blind spot worth 26 points
   per team total in §15.
3. **Do not benchmark the engine on this scale.** Greedy's thresholds come from
   the same projection model that computes the win probabilities grading it, so
   its errors are shared between deciding and being judged. Manager-versus-manager
   is fair — all ten are scored by a model none of them share — but
   manager-versus-engine is not.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from lockin.backtest import greedy_thresholds, player_games, starter_dnp_scale
from lockin.core.policy import Game
from lockin.core.projections import EWMAProjectionSource, ProjectionParams, SeasonPanel
from lockin.core.winprob import evaluate_lock
from lockin.projections import load_panel, observed_scores
from lockin.rollout import SimulationCache, decision_days, opponent_totals
from lockin.verify import scoring_settings

RESOLVED_DECISIONS = ("locked_early", "rode_to_end")
"""Statuses where we know what the manager chose. Ambiguous cases — several
games sharing the counted value — are excluded rather than guessed."""


def last_scored_week(conn: sqlite3.Connection) -> int:
    """The last week the league actually scored, from its own settings.

    Week 25 exists in the stats feed and carries a full set of starters, but the
    league never scored it: 37 of its 60 starter values are 0.0. Counting those
    as decisions reads every one as a catastrophic blunder — it cost roughly
    eight points of apparent points-capture per manager before this was caught.
    Read from ``last_scored_leg`` rather than hardcoded, since a season that ends
    early would move it.
    """
    row = conn.execute("SELECT payload_json FROM league_settings LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("no league settings ingested; run `lockin ingest`")
    return int(json.loads(row["payload_json"])["settings"]["last_scored_leg"])


@dataclass(frozen=True, slots=True)
class Decision:
    """One lock/pass call a manager actually faced."""

    week: int
    roster_id: int
    sleeper_id: str
    score: float
    """What was on the table to bank."""
    chose_lock: bool
    p_win_lock: float
    p_win_pass: float
    greedy_locks: bool
    """What the points-maximising policy would have done here."""

    @property
    def best(self) -> float:
        return max(self.p_win_lock, self.p_win_pass)

    @property
    def taken(self) -> float:
        return self.p_win_lock if self.chose_lock else self.p_win_pass

    @property
    def regret(self) -> float:
        """Win probability forfeited. Zero when they took the better side."""
        return self.best - self.taken

    @property
    def right(self) -> bool:
        return self.regret < 1e-12

    @property
    def divergent(self) -> bool:
        """Do the points and win-probability objectives disagree here?"""
        return self.greedy_locks != (self.p_win_lock > self.p_win_pass)


@dataclass(frozen=True, slots=True)
class Scorecard:
    roster_id: int
    decisions: int
    mean_regret: float
    right_rate: float
    divergent: int
    divergent_right_rate: float
    upside_share: float
    """Points capture — reported for contrast, never sorted on."""
    upside_decisions: int
    rode_to_zero: int


@dataclass(slots=True)
class ManagerReport:
    decisions: list[Decision] = field(default_factory=list)
    scorecards: list[Scorecard] = field(default_factory=list)

    def ranked(self) -> list[Scorecard]:
        """Best decision-making first: least win probability thrown away."""
        return sorted(self.scorecards, key=lambda s: s.mean_regret)

    def divergence_rate(self) -> float:
        return (
            sum(d.divergent for d in self.decisions) / len(self.decisions)
            if self.decisions
            else 0.0
        )

    def regret_by_agreement(self) -> tuple[float, float]:
        """Mean regret where the two objectives agree, and where they do not."""
        agree = [d.regret for d in self.decisions if not d.divergent]
        differ = [d.regret for d in self.decisions if d.divergent]
        return (
            float(np.mean(agree)) if agree else 0.0,
            float(np.mean(differ)) if differ else 0.0,
        )


def _upside(games: list[Game], counted: float) -> tuple[float, float] | None:
    """(captured, available) upside for one starter-week, or None if no choice existed."""
    if len(games) < 2:
        return None
    floor = games[-1].score if games[-1].played else 0.0
    played = [g.score for g in games if g.played]
    ceiling = max(played) if played else 0.0
    if ceiling - floor <= 0:
        return None  # riding was already optimal; nothing to get wrong
    return counted - floor, ceiling - floor


def evaluate_managers(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
    n_sims: int = 300,
    seed: int = 20260808,
    panel: SeasonPanel | None = None,
) -> ManagerReport:
    """Walk every roster-week and score the decisions its manager actually made."""
    scoring = scoring_settings(conn)
    panel = panel or load_panel(conn, season, params=params)
    source = EWMAProjectionSource(panel, scoring, params)
    scores = observed_scores(panel, scoring)
    scored_through = last_scored_week(conn)
    rng = np.random.default_rng(seed)

    lineups: dict[tuple[int, int], list[str]] = defaultdict(list)
    matchups: dict[tuple[int, int], int | None] = {}
    counted: dict[tuple[int, int, str], float | None] = {}
    for row in conn.execute(
        """
        SELECT week, roster_id, matchup_id, sleeper_id, counted_points
          FROM weekly_matchups_latest
         WHERE is_starter = 1
         ORDER BY week, roster_id, slot_index
        """
    ):
        key = (row["week"], row["roster_id"])
        lineups[key].append(row["sleeper_id"])
        matchups[key] = row["matchup_id"]
        counted[(*key, row["sleeper_id"])] = row["counted_points"]

    inferred = {
        (r["week"], r["roster_id"], r["sleeper_id"]): (r["status"], r["matched_game_index"])
        for r in conn.execute(
            "SELECT week, roster_id, sleeper_id, status, matched_game_index FROM lock_inferences"
        )
    }
    if not inferred:
        raise RuntimeError("no lock inferences; run `lockin locks` first")

    # Same point-in-time starter correction the backtest uses (§15): a lineup
    # slot is evidence of availability the hazard cannot otherwise see.
    starter_rows = np.zeros(len(panel.day), dtype=bool)
    for (week, _), starters in lineups.items():
        for pid in starters:
            hist = panel.histories.get(pid)
            if hist is not None:
                starter_rows[panel.offsets[pid] + np.nonzero(hist.week == week)[0]] = True
    panel_weeks = np.concatenate([h.week for h in panel.histories.values()])
    dnp_scale = {
        int(w): starter_dnp_scale(
            panel, source, starter_rows, int(w), int(panel.day[panel_weeks == w].min())
        )
        for w in np.unique(panel_weeks)
    }
    cache = SimulationCache(source=source, n_sims=n_sims, dnp_scale=dnp_scale)

    games_cache: dict[tuple[str, int], list[Game]] = {}

    def games_for(pid: str, week: int) -> list[Game]:
        if (pid, week) not in games_cache:
            games_cache[(pid, week)] = player_games(panel, scores, pid, week)
        return games_cache[(pid, week)]

    threshold_cache: dict[tuple[str, int], dict[int, float]] = {}

    def thresholds_for(pid: str, week: int) -> dict[int, float]:
        if (pid, week) not in threshold_cache:
            threshold_cache[(pid, week)] = greedy_thresholds(
                source, pid, games_for(pid, week), week, rng, n_sims
            )
        return threshold_cache[(pid, week)]

    opponents: dict[tuple[int, int], int] = {}
    by_matchup: dict[tuple[int, int], list[int]] = defaultdict(list)
    for (week, roster_id), matchup_id in matchups.items():
        if matchup_id is not None:
            by_matchup[(week, matchup_id)].append(roster_id)
    for (week, _), members in by_matchup.items():
        if len(members) == 2:
            opponents[(week, members[0])] = members[1]
            opponents[(week, members[1])] = members[0]

    report = ManagerReport()
    upside = defaultdict(lambda: [0.0, 0.0, 0])
    zeroes: dict[int, int] = defaultdict(int)

    for (week, roster_id), starters in sorted(lineups.items()):
        for pid in starters if week <= scored_through else []:
            got = counted.get((week, roster_id, pid))
            if got is None:
                continue
            u = _upside(games_for(pid, week), float(got))
            if u is not None:
                acc = upside[roster_id]
                acc[0] += u[0]
                acc[1] += u[1]
                acc[2] += 1
                zeroes[roster_id] += int(got == 0.0)

        opponent_id = opponents.get((week, roster_id))
        if opponent_id is None:
            continue
        mine = {p: g for p in starters if (g := games_for(p, week))}
        theirs = {p: g for p in lineups[(week, opponent_id)] if (g := games_for(p, week))}
        if not mine or not theirs:
            continue
        opponent_thresholds = {p: thresholds_for(p, week) for p in theirs}

        locked: dict[str, float] = {}
        for day in decision_days(mine):
            todays = sorted(
                (
                    (next(g.score for g in gs if g.day == day and g.played), pid)
                    for pid, gs in mine.items()
                    if pid not in locked
                    and any(g.day == day and g.played for g in gs)
                    and any(g.day > day for g in gs)
                ),
                reverse=True,
            )
            if not todays:
                continue
            opponent = opponent_totals(theirs, opponent_thresholds, week, day, cache, rng)
            for score, pid in todays:
                status, matched = inferred.get((week, roster_id, pid), (None, None))
                if status not in RESOLVED_DECISIONS:
                    continue
                games = mine[pid]
                position = next(i for i, g in enumerate(games) if g.day == day)
                unlocked = [p for p in mine if p not in locked]
                contributions = np.vstack(
                    [cache.contribution(p, mine[p], week, day, rng) for p in unlocked]
                )
                outcome = evaluate_lock(
                    banked=sum(locked.values()),
                    contributions=contributions,
                    player=unlocked.index(pid),
                    lock_value=score,
                    opponent=opponent,
                )
                chose_lock = status == "locked_early" and matched == position
                report.decisions.append(
                    Decision(
                        week=week,
                        roster_id=roster_id,
                        sleeper_id=pid,
                        score=score,
                        chose_lock=chose_lock,
                        p_win_lock=outcome.p_win_lock,
                        p_win_pass=outcome.p_win_pass,
                        greedy_locks=score
                        > thresholds_for(pid, week).get(games[position].index, float("inf")),
                    )
                )
                # The state must follow what the manager actually did, so each
                # later decision is judged from the position they were really in.
                if chose_lock:
                    locked[pid] = score

    by_roster: dict[int, list[Decision]] = defaultdict(list)
    for d in report.decisions:
        by_roster[d.roster_id].append(d)
    for roster_id, ds in by_roster.items():
        div = [d for d in ds if d.divergent]
        captured, available, n_up = upside[roster_id]
        report.scorecards.append(
            Scorecard(
                roster_id=roster_id,
                decisions=len(ds),
                mean_regret=float(np.mean([d.regret for d in ds])),
                right_rate=float(np.mean([d.right for d in ds])),
                divergent=len(div),
                divergent_right_rate=float(np.mean([d.right for d in div])) if div else 0.0,
                upside_share=captured / available if available else 0.0,
                upside_decisions=n_up,
                rode_to_zero=zeroes[roster_id],
            )
        )
    return report


def bootstrap_regret(
    report: ManagerReport, roster_id: int, *, resamples: int = 2000, seed: int = 1
) -> tuple[float, float]:
    """90% interval on a manager's mean regret.

    Around 220 decisions each is not many. Without this the middle of the table
    reads as an ordering when it is really a tie.
    """
    values = np.array([d.regret for d in report.decisions if d.roster_id == roster_id])
    if len(values) == 0:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    means = [values[rng.integers(0, len(values), len(values))].mean() for _ in range(resamples)]
    return float(np.percentile(means, 5)), float(np.percentile(means, 95))
