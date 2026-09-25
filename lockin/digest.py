"""Phase 6: the daily digest.

The product. Everything before this phase exists to make five lines of a phone
notification correct; this is where they get assembled.

**Read in the morning, which is the whole design constraint.** Architecture doc
§11 lists the contents, and every one of them is a statement about a day that has
not happened yet. That makes the digest the first consumer in the project that
must distinguish *what is known* from *what is still bankable* — the backtest
never had to, because it only ever asked about the end of a completed day. The
distinction is carried in ``lockin.rollout`` as ``known_through`` / ``act_from``
and threaded through every simulation here.

**As-of, not live.** There is no live league to run against: the 2026-27 league
does not exist yet (§7.3, re-checked 2026-08-15 — the endpoint still returns
``[]``), and the 2025-26 league is ``status: complete``. So the digest takes a
date and reconstructs the morning of that date from the recorded season, which is
exactly what §7.3 said the live paths would have to be smoke-tested against. The
same code path serves both; ``--date`` defaults to today, and in October it will
simply start landing on days with unplayed games.

**Point-in-time by construction, not by care.** :func:`lineup_as_of` reads each
game on or before the cutoff from the box scores and every game after it from
the NBA schedule (`lockin.slate`), which carries no scores at all — so a leak is
not a discipline the callers have to maintain, the future is not in the data
structure. ``tests/test_digest.py`` corrupts the post-cutoff scores and asserts
the digest is byte-identical, which is the check that would actually catch a
regression.

**What is deliberately not here: a recommended lineup.** §11's second item asks
for tonight's slot assignment including bench promotions, and §16 measured what
that advice would be worth. Following the model's lineup would have made nine of
the ten teams worse, by 20.4 points a week, because ``player_status`` is empty
and the projection layer cannot see the injury report the manager reads. Shipping
it would mean shipping advice known to be worse than doing nothing. What ships
instead is :func:`durability_warnings` — the same underlying quantity, framed as
a risk to check rather than an instruction to follow.
"""

from __future__ import annotations

import hashlib
import sqlite3
import textwrap
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from lockin import __version__, calendar, clock
from lockin.backtest import DEFAULT_PATHS, greedy_thresholds, starter_dnp_scale
from lockin.core.policy import Game
from lockin.core.projections import (
    EWMAProjectionSource,
    InsufficientHistory,
    ProjectionParams,
    SeasonPanel,
)
from lockin.core.winprob import win_probability
from lockin.projections import NoGamesYet, date_of, day_index, load_panel, observed_scores
from lockin.rollout import (
    SimulationCache,
    decision_for,
    opponent_totals,
    standing_thresholds,
    walk_locks,
)
from lockin.slate import WeekSlate, week_slate
from lockin.state import (
    SOURCE_ASSUMED,
    SOURCE_INFERRED,
    SOURCE_STAND_IN,
    SOURCE_SUPPLIED,
    infer_state,
    slate_final_at,
)
from lockin.store import runs
from lockin.verify import scoring_settings

FORWARD_NIGHTS = 3
"""How many nights of standing rules to print, tonight included.

§11 asks for two to three. Three is the upper end because the marginal night is
nearly free — the simulation is cached across nights — and because the failure
this output exists to survive is a check-in missed for longer than expected.
"""

WARN_DNP_ABOVE = 0.25
"""Flag an unlocked starter whose last game of the week is this likely to be a
DNP. Not tuned: it is the point where the downside — a zeroed slot, the single
most consequential outcome in the format (§12) — is likelier than one in four.
"""


@dataclass(frozen=True, slots=True)
class LockCall:
    """A lock/pass call on a game that has finished."""

    sleeper_id: str
    name: str
    day: int
    score: float
    lock: bool
    p_win_lock: float
    p_win_pass: float
    break_even: float
    """The lowest score worth banking in this state; LOCK exactly when the score
    reaches it. +inf when no score is: passing already wins every simulation.

    §11 asks for "the implied break-even printed alongside", and it is what makes
    a call auditable: a lock at 42 against a break-even of 41.5 is a different
    recommendation from the same lock against a break-even of 20, even though
    both print as LOCK.
    """
    expires_utc: str | None = None
    """His next tipoff: when this window closes and the call stops meaning
    anything (review finding 7). None when the schedule has no time for it."""

    @property
    def edge(self) -> float:
        return abs(self.p_win_lock - self.p_win_pass)


@dataclass(frozen=True, slots=True)
class StandingRule:
    """ "Lock him on this night if he clears X"."""

    sleeper_id: str
    name: str
    night: int
    threshold: float
    p_clear: float
    """P(he clears it), from the marginal projection. A rule he will meet one
    time in fifty is arithmetically correct and operationally noise."""
    idle_nights: int
    """Decision nights between now and this one, assumed idle per §7.2.

    Counted in *nights you would have had a call on*, not calendar days: a night
    on which nobody plays is not an assumption about your behaviour. Zero for
    tonight. Printed, because the assumption is what makes the number
    conditional, and the user is the one who knows whether it held.
    """
    games_after: int


@dataclass(frozen=True, slots=True)
class Warning:
    sleeper_id: str
    name: str
    kind: str
    detail: str
    short: str
    """The same thing in a phone's worth of characters. Carried rather than
    derived by slicing `detail`, so the notification cannot be broken by
    rewording the long form."""


@dataclass(slots=True)
class Digest:
    as_of: str
    week: int
    roster_id: int
    opponent_roster_id: int | None
    known_through: int
    """Proleptic Gregorian ordinal of the last day whose games are observed."""
    banked: dict[str, float] = field(default_factory=dict)
    calls: list[LockCall] = field(default_factory=list)
    rules: list[StandingRule] = field(default_factory=list)
    warnings: list[Warning] = field(default_factory=list)
    p_win: float | None = None
    margin: dict[str, float] = field(default_factory=dict)
    my_total: float | None = None
    opponent_total: float | None = None
    names: dict[str, str] = field(default_factory=dict)
    note: str | None = None
    """Why the digest is empty, when it is. A week with nothing to decide is a
    normal outcome — light-slate weeks are real (§7.7) — not an error."""
    abstained: bool = False
    """True when the digest declined to advise, as opposed to having nothing to
    decide. `note` says why."""
    state_source: str | None = None
    """Where `banked` came from: supplied, inferred from a poll, or assumed."""
    opponent_state: str | None = None
    """Where the opponent's banked scores came from: inferred, or the stand-in."""
    poll_observed_at: str | None = None
    n_sims: int | None = None
    seed: int | None = None
    model: str | None = None
    """Which engine produced it: lockin's version and a hash of the projection
    parameters. Recomputing later gives a different answer (§20), so the record
    must say what it was computed with."""

    @property
    def as_of_day(self) -> int:
        return self.known_through + 1


# ------------------------------------------------------------------ point in time


def lineup_as_of(
    ctx: DigestContext,
    sleeper_ids: list[str],
    week: int,
    known_through: int,
    *,
    live: bool = False,
) -> dict[str, list[Game]]:
    """The week's games for each starter, as known the morning after the cutoff.

    Games on or before ``known_through`` come from the panel with their real
    scores; games after it come from the NBA schedule, unplayed and scoreless
    (`lockin.slate`). The future is not blanked out of the data — it was never
    read — so a leak is not a discipline the callers have to maintain.

    Which nights a player has a game is known in advance, and the schedule is
    what knows it. Trimming instead would turn a Thursday-Saturday-Sunday week
    into a week that ends on Wednesday and make every threshold wrong in the
    safe-looking direction.
    """
    return week_slate(
        ctx.conn, ctx.panel, ctx.scores, ctx.season, week, sleeper_ids, known_through, live=live
    ).games


def resolve_week(conn: sqlite3.Connection, season: str, as_of: str) -> int | None:
    """Which fantasy week contains this date. See `lockin.calendar`."""
    return calendar.resolve_week(conn, season, as_of)


def roster_for_user(conn: sqlite3.Connection, user_id: str) -> int | None:
    row = conn.execute(
        "SELECT roster_id FROM rosters WHERE owner_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    return int(row["roster_id"]) if row else None


def player_names(conn: sqlite3.Connection, sleeper_ids: list[str]) -> dict[str, str]:
    if not sleeper_ids:
        return {}
    marks = ",".join("?" * len(sleeper_ids))
    return {
        row["sleeper_id"]: row["full_name"]
        for row in conn.execute(
            f"SELECT sleeper_id, full_name FROM players WHERE sleeper_id IN ({marks})",
            sleeper_ids,
        )
    }


# ---------------------------------------------------------------------- assembly


@dataclass(slots=True)
class DigestContext:
    """Everything a digest needs that does not depend on the date.

    Split out because building it costs a panel load and a hazard fit, and both
    `digest` and `explain` want it. Also because the season-wide fit has to
    happen *before* the as-of cutoff is applied, and doing that in the same
    function that enforces the cutoff is how the two get confused.
    """

    conn: sqlite3.Connection
    season: str
    panel: SeasonPanel
    source: EWMAProjectionSource
    scores: np.ndarray
    lineups: dict[tuple[int, int], list[str]]
    opponents: dict[tuple[int, int], int]
    dnp_scale: dict[int, float]

    def lineup_ids(self, week: int, roster_id: int) -> list[str]:
        return self.lineups.get((week, roster_id), [])


def load_context(
    conn: sqlite3.Connection,
    season: str,
    *,
    params: ProjectionParams | None = None,
    panel: SeasonPanel | None = None,
) -> DigestContext:
    scoring = scoring_settings(conn)
    panel = panel or load_panel(conn, season, params=params)
    source = EWMAProjectionSource(panel, scoring, params)
    scores = observed_scores(panel, scoring)

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

    opponents: dict[tuple[int, int], int] = {}
    by_matchup: dict[tuple[int, int], list[int]] = defaultdict(list)
    for (week, roster_id), matchup_id in matchups.items():
        if matchup_id is not None:
            by_matchup[(week, matchup_id)].append(roster_id)
    for (week, _), members in by_matchup.items():
        if len(members) == 2:
            opponents[(week, members[0])] = members[1]
            opponents[(week, members[1])] = members[0]

    # The started-player hazard correction, fit per week on strictly earlier
    # weeks. Live this is where the real injury feed replaces the proxy (§15).
    starter_rows = np.zeros(len(panel.day), dtype=bool)
    for (week, _), starters in lineups.items():
        for pid in starters:
            hist = panel.histories.get(pid)
            if hist is None:
                continue
            base = panel.offsets[pid]
            starter_rows[base + np.nonzero(hist.week == week)[0]] = True

    panel_weeks = np.concatenate([h.week for h in panel.histories.values()])
    dnp_scale = {
        int(week): starter_dnp_scale(
            panel, source, starter_rows, int(week), int(panel.day[panel_weeks == week].min())
        )
        for week in np.unique(panel_weeks)
        if (panel_weeks == week).any()
    }

    return DigestContext(
        conn=conn,
        season=season,
        panel=panel,
        source=source,
        scores=scores,
        lineups=lineups,
        opponents=opponents,
        dnp_scale=dnp_scale,
    )


def clearing_chance(
    ctx: DigestContext,
    sleeper_id: str,
    games: list[Game],
    week: int,
    known_through: int,
    night: int,
    threshold: float,
    rng: np.random.Generator,
) -> float:
    """P(he clears the threshold on ``night``), as of this morning.

    A rule he meets one night in fifty is arithmetically correct and
    operationally noise, so the threshold is printed with the chance of it
    firing. Two things this must not do, both of which the obvious one-line
    version does:

    **It must not project from ``night``.** ``project(as_of=night)`` would
    condition on every game between now and then — games that have not been
    played. The cutoff is ``known_through + 1`` like everything else here, and
    the intervening fixtures are *simulated* into the path rather than read.

    **It must carry the same DNP correction as the threshold.** The hazard
    over-predicts absence for started players by roughly two to one (§15).
    Without the correction this number would disagree with the simulation that
    produced the threshold it describes, in the alarming direction.
    """
    remaining = [g for g in games if g.day > known_through]
    if not remaining or all(g.day != night for g in remaining):
        return float("nan")
    try:
        paths = ctx.source.project_path(
            sleeper_id,
            known_through + 1,
            [g.day for g in remaining],
            [week] * len(remaining),
            rng=rng,
            n_paths=2000,
            dnp_scale=ctx.dnp_scale.get(week, 1.0),
        )
    except InsufficientHistory:
        return float("nan")
    column = next(i for i, g in enumerate(remaining) if g.day == night)
    return float((paths[:, column] > threshold).mean())


def durability_warnings(
    ctx: DigestContext,
    lineup: dict[str, list[Game]],
    locked: dict[str, float],
    week: int,
    known_through: int,
    names: dict[str, str],
) -> list[Warning]:
    """§11's fifth item: unlocked starters facing a final game with DNP risk.

    The exposure is asymmetric and that is why it is worth a line of its own. An
    unlocked starter whose last game of the week is still to come has no floor:
    if he does not play, the slot counts 0.0, and there is no later game to make
    it back. Everyone else at least gets another draw.

    The hazard is corrected by the same per-week ``dnp_scale`` the simulator
    uses, for the reason §15 gives: uncorrected it predicts 17.2% absence for
    started players against a realised 8.5%, and a warning that fires on half the
    roster every night is one nobody reads.
    """
    out: list[Warning] = []
    rng = np.random.default_rng(0)
    scale = ctx.dnp_scale.get(week, 1.0)
    for sleeper_id, games in lineup.items():
        if sleeper_id in locked:
            continue
        ahead = [g for g in games if g.day > known_through]
        if len(ahead) != 1:
            continue  # not his last, or nothing left at all
        try:
            dist = ctx.source.project(
                sleeper_id, known_through + 1, fantasy_week=week, rng=rng, n_draws=2000
            )
        except InsufficientHistory:
            continue
        p_dnp = float(np.clip(dist.p_dnp * scale, 0.0, 1.0))
        if p_dnp >= WARN_DNP_ABOVE:
            out.append(
                Warning(
                    sleeper_id=sleeper_id,
                    name=names.get(sleeper_id, sleeper_id),
                    kind="final-game DNP risk",
                    detail=(
                        f"unlocked, last game {date_of(ahead[0].day)},"
                        f" P(does not play) {p_dnp:.0%} — the slot counts 0.0 if he sits"
                    ),
                    short=f"{date_of(ahead[0].day)[5:]} last, {p_dnp:.0%} DNP",
                )
            )
    return sorted(out, key=lambda w: w.name)


DIGEST_RUN_UTC = "13:00:00+00:00"
"""When the morning digest runs — 09:00 US Eastern in daylight time — for a
replay that has no clock of its own. It bounds which polls a replay may read."""


def run_moment(as_of: str, now: datetime | None) -> str:
    """The instant the digest is taken to run: now, or that morning for a replay."""
    return now.astimezone(UTC).isoformat() if now is not None else f"{as_of}T{DIGEST_RUN_UTC}"


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def unfinished_slate(
    mine: WeekSlate, theirs: WeekSlate, as_of: str, now: datetime | None, names: dict[str, str]
) -> str | None:
    """Why a live run cannot yet treat last night as final, if it cannot.

    The digest reads every game dated yesterday as complete. Run shortly after
    midnight, that reads a half-played slate as final (review finding 7). The
    fixture states say directly whether a starter's game is in; the fixed
    completion hour in `lockin.clock` is the backstop when nothing else is known.
    """
    stuck = sorted({f"{names.get(p, p)} ({d})" for p, d in mine.unsettled + theirs.unsettled})
    if stuck:
        return (
            f"last night is not final yet: no final result for {', '.join(stuck)}."
            " Re-run once the morning ingest has it."
        )
    if now is not None and clock.too_early_for(as_of, now):
        return "last night's games may still be in progress. Re-run after 07:00 UTC."
    return None


def model_id(ctx: DigestContext) -> str:
    params = hashlib.sha1(repr(ctx.source.params).encode()).hexdigest()[:10]
    return f"lockin {__version__}; projection params {params}"


def stale_ingest(conn: sqlite3.Connection, week: int, known_through: int) -> str | None:
    """Why this morning's data cannot be trusted as fresh, if it cannot.

    Asked of the last *complete* ingest covering this week (`ingest_runs`), not
    of the newest log line: a players refresh today on top of a stats fetch
    yesterday, or a run that died after committing its first week, looked fresh
    to the old check (review finding 9). It must have finished after last
    night's games did, and must not have skipped the NBA step that says they had.
    """
    newest = runs.latest_complete(conn, week)
    if newest is not None and runs.skipped(newest) & runs.LIVE_REQUIRES:
        return (
            "the last ingest skipped the NBA schedule (--skip-nba), which is what says"
            " last night is final. Run `lockin ingest` without it."
        )
    run = runs.latest_complete(conn, week, live=True)
    if run is None:
        return f"no complete ingest covering week {week}. Run `lockin ingest --weeks current`."
    if run["finished_at"] < slate_final_at(known_through):
        return (
            f"the last complete ingest finished at {run['finished_at'][:16]}Z, before last"
            " night's games did. Check the ingest cron, then re-run."
        )
    return None


def cold_start(
    ctx: DigestContext,
    mine: dict[str, list[Game]],
    theirs: dict[str, list[Game]],
    week: int,
    known_through: int,
    names: dict[str, str],
) -> str | None:
    """Why this morning's projections cannot be trusted, if they cannot.

    Two reasons, both about the cold start. The league pool is below the size
    the cold-start gate certified (`ProjectionParams.min_pool_rows`), so every
    distribution's right tail is known to be too thin. Or a starter with games
    left cannot be projected at all. Either way the answer is to say so: the
    alternative the code used to take counted an unprojectable player's final
    game, which live has not been played, and turned "no information" into
    "certain to score nothing" (review finding 3).

    Probes on a generator of its own, so a digest that does advise draws
    exactly the numbers it drew before this check existed.
    """
    as_of = known_through + 1
    pool, need = ctx.panel.pool_size(as_of), ctx.source.params.min_pool_rows
    if pool < need:
        return (
            f"insufficient history: {pool} player-games played so far, and"
            f" projections are not calibrated until {need}. No advice today."
        )
    probe = np.random.default_rng(0)
    stuck = []
    for pid, games in (*mine.items(), *theirs.items()):
        if not any(g.day > known_through for g in games):
            continue
        try:
            ctx.source.project(pid, as_of, fantasy_week=week, rng=probe, n_draws=8)
        except InsufficientHistory:
            stuck.append(names.get(pid, pid))
    if stuck:
        return f"insufficient history: cannot project {', '.join(sorted(stuck))}. No advice today."
    return None


def abstain(conn: sqlite3.Connection, season: str, roster_id: int, as_of: str, note: str) -> Digest:
    """A digest that declines to advise, for mornings with nothing to project from.

    Persisted and sent like any other, so the page and the notification say why
    there is no advice rather than looking like a cron that did not run.
    """
    return Digest(
        as_of=as_of,
        week=resolve_week(conn, season, as_of) or 0,
        roster_id=roster_id,
        opponent_roster_id=None,
        known_through=day_index(as_of) - 1,
        note=note,
        abstained=True,
    )


def morning(
    conn: sqlite3.Connection,
    season: str,
    roster_id: int,
    as_of: str,
    *,
    n_sims: int = DEFAULT_PATHS,
    locked: dict[str, float] | None = None,
    live: bool = False,
    now: datetime | None = None,
    params: ProjectionParams | None = None,
) -> Digest:
    """The digest the cron runs: load, then build — or abstain before any game.

    One function for `lockin digest` and for the lifecycle rehearsal
    (`tests/test_lifecycle.py`), so the rehearsal exercises the sequence the
    cron runs rather than a copy of it.
    """
    try:
        ctx = load_context(conn, season, params=params)
    except NoGamesYet as exc:
        return abstain(conn, season, roster_id, as_of, f"insufficient history: {exc}")
    return build(
        ctx,
        roster_id,
        as_of,
        n_sims=n_sims,
        n_paths=n_sims,
        locked=locked,
        live=live,
        now=now,
    )


def build(
    ctx: DigestContext,
    roster_id: int,
    as_of: str,
    *,
    n_sims: int = DEFAULT_PATHS,
    n_paths: int = DEFAULT_PATHS,
    seed: int = 20260815,
    forward_nights: int = FORWARD_NIGHTS,
    locked: dict[str, float] | None = None,
    live: bool = False,
    now: datetime | None = None,
) -> Digest:
    """Assemble the digest for the morning of ``as_of``.

    ``locked`` is the state the week is already in, as you know it. Left None it
    is **read from the matchup poll** (`lockin.state`): every closed-window lock,
    on both teams. It used to be reconstructed by replaying this engine's own
    policy through yesterday, which assumed last night's recommendations had
    been taken and then dropped them from the calls meant to deliver them
    (review finding 2). A printed recommendation is not evidence it was acted on.

    Without a usable poll, a live run abstains and asks for ``--locked``. A
    replay of a past date — where no poll from that morning exists — falls back
    to reconstructing the closed windows only, and says it is an assumption.

    ``live`` is a run for today: the player-to-team join reads today's rosters,
    and the run refuses to read a slate that has not finished. ``now`` is the
    moment of the run, which closes any call whose next tipoff has passed.
    """
    conn = ctx.conn
    day = day_index(as_of)
    known_through = day - 1
    week = resolve_week(conn, ctx.season, as_of)
    if week is None:
        note = "no scheduled games on or after this date; the season is over"
        if calendar.opening_night(conn, ctx.season) is None:
            # Without the schedule a week is found from box scores dated on or
            # after this morning, which a season in progress does not have yet.
            note = (
                "no NBA schedule ingested, so this morning's week cannot be found."
                " Run `lockin ingest` without --skip-nba."
            )
        return Digest(
            as_of=as_of,
            week=0,
            roster_id=roster_id,
            opponent_roster_id=None,
            known_through=known_through,
            note=note,
        )

    starters = ctx.lineup_ids(week, roster_id)
    if not starters:
        return Digest(
            as_of=as_of,
            week=week,
            roster_id=roster_id,
            opponent_roster_id=None,
            known_through=known_through,
            note=f"no starters recorded for roster {roster_id} in week {week}",
        )

    opponent_id = ctx.opponents.get((week, roster_id))
    names = player_names(conn, starters + ctx.lineup_ids(week, opponent_id or -1))

    mine_slate = week_slate(
        conn, ctx.panel, ctx.scores, ctx.season, week, starters, known_through, live=live
    )
    mine = mine_slate.games
    digest = Digest(
        as_of=as_of,
        week=week,
        roster_id=roster_id,
        opponent_roster_id=opponent_id,
        known_through=known_through,
        names=names,
        n_sims=n_sims,
        seed=seed,
        model=model_id(ctx),
    )
    if opponent_id is None:
        # Weeks 23-24 drop eliminated teams and week 25 is unscored (§7.7).
        # Without an opponent there is no win probability to maximise, so there
        # is no recommendation to make rather than a worse one to invent.
        digest.note = f"roster {roster_id} has no matchup in week {week}; nothing to decide"
        return digest
    theirs_slate = week_slate(
        conn,
        ctx.panel,
        ctx.scores,
        ctx.season,
        week,
        ctx.lineup_ids(week, opponent_id),
        known_through,
        live=live,
    )
    theirs = theirs_slate.games
    # Where Sleeper and the NBA disagree about a starter's week. Surfaced with the
    # other warnings — in the notification and on the page — not preferred silently.
    for pid, detail in mine_slate.warnings + theirs_slate.warnings:
        digest.warnings.append(
            Warning(
                sleeper_id=pid,
                name=names.get(pid, pid),
                kind="schedule disagreement",
                detail=detail,
                short="schedule? check",
            )
        )
    if not (mine_slate.scheduled and theirs_slate.scheduled):
        digest.warnings.append(
            Warning(
                sleeper_id="",
                name="schedule",
                kind="no schedule",
                detail="no NBA schedule ingested; the rest of the week was read from box scores",
                short="no NBA schedule",
            )
        )
    if not mine or not theirs:
        digest.note = f"week {week} has no countable games for one of the two teams"
        return digest

    if live:
        reason = unfinished_slate(mine_slate, theirs_slate, as_of, now, names) or stale_ingest(
            conn, week, known_through
        )
        if reason:
            digest.note, digest.abstained = reason, True
            return digest

    reason = cold_start(ctx, mine, theirs, week, known_through, names)
    if reason:
        digest.note, digest.abstained = reason, True
        return digest

    rng = np.random.default_rng(seed)
    cache = SimulationCache(source=ctx.source, n_sims=n_sims, dnp_scale=ctx.dnp_scale)
    opponent_thresholds = {
        pid: greedy_thresholds(ctx.source, pid, theirs[pid], week, rng, n_paths) for pid in theirs
    }

    before = run_moment(as_of, now)
    opponent_state = infer_state(conn, week, opponent_id, theirs, known_through, before=before)
    opponent_known = opponent_state.banked if opponent_state.usable else None
    digest.opponent_state = SOURCE_INFERRED if opponent_known is not None else SOURCE_STAND_IN

    if locked is not None:
        # A banked score for someone who is not in the lineup would be added to
        # the total *and* leave him counted among the unlocked, double-counting
        # him and flattering every number downstream. Silence is the wrong
        # failure here: a typo'd id looks exactly like a comfortable lead.
        strangers = sorted(set(locked) - set(mine))
        if strangers:
            named = ", ".join(f"{names.get(s, s)} ({s})" for s in strangers)
            raise ValueError(
                f"locked contains players who are not week {week} starters"
                f" for roster {roster_id}: {named}"
            )
        digest.state_source = SOURCE_SUPPLIED
    else:
        state = infer_state(conn, week, roster_id, mine, known_through, before=before)
        if state.source == SOURCE_INFERRED and state.unresolved:
            unclear = ", ".join(sorted(names.get(p, p) for p in state.unresolved))
            digest.note = (
                f"lock state unclear: the latest poll matches none of the games for"
                f" {unclear}. Pass --locked with what you have banked."
            )
            digest.abstained = True
            return digest
        if state.source == SOURCE_INFERRED:
            locked, digest.state_source = dict(state.banked), SOURCE_INFERRED
            digest.poll_observed_at = state.poll_observed_at
            for pid in state.ambiguous:
                digest.warnings.append(
                    Warning(
                        sleeper_id=pid,
                        name=names.get(pid, pid),
                        kind="lock state ambiguous",
                        detail="his counted score matches his latest game and an earlier one;"
                        " if you locked the earlier one, ignore his call and pass --locked",
                        short="locked earlier? check",
                    )
                )
        elif live:
            digest.note = (
                f"lock state unknown: {state.reason}. Pass --locked with what you have"
                " banked, or run after the morning ingest."
            )
            digest.abstained = True
            return digest
        else:
            locked, _ = walk_locks(
                mine,
                theirs,
                opponent_thresholds,
                week,
                cache,
                rng,
                through_day=known_through,
            )
            digest.state_source = SOURCE_ASSUMED
    digest.banked = dict(locked)

    # -- 1. calls on games whose lock window is still open ----------------
    # One per player, not one night per team (review finding 6): a player who
    # played Monday and next plays Thursday can still bank Monday on Wednesday,
    # whatever his teammates did on Tuesday. The window closes at his next tip.
    for sleeper_id, games in mine.items():
        if sleeper_id in locked:
            continue
        seen = [g for g in games if g.day <= known_through]
        ahead = [g for g in games if g.day > known_through]
        if not seen or not ahead or not seen[-1].played:
            # Nothing played yet, nothing left to ride for, or his latest game
            # was a DNP — which closed the earlier window at its tip and left
            # nothing to bank.
            continue
        game = seen[-1]
        expires = mine_slate.tipoff.get((sleeper_id, ahead[0].day))
        if now is not None and expires and _utc(expires) <= now:
            continue  # he has tipped again: the window is shut
        call, break_even = decision_for(
            mine,
            theirs,
            opponent_thresholds,
            week,
            known_through,
            sleeper_id,
            game.score,
            locked,
            cache,
            rng,
            opponent_known=opponent_known,
        )
        digest.calls.append(
            LockCall(
                sleeper_id=sleeper_id,
                name=names.get(sleeper_id, sleeper_id),
                day=game.day,
                score=game.score,
                lock=call.lock,
                p_win_lock=call.p_win_lock,
                p_win_pass=call.p_win_pass,
                break_even=break_even,
                expires_utc=expires,
            )
        )
    digest.calls.sort(key=lambda c: -c.score)

    # -- 2. standing rules, tonight and the nights after -------------------
    nights = sorted(
        {g.day for pid, games in mine.items() if pid not in locked for g in games if g.day >= day}
    )[:forward_nights]
    for night in nights:
        rules = standing_thresholds(
            mine,
            theirs,
            opponent_thresholds,
            week,
            night,
            locked,
            cache,
            rng,
            known_through=known_through,
            opponent_known=opponent_known,
        )
        for sleeper_id, threshold in rules.items():
            if not np.isfinite(threshold):
                continue  # passing already wins every simulation: no score to name
            digest.rules.append(
                StandingRule(
                    sleeper_id=sleeper_id,
                    name=names.get(sleeper_id, sleeper_id),
                    night=night,
                    threshold=threshold,
                    p_clear=clearing_chance(
                        ctx,
                        sleeper_id,
                        mine[sleeper_id],
                        week,
                        known_through,
                        night,
                        threshold,
                        rng,
                    ),
                    idle_nights=sum(1 for n in nights if day <= n < night),
                    games_after=sum(1 for g in mine[sleeper_id] if g.day > night),
                )
            )

    # -- 3. where the matchup stands ---------------------------------------
    opponent = opponent_totals(
        theirs, opponent_thresholds, week, known_through, cache, rng, known=opponent_known
    )
    banked_total = sum(locked.values())
    unlocked = [pid for pid in mine if pid not in locked]
    if unlocked:
        contributions = np.vstack(
            [
                cache.contribution(pid, mine[pid], week, rng, known_through=known_through)
                for pid in unlocked
            ]
        )
        my_total = banked_total + contributions.sum(axis=0)
    else:
        my_total = np.full(n_sims, banked_total)
    digest.p_win = win_probability(my_total, opponent)
    digest.my_total = float(my_total.mean())
    digest.opponent_total = float(opponent.mean())
    margin = my_total - opponent
    digest.margin = {f"q{int(q * 100):02d}": float(np.quantile(margin, q)) for q in (0.1, 0.5, 0.9)}

    # -- 4. warnings --------------------------------------------------------
    digest.warnings += durability_warnings(ctx, mine, locked, week, known_through, names)
    return digest


# --------------------------------------------------------------------- rendering

WIDTH = 44
"""Characters. A push notification on a phone wraps past roughly this, and a
wrapped table is an unreadable table — which would fail the phase's exit
criterion however correct the numbers behind it were."""


def _short(name: str, width: int) -> str:
    """ "Karl-Anthony Towns" -> "K-A Towns". Surname is what identifies him."""
    if len(name) <= width:
        return name
    first, _, last = name.rpartition(" ")
    if not first:
        return name[:width]
    initials = "-".join(part[0] for part in first.replace("-", " ").split())
    return f"{initials} {last}"[:width]


def deadline_day(tipoff_utc: str | None) -> str | None:
    """The local date a window closes on, for grouping."""
    if tipoff_utc is None:
        return None
    return _utc(tipoff_utc).astimezone(clock.zone()).date().isoformat()


def deadline_label(tipoff_utc: str) -> str:
    """ "THU 7:30PM" — in the schedule's timezone, which is the one on the TV."""
    local = datetime.fromisoformat(tipoff_utc.replace("Z", "+00:00")).astimezone(clock.zone())
    return f"{local.strftime('%a').upper()} {local.strftime('%I:%M%p').lstrip('0')}"


def render(digest: Digest, *, compact: bool = False) -> str:
    """The digest as text, in the order §11 asks for it.

    Ordering is the whole design. What must happen *before tonight's tip* comes
    first, because it is the only part with a deadline; the standing rules that
    make a missed check-in survivable come next; the state of the matchup, which
    is context rather than an instruction, comes last. A digest that opened with
    the win probability would bury the one thing that expires.
    """
    day_name = datetime.fromordinal(digest.as_of_day).strftime("%a %-d %b")
    out = [f"LOCK-IN  {day_name}  wk {digest.week}"]
    if digest.note:
        out.extend(textwrap.wrap(digest.note, WIDTH))
        return "\n".join(out)

    banked = sum(digest.banked.values())
    out.append(
        f"roster {digest.roster_id} v {digest.opponent_roster_id}   P(win) {digest.p_win:.0%}"
    )

    # Grouped by the day each window closes — his next tip — under that day's
    # earliest tip. A phone line has no room for a time per player, and a
    # deadline stated early is one that is met; the exact one is on the page.
    by_day: dict[str | None, list[LockCall]] = defaultdict(list)
    for call in digest.calls:
        by_day[deadline_day(call.expires_utc)].append(call)
    for key in sorted(by_day, key=lambda d: (d is None, d or "")):
        calls = by_day[key]
        if key is None:
            night = datetime.fromordinal(calls[0].day).strftime("%a")
            out.append(f"\nLAST NIGHT ({night}) — do this now")
        else:
            first = min(c.expires_utc for c in calls if c.expires_utc)
            out.append(f"\nBEFORE {deadline_label(first)} TIP — lock or pass")
        for call in calls:
            verb = "LOCK" if call.lock else "pass"
            need = "ride" if not np.isfinite(call.break_even) else f"need {call.break_even:.0f}"
            out.append(f"  {verb}  {_short(call.name, 18):<18}{call.score:>6.1f}  {need}")

    by_night: dict[int, list[StandingRule]] = defaultdict(list)
    for rule in digest.rules:
        by_night[rule.night].append(rule)
    for night in sorted(by_night):
        label = datetime.fromordinal(night).strftime("%a %-d")
        when = "TONIGHT" if night == digest.as_of_day else f"{label.upper()}"
        idle = by_night[night][0].idle_nights
        suffix = f"  (assumes {idle} idle)" if idle else ""
        out.append(f"\n{when} — lock if he clears{suffix}")
        for rule in sorted(by_night[night], key=lambda r: -r.threshold):
            chance = "" if np.isnan(rule.p_clear) else f"  {rule.p_clear:.0%}"
            # Whole points. The Monte Carlo standard deviation on a threshold is
            # about a point at the default 2,000 sims, so a decimal place would be
            # advertising precision that is not there — and this is a number the
            # user applies from memory on a phone.
            out.append(f"  {_short(rule.name, 20):<20}{rule.threshold:>7.0f}{chance}")

    if digest.warnings:
        out.append("\nWATCH")
        for warn in digest.warnings:
            out.append(f"  {_short(warn.name, 20)} — {warn.short}")

    if not compact:
        out.append(
            f"\nBANKED {banked:.1f} across {len(digest.banked)} of 6"
            f"\nPROJECTED {digest.my_total:.0f} v {digest.opponent_total:.0f}"
            f"\nmargin p10/p50/p90"
            f"  {digest.margin['q10']:+.0f} / {digest.margin['q50']:+.0f}"
            f" / {digest.margin['q90']:+.0f}"
        )
        out.append(
            "\nForward nights assume you act on none of"
            "\nthe nights in between (§7.2) — the point"
            "\nis to survive a missed check-in."
        )
    return "\n".join(out)


# ------------------------------------------------------------------- persistence


def last_ingest_at(conn: sqlite3.Connection, week: int | None = None) -> str | None:
    """When the data under a digest was last complete, from `ingest_runs`.

    A database no run-recording ingest has touched yet falls back to the newest
    `ingest_log` line — the old signal, which a sub-step can refresh without the
    stats (review finding 9). The first ingest by current code ends that.
    """
    if runs.any_recorded(conn):
        run = runs.latest_complete(conn, week)
        return run["finished_at"] if run else None
    row = conn.execute(
        "SELECT MAX(finished_at) f FROM ingest_log WHERE source = 'sleeper'"
    ).fetchone()
    return row["f"] if row else None


def persist(
    conn: sqlite3.Connection,
    digest: Digest,
    *,
    state_supplied: bool = False,
    now: datetime | None = None,
) -> int:
    """Write the digest: one immutable run, and every row it produced.

    **Inserted, never replaced.** `generated_at` used to resolve to the second
    and every write was INSERT OR REPLACE, so two runs for one roster inside a
    second overwrote each other's header and kept each other's stale rows — a
    page could then show half of each (review finding 12). Every run now has
    its own `run_id`, its rows are keyed to it, and a collision is an error
    rather than a merge.

    Enough is kept to audit a call later without recomputing it — which §20
    says would give a different answer and §12 says the inputs no longer
    support: where the banked state came from and what it was, per player; the
    poll and the ingest it read, and how old the schedule and designations were;
    the simulation count, seed and model; and the warnings the notification
    carried.

    Written even when there are no calls — "no matchup this week" and "no
    advice today, and why" are real answers, and a page that showed nothing at
    all would be indistinguishable from a cron that never ran.

    ``now`` stamps the run; the clock by default. A rehearsal passes its
    synthetic morning, so `lockin shadow` can tell a run made that morning from
    a replay made afterwards.
    """
    run_id = uuid.uuid4().hex
    generated_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="microseconds")
    ingest = runs.latest_complete(conn, digest.week) if runs.any_recorded(conn) else None
    conn.execute(
        """
        INSERT INTO digest_runs
            (generated_at, roster_id, as_of, week, opponent_roster_id, p_win,
             projected, opponent_projected, margin_p10, margin_p50, margin_p90,
             banked_total, banked_slots, state_supplied, last_ingest_at, note,
             run_id, state_source, opponent_state, poll_observed_at, ingest_run_id,
             n_sims, seed, model, abstained, schedule_at, status_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            generated_at,
            digest.roster_id,
            digest.as_of,
            digest.week,
            digest.opponent_roster_id,
            digest.p_win,
            digest.my_total,
            digest.opponent_total,
            digest.margin.get("q10"),
            digest.margin.get("q50"),
            digest.margin.get("q90"),
            sum(digest.banked.values()),
            len(digest.banked),
            int(state_supplied or digest.state_source == SOURCE_SUPPLIED),
            last_ingest_at(conn, digest.week),
            digest.note,
            run_id,
            digest.state_source,
            digest.opponent_state,
            digest.poll_observed_at,
            ingest["run_id"] if ingest else None,
            digest.n_sims,
            digest.seed,
            digest.model,
            int(digest.abstained),
            runs.schedule_fetched_at(conn),
            runs.designations_read_at(conn),
        ),
    )

    def finite(x: float) -> float | None:
        return x if np.isfinite(x) else None

    rows = [
        (
            generated_at,
            digest.week,
            digest.roster_id,
            call.sleeper_id,
            "LOCK" if call.lock else "PASS",
            call.day,
            finite(call.break_even),
            call.p_win_lock,
            call.p_win_pass,
            call.p_win_lock - call.p_win_pass,
            f"{call.name} scored {call.score:.1f} on {date_of(call.day)};"
            + (
                f" break-even {call.break_even:.1f}"
                if np.isfinite(call.break_even)
                else " no score is worth banking"
            ),
            run_id,
            call.expires_utc,
            None,
            None,
        )
        for call in digest.calls
    ] + [
        (
            generated_at,
            digest.week,
            digest.roster_id,
            rule.sleeper_id,
            "THRESHOLD",
            rule.night,
            rule.threshold,
            None,
            None,
            None,
            f"{rule.name}: lock on {date_of(rule.night)} if he scores"
            f" {rule.threshold:.0f} ({rule.idle_nights} idle night(s) assumed, §7.2)",
            run_id,
            None,
            finite(rule.p_clear),
            rule.games_after,
        )
        for rule in digest.rules
    ]
    conn.executemany(
        """
        INSERT INTO recommendations
            (generated_at, week, roster_id, sleeper_id, action, for_day, threshold,
             ev_lock, ev_pass, win_prob_delta, rationale, run_id, expires_utc,
             p_clear, games_after)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.executemany(
        "INSERT INTO digest_banked (run_id, sleeper_id, score) VALUES (?, ?, ?)",
        [(run_id, pid, score) for pid, score in digest.banked.items()],
    )
    # One row per (player, kind), the table's key. A second warning under the
    # same key used to abort the whole insert — run header, calls and all — so
    # the first is kept and the repeat dropped.
    warnings: dict[tuple[str, str], Warning] = {}
    for w in digest.warnings:
        warnings.setdefault((w.sleeper_id, w.kind), w)
    conn.executemany(
        "INSERT INTO digest_warnings (run_id, sleeper_id, kind, detail, short)"
        " VALUES (?, ?, ?, ?, ?)",
        [(run_id, w.sleeper_id, w.kind, w.detail, w.short) for w in warnings.values()],
    )
    return len(rows)
