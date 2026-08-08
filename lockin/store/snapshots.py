"""Raw payload snapshots.

Sleeper rewrites completed seasons. Between 2026-08-05 and 2026-08-07 it changed
38% of week-12 starter values while leaving the box scores byte-identical — only
the selection of which game counts moved (implementation-plan.md §12). There is
no historical endpoint, so an unobserved change is unrecoverable.

`weekly_matchups` is append-only for this reason, but that was not enough: the
database gets rebuilt, and rebuilding destroyed 24 of 25 weeks of original
values. Snapshots therefore live **outside the database**, as files, under
version control — so `rm data/lockin.db` cannot touch them.

Deduplicated by content: a snapshot is written only when the payload differs
from the most recent one for that week. A stable season costs 25 small files;
the tree grows only when upstream actually changes, which makes the directory
listing itself the mutation history.

Only matchups are snapshotted. Box scores were byte-identical across the
observed mutation and are ~2MB per week, so they are refetchable rather than
irreplaceable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("snapshots")
MATCHUPS = "matchups"


def _canonical(payload: Any) -> str:
    """Stable serialisation, so equality means "same content", not "same bytes"."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def week_dir(root: Path, kind: str, season: str, week: int) -> Path:
    return root / kind / season / f"wk{week:02d}"


def list_snapshots(root: Path, kind: str, season: str, week: int) -> list[Path]:
    """All snapshots for a week, oldest first."""
    d = week_dir(root, kind, season, week)
    return sorted(d.glob("*.json")) if d.is_dir() else []


def load_snapshot(path: Path) -> Any:
    return json.loads(path.read_text())


def earliest(root: Path, kind: str, season: str, week: int) -> Any | None:
    """The first thing we ever saw — the closest available to ground truth."""
    paths = list_snapshots(root, kind, season, week)
    return load_snapshot(paths[0]) if paths else None


def latest(root: Path, kind: str, season: str, week: int) -> Any | None:
    paths = list_snapshots(root, kind, season, week)
    return load_snapshot(paths[-1]) if paths else None


def save(
    root: Path,
    kind: str,
    season: str,
    week: int,
    payload: Any,
    *,
    stamp: str,
) -> Path | None:
    """Write a snapshot unless it is identical to the most recent one.

    `stamp` must sort lexicographically in time order — use
    ``YYYYmmddTHHMMSSZ``. Returns the path written, or None if unchanged.
    """
    current = latest(root, kind, season, week)
    if current is not None and _canonical(current) == _canonical(payload):
        return None

    d = week_dir(root, kind, season, week)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stamp}.json"
    path.write_text(json.dumps(payload, sort_keys=True, indent=None))
    return path


def counted_values(payload: Any) -> dict[tuple[int, str], float]:
    """Flatten a matchups payload to {(roster_id, sleeper_id): counted_points}.

    Starters only — `points` sums the six starter slots, and a bench player's
    `players_points` can hold a stale value from a game played while started.
    """
    out: dict[tuple[int, str], float] = {}
    for team in payload or []:
        starters = team.get("starters") or []
        points = team.get("starters_points") or []
        for sleeper_id, value in zip(starters, points, strict=False):
            if sleeper_id and sleeper_id != "0":
                out[(team["roster_id"], sleeper_id)] = value
    return out


def diff_counted(before: Any, after: Any) -> list[tuple[int, str, float, float]]:
    """Starter values that changed between two snapshots of the same week."""
    a, b = counted_values(before), counted_values(after)
    changed = []
    for key in sorted(a.keys() & b.keys()):
        if abs(a[key] - b[key]) > 0.005:
            changed.append((key[0], key[1], a[key], b[key]))
    return changed
