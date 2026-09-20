"""Recover the original lock selections from the snapshot archive.

Sleeper rewrites completed seasons, but it does not lose them: nineteen days of
daily sampling show that a corrupted starter value reverts to the stored one at
the next rewrite, and that the archive's majority value agrees with the earliest
snapshot in 97% of slots (implementation-plan.md §12, "The locks are intact").

That makes the archive self-repairing. With six to ten observations of a week,
the value a slot keeps returning to is recoverable by counting, and it is a
better canon than whatever today's read happens to say — which is what the
2026-09-20 decision in §12 adopts.

Two halves, both read-only unless `apply` is called:

``consensus`` / ``plan``
    What the archive says, and where the database disagrees with it. Pure
    functions over payloads; `plan` reads `weekly_matchups_latest` and nothing
    else.

``apply``
    Appends the recovered values to `weekly_matchups` as a fresh observation.
    It never updates or deletes: the corrupted rows stay in the table as
    history, and every reader goes through `weekly_matchups_latest`, so a repair
    takes effect without any reader knowing it happened. Provenance lands in
    `ingest_log` under source `repair`.

**Starters only.** A bench player's `players_points` can hold a stale value from
a game played while started, so the archive is not authoritative for it — the
same reason `snapshots.counted_values` is starters-only. Bench rows are left
exactly as ingested.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from lockin.store import snapshots
from lockin.store.db import log_ingest

# Below this many observations of a week, "majority" is a word for a coin toss.
# Three is the point at which a single excursion can be outvoted.
MIN_OBSERVATIONS = 3

Slot = tuple[int, str]  # (roster_id, sleeper_id)


@dataclass(frozen=True)
class Consensus:
    """The archive's verdict on one starter slot."""

    week: int
    roster_id: int
    sleeper_id: str
    value: float
    votes: int
    observations: int
    tied: bool

    @property
    def share(self) -> float:
        return self.votes / self.observations if self.observations else 0.0


@dataclass(frozen=True)
class Repair:
    """A slot where the database disagrees with the archive."""

    consensus: Consensus
    db_value: float

    @property
    def delta(self) -> float:
        return self.consensus.value - self.db_value


def consensus_for_week(payloads: list[Any], week: int) -> dict[Slot, Consensus]:
    """Majority value per starter slot, from payloads given oldest-first.

    Ties break to the value seen earliest. In 57 of the 58 tied slots in the
    2025 archive the earliest observation is one of the tied leaders, and where
    it is not, first-seen is still the better prior — the earliest snapshot
    agrees with the clear-majority value 97% of the time.
    """
    seen: dict[Slot, list[float]] = defaultdict(list)
    for payload in payloads:
        for slot, value in snapshots.counted_values(payload).items():
            seen[slot].append(value)

    out: dict[Slot, Consensus] = {}
    for (roster_id, sleeper_id), values in seen.items():
        counts = Counter(values)
        best = max(counts.values())
        leaders = {v for v, n in counts.items() if n == best}
        winner = next(v for v in values if v in leaders)
        out[(roster_id, sleeper_id)] = Consensus(
            week=week,
            roster_id=roster_id,
            sleeper_id=sleeper_id,
            value=winner,
            votes=best,
            observations=len(values),
            tied=len(leaders) > 1,
        )
    return out


def consensus(
    root: Path,
    season: str,
    weeks: list[int],
    *,
    min_observations: int = MIN_OBSERVATIONS,
) -> tuple[dict[int, dict[Slot, Consensus]], list[int]]:
    """Consensus per week, plus the weeks skipped for want of observations."""
    found: dict[int, dict[Slot, Consensus]] = {}
    skipped: list[int] = []
    for week in weeks:
        paths = snapshots.list_snapshots(root, snapshots.MATCHUPS, season, week)
        if len(paths) < min_observations:
            skipped.append(week)
            continue
        found[week] = consensus_for_week([snapshots.load_snapshot(p) for p in paths], week)
    return found, skipped


def _db_starters(conn: sqlite3.Connection, weeks: list[int]) -> dict[int, dict[Slot, sqlite3.Row]]:
    """Latest observation of every starter row, keyed by week and slot."""
    out: dict[int, dict[Slot, sqlite3.Row]] = defaultdict(dict)
    marks = ",".join("?" * len(weeks))
    for row in conn.execute(
        "SELECT week, roster_id, matchup_id, sleeper_id, counted_points, slot_index, slot"
        f"  FROM weekly_matchups_latest WHERE is_starter = 1 AND week IN ({marks})",
        weeks,
    ):
        out[row["week"]][(row["roster_id"], row["sleeper_id"])] = row
    return out


def plan(
    conn: sqlite3.Connection,
    root: Path,
    season: str,
    weeks: list[int],
    *,
    min_observations: int = MIN_OBSERVATIONS,
) -> tuple[list[Repair], list[int]]:
    """Every starter whose stored value differs from the archive's majority."""
    by_week, skipped = consensus(root, season, weeks, min_observations=min_observations)
    stored = _db_starters(conn, sorted(by_week))
    repairs = []
    for week, slots in sorted(by_week.items()):
        for slot, verdict in sorted(slots.items()):
            row = stored.get(week, {}).get(slot)
            if row is None or row["counted_points"] is None:
                continue
            if abs(row["counted_points"] - verdict.value) > 0.005:
                repairs.append(Repair(consensus=verdict, db_value=row["counted_points"]))
    return repairs, skipped


def apply(
    conn: sqlite3.Connection,
    root: Path,
    season: str,
    repairs: list[Repair],
    *,
    observed_at: str,
    min_observations: int = MIN_OBSERVATIONS,
) -> tuple[int, int]:
    """Append the recovered values. Returns (starter rows, team rows) written.

    Appends rather than updates, so the corrupted values stay queryable as
    history and a repair can be audited — or undone by deleting one
    `observed_at`. Team totals are recomputed from the full repaired starter
    set, not adjusted by the delta, so `points` stays the sum of its six slots.
    """
    touched = sorted({(r.consensus.week, r.consensus.roster_id) for r in repairs})
    weeks = sorted({week for week, _ in touched})
    if not weeks:
        return 0, 0

    by_week, _ = consensus(root, season, weeks, min_observations=min_observations)
    stored = _db_starters(conn, weeks)

    starter_rows = 0
    for repair in repairs:
        c = repair.consensus
        row = stored[c.week][(c.roster_id, c.sleeper_id)]
        conn.execute(
            "INSERT OR REPLACE INTO weekly_matchups"
            " (week, roster_id, matchup_id, sleeper_id, counted_points, is_starter,"
            "  slot_index, slot, observed_at)"
            " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                c.week,
                c.roster_id,
                row["matchup_id"],
                c.sleeper_id,
                c.value,
                row["slot_index"],
                row["slot"],
                observed_at,
            ),
        )
        starter_rows += 1

    team_rows = 0
    for week, roster_id in touched:
        slots = [s for s in by_week[week] if s[0] == roster_id]
        points = sum(by_week[week][s].value for s in slots)
        prior = conn.execute(
            "SELECT matchup_id, custom_points FROM weekly_matchup_teams"
            " WHERE week = ? AND roster_id = ? ORDER BY observed_at DESC LIMIT 1",
            (week, roster_id),
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO weekly_matchup_teams"
            " (week, roster_id, matchup_id, points, custom_points, observed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                week,
                roster_id,
                prior["matchup_id"] if prior else None,
                points,
                prior["custom_points"] if prior else None,
                observed_at,
            ),
        )
        team_rows += 1

    for week in weeks:
        n = sum(1 for r in repairs if r.consensus.week == week)
        log_ingest(conn, "repair", f"weekly_matchups:week={week}", n, observed_at)
    return starter_rows, team_rows


# --------------------------------------------------------------------------
# Archive statistics — the evidence that the majority value is the stored one.
# Everything here comes from `snapshots/` alone; none of it opens the database.
# --------------------------------------------------------------------------


@dataclass
class Stats:
    weeks: int
    slots: int
    observations: int
    earliest_agrees: int
    earliest_total: int
    tied_slots: int
    stay_on: int
    stay_on_total: int
    return_to: int
    return_to_total: int
    excursions: Counter
    currently_off: int
    matchups_wrong: int
    matchups_total: int
    total_errors: list[float]

    @property
    def lines(self) -> list[str]:
        def pct(n: int, d: int) -> str:
            return f"{100 * n / d:.1f}%" if d else "n/a"

        runs = sum(self.excursions.values())
        out = [
            f"archive          {self.weeks} weeks, {self.slots} starter slots,"
            f" {self.observations} observations",
            f"earliest agrees  {self.earliest_agrees}/{self.earliest_total}"
            f" = {pct(self.earliest_agrees, self.earliest_total)}"
            "   (oldest snapshot vs the majority value)",
            f"tied slots       {self.tied_slots}, broken to the earliest observation",
            "",
            "reversion — a rewrite that regenerated the lock would make these equal",
            f"  stays on the majority value      {pct(self.stay_on, self.stay_on_total)}"
            f"   (n={self.stay_on_total})",
            f"  returns to it when currently off {pct(self.return_to, self.return_to_total)}"
            f"   (n={self.return_to_total})",
            "",
            "excursion length, in rewrite events before returning",
        ]
        for length in sorted(self.excursions):
            out.append(
                f"  {length:>2} change{'s' if length > 1 else ' '}"
                f"  {self.excursions[length]:>5}   {pct(self.excursions[length], runs)}"
            )
        errs = sorted(self.total_errors)
        out += [
            "",
            f"wrong winners    {self.matchups_wrong}/{self.matchups_total}"
            f" = {pct(self.matchups_wrong, self.matchups_total)} of matchup-observations",
            f"team total error median {median(errs):.1f}, max {max(errs):.1f}"
            if errs
            else "team total error none",
            f"currently off    {self.currently_off} starters, in the latest snapshot of each week",
        ]
        return out


def stats(
    root: Path,
    season: str,
    weeks: list[int],
    *,
    min_observations: int = MIN_OBSERVATIONS,
) -> Stats:
    """Measure the archive against itself. See §12 for what each number argues."""
    by_week, _ = consensus(root, season, weeks, min_observations=min_observations)
    st = Stats(
        weeks=len(by_week),
        slots=0,
        observations=0,
        earliest_agrees=0,
        earliest_total=0,
        tied_slots=0,
        stay_on=0,
        stay_on_total=0,
        return_to=0,
        return_to_total=0,
        excursions=Counter(),
        currently_off=0,
        matchups_wrong=0,
        matchups_total=0,
        total_errors=[],
    )

    for week, slots in sorted(by_week.items()):
        paths = snapshots.list_snapshots(root, snapshots.MATCHUPS, season, week)
        payloads = [snapshots.load_snapshot(p) for p in paths]
        series = [snapshots.counted_values(p) for p in payloads]

        # A rewrite event is a property of the WEEK, not of one slot: upstream
        # re-derives a whole week at a time, and within one event a given slot
        # may move or hold. Collapsing per slot instead would make every
        # transition a change by construction and "held its value" unmeasurable.
        # Snapshots dedup on the whole payload, so consecutive files can agree
        # on every starter value; those are not rewrites of anything we track.
        events = [0] + [i for i in range(1, len(series)) if series[i] != series[i - 1]]

        for slot, verdict in slots.items():
            st.slots += 1
            seq = [obs[slot] for obs in series if slot in obs]
            st.observations += len(seq)
            if verdict.tied:
                st.tied_slots += 1
            else:
                st.earliest_total += 1
                if seq and abs(seq[0] - verdict.value) <= 0.005:
                    st.earliest_agrees += 1
            if seq and abs(seq[-1] - verdict.value) > 0.005:
                st.currently_off += 1

            # A tied slot has no majority to be on or off, so it cannot speak to
            # whether the value reverts. Counted in the archive, excluded here.
            if verdict.tied:
                continue
            steps = [series[i][slot] for i in events if slot in series[i]]
            for a, b in zip(steps, steps[1:], strict=False):
                back = abs(b - verdict.value) <= 0.005
                if abs(a - verdict.value) <= 0.005:
                    st.stay_on_total += 1
                    st.stay_on += back
                else:
                    st.return_to_total += 1
                    st.return_to += back
            run = 0
            for value in steps:
                if abs(value - verdict.value) > 0.005:
                    run += 1
                elif run:
                    st.excursions[run] += 1
                    run = 0
            if run:
                st.excursions[run] += 1

        # Head-to-head: does this observation report the winner the archive does?
        totals = defaultdict(float)
        for slot, verdict in slots.items():
            totals[slot[0]] += verdict.value
        for payload, obs in zip(payloads, series, strict=False):
            served: dict[int, float] = defaultdict(float)
            for slot, value in obs.items():
                served[slot[0]] += value
            for roster_id, value in served.items():
                if abs(value - totals[roster_id]) > 0.005:
                    st.total_errors.append(abs(value - totals[roster_id]))
            pairing = {
                team["roster_id"]: team.get("matchup_id")
                for team in payload or []
                if team.get("matchup_id") is not None
            }
            done = set()
            for roster_id, matchup_id in pairing.items():
                if matchup_id in done:
                    continue
                opponents = [r for r, m in pairing.items() if m == matchup_id and r != roster_id]
                if not opponents:
                    continue
                done.add(matchup_id)
                other = opponents[0]
                st.matchups_total += 1
                if (served[roster_id] > served[other]) != (totals[roster_id] > totals[other]):
                    st.matchups_wrong += 1
    return st
