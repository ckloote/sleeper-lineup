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

from lockin import clock
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
    expires_utc: str | None = None
    """When the call stops meaning anything: his next tip."""

    def expired(self, now: datetime) -> bool:
        return self.expires_utc is not None and _utc(self.expires_utc) <= now

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
    last_ingest_at: str | None
    note: str | None
    items: tuple[Item, ...] = ()
    run_id: str | None = None
    state_source: str | None = None
    poll_observed_at: str | None = None
    abstained: bool = False
    banked: tuple[tuple[str, float], ...] = ()
    """(name, score) for each banked player."""
    warnings: tuple[tuple[str, str, str], ...] = ()
    """(name, kind, detail) — what the notification carried."""
    availability_days: int = 0
    """Distinct days with an availability capture, on or before this morning."""
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

        "Today" comes from :mod:`lockin.clock`, in the schedule's timezone. It
        used to come from `datetime.now(UTC)`, which on an Eastern machine rolls
        over at 7pm — so a digest generated that morning was labelled a day old
        the moment you opened the page after dinner, in red, telling you to
        re-run something that had already run.
        """
        return day_index(today or clock.today_iso()) - day_index(self.as_of)


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple) -> list[sqlite3.Row]:
    """A query against a table a database may be too old to have: none is empty.

    `lockin serve` holds a read-only connection and never applies the schema,
    so a page must degrade on an old file rather than return a 500.
    """
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def latest_run(conn: sqlite3.Connection, roster_id: int) -> Run | None:
    """The most recent digest for this roster, with everything it recorded.

    Rows are gathered by `run_id`, one run's and no other's. Before runs had
    ids they were matched on `generated_at`, which two runs inside one second
    shared — so a page could show the header of one and the calls of both.
    Rows from then still attach the old way.
    """
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
    keys = set(row.keys())

    def get(name: str):
        return row[name] if name in keys else None

    run_id = get("run_id")
    if run_id is not None:
        where, args = "c.run_id = ?", (run_id,)
    else:
        # Older rows predate the columns; roster_id NULL belongs to the only
        # roster that existed when they were written.
        where, args = (
            "c.generated_at = ? AND (c.roster_id = ? OR c.roster_id IS NULL)",
            (
                row["generated_at"],
                roster_id,
            ),
        )
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
            expires_utc=r["expires_utc"] if "expires_utc" in r.keys() else None,
        )
        for r in conn.execute(
            f"""
            SELECT c.*, p.full_name
              FROM recommendations c
              LEFT JOIN players p ON p.sleeper_id = c.sleeper_id
             WHERE {where}
             ORDER BY c.for_day, c.action, c.threshold DESC
            """,
            args,
        )
    ]
    banked = tuple(
        (r["full_name"] or r["sleeper_id"], r["score"])
        for r in _rows(
            conn,
            "SELECT b.sleeper_id, b.score, p.full_name FROM digest_banked b"
            " LEFT JOIN players p ON p.sleeper_id = b.sleeper_id"
            " WHERE b.run_id = ? ORDER BY b.score DESC",
            (run_id,),
        )
    )
    warnings = tuple(
        (r["full_name"] or r["sleeper_id"], r["kind"], r["detail"])
        for r in _rows(
            conn,
            "SELECT w.sleeper_id, w.kind, w.detail, p.full_name FROM digest_warnings w"
            " LEFT JOIN players p ON p.sleeper_id = w.sleeper_id"
            " WHERE w.run_id = ? ORDER BY p.full_name",
            (run_id,),
        )
    )
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
        last_ingest_at=row["last_ingest_at"],
        note=row["note"],
        items=tuple(items),
        run_id=run_id,
        state_source=get("state_source"),
        poll_observed_at=get("poll_observed_at"),
        abstained=bool(get("abstained")),
        banked=banked,
        warnings=warnings,
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
    # Days with a capture, whether or not anyone was flagged: a healthy day is a
    # captured day. Legacy `player_status` dates count too, so history carries.
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT day) total,
               COUNT(DISTINCT CASE WHEN day > ? THEN day END) recent
          FROM (SELECT substr(observed_at, 1, 10) AS day FROM status_captures
                UNION SELECT as_of FROM player_status)
         WHERE day <= ?
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


INGEST_STALE_HOURS = 30
"""How old the ingest may be before the digest is reading yesterday's games.

A daily cron gives roughly 24 hours between runs; 30 allows a late or slow one
without crying wolf, while still catching a run that simply did not happen.
"""


def ingest_warning(run: Run) -> str | None:
    """Whether the digest was built on data the ingest failed to refresh.

    The failure this catches is quiet by construction: if the 6:30 ingest dies,
    the 9:00 digest still runs, still finds box scores, and still produces
    confident calls — on last night's data minus last night. Nothing else on this
    page would look wrong.
    """
    if run.last_ingest_at is None:
        return "No ingest has been recorded, so these calls may rest on stale box scores."
    try:
        ingested = datetime.fromisoformat(run.last_ingest_at)
        generated = datetime.fromisoformat(run.generated_at)
    except ValueError:
        return None
    hours = (generated - ingested).total_seconds() / 3600
    if hours < INGEST_STALE_HOURS:
        return None
    return (
        f"The last successful ingest was {hours:.0f} hours before this digest."
        " Last night's games may not be in the data &mdash; check the ingest cron"
        " before acting on anything here."
    )


def _freshness(run: Run, today: str | None = None) -> tuple[str, str]:
    """(css class, sentence). The most important thing on the page."""
    age = run.age_days(today)
    if age <= 0:
        return "fresh", f"Advice for this morning, {run.as_of}."
    if age == 1:
        return "stale", "This is YESTERDAY's advice. Re-run `lockin digest`."
    return "stale", f"This is {age} days old ({run.as_of}). Re-run `lockin digest`."


def _deadline(item: Item, now: datetime) -> str:
    if item.expires_utc is None:
        return ""
    local = _utc(item.expires_utc).astimezone(clock.zone())
    when = f"{local.strftime('%a')} {local.strftime('%I:%M%p').lstrip('0').lower()}"
    if item.expired(now):
        return f"<div class=deadline>closed at {when} tip</div>"
    return f"<div class=deadline>by {when} tip</div>"


def render(run: Run | None, *, today: str | None = None, now: datetime | None = None) -> str:
    if run is None:
        return (
            "<!doctype html><meta charset=utf-8><title>Lock-in — tonight</title>"
            "<p>No digest has been run. Try <code>lockin digest</code>.</p>"
        )

    tone, sentence = _freshness(run, today)
    now = now or datetime.now(UTC)
    parts: list[str] = []

    # Directly under the staleness banner, because it is the same question asked
    # of the other input: that one says the advice is old, this says the data
    # under it is. Either makes the numbers below untrustworthy.
    stale_data = ingest_warning(run)
    if stale_data is not None:
        parts.append(f'<p class="banner stale" data-warning="ingest">{stale_data}</p>')

    if run.note:
        parts.append(f"<p class=note>{html.escape(run.note)}</p>")

    calls = run.calls
    if calls:
        rows = "".join(
            f"<tr{' class=expired' if i.expired(now) else ''}>"
            f'<td class=act><span class="tag {"lock" if i.action == "LOCK" else "pass"}">'
            f"{i.action}</span></td>"
            f"<td class=who>{html.escape(i.name)}"
            f"<div class=game>{date_of(i.for_day)} game</div>{_deadline(i, now)}</td>"
            f"<td class=num>{'ride' if i.threshold is None else f'{i.threshold:.0f}'}</td>"
            f"<td class=num>{'' if i.edge is None else f'{i.edge:.1%}'}</td>"
            "</tr>"
            for i in sorted(calls, key=lambda x: (x.expired(now), -(x.edge or 0)))
        )
        # The heading carries the verdict, because it is the part that gets
        # scanned. A section of four PASS rows under "do these now" told the
        # reader to act when the correct action was to do nothing — passing *is*
        # inaction, and only a LOCK has a deadline. A LOCK whose deadline has
        # passed is no longer one to act on, so it does not count either.
        locks = [i for i in calls if i.action == "LOCK" and not i.expired(now)]
        if locks:
            heading = "Lock now &mdash; before each player's next tip"
            hint = (
                "Marked LOCK: bank before his next game tips, or the score is gone."
                " The rest are worth riding."
            )
        elif any(i.action == "LOCK" for i in calls):
            heading = "Nothing to lock now &mdash; the lock windows have closed"
            hint = "The LOCK calls below expired at their tips. Re-run the digest."
        else:
            heading = "Nothing to lock &mdash; ride them all"
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

    if run.warnings:
        items_html = "".join(
            f"<li><strong>{html.escape(name)}</strong> &mdash; {html.escape(kind)}:"
            f" {html.escape(detail)}</li>"
            for name, kind, detail in run.warnings
        )
        parts.append(f"<h2>Watch</h2><ul class=watch>{items_html}</ul>")

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
            who = ", ".join(f"{html.escape(n)} {x:.1f}" for n, x in run.banked)
            banked = (
                f"<div>banked {run.banked_total:.1f} across {run.banked_slots} of 6"
                + (f" &mdash; {who}" if who else "")
                + "</div>"
            )
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

    if run.state_supplied or run.state_source == "supplied":
        provenance = "State was supplied on the command line."
    elif run.state_source == "inferred":
        provenance = (
            f"Banked state was read from the matchup poll at"
            f" {html.escape(run.poll_observed_at or '?')}. Last night's games are the calls"
            " above, whatever you did with them; a lock you made on one just makes its"
            " row moot."
        )
    else:
        provenance = (
            "Banked state was assumed by replaying the week under this engine's policy"
            " &mdash; the least stable number here (&sect;20). Pass <code>--locked</code>"
            " when you know it."
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
  .game, .deadline {{ font-weight:400; font-size:.75rem; color:var(--mute); }}
  tr.expired td {{ opacity:.45; }}
  .watch {{ padding-left:1.1rem; margin:.3rem 0; font-size:.9rem; }}
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
<p class="banner {tone}" data-warning="age">{sentence}</p>
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
