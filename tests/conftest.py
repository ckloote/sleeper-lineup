"""Test-suite configuration.

Several suites read the ingested season, resolving its path through
`Config.from_env()` at import time and skipping when it is absent. That happens
outside the CLI, so without this they would not see the `.env` the deployment
runbook writes — and on a host where the season lives in `data/lockin-2025.db`
they would all read the default path instead.

Loading it here keeps `uv run --frozen pytest` and `uv run --frozen lockin
verify` agreeing about which file is the season. An exported variable still
outranks the file; see `lockin.config.load_env_file`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lockin.config import Config, load_env_file

load_env_file()


@pytest.fixture(scope="session")
def season_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A private copy of the season database, taken once per test run.

    The suites that read the season apply the schema first, and applying the
    schema is a write. It used to be a harmless one — `CREATE ... IF NOT EXISTS`
    — so they opened the real file. Once `apply_schema` can migrate data, that
    would mean `pytest` migrating the season before anyone had checked the
    migration, so every such suite reads this copy instead.

    Copied with SQLite's backup API rather than `shutil.copy`, which would miss
    whatever is still in the WAL.
    """
    cfg = Config.from_env()
    if not cfg.db_path.exists():
        pytest.skip(f"no database at {cfg.db_path}; run `lockin ingest`")
    dst = tmp_path_factory.mktemp("season") / cfg.db_path.name
    src = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst
