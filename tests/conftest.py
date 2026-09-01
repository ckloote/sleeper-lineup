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

from lockin.config import load_env_file

load_env_file()
