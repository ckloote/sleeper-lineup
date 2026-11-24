"""Rotation regulars, and the bench-aware shadow the digest records beside itself.

A regular is someone this manager starts on most nights he has a game. On the
other nights he sits because he has no game, which says nothing about him. The
poll that shows who was in the lineup for a night's games is the first one
after that slate ends and before the next day's earliest tip.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from lockin import digest as digest_mod
from lockin.config import env_flag
from lockin.core.policy import Game
from lockin.digest import Digest, LockCall, StandingRule, persist, render_shadow
from lockin.projections import date_of, day_index
from lockin.rollout import Bench, SimulationCache, bench_rows
from lockin.rotation import Usage, lineup_usage, missed_last_game, regulars
from lockin.store.db import apply_schema

MON = day_index("2026-11-16")
ROSTER = 4


def panel(**players: list[tuple[int, bool]]) -> SimpleNamespace:
    """``name=[(day, played), ...]``, all in week 5."""
    return SimpleNamespace(
        histories={
            pid: SimpleNamespace(
                day=np.array([d for d, _ in games]),
                week=np.full(len(games), 5),
                played=np.array([p for _, p in games]),
            )
            for pid, games in players.items()
        }
    )


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c)
    yield c
    c.close()


def poll(conn, day: int, hour: str, lineup: dict[str, bool], week: int = 5) -> None:
    stamp = f"{date_of(day)}T{hour}+00:00"
    conn.executemany(
        "INSERT INTO weekly_matchups (week, roster_id, sleeper_id, is_starter, observed_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(week, ROSTER, pid, int(on), stamp) for pid, on in lineup.items()],
    )


def test_a_regular_sits_only_on_nights_he_has_no_game(conn):
    """KAT plays Mon, Wed, Fri and is started for each; benched Tue and Thu."""
    p = panel(kat=[(MON, True), (MON + 2, True), (MON + 4, True)])
    for day in range(MON, MON + 6):
        # The morning after a game night shows the lineup that game counted in.
        poll(conn, day, "11:30:00", {"kat": (day - 1) in (MON, MON + 2, MON + 4)})
    usage = lineup_usage(conn, p, ROSTER, through_day=MON + 4)
    assert usage["kat"] == Usage(3, 3)
    assert usage["kat"].regular


def test_the_mornings_own_poll_is_not_the_lineup_for_that_night(conn):
    """A game-day morning poll predates the manager setting that night's lineup."""
    p = panel(kat=[(MON, True)])
    poll(conn, MON, "11:30:00", {"kat": False})  # same morning: ignored
    poll(conn, MON + 1, "11:30:00", {"kat": True})  # after the slate: counts
    assert lineup_usage(conn, p, ROSTER, through_day=MON)["kat"] == Usage(1, 1)


def test_a_poll_after_the_next_tip_and_a_missing_poll_both_skip_the_night(conn):
    p = panel(kat=[(MON, True), (MON + 2, True)])
    poll(conn, MON + 1, "23:03:00", {"kat": False})  # too late for Monday's lineup
    assert "kat" not in lineup_usage(conn, p, ROSTER, through_day=MON + 2)


def test_nothing_after_the_decision_is_read(conn):
    p = panel(kat=[(MON, True), (MON + 2, True)])
    poll(conn, MON + 1, "11:30:00", {"kat": True})
    poll(conn, MON + 3, "11:30:00", {"kat": False})
    assert lineup_usage(conn, p, ROSTER, through_day=MON)["kat"] == Usage(1, 1)


def test_regulars_need_enough_nights_a_high_share_and_their_last_game():
    assert not Usage(2, 2).regular
    assert Usage(3, 5).regular  # 0.6
    assert not Usage(2, 4).regular


def test_who_counts_as_a_regular(conn):
    nights = [(MON + d, True) for d in range(4)]
    p = panel(
        kat=nights,
        fringe=nights,
        hurt=[*nights[:3], (MON + 3, False)],
    )
    for d in range(1, 5):
        poll(conn, MON + d, "11:30:00", {"kat": True, "fringe": d == 1, "hurt": True})
    assert regulars(conn, p, ROSTER, ["kat", "fringe", "hurt"], through_day=MON + 3) == ["kat"]
    assert missed_last_game(p, "hurt", MON + 3)
    assert not missed_last_game(p, "hurt", MON + 2)


class Source:
    """Records the hazard scale each projection is asked for."""

    def __init__(self):
        self.scales: list[float] = []

    def project_path(self, sleeper_id, day, days, weeks, *, rng, n_paths, dnp_scale=1.0):
        self.scales.append(dnp_scale)
        return np.full((n_paths, len(days)), 30.0)


def test_regulars_get_the_starter_correction_and_others_do_not():
    source = Source()
    cache = SimulationCache(source=source, n_sims=10, dnp_scale={5: 0.5})
    games = [Game(0, MON + 2, False, 0.0)]
    bench = Bench.from_lineup(("C",), ["s1"], {"kat": games, "other": games}, {}, regulars=["kat"])
    rows = bench_rows(
        cache, bench, {}, 5, np.random.default_rng(0), known_through=MON, start=MON + 1
    )
    assert set(rows) == {"kat", "other"}
    assert sorted(source.scales) == [0.5, 1.0]


def test_bench_shadow_defaults_on(monkeypatch):
    monkeypatch.delenv("LOCKIN_BENCH_SHADOW", raising=False)
    assert env_flag("LOCKIN_BENCH_SHADOW", default=True)
    monkeypatch.setenv("LOCKIN_BENCH_SHADOW", "0")
    assert not env_flag("LOCKIN_BENCH_SHADOW", default=True)


def call(pid: str, lock: bool, via: tuple[str, ...] = ()) -> LockCall:
    return LockCall(pid, pid.upper(), MON, 40.0, lock, 0.3, 0.2, 38.0, via=via)


def digest(**kw) -> Digest:
    return Digest(
        as_of=date_of(MON + 1),
        week=5,
        roster_id=ROSTER,
        opponent_roster_id=7,
        known_through=MON,
        **kw,
    )


def test_the_shadow_is_recorded_against_the_live_run(conn):
    shadow = digest(
        calls=[call("holiday", False, ("kat",))],
        rules=[StandingRule("allen", "ALLEN", MON + 1, 36.0, 0.2, 0, 1)],
        p_win=0.18,
        my_total=264.0,
        model="x; bench-aware",
        candidates=("kat",),
    )
    live = digest(calls=[call("holiday", True)], p_win=0.04, shadow=shadow)
    persist(conn, live)
    run_id = conn.execute("SELECT run_id FROM digest_runs").fetchone()[0]
    head = conn.execute("SELECT * FROM bench_shadow_runs").fetchone()
    assert head["run_id"] == run_id
    assert (head["p_win"], json.loads(head["candidates"]), head["error"]) == (0.18, ["kat"], None)
    rows = conn.execute(
        "SELECT sleeper_id, action, threshold, via FROM bench_shadow ORDER BY action"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("holiday", "PASS", 38.0, '["kat"]'),
        ("allen", "THRESHOLD", 36.0, None),
    ]
    # The live run's own rows are untouched by the shadow.
    actions = conn.execute("SELECT action FROM recommendations WHERE run_id = ?", (run_id,))
    assert [r[0] for r in actions] == ["LOCK"]


def test_a_failed_shadow_is_recorded_and_no_shadow_writes_nothing(conn):
    persist(conn, digest(shadow_error="ValueError: boom"))
    persist(conn, digest())
    assert [tuple(r) for r in conn.execute("SELECT error FROM bench_shadow_runs")] == [
        ("ValueError: boom",)
    ]


def test_the_log_line_names_only_the_calls_that_change():
    shadow = digest(
        calls=[call("holiday", False, ("kat",)), call("allen", False)],
        p_win=0.18,
        names={"kat": "Karl-Anthony Towns"},
        candidates=("kat",),
    )
    live = digest(calls=[call("holiday", True), call("allen", False)], shadow=shadow)
    text = render_shadow(live)
    assert "P(win) 18%" in text and "Karl-Anthony Towns" in text
    assert "HOLIDAY: LOCK -> PASS (passing lets you start Karl-Anthony Towns)" in text
    assert "ALLEN" not in text
    assert render_shadow(digest()) is None
    assert render_shadow(digest(shadow_error="boom")) == "bench shadow failed: boom"


def test_a_failing_shadow_never_costs_the_digest(monkeypatch):
    real = digest(calls=[call("holiday", True)])

    def build(ctx, roster_id, as_of, *, bench_aware, **_):
        if bench_aware:
            raise RuntimeError("bench exploded")
        return real

    monkeypatch.setattr(digest_mod, "load_context", lambda *a, **k: None)
    monkeypatch.setattr(digest_mod, "build", build)
    out = digest_mod.morning(None, "2026", ROSTER, date_of(MON + 1), shadow=True)
    assert out is real
    assert out.shadow is None and out.shadow_error == "RuntimeError: bench exploded"


def test_no_shadow_when_bench_aware_is_already_on_or_nothing_was_called(monkeypatch):
    seen = []

    def build(ctx, roster_id, as_of, *, bench_aware, **_):
        seen.append(bench_aware)
        return digest()

    monkeypatch.setattr(digest_mod, "load_context", lambda *a, **k: None)
    monkeypatch.setattr(digest_mod, "build", build)
    digest_mod.morning(None, "2026", ROSTER, date_of(MON + 1), shadow=True)
    digest_mod.morning(None, "2026", ROSTER, date_of(MON + 1), shadow=True, bench_aware=True)
    assert seen == [False, True]


def test_a_live_morning_takes_the_digest_and_its_shadow_at_one_instant(monkeypatch):
    seen = []

    def build(ctx, roster_id, as_of, *, bench_aware, now, **_):
        seen.append(now)
        return digest(calls=[call("holiday", True)])

    monkeypatch.setattr(digest_mod, "load_context", lambda *a, **k: None)
    monkeypatch.setattr(digest_mod.calendar, "disagreement", lambda *a: None)
    monkeypatch.setattr(digest_mod, "build", build)
    out = digest_mod.morning(None, "2026", ROSTER, date_of(MON + 1), live=True, shadow=True)
    assert out.shadow is not None
    assert len(seen) == 2 and seen[0] is seen[1]
    assert seen[0] is not None and seen[0].tzinfo is not None
