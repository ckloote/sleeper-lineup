"""What the engine last advised, as a page — a reader of `recommendations`.

The digest is delivered by push notification, which is the right shape for
something with a deadline and the wrong shape for something you want to check
twice. Miss the notification and the advice is out of view; the numbers exist in
`recommendations` and `digest_runs` and nothing surfaced them.

**Reads, never recomputes.** This is not the same rule as the dashboard's, where
recomputing would merely be slow. Here it would be *wrong*:

- Recomputing gives a different answer. The reconstructed banked state is a
  chain of near-tied calls, and thresholds carry 1-3 points of Monte Carlo noise
  (§20). A page that recomputed would disagree with the notification you acted
  on, with no way to tell which was which.
- The inputs are rewritten upstream. §12 — Sleeper changed 38% of week-12
  starter values on a completed season. "What did it say on the day" stops being
  recoverable the moment the day passes.

**Staleness is the headline, not a footnote.** A recommendations page whose
whole failure mode is showing yesterday's calls as though they were today's must
say how old it is before it says anything else. If the run is not from today,
that is the first thing on the page and it is coloured.

This renders the last run for one roster. It is deliberately not a history
browser: the question is "what am I supposed to do", and offering a date picker
would invite reading a stale answer on purpose.
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from lockin.projections import date_of, day_index


@dataclass(frozen=True, slots=True)
class Item:
    sleeper_id: str
    name: str
    action: str
    for_day: int
    threshold: float | None
    ev_lock: float | None
    ev_pass: float | None
    rationale: str

    @property
    def is_call(self) -> bool:
        return self.action in ("LOCK", "PASS")

    @property
    def edge(self) -> float | None:
        if self.ev_lock is None or self.ev_pass is None:
            return None
        return abs(self.ev_lock - self.ev_pass)


@dataclass(frozen=True, slots=True)
class Run:
    generated_at: str
    roster_id: int
    as_of: str
    week: int
    opponent_roster_id: int | None
    p_win: float | None
    projected: float | None
    opponent_projected: float | None
    margin_p10: float | None
    margin_p50: float | None
    margin_p90: float | None
    banked_total: float | None
    banked_slots: int | None
    state_supplied: bool
    note: str | None
    items: tuple[Item, ...] = ()
    availability_days: int = 0
    """Distinct days of `player_status` on or before this morning."""
    recent_availability_days: int = 0
    """The same, within the last 30 days — the number that says whether the
    capture is still *running*, as opposed to having run once in October."""

    @property
    def calls(self) -> list[Item]:
        return [i for i in self.items if i.is_call]

    @property
    def rules(self) -> list[Item]:
        return [i for i in self.items if i.action == "THRESHOLD"]

    def age_days(self, today: str | None = None) -> int:
        """How many days since the morning this describes.

        Measured against `as_of` rather than `generated_at`: what matters is
        whether the advice is about today, not when the process happened to run.
        """
        now = today or datetime.now(UTC).date().isoformat()
        return day_index(now) - day_index(self.as_of)


def latest_run(conn: sqlite3.Connection, roster_id: int) -> Run | None:
    """The most recent digest for this roster, with its calls attached."""
    row = conn.execute(
        """
        SELECT * FROM digest_runs
         WHERE roster_id = ?
         ORDER BY generated_at DESC
         LIMIT 1
        """,
        (roster_id,),
    ).fetchone()
    if row is None:
        return None

    items = [
        Item(
            sleeper_id=r["sleeper_id"],
            name=r["full_name"] or r["sleeper_id"],
            action=r["action"],
            for_day=r["for_day"],
            threshold=r["threshold"],
            ev_lock=r["ev_lock"],
            ev_pass=r["ev_pass"],
            rationale=r["rationale"] or "",
        )
        for r in conn.execute(
            """
            SELECT c.*, p.full_name
              FROM recommendations c
              LEFT JOIN players p ON p.sleeper_id = c.sleeper_id
             WHERE c.generated_at = ?
               -- Older rows predate the column and are NULL; they belong to the
               -- only roster that existed when they were written.
               AND (c.roster_id = ? OR c.roster_id IS NULL)
             ORDER BY c.for_day, c.action, c.threshold DESC
            """,
            (row["generated_at"], roster_id),
        )
    ]
    return Run(
        generated_at=row["generated_at"],
        roster_id=row["roster_id"],
        as_of=row["as_of"],
        week=row["week"],
        opponent_roster_id=row["opponent_roster_id"],
        p_win=row["p_win"],
        projected=row["projected"],
        opponent_projected=row["opponent_projected"],
        margin_p10=row["margin_p10"],
        margin_p50=row["margin_p50"],
        margin_p90=row["margin_p90"],
        banked_total=row["banked_total"],
        banked_slots=row["banked_slots"],
        state_supplied=bool(row["state_supplied"]),
        note=row["note"],
        items=tuple(items),
        **availability_coverage(conn, row["as_of"]),
    )


def availability_coverage(conn: sqlite3.Connection, as_of: str) -> dict[str, int]:
    """How much injury-designation history exists as of this morning.

    Two numbers because they answer different questions. The total says whether
    there is enough history to attempt §19's start/sit gate; the recent count
    says whether the capture is still running at all. A capture that stopped in
    November still reports a healthy total forever, which is exactly the failure
    that made `ingest` drop its `--full` flag.
    """
    window = date_of(day_index(as_of) - 30)
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT as_of) total,
               COUNT(DISTINCT CASE WHEN as_of > ? THEN as_of END) recent
          FROM player_status
         WHERE as_of <= ?
        """,
        (window, as_of),
    ).fetchone()
    return {
        "availability_days": int(row["total"]),
        "recent_availability_days": int(row["recent"]),
    }


REVISIT_WEEK = 10
"""When to start prompting for the start/sit modelling work (§19).

Not a deadline, an earliest-useful point: ten weeks is roughly 100 roster-weeks
of lineup decisions across the league, which is the sample §19 argues is needed
to build a gate rather than to ship on faith.
"""

CAPTURE_HEALTHY = 20
"""Days of designation capture in the last 30 that count as "still running".

Not 30. Cron misses days — a reboot, a Sleeper outage, a laptop asleep — and a
prompt that cried failure over one missed morning would be ignored by the time
it mattered.
"""


def modelling_prompt(run: Run) -> tuple[str, str] | None:
    """(css class, message) reminding you the start/sit question is reopenable.

    Deliberately reports *readiness* rather than counting weeks. Week 10 with a
    stalled capture is not "time to build the model", it is "your irreplaceable
    data stopped arriving" — the more urgent message, and the one a bare
    week-number reminder would bury under an invitation to do modelling that
    cannot be gated.

    This will show every day from week 10 onward, which is intended and will
    eventually be irritating. Silencing it means doing the work or raising
    `REVISIT_WEEK`; there is no dismiss button, because a reminder you can wave
    away is one you will wave away.
    """
    if run.week < REVISIT_WEEK:
        return None
    if run.recent_availability_days < CAPTURE_HEALTHY:
        return (
            "alarm",
            f"Only {run.recent_availability_days} of the last 30 days have injury"
            f" designations. <strong>The capture has stopped</strong> &mdash; check the"
            f" ingest cron. It cannot be backfilled, and start/sit can never be"
            f" gated without it (&sect;19).",
        )
    return (
        "prompt",
        f"Week {run.week}, and {run.availability_days} days of availability data have"
        f" accumulated. That is enough to attempt the start/sit gate &mdash; value every"
        f" rostered player point-in-time, pick the best legal six, and check whether it"
        f" now beats the managers it lost to by 20.4 points a week (&sect;19). Nothing"
        f" reads <code>player_status</code> yet, so this is real work, and it moves the"
        f" lock thresholds too.",
    )


def _freshness(run: Run, today: str | None = None) -> tuple[str, str]:
    """(css class, sentence). The most important thing on the page."""
    age = run.age_days(today)
    if age <= 0:
        return "fresh", f"Advice for this morning, {run.as_of}."
    if age == 1:
        return "stale", "This is YESTERDAY's advice. Re-run `lockin digest`."
    return "stale", f"This is {age} days old ({run.as_of}). Re-run `lockin digest`."


def render(run: Run | None, *, today: str | None = None) -> str:
    if run is None:
        return (
            "<!doctype html><meta charset=utf-8><title>Lock-in — tonight</title>"
            "<p>No digest has been run. Try <code>lockin digest</code>.</p>"
        )

    tone, sentence = _freshness(run, today)
    parts: list[str] = []

    if run.note:
        parts.append(f"<p class=note>{html.escape(run.note)}</p>")

    calls = run.calls
    if calls:
        night = date_of(calls[0].for_day)
        rows = "".join(
            "<tr>"
            f'<td class=act><span class="tag {"lock" if i.action == "LOCK" else "pass"}">'
            f"{i.action}</span></td>"
            f"<td class=who>{html.escape(i.name)}</td>"
            f"<td class=num>{'' if i.threshold is None else f'{i.threshold:.0f}'}</td>"
            f"<td class=num>{'' if i.edge is None else f'{i.edge:.1%}'}</td>"
            "</tr>"
            for i in sorted(calls, key=lambda x: -(x.edge or 0))
        )
        # The heading carries the verdict, because it is the part that gets
        # scanned. A section of four PASS rows under "do these now" told the
        # reader to act when the correct action was to do nothing — passing *is*
        # inaction, and only a LOCK has a deadline.
        locks = [i for i in calls if i.action == "LOCK"]
        if locks:
            heading = f"Lock now &mdash; {night}"
            hint = (
                "Marked LOCK: bank before his next game tips, or the score is gone."
                " The rest are worth riding."
            )
        else:
            heading = f"Nothing to lock &mdash; {night}"
            hint = "Every one of these is worth riding. No action needed tonight."
        parts.append(
            f"<h2>{heading}</h2>"
            f"<p class=hint>{hint} &lsquo;Break-even&rsquo; is the score he would have"
            " needed for locking to be correct.</p>"
            "<table><thead><tr><th></th><th>Player</th>"
            "<th class=num>Break-even</th><th class=num>Worth</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    by_night: dict[int, list[Item]] = {}
    for item in run.rules:
        by_night.setdefault(item.for_day, []).append(item)
    for day in sorted(by_night):
        label = "Tonight" if date_of(day) == run.as_of else date_of(day)
        rows = "".join(
            "<tr>"
            f"<td class=who>{html.escape(i.name)}</td>"
            f"<td class=num><strong>{i.threshold:.0f}</strong></td>"
            "</tr>"
            for i in sorted(by_night[day], key=lambda x: -(x.threshold or 0))
        )
        parts.append(
            f"<h2>{html.escape(label)}</h2>"
            "<p class=hint>Lock him if he clears this.</p>"
            "<table><thead><tr><th>Player</th>"
            "<th class=num>Clears</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    state = ""
    if run.p_win is not None:
        margin = ""
        if run.margin_p50 is not None:
            margin = (
                f"<div class=margin>margin {run.margin_p10:+.0f}"
                f" / <strong>{run.margin_p50:+.0f}</strong>"
                f" / {run.margin_p90:+.0f}</div>"
            )
        banked = ""
        if run.banked_slots:
            banked = f"<div>banked {run.banked_total:.1f} across {run.banked_slots} of 6</div>"
        # Each line guarded on its own field rather than on `p_win` standing in
        # for all of them. A row carrying a win probability and nothing else is
        # not a shape `persist` produces, but this is rendered into an HTTP
        # response: a missing number must drop a line, not return a 500.
        projected = ""
        if run.projected is not None and run.opponent_projected is not None:
            projected = f"<div>projected {run.projected:.0f} v {run.opponent_projected:.0f}</div>"
        state = (
            "<div class=state>"
            f"<div class=pwin>{run.p_win:.0%}</div>"
            f"<div class=pwinlabel>chance to win, roster {run.roster_id}"
            f" v {run.opponent_roster_id}</div>"
            f"{projected}{margin}{banked}</div>"
        )

    prompt = ""
    if (found := modelling_prompt(run)) is not None:
        tone_class, message = found
        # Above the footer, below the advice. It is a standing invitation, not
        # something with a deadline, and putting it near the staleness banner
        # would make the two compete on a morning when only one of them expires.
        prompt = f'<p class="callout {tone_class}">{message}</p>'

    provenance = (
        "State was supplied on the command line."
        if run.state_supplied
        else "Banked state was inferred by replaying the week &mdash; the least stable"
        " number here (&sect;20). Pass <code>--locked</code> when you know it."
    )

    return f"""<!doctype html>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Lock-in — what to do</title>
<style>
  :root {{ color-scheme: light dark; --fg:#111; --bg:#fff; --mute:#666;
           --line:#e3e3e3; --lock:#0b6b3a; --lockbg:#e6f4ec; --pass:#5a5a5a;
           --passbg:#eee; --freshbg:#e6f4ec; --freshfg:#0b6b3a;
           --stalebg:#fdecea; --stalefg:#a8261c;
           --promptbg:#eef2fb; --promptfg:#28456e; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg:#e8e8e8; --bg:#16181c; --mute:#9aa0a6; --line:#2c2f36;
             --lock:#6fd39b; --lockbg:#12331f; --pass:#b0b4b8; --passbg:#24272c;
             --freshbg:#12331f; --freshfg:#6fd39b;
             --stalebg:#3a1b18; --stalefg:#ff9a90;
             --promptbg:#1b2432; --promptfg:#a8c4ec; }}
  }}
  body {{ margin:0 auto; padding:1rem; max-width:34rem; background:var(--bg);
          color:var(--fg); font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
          Roboto,sans-serif; }}
  h1 {{ font-size:1.2rem; margin:0 0 .6rem; }}
  h2 {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.05em;
        color:var(--mute); margin:1.6rem 0 .2rem; }}
  .banner {{ border-radius:8px; padding:.7rem .85rem; margin:0 0 1rem;
             font-weight:600; }}
  .fresh {{ background:var(--freshbg); color:var(--freshfg); }}
  .stale {{ background:var(--stalebg); color:var(--stalefg); }}
  .hint {{ color:var(--mute); font-size:.82rem; margin:.1rem 0 .5rem; }}
  table {{ border-collapse:collapse; width:100%; }}
  th {{ font-size:.7rem; text-transform:uppercase; letter-spacing:.04em;
        color:var(--mute); font-weight:600; text-align:left;
        padding:.2rem .4rem; }}
  td {{ padding:.5rem .4rem; border-top:1px solid var(--line); }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.act {{ width:4.4rem; }}
  .who {{ font-weight:600; }}
  .tag {{ display:inline-block; padding:.12rem .45rem; border-radius:4px;
          font-size:.72rem; font-weight:700; letter-spacing:.04em; }}
  .lock {{ background:var(--lockbg); color:var(--lock); }}
  .pass {{ background:var(--passbg); color:var(--pass); }}
  /* Above the advice, not below it: where the matchup stands is the context
     everything else is read against, and it was doing no work at the bottom. */
  .state {{ margin:0 0 .5rem; padding-bottom:.9rem;
            border-bottom:1px solid var(--line);
            color:var(--mute); font-size:.85rem; }}
  .pwin {{ font-size:2.4rem; font-weight:700; color:var(--fg);
           font-variant-numeric:tabular-nums; line-height:1.1; }}
  .pwinlabel {{ margin-bottom:.5rem; }}
  .margin {{ font-variant-numeric:tabular-nums; }}
  .note {{ color:var(--mute); }}
  .callout {{ margin:2rem 0 0; padding:.75rem .9rem; border-radius:8px;
              font-size:.85rem; line-height:1.45; }}
  .callout.prompt {{ background:var(--promptbg); color:var(--promptfg); }}
  .callout.alarm {{ background:var(--stalebg); color:var(--stalefg); }}
  footer {{ margin-top:1.6rem; color:var(--mute); font-size:.75rem; }}
  code {{ font-size:.85em; }}
</style>

<h1>What to do &mdash; week {run.week}</h1>
<p class="banner {tone}">{sentence}</p>
{state}
{"".join(parts)}
{prompt}
<footer>
Read from <code>recommendations</code>, not recomputed &mdash; this is what the engine
actually said at {html.escape(run.generated_at)}, which recomputing would not reproduce
(&sect;20) and which the upstream data no longer supports rebuilding (&sect;12).
<br>{provenance}
<br>No start/sit advice: the model&rsquo;s lineup picks are worse than yours (&sect;16).
</footer>
"""
