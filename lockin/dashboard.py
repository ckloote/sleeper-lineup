"""The manager-quality dashboard: a second reader of SQLite, nothing more.

§6's Phase 6 requirement. The league standings already say who won; what nobody
can see is who *decided* well, and the Phase 5 machinery answers it.

**Reads `manager_scorecards` and `roster_strength`. Never computes.** Producing
those rows costs several seconds of Monte Carlo — fine for a command, hopeless
for a page load — and the design rule is that SQLite is the contract. Run
`lockin managers` to refresh them; this renders whatever is there and prints how
old it is.

**Static HTML, no server.** A file the user opens, or drops next to the digest on
the Pi. Nothing to keep running, nothing to secure, and it works from a phone
over `file://`. The four rules §6 attaches to this page are each a way the data
could be made to lie, and each is enforced here rather than left to whoever
writes the markup:

1. **Sorted on `squandered_share`, never on `upside_share`.** §6 says
   `mean_regret`; §16 superseded that — raw regret is P(wrong) × E[stake] and the
   second factor is circumstance, so a manager blown out every week earns a low
   regret for free. `lockin managers` and the schema both already rank on the
   normalised share, and this follows them. The columns are not sortable at all:
   the page has one ordering and it is the defensible one.
2. **The band is rendered, on the ranked quantity.** `share_lo`/`share_hi`
   overlap across most of the table. Bars are drawn so that overlap is the first
   thing visible, because a bare 1-to-10 list asserts a precision that is not
   there.
3. **The §12 caveat is on the page, not in a footnote.** It sits above the table.
4. **The engine is not on the same axis.** Greedy is graded by the model that
   sets its own thresholds, so a column showing it beating every human would be
   an artefact of the shared model rather than a result.
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass

CAVEAT = (
    "Sleeper rewrites completed seasons and publishes no history (§12). This "
    "ranks how <strong>today's</strong> data makes each manager look, not a "
    "certified record of what they did."
)

LIMITS = [
    (
        "Model error is charged to the manager",
        "A call scored wrong may reflect injury news the projection layer cannot "
        "see — the blind spot worth 26 points per team total in §15.",
    ),
    (
        "Lock/pass only",
        "Who to <em>start</em> is a separate and probably larger decision, and it "
        "is not measurable yet: the model's own lineup picks are ~20 points a week "
        "worse than the managers' (§16), so there is nothing to grade them against.",
    ),
    (
        "The engine is not on this scale",
        "Greedy's thresholds come from the same projection model that computes the "
        "win probabilities grading it, so its errors are shared between deciding "
        "and being judged. Manager-versus-manager is fair because all ten are "
        "graded by a model none of them share.",
    ),
]


@dataclass(frozen=True, slots=True)
class Row:
    roster_id: int
    manager: str
    decisions: int
    squandered_share: float
    share_lo: float | None
    share_hi: float | None
    mean_stake: float
    mean_regret: float
    right_rate: float
    upside_share: float
    rode_to_zero: int
    ceiling: float | None
    lineup_gap: float | None
    availability: float | None


def load(conn: sqlite3.Connection, labels: dict[int, str] | None = None) -> list[Row]:
    """Every scorecard, best decision-making first.

    The ORDER BY is in the query rather than left to the caller so there is one
    place the ordering is decided, and it is the same place the comment
    explaining it lives.
    """
    labels = labels or {}
    return [
        Row(
            roster_id=r["roster_id"],
            # Empty, not "roster N". The roster id is already rendered beneath
            # the name; defaulting to it prints the same string twice.
            manager=labels.get(r["roster_id"], ""),
            decisions=r["decisions"],
            squandered_share=r["squandered_share"],
            share_lo=r["share_lo"],
            share_hi=r["share_hi"],
            mean_stake=r["mean_stake"],
            mean_regret=r["mean_regret"],
            right_rate=r["right_rate"],
            upside_share=r["upside_share"],
            rode_to_zero=r["rode_to_zero"],
            ceiling=r["ceiling"],
            lineup_gap=r["lineup_gap"],
            availability=r["availability"],
        )
        for r in conn.execute(
            """
            SELECT s.*, r.ceiling, r.lineup_gap, r.availability
              FROM manager_scorecards s
              LEFT JOIN roster_strength r ON r.roster_id = s.roster_id
             ORDER BY s.squandered_share ASC
            """
        )
    ]


def computed_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(computed_at) c FROM manager_scorecards").fetchone()
    return row["c"] if row else None


def _bar(row: Row, lo: float, hi: float) -> str:
    """One row's band, positioned on a shared scale.

    The point of the graphic is the *overlap*, so every bar shares one axis and
    the interval is drawn as the bar. A dot with whiskers would read as an
    estimate with decoration; a bar that visibly covers its neighbours reads as
    what it is — a tie.
    """
    span = (hi - lo) or 1.0
    band_lo = row.share_lo if row.share_lo is not None else row.squandered_share
    band_hi = row.share_hi if row.share_hi is not None else row.squandered_share
    left = 100 * (band_lo - lo) / span
    width = max(100 * (band_hi - band_lo) / span, 1.2)
    point = 100 * (row.squandered_share - lo) / span
    return (
        f'<div class="track">'
        f'<div class="band" style="left:{left:.1f}%;width:{width:.1f}%"></div>'
        f'<div class="point" style="left:{point:.1f}%"></div>'
        f"</div>"
    )


def render(rows: list[Row], *, stamp: str | None = None) -> str:
    """The whole page, self-contained."""
    if not rows:
        return (
            "<!doctype html><meta charset=utf-8><title>Lock-in</title>"
            "<p>No scorecards yet. Run <code>lockin managers</code>.</p>"
        )

    lows = [r.share_lo if r.share_lo is not None else r.squandered_share for r in rows]
    highs = [r.share_hi if r.share_hi is not None else r.squandered_share for r in rows]
    lo, hi = min(lows), max(highs)
    pad = (hi - lo) * 0.05 or 0.001
    lo, hi = lo - pad, hi + pad

    body = []
    for rank, row in enumerate(rows, 1):
        # Without `--names` there is no display name to show, so the roster id
        # is promoted to the line rather than repeated under itself.
        who = (
            f"{html.escape(row.manager)}<span class=rid>roster {row.roster_id}</span>"
            if row.manager
            else f"roster {row.roster_id}"
        )
        body.append(
            "<tr>"
            f"<td class=rank>{rank}</td>"
            f"<td class=who>{who}</td>"
            f"<td class=num><strong>{row.squandered_share:.1%}</strong></td>"
            f"<td class=bar>{_bar(row, lo, hi)}</td>"
            f"<td class=num>{1 - row.right_rate:.1%}</td>"
            f"<td class=num>{row.mean_stake:.2%}</td>"
            f"<td class=num>{row.decisions}</td>"
            f'<td class="num muted">{row.upside_share:.1%}</td>'
            f"<td class=num>{row.rode_to_zero}</td>"
            f"<td class=num>{'' if row.ceiling is None else f'{row.ceiling:.0f}'}</td>"
            f"<td class=num>{'' if row.lineup_gap is None else f'{row.lineup_gap:.0f}'}</td>"
            "</tr>"
        )

    limits = "".join(
        f"<li><strong>{html.escape(title)}.</strong> {text}</li>" for title, text in LIMITS
    )
    freshness = (
        f"<p class=stamp>Scorecards computed {html.escape(stamp)}. "
        f"Run <code>lockin managers</code> to refresh.</p>"
        if stamp
        else ""
    )

    return f"""<!doctype html>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Lock-in — who decided well</title>
<style>
  :root {{ color-scheme: light dark; --fg:#111; --bg:#fff; --mute:#666;
           --line:#e3e3e3; --band:#9db8d6; --point:#1d4e79; --warn:#8a5a00;
           --warnbg:#fff8e6; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg:#e8e8e8; --bg:#16181c; --mute:#9aa0a6; --line:#2c2f36;
             --band:#3f5c7d; --point:#8fc0ef; --warn:#e0b356; --warnbg:#2a2416; }}
  }}
  /* Wide enough for eleven columns; the prose below is pulled back in
     separately, because a 68rem line length is miserable to read. */
  body {{ margin:0 auto; padding:1.25rem; max-width:68rem; background:var(--bg);
          color:var(--fg); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
          Roboto,sans-serif; }}
  h1 {{ font-size:1.35rem; margin:0 0 .2rem; }}
  .sub {{ color:var(--mute); margin:0 0 1rem; }}
  .caveat {{ background:var(--warnbg); color:var(--warn); border-radius:6px;
             padding:.7rem .9rem; margin:0 0 1.2rem; font-size:.92rem; }}
  .scroll {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:.9rem; }}
  th, td {{ padding:.45rem .4rem; border-bottom:1px solid var(--line);
            text-align:left; white-space:nowrap; }}
  /* Headers wrap, cells do not. A header is prose and can take two lines; a
     number that wraps is unreadable. Sharing one nowrap rule made the widest
     *label* set the column width, which is what forced the whole table to
     scroll sideways. */
  th {{ font-weight:600; font-size:.75rem; text-transform:uppercase;
        letter-spacing:.03em; color:var(--mute); white-space:normal;
        vertical-align:bottom; }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.rank {{ color:var(--mute); }}
  .who {{ font-weight:600; }}
  .rid {{ display:block; font-weight:400; font-size:.75rem; color:var(--mute); }}
  td.bar, th.bar {{ width:30%; min-width:9rem; }}
  .track {{ position:relative; height:1.1rem; }}
  .band {{ position:absolute; top:.32rem; height:.46rem; border-radius:3px;
           background:var(--band); }}
  .point {{ position:absolute; top:.2rem; width:2px; height:.7rem;
            background:var(--point); }}
  .muted {{ color:var(--mute); }}
  .sub, .notes {{ max-width:44rem; }}
  .notes {{ margin-top:1.4rem; font-size:.88rem; color:var(--mute); }}
  .notes li {{ margin-bottom:.5rem; }}
  .stamp {{ font-size:.8rem; color:var(--mute); }}
  code {{ font-size:.85em; }}
</style>

<h1>Who decided well</h1>
<p class=sub>Lock/pass decision quality, 2025-26, with roster talent divided out.
Lower is better &mdash; but read the bars, not the ranks:
<strong>overlapping bars are a tie</strong>.</p>

<p class=caveat>{CAVEAT}</p>

<div class=scroll>
<table>
  <thead><tr>
    <th class=num>#</th><th>Manager</th>
    <th class=num>Squan&shy;dered</th>
    <th class=bar>90% band</th>
    <th class=num>Wrong</th><th class=num>Stake</th><th class=num>n</th>
    <th class=num>Pts cap</th><th class=num>Zeros</th>
    <th class=num>Ceiling</th><th class=num>Lineup cost</th>
  </tr></thead>
  <tbody>{"".join(body)}</tbody>
</table>
</div>

<div class=notes>
<p><strong>Squandered</strong> is regret as a share of the win probability that
was at stake, which divides out circumstance: a hopeless matchup carries a mean
stake of 3.0% against 10.4% in a live one, so being blown out repeatedly earns a
low raw regret for free. <strong>Stake</strong> is that circumstance, shown so it
can be judged.</p>

<p><strong>Pts cap</strong> is the older points-capture metric, shown for
contrast only. The table is deliberately <em>not</em> sortable by it: it scores a
correct variance-taking decision as a blunder, and a page that let you order by
it would have published a wrong ranking.</p>

<p><strong>Ceiling</strong> and <strong>lineup cost</strong> describe the team,
not the manager's lock/pass calls — the best legal six from the whole roster with
every lock perfect, and what the lineups actually cost against it. Lineup cost is
a price, not a verdict: judging it needs an injury feed nobody has.</p>

<p>Three limits, which apply to every number above:</p>
<ol>{limits}</ol>
{freshness}
</div>
"""
