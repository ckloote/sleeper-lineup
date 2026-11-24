"""Bench-aware lock/pass: passing keeps a slot open for a bench player.

The pass branch used to be the player's own continuation and nothing else, so a
decent night from a mid-level starter with one game left was banked even when a
better bench player had several games still to come. These pin the fix at the
level it lives: `with_bench` fields each branch, and the winprob functions
compare them.
"""

from __future__ import annotations

import numpy as np
import pytest

from lockin.config import env_flag
from lockin.core.policy import Game
from lockin.core.winprob import evaluate_lock, lock_threshold
from lockin.rollout import Bench, with_bench

N = 4000


def row(mean: float, sd: float = 6.0, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(mean, sd, N).clip(min=0.0)


def bench(slots, starters, positions, bench_games=None) -> Bench:
    games = bench_games or {}
    return Bench.from_lineup(slots, starters, games, positions)


def decide(plan, score, opponent, banked=0.0):
    args = dict(
        banked=banked,
        contributions=plan.passing[None, :],
        player=0,
        opponent=opponent,
        others_if_locked=plan.others_if_locked,
    )
    return evaluate_lock(**args, lock_value=score), lock_threshold(**args)


def test_others_if_locked_omitted_is_the_old_comparison():
    contributions = np.vstack([row(25, seed=1), row(30, seed=2)])
    opponent = row(55, sd=10, seed=3)
    old = evaluate_lock(0.0, contributions, 0, 30.0, opponent)
    same = evaluate_lock(0.0, contributions, 0, 30.0, opponent, others_if_locked=None)
    assert old == same
    assert lock_threshold(0.0, contributions, 0, opponent) == lock_threshold(
        0.0, contributions, 0, opponent, others_if_locked=None
    )


def test_a_better_bench_player_turns_a_lock_into_a_pass():
    """30 tonight, one game left worth ~25: bank it, unless the bench has a 40."""
    slots = ("C", "UTIL")
    b = bench(slots, ["s1", "s2"], {"s1": ["C"], "s2": ["SF"], "b1": ["C"]}, {"b1": []})
    # s2 is strong enough to keep UTIL in either branch, so b1 only plays if
    # s1's slot stays open.
    rows = {"s1": row(25, seed=1), "s2": row(45, seed=2)}
    opponent = row(75, sd=12, seed=3)

    old = evaluate_lock(0.0, np.vstack([rows["s1"], rows["s2"]]), 0, 30.0, opponent)
    assert old.lock

    plan = with_bench(b, rows, {"b1": row(40, seed=4)}, [], "s1", N)
    assert plan is not None
    assert plan.brought_in == ("b1",)
    call, threshold = decide(plan, 30.0, opponent)
    assert not call.lock
    assert threshold > 30.0


def test_an_ineligible_bench_player_changes_nothing():
    """A guard cannot take a C-only slot, however good he is."""
    slots = ("C",)
    b = bench(slots, ["s1"], {"s1": ["C"], "b1": ["PG"]}, {"b1": []})
    rows = {"s1": row(25, seed=1)}
    opponent = row(30, sd=10, seed=3)

    plan = with_bench(b, rows, {"b1": row(40, seed=4)}, [], "s1", N)
    assert plan is not None
    assert plan.brought_in == ()
    np.testing.assert_array_equal(plan.passing, rows["s1"])
    old = evaluate_lock(0.0, rows["s1"][None, :], 0, 30.0, opponent)
    new, _ = decide(plan, 30.0, opponent)
    assert (new.lock, new.p_win_pass, new.p_win_lock) == (
        old.lock,
        old.p_win_pass,
        old.p_win_lock,
    )


def test_one_bench_player_fills_one_slot():
    slots = ("UTIL", "UTIL")
    b = bench(slots, ["s1", "s2"], {"s1": ["SF"], "s2": ["SF"], "b1": ["SF"]}, {"b1": []})
    rows = {"s1": row(20, sd=0.1, seed=1), "s2": row(22, sd=0.1, seed=2)}
    plan = with_bench(b, rows, {"b1": row(40, sd=0.1, seed=4)}, [], None, N)
    assert plan is not None
    assert sorted(plan.fielded.values()) == ["b1", "s2"]
    assert plan.passing.mean() == pytest.approx(62, abs=0.5)


def test_locking_frees_the_bench_player_for_another_slot():
    """The lock branch fields its other slots with the bench too."""
    slots = ("C", "UTIL")
    b = bench(slots, ["s1", "s2"], {"s1": ["C"], "s2": ["SF"], "b1": ["C"]}, {"b1": []})
    rows = {"s1": row(25, sd=0.1, seed=1), "s2": row(10, sd=0.1, seed=2)}
    plan = with_bench(b, rows, {"b1": row(40, sd=0.1, seed=4)}, [], "s1", N)
    assert plan is not None
    # Locked, s1 holds C, so b1 goes to UTIL over the 10-point s2.
    assert plan.others_if_locked.mean() == pytest.approx(40, abs=0.5)
    assert plan.brought_in == ()


def test_locked_players_stay_pinned_to_their_slots():
    slots = ("C", "UTIL")
    b = bench(slots, ["s1", "s2"], {"s1": ["C"], "s2": ["C"], "b1": ["C"]}, {"b1": []})
    rows = {"s2": row(10, sd=0.1, seed=2)}
    plan = with_bench(b, rows, {"b1": row(40, sd=0.1, seed=4)}, ["s1"], None, N)
    assert plan is not None
    assert plan.fielded["C"] == "s1"
    assert plan.fielded["UTIL"] == "b1"


def test_no_usable_bench_falls_back():
    b = bench(("C",), ["s1"], {"s1": ["C"]})
    assert with_bench(b, {"s1": row(25)}, {}, [], "s1", N) is None


def test_an_illegal_lineup_on_file_falls_back():
    """Positions that cannot make a legal lineup are no reason to stop advising."""
    b = bench(("C",), ["s1"], {"s1": ["PG"], "b1": ["PG"]}, {"b1": []})
    assert with_bench(b, {"s1": row(25)}, {"b1": row(40)}, [], "s1", N) is None


def test_bench_excludes_starters_and_needs_one_starter_per_slot():
    games = {"s1": [Game(0, 1, True, 10.0)], "b1": [Game(0, 2, False, 0.0)]}
    b = Bench.from_lineup(("C",), ["s1"], games, {"s1": ["C"], "b1": ["C"]})
    assert list(b.games) == ["b1"]
    assert b.slot_of == {"s1": "C"}
    with pytest.raises(ValueError):
        Bench.from_lineup(("C", "UTIL"), ["s1"], games, {})


def test_duplicate_slots_get_distinct_keys():
    b = Bench.from_lineup(("UTIL", "UTIL"), ["s1", "s2"], {}, {})
    assert b.slot_of == {"s1": "UTIL#1", "s2": "UTIL#2"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", False), ("0", False), ("off", False), ("1", True), ("TRUE", True), (" yes ", True)],
)
def test_env_flag(monkeypatch, raw, expected):
    monkeypatch.setenv("LOCKIN_BENCH_AWARE", raw)
    assert env_flag("LOCKIN_BENCH_AWARE") is expected


def test_env_flag_rejects_typos(monkeypatch):
    monkeypatch.setenv("LOCKIN_BENCH_AWARE", "ture")
    with pytest.raises(ValueError):
        env_flag("LOCKIN_BENCH_AWARE")


class RecordingSource:
    """Hands out fixed paths and records which days it was asked to project."""

    def __init__(self, paths: np.ndarray):
        self.paths = paths
        self.days: list[list[int]] = []

    def project_path(self, sleeper_id, day, days, weeks, *, rng, n_paths, dnp_scale=1.0):
        self.days.append(list(days))
        return self.paths[:n_paths, : len(days)]


def test_a_swap_in_is_simulated_through_the_games_he_sits_out():
    """Wednesday on the bench still happens: it is projected, never counted."""
    from lockin.rollout import SimulationCache

    # Wednesday 90 on every path: banked if it could count. Thursday 20, Friday 30.
    paths = np.tile([90.0, 20.0, 30.0], (8, 1))
    source = RecordingSource(paths)
    cache = SimulationCache(source=source, n_sims=8)
    games = [Game(0, 10, False, 0.0), Game(1, 11, False, 0.0), Game(2, 12, False, 0.0)]
    row = cache.bench_contribution(
        "b1", games, 5, np.random.default_rng(0), known_through=9, start=11
    )
    assert source.days == [[10, 11, 12]]
    # Thursday 20 is below riding on to Friday's 30, so Friday counts.
    np.testing.assert_array_equal(row, np.full(8, 30.0))


def test_a_swap_in_with_nothing_left_after_the_swap_is_no_option():
    from lockin.rollout import SimulationCache

    source = RecordingSource(np.full((8, 3), 50.0))
    cache = SimulationCache(source=source, n_sims=8)
    games = [Game(0, 10, False, 0.0)]
    rng = np.random.default_rng(0)
    assert cache.bench_contribution("b1", games, 5, rng, known_through=9, start=11) is None
    assert source.days == []


class NoisySource:
    """Draws that depend on the rng, so a served sample shows whose stream it came from."""

    def project_path(self, sleeper_id, day, days, weeks, *, rng, n_paths, dnp_scale=1.0):
        return rng.normal(30.0, 12.0, (n_paths, len(days))).clip(min=0.0)


def _after_bench_replay(isolate: bool | None):
    """The shared cache after roster A's baseline and, optionally, its bench replay.

    ``isolate`` None skips the bench replay; True runs it on `isolated()`.
    """
    from lockin.rollout import SimulationCache, replay_week

    def week(*days):
        return [Game(i, d, True, 10.0 + 7 * i) for i, d in enumerate(days)]

    a = {"a1": week(1, 3, 6), "a2": week(2, 5)}
    opp = {"o1": week(1, 4, 6)}
    greedy = {pid: {g.index: 25.0 for g in games} for pid, games in {**a, **opp}.items()}
    cache = SimulationCache(source=NoisySource(), n_sims=64)
    replay_week(a, opp, greedy, 5, cache, np.random.default_rng(1))
    if isolate is not None:
        # No bench candidates at all: the swap step still runs every night.
        empty = Bench.from_lineup(("UTIL", "UTIL"), list(a), {}, {})
        shared = cache.isolated() if isolate else cache
        replay_week(a, opp, greedy, 5, shared, np.random.default_rng(1), bench=empty)
    return cache


def test_the_bench_replay_cannot_move_a_later_baseline():
    """Whatever a later roster-week's baseline reads is what the baseline wrote."""
    baseline = _after_bench_replay(None)
    after = _after_bench_replay(True)
    assert after.contributions.keys() == baseline.contributions.keys()
    for key, row in baseline.contributions.items():
        np.testing.assert_array_equal(after.contributions[key], row)
    assert not after.bench_rows


def test_without_isolation_the_bench_replay_leaks():
    """Pins that the setup above can see the leak, so its pass means something:
    the swap step draws continuations on nights the baseline never visited."""
    baseline = _after_bench_replay(None)
    leaked = _after_bench_replay(False)
    assert set(leaked.contributions) > set(baseline.contributions)


def test_an_isolated_cache_reads_the_baseline_draws():
    """Pairing: where both walks ask for the same continuation, they share it."""
    cache = _after_bench_replay(None)
    view = cache.isolated()
    key = next(iter(cache.contributions))
    assert view.contributions[key] is cache.contributions[key]


def _slate(tips: dict[tuple[str, int], str]):
    from lockin.slate import WeekSlate

    slate = WeekSlate()
    slate.tipoff.update(tips)
    return slate


TODAY = 739943  # 2026-11-23


def test_a_bench_player_whose_game_has_tipped_is_promotable_only_from_tomorrow():
    from lockin.digest import tipped

    games = {
        "gone": [Game(0, TODAY, False, 0.0), Game(1, TODAY + 1, False, 0.0)],
        "later": [Game(0, TODAY, False, 0.0)],
        "unknown": [Game(0, TODAY, False, 0.0)],
    }
    slate = _slate(
        {
            ("gone", TODAY): "2026-11-23T19:00:00+00:00",
            ("later", TODAY): "2026-11-24T00:30:00+00:00",
        }
    )
    now = "2026-11-23T20:00:00+00:00"
    assert tipped(slate, games, TODAY - 1, before=now, live=True) == {
        "gone": TODAY + 1,
        "unknown": TODAY + 1,
    }
    # A replay runs that morning, before any tip, and an unknown tip is no bar.
    morning = "2026-11-23T13:00:00+00:00"
    assert tipped(slate, games, TODAY - 1, before=morning, live=False) == {}


def test_no_swap_into_a_game_already_under_way():
    """His only game left has tipped: the call is priced as if he were not there."""
    from lockin.rollout import SimulationCache, bench_rows

    source = RecordingSource(np.full((8, 2), 50.0))
    cache = SimulationCache(source=source, n_sims=8)
    games = {"b1": [Game(0, TODAY, False, 0.0)]}
    rng = np.random.default_rng(0)
    open_ = Bench.from_lineup(("C",), ["s1"], games, {})
    shut = Bench.from_lineup(("C",), ["s1"], games, {}, promotable_from={"b1": TODAY + 1})
    assert set(bench_rows(cache, open_, {}, 5, rng, known_through=TODAY - 1, start=TODAY)) == {"b1"}
    assert bench_rows(cache, shut, {}, 5, rng, known_through=TODAY - 1, start=TODAY) == {}
    assert shut.copy().promotable_from == {"b1": TODAY + 1}


def test_a_tipped_game_is_still_simulated_before_the_next_one_counts():
    from lockin.rollout import SimulationCache, bench_rows

    # Today 90 on every path, tomorrow 20: today must not be what counts.
    source = RecordingSource(np.tile([90.0, 20.0], (8, 1)))
    cache = SimulationCache(source=source, n_sims=8)
    games = {"b1": [Game(0, TODAY, False, 0.0), Game(1, TODAY + 1, False, 0.0)]}
    shut = Bench.from_lineup(("C",), ["s1"], games, {}, promotable_from={"b1": TODAY + 1})
    rows = bench_rows(
        cache, shut, {}, 5, np.random.default_rng(0), known_through=TODAY - 1, start=TODAY
    )
    assert source.days == [[TODAY, TODAY + 1]]
    np.testing.assert_array_equal(rows["b1"], np.full(8, 20.0))


def test_a_live_build_without_now_reads_the_clock_once_for_every_deadline(monkeypatch):
    """Calls used the real clock and the bench the replay morning, when ``now`` was omitted."""
    from types import SimpleNamespace

    from lockin import digest as digest_mod

    seen = []

    class Stop(Exception):
        pass

    def slate_in_progress(as_of, now):
        seen.append(now)
        raise Stop

    monkeypatch.setattr(digest_mod.calendar, "disagreement", lambda *a: None)
    monkeypatch.setattr(digest_mod, "resolve_week", lambda *a: 5)
    monkeypatch.setattr(digest_mod, "stats_evidence", lambda *a: SimpleNamespace(problem=None))
    monkeypatch.setattr(digest_mod, "slate_in_progress", slate_in_progress)
    ctx = SimpleNamespace(conn=None, season="2026")
    with pytest.raises(Stop):
        digest_mod.build(ctx, 4, "2026-11-23", live=True, bench_aware=True)
    assert seen and seen[0] is not None and seen[0].tzinfo is not None


def test_an_unknown_tip_bars_only_a_game_dated_today():
    """A game later in the week cannot have tipped, whatever its hour."""
    from lockin.digest import tipped

    games = {"later": [Game(0, TODAY + 2, False, 0.0)], "today": [Game(0, TODAY, False, 0.0)]}
    now = "2026-11-23T20:00:00+00:00"
    assert tipped(_slate({}), games, TODAY - 1, before=now, live=True) == {"today": TODAY + 1}


def test_the_bench_assumption_names_the_first_game_it_counts():
    from lockin.digest import starts_from

    games = {"b1": [Game(0, TODAY, False, 0.0), Game(1, TODAY + 1, False, 0.0)]}
    open_ = Bench.from_lineup(("C",), ["s1"], games, {})
    shut = Bench.from_lineup(("C",), ["s1"], games, {}, promotable_from={"b1": TODAY + 1})
    assert starts_from(open_, "b1", TODAY - 1) == "tonight"
    assert starts_from(shut, "b1", TODAY - 1) == "2026-11-24"


def test_the_swap_names_who_the_fielded_lineup_benches_not_the_deciding_player():
    """Review of PR #3: passing on 1006 moved him UTIL -> F, benched 1003, and
    started 1002 at UTIL. "Bench him and start them" told the manager to bench
    1006, the 30-point player the model kept, and keep the 11-point 1003."""
    from lockin.digest import Digest, LockCall, swap_text

    slots = ("F", "UTIL")
    positions = {"1003": ["SF"], "1006": ["SF"], "1002": ["PG"]}
    b = bench(slots, ["1003", "1006"], positions, {"1002": []})
    rows = {"1003": row(11, sd=0.1, seed=1), "1006": row(30, sd=0.1, seed=2)}
    plan = with_bench(b, rows, {"1002": row(25, sd=0.1, seed=4)}, [], "1006", N)
    assert plan is not None
    assert plan.fielded == {"F": "1006", "UTIL": "1002"}
    assert plan.brought_in == ("1002",)
    assert plan.sent_out == ("1003",)
    assert plan.moves(b.slot_of) == (("1006", "F"), ("1002", "UTIL"))

    call = LockCall(
        "1006",
        "P1006",
        1,
        20.0,
        False,
        0.1,
        0.2,
        25.0,
        via=plan.brought_in,
        bench_out=plan.sent_out,
        moves=plan.moves(b.slot_of),
    )
    names = {"1002": "P1002", "1003": "P1003", "1006": "P1006"}
    d = Digest("2026-11-23", 5, 4, 7, 0, names=names, calls=[call])
    assert swap_text(d, call) == "; passing lets you start P1002 in place of P1003"
    assert "P1006" not in swap_text(d, call)


def test_the_swap_starts_every_bench_player_the_pass_lineup_uses():
    """Review of PR #3: both lineups started 1004 at G, so ``via`` was only 1002,
    and "start 1002, bench 1003 and 1007" left G empty. Arrivals come from the
    pass lineup against the current slots, not from the difference between
    the two branches."""
    slots = ("F", "G", "UTIL")
    positions = {"1003": ["SF"], "1007": ["PG"], "1006": ["SF"], "1004": ["PG"], "1002": ["PG"]}
    b = bench(slots, ["1003", "1007", "1006"], positions, {"1004": [], "1002": []})
    rows = {
        "1003": row(11, sd=0.1, seed=1),
        "1007": row(5, sd=0.1, seed=2),
        "1006": row(30, sd=0.1, seed=3),
    }
    extra = {"1004": row(35, sd=0.1, seed=4), "1002": row(25, sd=0.1, seed=5)}
    plan = with_bench(b, rows, extra, [], "1006", N)
    assert plan is not None
    # 1002 and 1004 are both guards, so which of G and UTIL each takes is a tie.
    assert plan.fielded["F"] == "1006"
    assert sorted(plan.fielded.values()) == ["1002", "1004", "1006"]
    assert plan.brought_in == ("1002",)
    assert plan.arriving == ("1002", "1004")
    assert plan.sent_out == ("1003", "1007")


def test_a_starter_with_no_games_is_named_when_his_slot_goes_to_someone_else():
    """Review of PR #3: 1003 held F with no games this week, so he had no row
    and vanished from the instructions while 1006 took F."""
    slots = ("F", "UTIL")
    positions = {"1003": ["SF"], "1006": ["SF"], "1002": ["PG"], "1008": ["SF"]}
    b = bench(slots, ["1003", "1006"], positions, {"1002": [], "1008": []})
    rows = {"1006": row(30, sd=0.1, seed=2)}  # 1003: no games, no row
    # 1008 is there so the lock lineup, 1006 pinned at UTIL, can fill F.
    extra = {"1002": row(25, sd=0.1, seed=4), "1008": row(8, sd=0.1, seed=5)}
    plan = with_bench(b, rows, extra, [], "1006", N)
    assert plan is not None
    assert plan.fielded == {"F": "1006", "UTIL": "1002"}
    assert plan.sent_out == ("1003",)


def test_a_starter_whose_slot_stays_empty_is_not_sent_to_the_bench():
    from lockin.rollout import lineup_changes

    b = bench(("F", "C"), ["1003", "1005"], {"1003": ["SF"], "1005": ["C"]})
    # Nobody can play C this week: 1005 stays where he is, harmlessly.
    assert lineup_changes(b, {"F": "1003"}) == ((), ())
    assert lineup_changes(b, {"F": "1009"}) == (("1003",), ("1009",))


def test_the_instructions_name_every_arrival_and_departure():
    from lockin.digest import Digest, LockCall, render, swap_text

    call = LockCall(
        "1006",
        "P1006",
        1,
        20.0,
        False,
        0.1,
        0.2,
        25.0,
        via=("1002",),
        bench_in=("1002", "1004"),
        bench_out=("1003", "1007"),
    )
    names = {p: f"P{p}" for p in ("1002", "1003", "1004", "1006", "1007")}
    d = Digest(
        "2026-11-23",
        5,
        4,
        7,
        0,
        names=names,
        calls=[call],
        p_win=0.5,
        my_total=200.0,
        opponent_total=210.0,
        margin={"q10": -40, "q50": -10, "q90": 20},
    )
    assert swap_text(d, call) == "; passing lets you start P1002, P1004 in place of P1003, P1007"
    text = render(d)
    assert "pass: start P1002, P1004" in text and "and bench P1003, P1007" in text
