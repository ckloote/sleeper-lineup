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

**Point-in-time by construction, not by care.** :func:`lineup_as_of` blanks the
score and the played flag on every game after the cutoff before anything else
sees them, so a leak is not a discipline the callers have to maintain — the
future is not in the data structure. ``tests/test_digest.py`` corrupts the
post-cutoff scores and asserts the digest is byte-identical, which is the check
that would actually catch a regression.

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

import sqlite3
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from lockin.backtest import greedy_thresholds, player_games, starter_dnp_scale
from lockin.core.policy import Game
from lockin.core.projections import (
    EWMAProjectionSource,
    InsufficientHistory,
    ProjectionParams,
    SeasonPanel,
)
from lockin.core.winprob import win_probability
from lockin.projections import date_of, day_index, load_panel, observed_scores
from lockin.rollout import (
    SimulationCache,
    decision_for,
    opponent_totals,
    standing_thresholds,
    walk_locks,
)
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
    """What the score would have had to be for the call to flip.

    §11 asks for "the implied break-even printed alongside", and it is what makes
    a call auditable: a lock at 42 against a break-even of 41.6 is a different
    recommendation from the same lock against a break-even of 20, even though
    both print as LOCK.
    """

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

    @property
    def as_of_day(self) -> int:
        return self.known_through + 1


# ------------------------------------------------------------------ point in time


def lineup_as_of(
    panel: SeasonPanel,
    scores: np.ndarray,
    sleeper_ids: list[str],
    week: int,
    known_through: int,
) -> dict[str, list[Game]]:
    """The week's games for each starter, with everything after the cutoff blanked.

    The schedule is known in advance — which nights a player has a game is not a
    result — so the ``day`` field survives. The *outcome* does not: score goes to
    zero and ``played`` to False for every game after ``known_through``.

    Blanking rather than trimming matters. The simulator needs to know a player
    has three games left, not merely that he has some; trimming would silently
    turn a Thursday-Saturday-Sunday week into a week that ends on Wednesday and
    make every threshold wrong in the safe-looking direction.
    """
    out: dict[str, list[Game]] = {}
    for sleeper_id in sleeper_ids:
        games = player_games(panel, scores, sleeper_id, week)
        if not games:
            continue
        out[sleeper_id] = [
            g if g.day <= known_through else Game(index=g.index, day=g.day, played=False, score=0.0)
            for g in games
        ]
    return out


def resolve_week(conn: sqlite3.Connection, season: str, as_of: str) -> int | None:
    """Which fantasy week contains this date.

    Weeks are contiguous in the stat feed but not gapless — week 18 starts three
    days after week 17 ends, because of the All-Star break — so a date can fall
    between two weeks. Such a date belongs to the week that is about to start:
    there is nothing to decide during the break, and the useful digest on the
    Tuesday of a break is the one that previews Thursday.
    """
    row = conn.execute(
        """
        SELECT fantasy_week FROM box_scores
         WHERE season = ? AND game_date >= ?
         ORDER BY game_date LIMIT 1
        """,
        (season, as_of),
    ).fetchone()
    if row is not None:
        return int(row["fantasy_week"])
    return None


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


def build(
    ctx: DigestContext,
    roster_id: int,
    as_of: str,
    *,
    n_sims: int = 400,
    n_paths: int = 400,
    seed: int = 20260815,
    forward_nights: int = FORWARD_NIGHTS,
    locked: dict[str, float] | None = None,
) -> Digest:
    """Assemble the digest for the morning of ``as_of``.

    ``locked`` is the state the week is already in. Left None it is reconstructed
    by walking the week under the rollout policy through yesterday — i.e. on the
    assumption that you took the engine's own advice. That is the only assumption
    available offline and it is stated in the output, because the alternative
    live source does not exist yet: inferring an opponent's locks needs the
    polling history §10 describes, and nothing has been polled (§15).

    **Pass it in whenever you know it.** The reconstruction is the least stable
    number the digest produces: it is a chain of near-tied lock/pass calls, and
    resampling flips enough of them that the same date reconstructs 1 to 3 locks
    across seeds (§20). Live this does not arise — you know what you locked, and
    supplying it removes the noise rather than averaging it. Everything
    downstream is stable once the state is fixed: the calls are identical across
    seeds at the default 400 simulations.
    """
    conn = ctx.conn
    day = day_index(as_of)
    known_through = day - 1
    week = resolve_week(conn, ctx.season, as_of)
    if week is None:
        return Digest(
            as_of=as_of,
            week=0,
            roster_id=roster_id,
            opponent_roster_id=None,
            known_through=known_through,
            note="no scheduled games on or after this date; the season is over",
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

    mine = lineup_as_of(ctx.panel, ctx.scores, starters, week, known_through)
    digest = Digest(
        as_of=as_of,
        week=week,
        roster_id=roster_id,
        opponent_roster_id=opponent_id,
        known_through=known_through,
        names=names,
    )
    if opponent_id is None:
        # Weeks 23-24 drop eliminated teams and week 25 is unscored (§7.7).
        # Without an opponent there is no win probability to maximise, so there
        # is no recommendation to make rather than a worse one to invent.
        digest.note = f"roster {roster_id} has no matchup in week {week}; nothing to decide"
        return digest
    theirs = lineup_as_of(
        ctx.panel, ctx.scores, ctx.lineup_ids(week, opponent_id), week, known_through
    )
    if not mine or not theirs:
        digest.note = f"week {week} has no countable games for one of the two teams"
        return digest

    rng = np.random.default_rng(seed)
    cache = SimulationCache(source=ctx.source, n_sims=n_sims, dnp_scale=ctx.dnp_scale)
    opponent_thresholds = {
        pid: greedy_thresholds(ctx.source, pid, theirs[pid], week, rng, n_paths) for pid in theirs
    }

    if locked is None:
        locked, _ = walk_locks(
            mine,
            theirs,
            opponent_thresholds,
            week,
            cache,
            rng,
            through_day=known_through,
        )
    else:
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
    digest.banked = dict(locked)

    # -- 1. last night's calls --------------------------------------------
    played_days = sorted({g.day for games in mine.values() for g in games if g.played})
    if played_days:
        last_night = played_days[-1]
        # The break-evens for that night, computed once. `known_through` and the
        # night coincide here — the games are played and past banking — which is
        # the end-of-day case, so these are exactly the thresholds the calls
        # below are being taken against rather than a second opinion.
        break_evens = standing_thresholds(
            mine,
            theirs,
            opponent_thresholds,
            week,
            last_night,
            locked,
            cache,
            rng,
            known_through=last_night,
        )
        for sleeper_id, games in mine.items():
            if sleeper_id in locked:
                continue
            tonight = next((g for g in games if g.day == last_night and g.played), None)
            if tonight is None or not any(g.day > last_night for g in games):
                continue  # did not play, or nothing left to ride for
            call = decision_for(
                mine,
                theirs,
                opponent_thresholds,
                week,
                last_night,
                sleeper_id,
                tonight.score,
                locked,
                cache,
                rng,
            )
            digest.calls.append(
                LockCall(
                    sleeper_id=sleeper_id,
                    name=names.get(sleeper_id, sleeper_id),
                    day=last_night,
                    score=tonight.score,
                    lock=call.lock,
                    p_win_lock=call.p_win_lock,
                    p_win_pass=call.p_win_pass,
                    break_even=break_evens.get(sleeper_id, float("nan")),
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
        )
        for sleeper_id, threshold in rules.items():
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
    opponent = opponent_totals(theirs, opponent_thresholds, week, known_through, cache, rng)
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
    digest.warnings = durability_warnings(ctx, mine, locked, week, known_through, names)
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

    if digest.calls:
        night = datetime.fromordinal(digest.calls[0].day).strftime("%a")
        out.append(f"\nLAST NIGHT ({night}) — do this now")
        for call in digest.calls:
            verb = "LOCK" if call.lock else "pass"
            out.append(
                f"  {verb}  {_short(call.name, 18):<18}{call.score:>6.1f}"
                f"  need {call.break_even:.0f}"
            )

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
            # 1-3 points at the default 400 sims, so a decimal place would be
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


def last_ingest_at(conn: sqlite3.Connection) -> str | None:
    """When the ingest last finished, from its own log.

    Recorded with every digest because a digest running on data a failed cron
    never refreshed is indistinguishable from a healthy one: it reads yesterday's
    box scores, makes confident calls and says nothing. This is the only signal
    that separates the two, and it costs one query.
    """
    row = conn.execute(
        "SELECT MAX(finished_at) f FROM ingest_log WHERE source = 'sleeper'"
    ).fetchone()
    return row["f"] if row else None


def persist(conn: sqlite3.Connection, digest: Digest, *, state_supplied: bool = False) -> int:
    """Write the digest to `recommendations` and `digest_runs`.

    Keyed by ``generated_at``, so a re-run appends rather than overwrites and the
    record of what was advised at the time survives a model change. That matters
    here more than elsewhere: §12 established that the upstream data is rewritten
    under us, so "what did the engine say on the day" is not recoverable by
    recomputation.

    Two tables, because a call means nothing without the state it was taken
    against. `digest_runs` carries the win probability, the projected totals and
    what was already banked, so `lockin advice` can render a past digest exactly
    rather than approximately. It is written even when there are no calls to
    make — "no matchup this week" is a real answer and a page that showed
    nothing at all would be indistinguishable from a cron that never ran.
    """
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR REPLACE INTO digest_runs
            (generated_at, roster_id, as_of, week, opponent_roster_id, p_win,
             projected, opponent_projected, margin_p10, margin_p50, margin_p90,
             banked_total, banked_slots, state_supplied, last_ingest_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            int(state_supplied),
            last_ingest_at(conn),
            digest.note,
        ),
    )

    rows = [
        (
            generated_at,
            digest.week,
            digest.roster_id,
            call.sleeper_id,
            "LOCK" if call.lock else "PASS",
            call.day,
            call.break_even,
            call.p_win_lock,
            call.p_win_pass,
            call.p_win_lock - call.p_win_pass,
            f"{call.name} scored {call.score:.1f} on {date_of(call.day)};"
            f" break-even {call.break_even:.1f}",
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
            f"{rule.name}: lock on {date_of(rule.night)} if he clears"
            f" {rule.threshold:.0f} ({rule.idle_nights} idle night(s) assumed, §7.2)",
        )
        for rule in digest.rules
    ]
    if rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO recommendations
                (generated_at, week, roster_id, sleeper_id, action, for_day, threshold,
                 ev_lock, ev_pass, win_prob_delta, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)
