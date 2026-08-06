"""Shape validation for ingested payloads.

Architecture doc design rule 4: fail loud on schema drift. Both upstreams are
unofficial and change without notice. A stale cache degrading gracefully is
acceptable; silent wrong numbers are not.

Every assertion here raises rather than warns. The cost of a false alarm is a
failed cron run and an email; the cost of a missed drift is confident wrong
recommendations for however long it takes someone to notice.
"""

from __future__ import annotations

from typing import Any


class SchemaDriftError(RuntimeError):
    """An upstream payload did not have the shape we depend on."""


def require_mapping(value: Any, context: str) -> dict:
    if not isinstance(value, dict):
        raise SchemaDriftError(f"{context}: expected object, got {type(value).__name__}")
    return value


def require_list(value: Any, context: str) -> list:
    if not isinstance(value, list):
        raise SchemaDriftError(f"{context}: expected array, got {type(value).__name__}")
    return value


def require_keys(payload: dict, keys: set[str], context: str) -> None:
    missing = keys - payload.keys()
    if missing:
        raise SchemaDriftError(f"{context}: missing keys {sorted(missing)}")


def require_nonempty(seq: Any, context: str) -> None:
    if not seq:
        raise SchemaDriftError(f"{context}: empty, expected at least one element")


LEAGUE_KEYS = {
    "league_id",
    "season",
    "scoring_settings",
    "roster_positions",
    "settings",
    "total_rosters",
}

MATCHUP_KEYS = {
    "roster_id",
    "matchup_id",
    "players",
    "players_points",
    "starters",
    "starters_points",
    "points",
}

STAT_ROW_KEYS = {"player_id", "game_id", "date", "week", "season", "season_type", "stats"}


def validate_league(payload: Any) -> dict:
    league = require_mapping(payload, "league")
    require_keys(league, LEAGUE_KEYS, "league")
    require_mapping(league["scoring_settings"], "league.scoring_settings")
    require_nonempty(
        require_list(league["roster_positions"], "league.roster_positions"),
        "league.roster_positions",
    )
    return league


def validate_matchups(payload: Any, week: int) -> list[dict]:
    rows = require_list(payload, f"matchups[week={week}]")
    for row in rows:
        require_keys(
            require_mapping(row, f"matchups[week={week}] row"),
            MATCHUP_KEYS,
            f"matchups[week={week}] row",
        )
    return rows


def validate_stat_rows(payload: Any, week: int) -> list[dict]:
    rows = require_list(payload, f"stats[week={week}]")
    for row in rows:
        require_keys(
            require_mapping(row, f"stats[week={week}] row"),
            STAT_ROW_KEYS,
            f"stats[week={week}] row",
        )
        require_mapping(row["stats"], f"stats[week={week}] row.stats")
    return rows


def check_shot_consistency(stats: dict, context: str) -> None:
    """Sleeper supplies missed shots directly AND the attempt/made pair.

    They must agree. If they ever stop agreeing, the scoring engine's derivation
    of missed shots (fga - fgm) is no longer equivalent to the recorded fgmi and
    every score built on it is suspect.
    """
    for made, att, missed in (
        ("fgm", "fga", "fgmi"),
        ("ftm", "fta", "ftmi"),
        ("tpm", "tpa", "tpmi"),
    ):
        m, a, mi = stats.get(made, 0) or 0, stats.get(att, 0) or 0, stats.get(missed, 0) or 0
        if a and abs((a - m) - mi) > 1e-9:
            raise SchemaDriftError(f"{context}: {missed}={mi} but {att}-{made}={a - m}")
