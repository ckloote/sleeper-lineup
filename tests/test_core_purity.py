"""`lockin.core` must stay pure.

Architecture doc design rule 1: core takes arrays and dataclasses and returns
arrays and dataclasses. Zero network, zero database, zero clock reads. A
stopping policy entangled with I/O cannot be validated, and the whole backtest
depends on being able to replay core deterministically.

Two complementary checks: a static one that catches an I/O import creeping into
a module, and a runtime one that catches an actual call.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

CORE = pathlib.Path(__file__).resolve().parent.parent / "lockin" / "core"

# Modules that imply I/O, nondeterminism, or a clock read. `random` is barred
# too: simulation must take an explicit numpy Generator so a backtest can be
# reproduced exactly.
FORBIDDEN = {
    "socket",
    "requests",
    "urllib",
    "urllib3",
    "http",
    "sqlite3",
    "time",
    "datetime",
    "random",
    "os",
    "pathlib",
    "subprocess",
    "lockin.store",
    "lockin.ingest",
    "lockin.config",
}


def core_modules() -> list[pathlib.Path]:
    return sorted(CORE.glob("*.py"))


def test_core_has_modules_to_check():
    assert core_modules(), "no modules found in lockin/core"


@pytest.mark.parametrize("path", core_modules(), ids=lambda p: p.name)
def test_core_module_imports_nothing_that_does_io(path):
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offending = {
        name for name in imported for bad in FORBIDDEN if name == bad or name.startswith(bad + ".")
    }
    assert not offending, f"{path.name} imports {sorted(offending)}"


def test_scoring_performs_no_io_when_called(monkeypatch):
    """Static imports are not enough — a lazy import inside a function would pass."""
    import socket
    import sqlite3

    def explode(*args, **kwargs):
        raise AssertionError("core performed I/O")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(sqlite3, "connect", explode)

    from lockin.core.scoring import StatLine, score_line

    assert score_line(StatLine(pts=21, ast=5, oreb=7, dreb=5), {"pts": 1.0}) == 21.0


def test_scoring_is_deterministic():
    """Same input, same output — the backtest depends on it."""
    from lockin.core.scoring import StatLine, score_line

    line = StatLine(pts=21, ast=5, oreb=7, dreb=5, stl=2, fgm=9, fga=12)
    scoring = {"pts": 1.0, "ast": 1.0, "oreb": 1.5, "dreb": 1.0, "stl": 2.0, "dd": 10.0}
    assert len({score_line(line, scoring) for _ in range(50)}) == 1
