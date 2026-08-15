"""The manager-quality page, and the four ways §6 says it could lie.

Each rule in the Phase 6 dashboard requirement describes a specific wrong page,
so each is tested as "the wrong page is not what we produce" rather than as a
property of the markup. A page that merely *happens* to be sorted correctly
today is one refactor away from being sorted by whatever is convenient.

Also here: a smoke test on `evaluate_managers`. It was the one entry point in
the project with no test touching it, which is how a `SimulationCache`
signature change reached a released command instead of a failing test.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace

import pytest

from lockin import dashboard, managers
from lockin.config import Config
from lockin.store.db import apply_schema, session

cfg = Config.from_env()
pytestmark = pytest.mark.skipif(
    not cfg.db_path.exists(), reason=f"no database at {cfg.db_path}; run `lockin ingest`"
)


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    apply_schema(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def rows(conn):
    loaded = dashboard.load(conn)
    if not loaded:
        pytest.skip("no scorecards stored; run `lockin managers`")
    return loaded


@pytest.fixture(scope="module")
def page(rows):
    return dashboard.render(rows, stamp="2026-08-15T00:00:00+00:00")


# --------------------------------------------------------------- rule 1: order


def test_the_page_is_ordered_on_the_normalised_share(rows):
    """§16's correction, not §6's original `mean_regret`.

    Raw regret is P(wrong) x E[stake], and the second factor is circumstance: a
    manager blown out every week collects a low regret for free. §6 predates
    that finding; the schema and `lockin managers` both already rank on the
    share, and the page follows them rather than the stale instruction.
    """
    shares = [r.squandered_share for r in rows]
    assert shares == sorted(shares), "the page must be ordered best-first on the share"


def test_the_page_offers_no_way_to_sort_by_points_capture(page):
    """Points capture scores correct variance-taking as a blunder.

    It is carried for contrast, so the enforcement is that nothing on the page
    can reorder by it — no script, no sortable headers, no links.
    """
    assert "Pts cap" in page, "the contrast column should still be shown"
    # The mechanisms, not the word — the prose on the page says "not sortable".
    assert "<script" not in page.lower()
    assert "onclick" not in page.lower()
    assert "data-sort" not in page.lower()
    assert "<a " not in page.lower(), "a header link is a sort control"


def test_the_ranking_does_not_agree_with_points_capture(rows):
    """If it did, the distinction the page rests on would be untestable here.

    Not a property of the code — a fact about the season that makes rule 1 load
    bearing. Were the two orderings identical, sorting by the wrong column would
    be harmless and the rule would be decoration.
    """
    by_share = [r.roster_id for r in rows]
    by_upside = [r.roster_id for r in sorted(rows, key=lambda r: -r.upside_share)]
    assert by_share != by_upside


# ---------------------------------------------------------------- rule 2: bands


def test_every_row_renders_a_band(page, rows):
    assert page.count('class="band"') + page.count("class=band") >= len(rows)


def test_the_bands_are_drawn_on_the_column_the_table_is_sorted_by(rows):
    """A band on `mean_regret` beside a `squandered_share` ordering would be
    expressing uncertainty about a different number from the one setting the
    order — which looks rigorous and is not."""
    for row in rows:
        assert row.share_lo is not None, "run `lockin managers` to populate share_lo/hi"
        assert row.share_lo <= row.squandered_share <= row.share_hi


def test_the_page_says_overlapping_bars_are_a_tie(page):
    """Bars alone do not tell you what to conclude from them.

    The instruction started life in the column header, which is what made the
    table scroll sideways; it moved to the subtitle rather than being dropped,
    and this pins that it is still somewhere a reader will meet it *before* the
    table.
    """
    assert "overlapping bars are a tie" in page
    assert page.index("overlapping bars are a tie") < page.index("<table")


def test_the_bands_actually_overlap(rows):
    """§6: "the bands overlap from about rank 2 to rank 8".

    The reason the rule exists. If the intervals were disjoint a bare ordered
    list would be honest, and drawing bars would be pointless.
    """
    overlaps = sum(
        1
        for a, b in zip(rows, rows[1:], strict=False)
        if a.share_hi >= b.share_lo  # adjacent rows are not distinguishable
    )
    assert overlaps >= len(rows) // 2, "expected most adjacent pairs to be ties"


def test_bar_geometry_stays_inside_the_track(rows):
    """A band clipped at the edge would silently understate the uncertainty."""
    lows = [r.share_lo for r in rows]
    highs = [r.share_hi for r in rows]
    lo, hi = min(lows), max(highs)
    pad = (hi - lo) * 0.05 or 0.001
    lo, hi = lo - pad, hi + pad
    for row in rows:
        markup = dashboard._bar(row, lo, hi)
        left, width = (float(x) for x in re.findall(r"(\d+\.\d)%", markup)[:2])
        assert 0 <= left <= 100
        assert left + width <= 100.5


# --------------------------------------------------------------- rule 3: caveat


def test_the_mutation_caveat_is_on_the_page_not_in_a_footnote(page):
    """§12. It has to be read before the table, not after it."""
    caveat = page.index("§12")
    table = page.index("<table")
    assert caveat < table, "the caveat must precede the table it qualifies"


def test_the_three_limits_are_carried(page):
    for title, _ in dashboard.LIMITS:
        assert title in page


# --------------------------------------------------------------- rule 4: engine


def test_the_engine_is_not_ranked_beside_the_humans(conn, page):
    """Greedy is graded by the model that sets its own thresholds, so a column
    showing it beating every human would be an artefact of the shared model."""
    roster_ids = {r["roster_id"] for r in conn.execute("SELECT roster_id FROM manager_scorecards")}
    assert all(isinstance(rid, int) and rid > 0 for rid in roster_ids)
    for word in ("greedy", "rollout", "engine  ", "Engine</"):
        assert word not in page, f"{word!r} must not appear as a ranked row"


# ------------------------------------------------------------------- rendering


def test_the_page_is_self_contained(page):
    """It is opened over file:// from a phone. No network, no build step."""
    assert "http://" not in page
    assert "https://" not in page
    assert "<link" not in page.lower()


def test_an_empty_database_renders_an_instruction_not_a_crash(tmp_path):
    with session(tmp_path / "empty.db") as fresh:
        assert dashboard.load(fresh) == []
        page = dashboard.render([])
    assert "lockin managers" in page


def test_manager_names_are_escaped(rows):
    """Display names come from the Sleeper API — untrusted text in a web page."""
    hostile = replace(rows[0], manager="<img src=x onerror=1>")
    page = dashboard.render([hostile])
    assert "<img src=x" not in page
    assert "&lt;img" in page


# ------------------------------------------------------------------ regression


def test_evaluate_managers_runs_end_to_end(conn):
    """The gap that let a signature change ship.

    Cheap on purpose — 20 simulations is far too few for the numbers to mean
    anything, and that is fine: this asserts the call graph holds together, not
    that the estimates are good. The estimates are gated by `lockin backtest`.
    """
    report = managers.evaluate_managers(conn, cfg.season, n_sims=20)
    assert report.decisions
    assert len(report.scorecards) == 10
    ranked = report.ranked()
    assert [s.squandered_share for s in ranked] == sorted(s.squandered_share for s in ranked)


def test_the_share_band_brackets_the_point_estimate(conn):
    """The bootstrap resamples the ratio of sums, matching how it is computed."""
    report = managers.evaluate_managers(conn, cfg.season, n_sims=20)
    for card in report.scorecards:
        lo, hi = managers.bootstrap_squandered(report, card.roster_id, resamples=200)
        assert lo <= card.squandered_share <= hi
