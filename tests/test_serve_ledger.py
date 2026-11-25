"""The one route that writes: the advice page's Locked / Passed buttons.

It is the only thing on the network that can change the database, so most of
these tests are about what it refuses. Everything refused writes nothing.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from live_fixture import OPENING, advising_morning

from lockin import advice, serve
from lockin import digest as digest_mod
from lockin.store.db import connect_readonly, session


@pytest.fixture(scope="module")
def morning(tmp_path_factory):
    _, _, report, now = advising_morning(tmp_path_factory.mktemp("ledger"), ledger=True)
    assert report.state_source == "ledger" and report.calls and report.rules
    return report, now


def opening(conn) -> None:
    """The season's first game, which is all the write route's calendar needs."""
    conn.execute(
        "INSERT INTO nba_schedule (nba_game_id, season, game_date, home_team, away_team)"
        " VALUES ('0022600001', '2026', ?, 'ATL', 'BOS')",
        (OPENING.isoformat(),),
    )


@pytest.fixture
def served(tmp_path, morning) -> Iterator[tuple[str, object, object]]:
    report, now = morning
    db = tmp_path / "serve.db"
    with session(db) as conn:
        opening(conn)
        digest_mod.persist(conn, report, now=now)
        conn.executemany(
            "INSERT OR REPLACE INTO players"
            " (sleeper_id, full_name, positions, updated_at) VALUES (?, ?, '[]', 't')",
            list(report.names.items()),
        )
        run_id = conn.execute("SELECT run_id FROM digest_runs").fetchone()[0]
    httpd = serve.build_server(
        db,
        report.roster_id,
        host="127.0.0.1",
        port=0,
        quiet=True,
        season="2026",
        today=lambda: report.as_of,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, db, run_id
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post(base, fields=None, *, body=None, path="/ledger", headers=None):
    data = body if body is not None else urllib.parse.urlencode(fields or {}).encode()
    sent = {"Content-Type": "application/x-www-form-urlencoded", "Origin": base}
    sent.update(headers or {})
    sent = {k: v for k, v in sent.items() if v is not None}
    req = urllib.request.Request(base + path, data=data, headers=sent, method="POST")
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=10) as response:
            return response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.status, dict(exc.headers)


def page(base) -> str:
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        return response.read().decode()


def rows(db) -> list[tuple]:
    conn = connect_readonly(db)
    try:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT sleeper_id, game_day, decision, source, run_id FROM lock_ledger"
                " ORDER BY entry_id"
            )
        ]
    finally:
        conn.close()


def answer_for(morning, run, decision="LOCK", **over):
    call = morning[0].calls[0]
    fields = {
        "run_id": run,
        "sleeper_id": call.sleeper_id,
        "game_day": str(call.day),
        "decision": decision,
    }
    fields.update(over)
    return fields


def test_the_page_offers_an_answer_for_every_call(served, morning):
    base, _, _ = served
    text = page(base)
    assert text.count('action="/ledger"') == 2 * len(morning[0].calls)
    assert "Locked</button>" in text and "Passed</button>" in text
    assert "your answers on this page" in text


def test_a_tap_records_the_answer_and_returns_to_the_page(served, morning):
    base, db, run_id = served
    status, headers = post(base, answer_for(morning, run_id))
    assert (status, headers.get("Location")) == (303, "/")
    call = morning[0].calls[0]
    assert rows(db) == [(call.sleeper_id, call.day, "LOCK", "page", run_id)]
    text = page(base)
    assert "&#10003; locked" in text and "Undo</button>" in text
    assert "Recorded this week" in text


def test_undo_withdraws_it(served, morning):
    base, db, run_id = served
    post(base, answer_for(morning, run_id))
    assert post(base, answer_for(morning, run_id, "UNDO"))[0] == 303
    assert [r[2] for r in rows(db)] == ["LOCK", "UNDO"]
    assert "Recorded this week" not in page(base)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Origin": "http://evil.example"}, id="another site"),
        pytest.param({"Origin": "null"}, id="opaque origin"),
        pytest.param({"Origin": None}, id="no origin or referer"),
        pytest.param({"Sec-Fetch-Site": "cross-site"}, id="browser says cross-site"),
    ],
)
def test_a_post_from_anywhere_but_the_page_is_refused(served, morning, headers):
    base, db, run_id = served
    assert post(base, answer_for(morning, run_id), headers=headers)[0] == 403
    assert rows(db) == []


def test_a_matching_referer_stands_in_for_origin(served, morning):
    base, db, run_id = served
    status, _ = post(
        base, answer_for(morning, run_id), headers={"Origin": None, "Referer": base + "/"}
    )
    assert status == 303 and len(rows(db)) == 1


@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"decision": "MAYBE"}, id="unknown decision"),
        pytest.param({"game_day": "tomorrow"}, id="not a day"),
        pytest.param({"sleeper_id": "nobody"}, id="not a call"),
        pytest.param({"run_id": "no-such-run"}, id="not a run"),
        pytest.param({"extra": "x"}, id="an extra field"),
    ],
)
def test_anything_but_an_open_call_is_refused(served, morning, over):
    base, db, run_id = served
    assert post(base, answer_for(morning, run_id, **over))[0] == 400
    assert rows(db) == []


def test_a_standing_rule_is_not_a_call(served, morning):
    base, db, run_id = served
    rule = morning[0].rules[0]
    fields = answer_for(morning, run_id, sleeper_id=rule.sleeper_id, game_day=str(rule.night))
    if (rule.sleeper_id, rule.night) in {(c.sleeper_id, c.day) for c in morning[0].calls}:
        pytest.skip("this rule shares its game with a call")
    assert post(base, fields)[0] == 400 and rows(db) == []


def test_a_body_too_big_or_not_a_form_is_refused(served, morning):
    base, db, run_id = served
    assert post(base, body=b"x" * (serve.MAX_BODY + 1))[0] == 413
    status, _ = post(base, answer_for(morning, run_id), headers={"Content-Type": "text/plain"})
    assert status == 415
    assert rows(db) == []


def test_no_other_path_takes_a_post(served, morning):
    base, db, run_id = served
    assert post(base, answer_for(morning, run_id), path="/")[0] == 404
    assert rows(db) == []


def test_a_database_without_the_table_is_not_given_one(served, morning):
    base, db, run_id = served
    with session(db) as conn:
        conn.execute("DROP TABLE lock_ledger")
    assert post(base, answer_for(morning, run_id))[0] == 503
    conn = connect_readonly(db)
    try:
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='lock_ledger'").fetchone() is None
        )
    finally:
        conn.close()


def test_a_replays_calls_cannot_be_answered(tmp_path, morning):
    report, now = morning
    db = tmp_path / "replay.db"
    with session(db) as conn:
        opening(conn)
        digest_mod.persist(conn, replace(report, retrospective=True), now=now)
        run_id = conn.execute("SELECT run_id FROM digest_runs").fetchone()[0]
    httpd = serve.build_server(
        db,
        report.roster_id,
        host="127.0.0.1",
        port=0,
        quiet=True,
        season="2026",
        today=lambda: report.as_of,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert post(base, answer_for(morning, run_id))[0] == 400
        assert 'action="/ledger"' not in page(base)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_call_dropped_from_the_page_stays_answerable(tmp_path, morning):
    """A lock nobody recorded is missing from every total for the rest of the week."""
    report, now = morning
    db = tmp_path / "later.db"
    call = report.calls[0]
    with session(db) as conn:
        digest_mod.persist(conn, report, now=now)
        first = conn.execute("SELECT run_id FROM digest_runs").fetchone()[0]
        later = replace(report, calls=[c for c in report.calls if c.sleeper_id != call.sleeper_id])
        digest_mod.persist(conn, later, now=now + timedelta(hours=1))
    conn = connect_readonly(db)
    try:
        run = advice.latest_run(conn, report.roster_id)
    finally:
        conn.close()
    assert [(i.sleeper_id, i.run_id) for i in run.unanswered] == [(call.sleeper_id, first)]
    text = advice.render(run, interactive=True)
    assert "Not answered from earlier this week" in text
    assert f'value="{first}"' in text


def test_the_written_file_has_no_buttons(served, morning):
    _, db, _ = served
    conn = connect_readonly(db)
    try:
        text = advice.render(advice.latest_run(conn, morning[0].roster_id))
    finally:
        conn.close()
    assert 'action="/ledger"' not in text and "not answered" in text


def test_a_server_that_does_not_know_its_season_takes_no_answers(tmp_path, morning):
    report, now = morning
    db = tmp_path / "noseason.db"
    with session(db) as conn:
        opening(conn)
        digest_mod.persist(conn, report, now=now)
        run_id = conn.execute("SELECT run_id FROM digest_runs").fetchone()[0]
    httpd = serve.build_server(db, report.roster_id, host="127.0.0.1", port=0, quiet=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert post(base, answer_for(morning, run_id))[0] == 400
        assert rows(db) == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_a_replay_shows_its_mornings_answers_and_takes_none(tmp_path, morning):
    """Undo on a replay would post the live run's id, which the route accepts."""
    from lockin import ledger

    report, now = morning
    db = tmp_path / "replayed.db"
    call = report.calls[0]
    with session(db) as conn:
        opening(conn)
        digest_mod.persist(conn, report, now=now)
        live_id = conn.execute("SELECT run_id FROM digest_runs").fetchone()[0]
        for decision, at in (("LOCK", "02:00"), ("UNDO", "20:00")):
            ledger.record(
                conn,
                week=report.week,
                roster_id=report.roster_id,
                sleeper_id=call.sleeper_id,
                game_day=call.day,
                decision=decision,
                source="page",
                run_id=live_id,
                recorded_at=f"{report.as_of}T{at}:00+00:00",
            )
        digest_mod.persist(conn, replace(report, retrospective=True), now=now + timedelta(days=1))
    conn = connect_readonly(db)
    try:
        run = advice.latest_run(conn, report.roster_id)
    finally:
        conn.close()
    assert run.retrospective
    # As that morning knew it: the lock, not the undo recorded that evening.
    assert [(i.sleeper_id, i.answer) for i in run.recorded] == [(call.sleeper_id, "LOCK")]
    text = advice.render(run, interactive=True)
    assert 'action="/ledger"' not in text and "Undo</button>" not in text
