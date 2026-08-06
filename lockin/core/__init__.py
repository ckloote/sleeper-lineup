"""Pure decision logic. No network, no database, no clock.

Enforced by ``tests/test_core_purity.py``: a stopping policy entangled with I/O
cannot be validated, so the boundary is a test rather than a convention.
"""

from lockin.core.scoring import (
    StatLine,
    UnscorableStat,
    break_even_rate,
    count_doubles,
    derive_bonuses,
    line_from_stats,
    score_line,
    score_recorded,
    shot_values,
)

__all__ = [
    "StatLine",
    "UnscorableStat",
    "break_even_rate",
    "count_doubles",
    "derive_bonuses",
    "line_from_stats",
    "score_line",
    "score_recorded",
    "shot_values",
]
