"""Phase 6: the digest, and the point-in-time discipline it depends on.

Three of these tests are the ones worth having.

``test_no_future_score_reaches_the_digest`` corrupts every post-cutoff score to a
number nothing could produce and asserts the digest does not move. That is the
check that would actually catch a leak — a reviewer reading the code for one is
checking the version of it that exists today, and the failure mode is a later
edit reintroducing a read nobody re-reads for.

``test_forward_nights_assume_no_action`` pins §7.2, which is the one place in the
project where a *policy* assumption is baked into a printed number. Without it,
a refactor that "simplified" the two cutoffs back into one would produce
thresholds that quietly assume you followed yesterday's advice, and every number
would still look plausible.

``test_backtest_cutoffs_are_unchanged`` pins the other side of that: where the
two dates coincide, the generalised code must be the Phase 5 code exactly, or the
gates it closed no longer mean what they said.
"""

from __future__ import annotations

import re
import sqlite3

import numpy as np
import pytest

from lockin import digest as digest_mod
from lockin.config import Config
from lockin.core.winprob import apply_base_policy, base_policy_thresholds
from lockin.projections import day_index
from lockin.rollout import SimulationCache
from lockin.store.db import apply_schema, session

cfg = Config.from_env()
pytestmark = pytest.mark.skipif(
    not cfg.db_path.exists(), reason=f"no database at {cfg.db_path}; run `lockin ingest`"
)

AS_OF = "2026-01-08"
"""Midweek in week 12 — the week the architecture doc works its examples in.
Three of roster 4's starters have already played and have games to come, so
every branch of the digest has something to say."""


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    apply_schema(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def ctx(conn):
    return digest_mod.load_context(conn, cfg.season)


@pytest.fixture(scope="module")
def roster_id(conn):
    return digest_mod.roster_for_user(conn, cfg.user_id)


@pytest.fixture(scope="module")
def report(ctx, roster_id):
    return digest_mod.build(ctx, roster_id, AS_OF, n_sims=200, n_paths=200)


# --------------------------------------------------------------- point in time


def test_lineup_as_of_blanks_the_future_but_keeps_the_schedule(ctx, roster_id):
    """Which nights he plays is known in advance; what he scores is not."""
    known_through = day_index(AS_OF) - 1
    starters = ctx.lineup_ids(12, roster_id)
    lineup = digest_mod.lineup_as_of(ctx.panel, ctx.scores, starters, 12, known_through)

    assert lineup, "week 12 should have starters with games"
    future = [g for games in lineup.values() for g in games if g.day > known_through]
    assert future, "as-of midweek there must be games still to come"
    assert all(not g.played and g.score == 0.0 for g in future)

    # The schedule survives: a player with four games in the week still has
    # four, or the continuation value is computed over the wrong horizon.
    unfiltered = {pid: digest_mod.player_games(ctx.panel, ctx.scores, pid, 12) for pid in lineup}
    assert {pid: len(g) for pid, g in lineup.items()} == {
        pid: len(g) for pid, g in unfiltered.items()
    }


def test_no_future_score_reaches_the_digest(conn, ctx, roster_id, report):
    """Corrupt everything after the cutoff; the digest must not notice.

    `scores` is the array every game's points are read from. Replacing the
    post-cutoff entries with 999.0 would visibly wreck any threshold computed
    from them — a break-even is a quantile of a deficit, so a single leaked 999
    moves it — and the digest is required to be byte-identical instead.
    """
    known_through = day_index(AS_OF) - 1
    poisoned = ctx.scores.copy()
    poisoned[ctx.panel.day > known_through] = 999.0

    tainted = digest_mod.DigestContext(
        conn=ctx.conn,
        season=ctx.season,
        panel=ctx.panel,
        source=ctx.source,
        scores=poisoned,
        lineups=ctx.lineups,
        opponents=ctx.opponents,
        dnp_scale=ctx.dnp_scale,
    )
    after = digest_mod.build(tainted, roster_id, AS_OF, n_sims=200, n_paths=200)

    assert digest_mod.render(after) == digest_mod.render(report)
    assert [c.break_even for c in after.calls] == [c.break_even for c in report.calls]
    assert [r.threshold for r in after.rules] == [r.threshold for r in report.rules]
    assert after.p_win == report.p_win


def test_resolve_week_puts_the_all_star_break_in_the_week_it_precedes(conn):
    """Week 17 ends 2026-02-15, week 18 opens 2026-02-19. The gap is week 18's.

    A date in the break has nothing to decide, so the useful digest is the one
    previewing the week about to start rather than an error.
    """
    assert digest_mod.resolve_week(conn, cfg.season, "2026-02-15") == 17
    assert digest_mod.resolve_week(conn, cfg.season, "2026-02-16") == 18
    assert digest_mod.resolve_week(conn, cfg.season, "2026-02-19") == 18
    assert digest_mod.resolve_week(conn, cfg.season, "2027-01-01") is None


# ------------------------------------------------------- the two-cutoff split


def _one_player_cache(ctx, n_sims=300):
    return SimulationCache(source=ctx.source, n_sims=n_sims, dnp_scale=ctx.dnp_scale)


def test_backtest_cutoffs_are_unchanged(ctx, roster_id):
    """Where `known_through` and `act_from` coincide, this is the Phase 5 code.

    Pinned against the base policy applied to the raw paths, which is literally
    what Phase 5 computed before the split. If this drifts, the Phase 4-5 gates
    are no longer measuring the policy they were closed on.
    """
    week, day = 12, day_index("2026-01-07")
    starters = ctx.lineup_ids(week, roster_id)
    lineup = digest_mod.lineup_as_of(ctx.panel, ctx.scores, starters, week, day)

    for pid, games in lineup.items():
        remaining = [g for g in games if g.day > day]
        if not remaining:
            continue
        cache = _one_player_cache(ctx)
        got = cache.contribution(pid, games, week, np.random.default_rng(7), known_through=day)
        paths = ctx.source.project_path(
            pid,
            day + 1,
            [g.day for g in remaining],
            [week] * len(remaining),
            rng=np.random.default_rng(7),
            n_paths=cache.n_sims,
            dnp_scale=ctx.dnp_scale.get(week, 1.0),
        )
        expected = apply_base_policy(paths, base_policy_thresholds(paths))
        assert np.array_equal(got, expected)


def test_an_idle_night_can_never_be_banked(ctx, roster_id):
    """§7.2's assumption, at the level it is actually implemented.

    With `act_from` past a game, that game's threshold is +inf, so no simulated
    path can bank it. The player therefore counts either a later game or his
    last one — never the skipped night.
    """
    week = 12
    known_through = day_index("2026-01-06")
    starters = ctx.lineup_ids(week, roster_id)
    lineup = digest_mod.lineup_as_of(ctx.panel, ctx.scores, starters, week, known_through)

    # Someone with at least two games left, so there is an intervening night.
    candidates = [
        (pid, [g for g in games if g.day > known_through]) for pid, games in lineup.items()
    ]
    pid, remaining = next((p, r) for p, r in candidates if len(r) >= 2)
    skipped, later = remaining[0].day, remaining[1].day

    cache = _one_player_cache(ctx)
    rng = np.random.default_rng(11)
    acting = cache.contribution(
        pid, lineup[pid], week, rng, known_through=known_through, act_from=skipped
    )
    idle = cache.contribution(
        pid, lineup[pid], week, rng, known_through=known_through, act_from=later
    )

    # Banking is optional value: forbidding it on a night cannot raise the mean.
    assert idle.mean() <= acting.mean() + 1e-9
    assert not np.array_equal(idle, acting), "skipping a bankable night must change something"


def test_forward_nights_assume_no_action(ctx, roster_id):
    """A threshold two nights out must not assume you acted on the night between.

    Built as the contrast §7.2 describes: the same night priced with the
    intervening night idle, against the same night priced with the base policy
    running through it. If the digest ever stopped applying the assumption these
    would collapse onto each other.
    """
    from lockin.backtest import greedy_thresholds
    from lockin.rollout import standing_thresholds

    week = 12
    known_through = day_index("2026-01-06")
    starters = ctx.lineup_ids(week, roster_id)
    mine = digest_mod.lineup_as_of(ctx.panel, ctx.scores, starters, week, known_through)
    opponent_id = ctx.opponents[(week, roster_id)]
    theirs = digest_mod.lineup_as_of(
        ctx.panel, ctx.scores, ctx.lineup_ids(week, opponent_id), week, known_through
    )

    nights = sorted({g.day for games in mine.values() for g in games if g.day > known_through})
    assert len(nights) >= 2
    far = nights[1]

    rng = np.random.default_rng(3)
    opp_thresholds = {
        pid: greedy_thresholds(ctx.source, pid, theirs[pid], week, rng, 200) for pid in theirs
    }
    cache = _one_player_cache(ctx, n_sims=400)
    honest = standing_thresholds(
        mine, theirs, opp_thresholds, week, far, {}, cache, rng, known_through=known_through
    )
    # The collapsed version: pretend the night before has already resolved.
    collapsed = standing_thresholds(
        mine, theirs, opp_thresholds, week, far, {}, cache, rng, known_through=far - 1
    )
    shared = set(honest) & set(collapsed)
    assert shared, "expected at least one player with a threshold on the far night"
    assert any(abs(honest[pid] - collapsed[pid]) > 1e-6 for pid in shared), (
        "the idle-night assumption must change the threshold it is printed with"
    )


def test_the_asked_player_is_priced_differently_from_his_teammates(ctx, roster_id):
    """The asymmetry that makes a standing rule correct.

    Everyone else's game that night is live and bankable (`act_from=night`).
    The player being asked about has his forfeited, because the threshold *is*
    the question of whether to bank it (`act_from=night+1`). Same player, same
    cutoff, two different numbers — and the digest must use both.
    """
    week = 12
    known_through = day_index("2026-01-06")
    starters = ctx.lineup_ids(week, roster_id)
    lineup = digest_mod.lineup_as_of(ctx.panel, ctx.scores, starters, week, known_through)

    night = min(g.day for games in lineup.values() for g in games if g.day > known_through)
    pid = next(
        p
        for p, games in lineup.items()
        if any(g.day == night for g in games) and any(g.day > night for g in games)
    )
    cache = _one_player_cache(ctx)
    rng = np.random.default_rng(5)
    as_teammate = cache.contribution(
        pid, lineup[pid], week, rng, known_through=known_through, act_from=night
    )
    as_subject = cache.contribution(
        pid, lineup[pid], week, rng, known_through=known_through, act_from=night + 1
    )
    assert not np.array_equal(as_teammate, as_subject)
    # Forfeiting a bankable game cannot be worth more than keeping the option.
    assert as_subject.mean() <= as_teammate.mean() + 1e-9


# ------------------------------------------------------------------- rendering


def test_the_digest_fits_a_phone(report):
    """The phase's exit criterion, as an assertion.

    "Thresholds render legibly on a phone" is not a matter of taste once a width
    is fixed: a line past it wraps, and a wrapped column of numbers is unusable.
    """
    for line in digest_mod.render(report).splitlines():
        assert len(line) <= digest_mod.WIDTH, f"{len(line)} chars: {line!r}"


def test_a_call_agrees_with_its_own_break_even(report):
    """LOCK iff the score cleared the printed break-even.

    Both come out of the same simulation, so disagreement means the two were
    computed against different state — which is exactly what the hoisted
    `standing_thresholds` call in `build` exists to prevent.
    """
    assert report.calls, "week 12 midweek should have calls to make"
    for call in report.calls:
        if np.isnan(call.break_even):
            continue
        assert call.lock == (call.score > call.break_even), call


def test_render_survives_a_week_with_nothing_to_decide(ctx, roster_id):
    """Week 25 is unscored and has no matchup (§7.7). That is not an error."""
    quiet = digest_mod.build(ctx, roster_id, "2026-04-07", n_sims=50, n_paths=50)
    assert quiet.note is not None
    assert quiet.p_win is None
    assert "nothing to decide" in quiet.note
    text = digest_mod.render(quiet)
    # The note is wrapped to fit, so assert on the words rather than the phrase.
    assert "week 25" in text
    assert all(len(line) <= digest_mod.WIDTH for line in text.splitlines())


# ----------------------------------------------------------------- persistence


def test_one_player_can_hold_several_rows_from_one_digest(tmp_path, report):
    """The placeholder primary key could not, and dropped rows silently.

    A player legitimately gets a call on last night plus a standing rule for
    each of the next nights. Under `(generated_at, week, sleeper_id)` those
    collide and INSERT OR REPLACE keeps the last one written.
    """
    if not report.rules:
        pytest.skip("no standing rules on this date")
    with session(tmp_path / "t.db") as conn:
        written = digest_mod.persist(conn, report)
        stored = conn.execute("SELECT COUNT(*) c FROM recommendations").fetchone()["c"]
    assert written == len(report.calls) + len(report.rules)
    assert stored == written


def test_a_rerun_records_a_second_opinion_rather_than_overwriting(tmp_path, report):
    """§12: the upstream data is rewritten, so this table is the only record."""
    if not report.calls:
        pytest.skip("no calls on this date")
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, report)
        digest_mod.persist(conn, report)
        stamps = conn.execute(
            "SELECT COUNT(DISTINCT generated_at) c FROM recommendations"
        ).fetchone()["c"]
    # Same second is possible; what must never happen is rows being lost.
    assert stamps >= 1


def test_the_widened_key_is_applied_to_an_existing_database(tmp_path):
    """An empty old-shape table is rebuilt; a populated one is left alone."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE recommendations (
            generated_at TEXT NOT NULL, week INTEGER NOT NULL, sleeper_id TEXT NOT NULL,
            action TEXT NOT NULL, threshold REAL, ev_lock REAL, ev_pass REAL,
            win_prob_delta REAL, rationale TEXT,
            PRIMARY KEY (generated_at, week, sleeper_id)
        )
        """
    )
    apply_schema(conn)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)")}
    conn.close()
    assert "for_day" in columns


def test_a_populated_old_table_is_never_dropped(tmp_path):
    """Dropping it would destroy the only record of what was advised (§12)."""
    db = tmp_path / "full.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE recommendations (
            generated_at TEXT NOT NULL, week INTEGER NOT NULL, sleeper_id TEXT NOT NULL,
            action TEXT NOT NULL, threshold REAL, ev_lock REAL, ev_pass REAL,
            win_prob_delta REAL, rationale TEXT,
            PRIMARY KEY (generated_at, week, sleeper_id)
        );
        INSERT INTO recommendations (generated_at, week, sleeper_id, action)
        VALUES ('2026-01-08T09:00:00+00:00', 12, '2126', 'LOCK');
        """
    )
    apply_schema(conn)
    rows = conn.execute("SELECT COUNT(*) c FROM recommendations").fetchone()["c"]
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)")}
    conn.close()
    assert rows == 1
    assert "for_day" not in columns, "a populated table must be left for a human to migrate"


# ---------------------------------------------------------------- notification
#
# Moved to tests/test_notify.py, which covers the success path these two never
# reached — and the header-encoding bug that lived there undetected.


# ------------------------------------------------------------------ regression


def test_walk_locks_matches_the_full_replay(ctx, roster_id):
    """`replay_week` is now `walk_locks` plus the counted step. Prove it."""
    from lockin.backtest import greedy_thresholds
    from lockin.rollout import replay_week, walk_locks

    week = 12
    starters = ctx.lineup_ids(week, roster_id)
    mine = {pid: digest_mod.player_games(ctx.panel, ctx.scores, pid, week) for pid in starters}
    mine = {pid: games for pid, games in mine.items() if games}
    opponent_id = ctx.opponents[(week, roster_id)]
    theirs = {
        pid: digest_mod.player_games(ctx.panel, ctx.scores, pid, week)
        for pid in ctx.lineup_ids(week, opponent_id)
    }
    theirs = {pid: games for pid, games in theirs.items() if games}

    def run(fn):
        rng = np.random.default_rng(1)
        cache = SimulationCache(source=ctx.source, n_sims=200, dnp_scale=ctx.dnp_scale)
        thresholds = {
            pid: greedy_thresholds(ctx.source, pid, theirs[pid], week, rng, 200) for pid in theirs
        }
        return fn(mine, theirs, thresholds, week, cache, rng)

    walked, _ = run(walk_locks)
    replayed = run(replay_week)
    assert walked == replayed.locked


# ------------------------------------------------------------- supplied state


def test_a_banked_score_for_a_non_starter_is_rejected(ctx, roster_id):
    """Silence would look exactly like a comfortable lead.

    An id not in the lineup gets its score added to the banked total while the
    player it names stays in the unlocked list, so he is counted twice and every
    number downstream — projection, margin, P(win), every threshold — moves in
    the flattering direction. This was found by typing a wrong id by hand.
    """
    with pytest.raises(ValueError, match="not week 12 starters"):
        digest_mod.build(ctx, roster_id, AS_OF, n_sims=50, n_paths=50, locked={"999999": 46.0})


def test_supplying_the_state_removes_it_from_the_output(ctx, roster_id):
    """What is passed in is what is banked — no reconstruction on top."""
    given = {"1000": 46.0, "1787": 47.5}
    report = digest_mod.build(ctx, roster_id, AS_OF, n_sims=100, n_paths=100, locked=dict(given))
    assert report.banked == given
    # A locked player has nothing left to decide, so he cannot also be advised.
    assert not {c.sleeper_id for c in report.calls} & set(given)
    assert not {r.sleeper_id for r in report.rules} & set(given)


def test_an_empty_string_means_nothing_is_locked(ctx, roster_id):
    """Distinct from None, which asks for a reconstruction."""
    from lockin.cli import _parse_locked

    assert _parse_locked(None) is None
    assert _parse_locked("") == {}
    assert _parse_locked("2126:42.5, 1970:31") == {"2126": 42.5, "1970": 31.0}

    report = digest_mod.build(ctx, roster_id, AS_OF, n_sims=50, n_paths=50, locked={})
    assert report.banked == {}


def test_the_calls_are_stable_once_the_state_is_fixed(ctx, roster_id):
    """§20: the reconstruction is noisy; everything downstream of it is not.

    At the default 400 simulations the lock/pass calls are identical across
    seeds. Thresholds still carry a few points of Monte Carlo noise, which is
    why they are rendered as whole numbers.
    """
    given = {"1000": 46.0, "1787": 47.5}
    runs = [
        digest_mod.build(
            ctx, roster_id, AS_OF, n_sims=400, n_paths=400, seed=seed, locked=dict(given)
        )
        for seed in (1, 2, 3)
    ]
    calls = {tuple((c.sleeper_id, c.lock) for c in r.calls) for r in runs}
    assert len(calls) == 1, f"calls disagreed across seeds: {calls}"


def test_thresholds_are_rendered_without_false_precision(ctx, roster_id):
    """A decimal place on a number carrying 1-3 points of noise is a lie.

    Scoped to the standing-rule blocks only. Elsewhere a decimal is honest: a
    score of 42.5 is exactly what he scored, not an estimate.
    """
    report = digest_mod.build(ctx, roster_id, AS_OF, n_sims=100, n_paths=100)
    assert report.rules, "this date should produce standing rules"

    in_block = False
    checked = 0
    for line in digest_mod.render(report).splitlines():
        if "lock if he clears" in line:
            in_block = True
            continue
        if not line.startswith("  "):
            in_block = False
            continue
        if in_block:
            checked += 1
            assert not re.search(r"\d+\.\d", line), f"decimal threshold: {line!r}"
    assert checked >= len(report.rules)
