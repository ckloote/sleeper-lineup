"""The scoring function.

Pure: no network, no database, no clock. Everything is driven by the league's
``scoring_settings``, never by hardcoded weights — this format is unusual enough
(total rebounds pay 0.0, a double-double pays 10.0) that any valuation inherited
from published fantasy rankings would be wrong.

Two entry points, and the distinction between them is the whole point of this
module:

``score_recorded``
    Scores a Sleeper stat line as recorded, where ``dd``/``td`` and the
    missed-shot counts arrive precomputed. Used to reconstruct history.

``score_line``
    Scores a component stat line, DERIVING the double-double and triple-double
    flags and the missed-shot counts. This is the path the simulator uses, since
    a simulated game has components and nothing else.

They must agree on every real game. If they ever disagree, simulated scores are
wrong in a way that no amount of downstream testing would reveal — and the
disagreement would live exactly at the 10/10 boundary, which is where the
distribution's shape matters most.

``score_matrix`` is ``score_line`` over a whole array of component lines at once.
The projection layer scores millions of simulated lines per calibration run, so
the per-line Python path is too slow to use directly — but it stays the
definition of correctness, and a test asserts the two agree line by line.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields

import numpy as np

# Categories that count toward double-double / triple-double detection.
# REB is TOTAL rebounds, even though total rebounds score 0.0 in this league.
DOUBLE_CATEGORIES = ("pts", "reb", "ast", "stl", "blk")
DOUBLE_THRESHOLD = 10


class UnscorableStat(ValueError):
    """The league weights a stat the component model cannot produce."""


@dataclass(frozen=True, slots=True)
class StatLine:
    """A single player-game, in components only.

    Missed shots and total rebounds are derived rather than stored, so a caller
    cannot supply an internally inconsistent line.
    """

    pts: float = 0.0
    ast: float = 0.0
    oreb: float = 0.0
    dreb: float = 0.0
    stl: float = 0.0
    blk: float = 0.0
    tov: float = 0.0
    fgm: float = 0.0
    fga: float = 0.0
    ftm: float = 0.0
    fta: float = 0.0
    tpm: float = 0.0
    tpa: float = 0.0
    tech: float = 0.0
    flagrant: float = 0.0

    @property
    def reb(self) -> float:
        return self.oreb + self.dreb

    @property
    def fgmi(self) -> float:
        return self.fga - self.fgm

    @property
    def ftmi(self) -> float:
        return self.fta - self.ftm

    @property
    def tpmi(self) -> float:
        return self.tpa - self.tpm


def count_doubles(line: StatLine) -> int:
    """How many of PTS/REB/AST/STL/BLK reached double figures."""
    values = {
        "pts": line.pts,
        "reb": line.reb,
        "ast": line.ast,
        "stl": line.stl,
        "blk": line.blk,
    }
    return sum(1 for cat in DOUBLE_CATEGORIES if values[cat] >= DOUBLE_THRESHOLD)


def derive_bonuses(line: StatLine, *, td_stacks_dd: bool = True) -> tuple[int, int]:
    """Derive the (dd, td) flags from components.

    ``td_stacks_dd`` defaults to True, which is the verified behaviour: a
    triple-double carries BOTH flags and pays dd + td = 30, not 20. Confirmed
    against recorded ``players_points`` on games where the stacked total matches
    to the cent and the superseded total matches nothing. It remains a flag
    because it is a league setting we inferred rather than one Sleeper states.
    """
    n = count_doubles(line)
    td = 1 if n >= 3 else 0
    if td and not td_stacks_dd:
        return 0, td
    return (1 if n >= 2 else 0), td


# Sleeper stat key -> attribute on StatLine. Keys absent here (bonus_pt_40p,
# pf, plus_minus, and the quarter splits) are either unscored in this league or
# not stats at all; they are ignored rather than silently contributing.
_COMPONENT_KEYS: dict[str, str] = {
    "pts": "pts",
    "ast": "ast",
    "oreb": "oreb",
    "dreb": "dreb",
    "reb": "reb",
    "stl": "stl",
    "blk": "blk",
    "to": "tov",
    "fgm": "fgm",
    "fga": "fga",
    "fgmi": "fgmi",
    "ftm": "ftm",
    "fta": "fta",
    "ftmi": "ftmi",
    "tpm": "tpm",
    "tpa": "tpa",
    "tpmi": "tpmi",
    "tf": "tech",
    "ff": "flagrant",
}


def score_line(line: StatLine, scoring: Mapping[str, float], *, td_stacks_dd: bool = True) -> float:
    """Score a component stat line, deriving bonuses and missed shots.

    This is the simulator's path: a simulated game has components and nothing
    else, so dd/td must be computed at the 10/10 boundary rather than supplied.
    """
    dd, td = derive_bonuses(line, td_stacks_dd=td_stacks_dd)
    total = 0.0
    for key, weight in scoring.items():
        if not weight:
            continue
        if key == "dd":
            total += weight * dd
        elif key == "td":
            total += weight * td
        else:
            attr = _COMPONENT_KEYS.get(key)
            if attr is None:
                # A weighted stat the simulator cannot represent. Skipping it
                # would quietly under-score every simulated game, so refuse.
                raise UnscorableStat(
                    f"scoring_settings weights {key!r} at {weight}, but StatLine has no"
                    f" component for it; add it to _COMPONENT_KEYS and StatLine"
                )
            total += weight * getattr(line, attr)
    return round(total, 2)


def score_recorded(stats: Mapping[str, float], scoring: Mapping[str, float]) -> float:
    """Score a Sleeper stat line exactly as recorded.

    Every weighted key in ``scoring_settings`` is read straight from the payload,
    including ``dd``/``td`` and the missed-shot counts. Sleeper omits zero-valued
    keys, so an absent key is a zero.

    Used to reconstruct history. Deliberately does NOT derive anything, so that
    comparing it against :func:`score_line` is a real test of the derivation
    rather than a tautology.
    """
    return round(
        sum(weight * (stats.get(key) or 0.0) for key, weight in scoring.items() if weight),
        2,
    )


def line_from_stats(stats: Mapping[str, float]) -> StatLine:
    """Build a component StatLine from a Sleeper stat payload.

    Reads only components. ``dd``, ``td``, ``fgmi``, ``ftmi``, ``tpmi`` and
    ``reb`` in the payload are ignored on purpose — they are exactly what
    :func:`score_line` must reconstruct on its own.
    """

    def g(key: str) -> float:
        return float(stats.get(key) or 0.0)

    return StatLine(
        pts=g("pts"),
        ast=g("ast"),
        oreb=g("oreb"),
        dreb=g("dreb"),
        stl=g("stl"),
        blk=g("blk"),
        tov=g("to"),
        fgm=g("fgm"),
        fga=g("fga"),
        ftm=g("ftm"),
        fta=g("fta"),
        tpm=g("tpm"),
        tpa=g("tpa"),
        tech=g("tf"),
        flagrant=g("ff"),
    )


# --- vectorised path --------------------------------------------------------
#
# Column order is taken from StatLine's own fields rather than written out, so a
# component added to the dataclass cannot silently desynchronise the two paths.

COMPONENT_ORDER: tuple[str, ...] = tuple(f.name for f in fields(StatLine))
COMPONENT_INDEX: dict[str, int] = {name: i for i, name in enumerate(COMPONENT_ORDER)}

# Attributes StatLine exposes as properties rather than storing, expressed as
# the columns they are derived from.
_DERIVED: dict[str, tuple[str, str]] = {
    "reb": ("oreb", "dreb"),
    "fgmi": ("fga", "fgm"),
    "ftmi": ("fta", "ftm"),
    "tpmi": ("tpa", "tpm"),
}


def _column(comps: np.ndarray, attr: str) -> np.ndarray:
    idx = COMPONENT_INDEX.get(attr)
    if idx is not None:
        return comps[:, idx]
    a, b = _DERIVED[attr]
    lhs, rhs = comps[:, COMPONENT_INDEX[a]], comps[:, COMPONENT_INDEX[b]]
    return lhs + rhs if attr == "reb" else lhs - rhs


def score_matrix(
    comps: np.ndarray, scoring: Mapping[str, float], *, td_stacks_dd: bool = True
) -> np.ndarray:
    """Score an ``(n, len(COMPONENT_ORDER))`` array of component lines.

    Semantically identical to calling :func:`score_line` on each row — same
    derived bonuses, same derived missed shots, same refusal to ignore a
    weighted stat it cannot represent. It exists only because the calibration
    run scores tens of millions of simulated lines and the per-line path is
    around three orders of magnitude too slow for that.
    """
    if comps.ndim != 2 or comps.shape[1] != len(COMPONENT_ORDER):
        raise ValueError(f"expected an (n, {len(COMPONENT_ORDER)}) array, got {comps.shape}")

    doubles = np.zeros(len(comps), dtype=np.int64)
    for cat in DOUBLE_CATEGORIES:
        doubles += _column(comps, cat) >= DOUBLE_THRESHOLD
    td = (doubles >= 3).astype(np.float64)
    dd = (doubles >= 2).astype(np.float64)
    if not td_stacks_dd:
        dd = np.where(td > 0, 0.0, dd)

    total = np.zeros(len(comps), dtype=np.float64)
    for key, weight in scoring.items():
        if not weight:
            continue
        if key == "dd":
            total += weight * dd
        elif key == "td":
            total += weight * td
        else:
            attr = _COMPONENT_KEYS.get(key)
            if attr is None:
                raise UnscorableStat(
                    f"scoring_settings weights {key!r} at {weight}, but StatLine has no"
                    f" component for it; add it to _COMPONENT_KEYS and StatLine"
                )
            total += weight * _column(comps, attr)
    return np.round(total, 2)


# --- derived economics ------------------------------------------------------
#
# The architecture doc states these as consequences of the scoring table; they
# are computed here so the tests assert against the league's actual settings
# rather than against numbers copied into a test file.


def break_even_rate(made_value: float, missed_value: float) -> float:
    """Shooting percentage at which an attempt is EV-neutral.

    Solves ``p*made + (1-p)*missed = 0``. A made three is worth 5.5 in this
    league (3 points + 0.5 fgm + 2.0 tpm) against -1.0 for a miss, giving a
    break-even of 15.4% — which is why the format overweights three-point
    volume relative to standard scoring.
    """
    span = made_value - missed_value
    if span == 0:
        raise ValueError("made and missed values are equal; no break-even exists")
    return -missed_value / span


def shot_values(scoring: Mapping[str, float]) -> dict[str, tuple[float, float]]:
    """(made, missed) fantasy value per attempt, by shot type.

    A made three pays pts(3) + fgm + tpm; a made two pays pts(2) + fgm; a made
    free throw pays pts(1) + ftm. Missing pays the corresponding penalty.
    """
    pts = scoring.get("pts", 0.0)
    fgm, fgmi = scoring.get("fgm", 0.0), scoring.get("fgmi", 0.0)
    ftm, ftmi = scoring.get("ftm", 0.0), scoring.get("ftmi", 0.0)
    tpm, tpmi = scoring.get("tpm", 0.0), scoring.get("tpmi", 0.0)
    return {
        "three": (3 * pts + fgm + tpm, fgmi + tpmi),
        "two": (2 * pts + fgm, fgmi),
        "free_throw": (1 * pts + ftm, ftmi),
    }
