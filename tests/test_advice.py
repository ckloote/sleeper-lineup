"""The recommendations page — a reader of what the digest already decided.

The property worth testing is not that it renders. It is that what comes back
out equals what went in, because the whole reason this module exists is that
recomputing gives a different answer: the reconstructed banked state is a chain
of near-tied calls and thresholds carry Monte Carlo noise (§20), so a page that
recomputed would quietly disagree with the notification the user acted on.
`test_the_page_reproduces_the_digest_exactly` is that property.

The second theme is staleness. The failure mode of a recommendations page is
showing yesterday's calls as though they were today's, so age is measured from
`as_of` rather than from when the process ran, and the banner has to precede the
advice it qualifies.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from lockin import advice
from lockin import digest as digest_mod
from lockin.config import Config
from lockin.projections import date_of, day_index
from lockin.store.db import apply_schema, session

cfg = Config.from_env()
pytestmark = pytest.mark.skipif(
    not cfg.db_path.exists(), reason=f"no database at {cfg.db_path}; run `lockin ingest`"
)

AS_OF = "2026-01-08"
BANKED = {"1000": 46.0, "1787": 47.5}


@pytest.fixture(scope="module")
def source_conn():
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    apply_schema(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def report(source_conn):
    """A real digest, state supplied so it is reproducible across runs."""
    ctx = digest_mod.load_context(source_conn, cfg.season)
    roster_id = digest_mod.roster_for_user(source_conn, cfg.user_id)
    return digest_mod.build(ctx, roster_id, AS_OF, n_sims=200, n_paths=200, locked=dict(BANKED))


def seed_players(conn, report):
    """Copy across the `players` rows a real database would already hold.

    `advice` resolves names by joining `players`, so a scratch database without
    it silently renders sleeper ids. Seeding keeps the fixture honest rather
    than weakening the assertion to match an unrealistic setup.
    """
    conn.executemany(
        "INSERT OR REPLACE INTO players"
        " (sleeper_id, full_name, positions, updated_at) VALUES (?, ?, '[]', 'test')",
        list(report.names.items()),
    )


@pytest.fixture
def stored(tmp_path, report):
    """Persist into a scratch database and read it back. The round trip."""
    with session(tmp_path / "advice.db") as conn:
        digest_mod.persist(conn, report, state_supplied=True)
        seed_players(conn, report)
        yield conn, advice.latest_run(conn, report.roster_id)


# ------------------------------------------------------------------ round trip


def test_the_page_reproduces_the_digest_exactly(stored, report):
    """Read back == what was decided. The reason the module is a reader.

    Compared on the numbers a user acts on: which players, lock or pass, the
    break-even, and every standing threshold. A recomputation would move all of
    them by a point or two and nobody could tell which version was advised.
    """
    _, run = stored
    assert run is not None

    calls = {i.sleeper_id: i for i in run.calls}
    assert set(calls) == {c.sleeper_id for c in report.calls}
    for call in report.calls:
        item = calls[call.sleeper_id]
        assert item.action == ("LOCK" if call.lock else "PASS")
        assert item.threshold == pytest.approx(call.break_even)
        assert item.ev_lock == pytest.approx(call.p_win_lock)
        assert item.ev_pass == pytest.approx(call.p_win_pass)

    rules = {(i.sleeper_id, i.for_day): i.threshold for i in run.rules}
    assert rules == {(r.sleeper_id, r.night): r.threshold for r in report.rules}


def test_the_run_carries_the_state_the_calls_were_taken_against(stored, report):
    """A call means nothing without it, and it used to be stored nowhere."""
    _, run = stored
    assert run.p_win == pytest.approx(report.p_win)
    assert run.projected == pytest.approx(report.my_total)
    assert run.opponent_projected == pytest.approx(report.opponent_total)
    assert run.banked_total == pytest.approx(sum(BANKED.values()))
    assert run.banked_slots == len(BANKED)
    assert run.opponent_roster_id == report.opponent_roster_id
    assert run.state_supplied is True


def test_names_are_resolved_not_left_as_ids(stored):
    _, run = stored
    assert all(not i.name.isdigit() for i in run.items), [i.name for i in run.items]


def test_a_player_missing_from_the_reference_table_degrades_to_his_id(tmp_path, report):
    """Ugly, but readable and never a crash.

    `players` is refreshed on every ingest, so this should not arise — but a
    LEFT JOIN that produced `None` here would put "None" on the page, which is
    worse than an id you can look up.
    """
    with session(tmp_path / "bare.db") as conn:
        digest_mod.persist(conn, report, state_supplied=True)  # no players seeded
        run = advice.latest_run(conn, report.roster_id)
    assert run.items
    assert all(i.name == i.sleeper_id for i in run.items)
    assert "None" not in advice.render(run, today=AS_OF)


# ----------------------------------------------------------------- which run


def test_the_latest_run_wins(tmp_path, report):
    """Re-running is a second opinion; the page shows the current one."""
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, report, state_supplied=True)
        newer = replace(report, p_win=0.99) if False else report
        conn.execute(
            "INSERT OR REPLACE INTO digest_runs"
            " (generated_at, roster_id, as_of, week, p_win, state_supplied)"
            " VALUES ('2099-01-01T00:00:00+00:00', ?, ?, ?, 0.99, 1)",
            (newer.roster_id, AS_OF, newer.week),
        )
        run = advice.latest_run(conn, report.roster_id)
    assert run.generated_at == "2099-01-01T00:00:00+00:00"
    assert run.p_win == pytest.approx(0.99)


def test_another_rosters_run_is_not_shown(tmp_path, report):
    """The reason `roster_id` was added: two rosters used to interleave."""
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, report, state_supplied=True)
        other = report.roster_id + 1
        assert advice.latest_run(conn, other) is None
        run = advice.latest_run(conn, report.roster_id)
    assert run.roster_id == report.roster_id


def test_rows_written_before_roster_id_existed_still_attach(tmp_path, report):
    """Nullable column, and the old rows belong to the only roster there was."""
    with session(tmp_path / "t.db") as conn:
        digest_mod.persist(conn, report, state_supplied=True)
        conn.execute("UPDATE recommendations SET roster_id = NULL")
        run = advice.latest_run(conn, report.roster_id)
    assert len(run.items) == len(report.calls) + len(report.rules)


def test_no_digest_yet_is_not_an_error(tmp_path):
    with session(tmp_path / "empty.db") as conn:
        assert advice.latest_run(conn, 4) is None
    page = advice.render(None)
    assert "lockin digest" in page


# ------------------------------------------------------------------ staleness


def test_age_is_measured_from_the_morning_described_not_the_run_time(stored):
    """`generated_at` is when the process ran; `as_of` is what it is about.

    A digest re-rendered at midnight for the previous morning is a day old
    whatever its timestamp says, and that is what the reader needs told.
    """
    _, run = stored
    assert run.age_days(AS_OF) == 0
    assert run.age_days(date_of(day_index(AS_OF) + 1)) == 1
    assert run.age_days(date_of(day_index(AS_OF) + 30)) == 30


@pytest.mark.parametrize(
    ("offset", "tone", "needle"),
    [
        (0, "fresh", "this morning"),
        (1, "stale", "YESTERDAY"),
        (5, "stale", "5 days old"),
    ],
)
def test_the_banner_states_the_age(stored, offset, tone, needle):
    _, run = stored
    today = date_of(day_index(AS_OF) + offset)
    css, sentence = advice._freshness(run, today)
    assert css == tone
    assert needle in sentence


def test_the_staleness_banner_precedes_the_advice(stored):
    """It qualifies everything below it, so it cannot come after."""
    _, run = stored
    page = advice.render(run, today=date_of(day_index(AS_OF) + 3))
    # Keyed on the first section heading rather than its wording, which now
    # varies with whether there is anything to lock.
    assert page.index("banner") < page.index("<h2>")
    assert 'class="banner stale"' in page


def test_a_fresh_page_is_not_dressed_as_a_warning(stored):
    _, run = stored
    page = advice.render(run, today=AS_OF)
    assert 'class="banner fresh"' in page
    # The CSS defines both palettes; what must not appear is the stale *class*.
    assert 'class="banner stale"' not in page


# ------------------------------------------------------------------ rendering


def test_the_page_is_self_contained(stored):
    """Opened from a phone. No network, no build step."""
    _, run = stored
    page = advice.render(run, today=AS_OF)
    assert "http://" not in page
    assert "https://" not in page
    assert "<script" not in page.lower()
    assert "<link" not in page.lower()


def test_every_call_and_rule_reaches_the_page(stored, report):
    _, run = stored
    page = advice.render(run, today=AS_OF)
    for item in run.items:
        assert item.name in page


def test_player_names_are_escaped(stored):
    """They come from Sleeper, which is to say from other users."""
    _, run = stored
    hostile = replace(run.items[0], name="<img src=x onerror=1>")
    page = advice.render(replace(run, items=(hostile,)), today=AS_OF)
    assert "<img src=x" not in page
    assert "&lt;img" in page


def test_a_week_with_nothing_to_decide_renders_its_note(tmp_path, source_conn):
    """Week 25 has no matchup (§7.7). The page must say so, not show blank."""
    ctx = digest_mod.load_context(source_conn, cfg.season)
    roster_id = digest_mod.roster_for_user(source_conn, cfg.user_id)
    quiet = digest_mod.build(ctx, roster_id, "2026-04-07", n_sims=50, n_paths=50)
    assert quiet.note

    with session(tmp_path / "quiet.db") as conn:
        digest_mod.persist(conn, quiet)
        run = advice.latest_run(conn, roster_id)

    assert run is not None, "a run with no calls must still be recorded"
    assert run.items == ()
    page = advice.render(run, today="2026-04-07")
    assert "nothing to decide" in page


def test_an_inferred_state_is_disclosed_on_the_page(tmp_path, source_conn):
    """§20: it is the least stable number here, so it is not presented as fact."""
    ctx = digest_mod.load_context(source_conn, cfg.season)
    roster_id = digest_mod.roster_for_user(source_conn, cfg.user_id)
    inferred = digest_mod.build(ctx, roster_id, AS_OF, n_sims=100, n_paths=100)

    with session(tmp_path / "inferred.db") as conn:
        digest_mod.persist(conn, inferred, state_supplied=False)
        run = advice.latest_run(conn, roster_id)

    assert run.state_supplied is False
    page = advice.render(run, today=AS_OF)
    assert "--locked" in page
    assert "least stable" in page


# ------------------------------------------------------- the heading is a verdict


def test_an_all_pass_night_does_not_tell_you_to_act(stored):
    """Passing is inaction. "Do these now" over four PASS rows was an instruction
    to do something when the correct move was to do nothing."""
    _, run = stored
    assert all(i.action == "PASS" for i in run.calls), "this date should be all pass"
    page = advice.render(run, today=AS_OF)
    assert "Nothing to lock" in page
    assert "Lock now" not in page
    assert "No action needed" in page


def test_a_night_with_a_lock_says_so_in_the_heading(stored):
    """The heading is what gets scanned, so it carries the verdict."""
    _, run = stored
    with_lock = replace(run, items=(replace(run.calls[0], action="LOCK"), *run.items[1:]))
    page = advice.render(with_lock, today=AS_OF)
    assert "Lock now" in page
    assert "Nothing to lock" not in page
    assert "before his next game tips" in page


def test_the_heading_tracks_the_calls_not_the_count(stored):
    """One lock among several passes is still a night you must act on."""
    _, run = stored
    only_passes = advice.render(run, today=AS_OF)
    one_lock = advice.render(
        replace(run, items=(replace(run.calls[-1], action="LOCK"), *run.items[:-1])),
        today=AS_OF,
    )
    assert "Nothing to lock" in only_passes
    assert "Lock now" in one_lock


# ------------------------------------------------------------------ page order


def test_where_the_matchup_stands_comes_before_the_advice(stored):
    """Context is read first; it was doing no work at the bottom of the page."""
    _, run = stored
    page = advice.render(run, today=AS_OF)
    assert page.index("class=state") < page.index("<h2>")
    assert page.index("class=pwin") < page.index("Nothing to lock")


def test_the_banner_still_outranks_everything(stored):
    """Staleness qualifies the state block too, so it cannot slip below it."""
    _, run = stored
    page = advice.render(run, today=date_of(day_index(AS_OF) + 2))
    assert page.index("banner") < page.index("class=state")
