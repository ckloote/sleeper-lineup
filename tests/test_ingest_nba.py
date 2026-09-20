"""The NBA schedule ingest.

Rewritten when the schedule source moved from LeagueGameFinder to
ScheduleLeagueV2. The old source returned games that had been *played*, which
had gone unnoticed for the project's whole life because it had only ever run
against a finished season — and which would have failed on day one of 2026-27.

Three of the tests below are that regression in miniature: a season with no
results must ingest, a game nobody has played must be written, and a fixture
whose teams are still undecided must not be fatal.
"""

from __future__ import annotations

import pathlib

import pytest

from lockin.ingest import nba as nba_ingest
from lockin.ingest.nba import _season_label
from lockin.ingest.validate import SchemaDriftError
from lockin.store.db import session

FINAL, SCHEDULED = 3, 1


def game(gid, date, home, away, *, tip="T23:00:00Z", status=SCHEDULED):
    return {
        "gameId": gid,
        "gameDateEst": f"{date}T00:00:00",
        "gameDateTimeUTC": f"{date}{tip}" if tip else None,
        "gameStatus": status,
        "homeTeam": {"teamTricode": home} if home else {},
        "awayTeam": {"teamTricode": away} if away else {},
    }


def schedule(*games, wrap=True):
    """A ScheduleLeagueV2 payload holding these games, one per date."""
    dates = [{"gameDate": g["gameDateEst"], "games": [g]} for g in games]
    return {"leagueSchedule": {"gameDates": dates}} if wrap else {"leagueSchedule": {}}


@pytest.fixture
def feed(monkeypatch):
    """Serve a canned ScheduleLeagueV2 payload."""

    def serve(payload):
        from nba_api.stats.endpoints import scheduleleaguev2

        class Fake:
            def __init__(self, *a, **k):
                pass

            def get_dict(self):
                return payload

        monkeypatch.setattr(scheduleleaguev2, "ScheduleLeagueV2", Fake)

    return serve


@pytest.fixture
def conn(tmp_path):
    with session(pathlib.Path(tmp_path) / "season.db", create=True) as c:
        yield c


def rows(conn):
    return {
        r["nba_game_id"]: (r["game_date"], r["home_team"], r["away_team"], r["tipoff_utc"])
        for r in conn.execute("SELECT * FROM nba_schedule")
    }


# --- the regression the port exists for ----------------------------------


def test_a_game_nobody_has_played_is_still_written(feed, conn):
    """The whole point. LeagueGameFinder could not see this row at all, so
    `nba_schedule` never held tonight's fixture and day-one.md step 5's
    fallback had nothing to read."""
    feed(schedule(game("0022600001", "2026-10-20", "OKC", "HOU", status=SCHEDULED)))

    result = nba_ingest.ingest_schedule(conn, "2026")

    assert result.written == 1
    assert result.unplayed == 1, "an unplayed game must be visible as such in the log"
    assert rows(conn)["0022600001"] == ("2026-10-20", "OKC", "HOU", "2026-10-20T23:00:00Z")


def test_a_season_with_no_results_yet_does_not_raise(feed, conn):
    """`lockin ingest` failed here with SchemaDriftError before the port —
    every run, from the rollover until the first game finished."""
    feed(schedule())

    result = nba_ingest.ingest_schedule(conn, "2026")

    assert result == (0, 0, 0)


def test_a_fixture_with_no_teams_yet_is_counted_not_fatal(feed, conn):
    """The 2026-27 NBA Cup final is already on the calendar with both tricodes
    empty. The previous implementation raised on exactly this shape."""
    feed(
        schedule(
            game("0062600001", "2026-12-15", None, None),
            game("0022600001", "2026-10-20", "OKC", "HOU"),
        )
    )

    result = nba_ingest.ingest_schedule(conn, "2026")

    assert (result.written, result.undecided) == (1, 1)
    assert "0062600001" not in rows(conn)


# --- what is kept and what is skipped ------------------------------------


def test_preseason_is_skipped_so_its_foreign_clubs_never_land(feed, conn):
    """2025-26's preseason brings GUA, HAP, MEL and SEM. `mark_exhibitions`
    reads its notion of a real NBA team out of this table."""
    feed(
        schedule(
            game("0012600001", "2026-10-05", "MEL", "OKC"),
            game("0022600001", "2026-10-20", "OKC", "HOU"),
        )
    )

    nba_ingest.ingest_schedule(conn, "2026")

    teams = {t for r in rows(conn).values() for t in r[1:3]}
    assert "MEL" not in teams
    assert teams == {"OKC", "HOU"}


def test_the_all_star_game_is_skipped_so_exhibitions_stay_detectable(feed, conn):
    """The load-bearing one.

    All-Star weekend's tricodes are STP and STR — precisely the pair
    `mark_exhibitions` exists to catch. Ingest them and the All-Star game reads
    as a real fixture, so the engine believes an All-Star's week ends on a low
    exhibition score and banks far too eagerly before the break.
    """
    feed(
        schedule(
            game("0032600001", "2027-02-14", "STP", "STR"),
            game("0022600001", "2026-10-20", "OKC", "HOU"),
        )
    )
    nba_ingest.ingest_schedule(conn, "2026")
    conn.execute(
        "INSERT INTO game_links (sleeper_game_id, game_date, team_a, team_b, occurred)"
        " VALUES ('s1', '2027-02-14', 'STP', 'STR', 1)"
    )

    marked = nba_ingest.mark_exhibitions(conn, "2026")

    assert marked == 1, "the All-Star fixture must still be flagged as an exhibition"


def test_playoff_and_play_in_games_are_kept(feed, conn):
    """Real games between real teams. Their absence before was an artifact of
    LeagueGameFinder's "Regular Season" default, not a decision."""
    feed(
        schedule(
            game("0042600001", "2027-04-20", "OKC", "HOU"),
            game("0052600001", "2027-04-15", "GSW", "LAL"),
        )
    )

    nba_ingest.ingest_schedule(conn, "2026")

    assert set(rows(conn)) == {"0042600001", "0052600001"}


# --- re-fetching ---------------------------------------------------------


def test_a_rescheduled_game_moves(feed, conn):
    feed(schedule(game("0022600001", "2026-12-01", "OKC", "HOU")))
    nba_ingest.ingest_schedule(conn, "2026")

    feed(schedule(game("0022600001", "2027-03-14", "OKC", "HOU")))
    nba_ingest.ingest_schedule(conn, "2026")

    assert rows(conn)["0022600001"][0] == "2027-03-14"


def test_a_refetch_without_a_tipoff_does_not_blank_the_one_we_have(feed, conn):
    """A rescheduled game briefly carries no time, and the digest needs one to
    say when tonight's lock window closes."""
    feed(schedule(game("0022600001", "2026-12-01", "OKC", "HOU", tip="T23:00:00Z")))
    nba_ingest.ingest_schedule(conn, "2026")

    feed(schedule(game("0022600001", "2026-12-01", "OKC", "HOU", tip=None)))
    nba_ingest.ingest_schedule(conn, "2026")

    assert rows(conn)["0022600001"][3] == "2026-12-01T23:00:00Z"


# --- drift ---------------------------------------------------------------


def test_a_payload_without_game_dates_is_drift(feed, conn):
    feed(schedule(wrap=False))

    with pytest.raises(SchemaDriftError, match="gameDates"):
        nba_ingest.ingest_schedule(conn, "2026")


def test_a_game_without_an_id_is_drift(feed, conn):
    g = game("0022600001", "2026-10-20", "OKC", "HOU")
    del g["gameId"]
    feed(schedule(g))

    with pytest.raises(SchemaDriftError, match="no gameId"):
        nba_ingest.ingest_schedule(conn, "2026")


def test_a_self_matchup_is_drift(feed, conn):
    """Kept from the LeagueGameFinder era: an earlier implementation produced
    "DET @ DET" fixtures that silently failed to link."""
    feed(schedule(game("0022600001", "2026-10-20", "DET", "DET")))

    with pytest.raises(SchemaDriftError, match="playing itself"):
        nba_ingest.ingest_schedule(conn, "2026")


def test_an_unparseable_date_is_drift(feed, conn):
    g = game("0022600001", "2026-10-20", "OKC", "HOU")
    g["gameDateEst"] = "next Tuesday"
    feed(schedule(g))

    with pytest.raises(SchemaDriftError, match="expected a date"):
        nba_ingest.ingest_schedule(conn, "2026")


def test_season_label_maps_sleeper_season_to_nba_season():
    assert _season_label("2025") == "2025-26"
    assert _season_label("2026") == "2026-27"
    assert _season_label("1999") == "1999-00"
