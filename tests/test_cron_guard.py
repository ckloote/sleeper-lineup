"""`scripts/cron-guard` — the thing that speaks up when cron breaks.

On 2026-09-10 the observe and ingest jobs began failing with `Temporary failure
in name resolution` and kept failing for four days. Both redirected stderr into
a log nobody reads, so nothing reported it; the only symptom was the next
successful run reporting five days of upstream drift as one (§12).

The guard is safety equipment, which is exactly the kind of code that is never
exercised until the day it matters. So it is tested against a real subprocess
and a real HTTP server rather than mocked: the failure mode to design against is
a guard that is itself broken and silent about it.
"""

from __future__ import annotations

import http.server
import os
import pathlib
import subprocess
import threading

import pytest

GUARD = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cron-guard"


class Collector(http.server.BaseHTTPRequestHandler):
    """A stand-in for ntfy that remembers what it was sent."""

    posts: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", 0))
        Collector.posts.append(
            {
                "topic": self.path.lstrip("/"),
                "title": self.headers.get("Title", ""),
                "priority": self.headers.get("Priority", ""),
                "tags": self.headers.get("Tags", ""),
                "body": self.rfile.read(length).decode(),
            }
        )
        # A job named for the failure it should produce, so the refused-send
        # path can be tested without taking the server down. Keyed on the title
        # rather than the path, because the path is the topic, not the job.
        self.send_response(500 if "refuse" in self.headers.get("Title", "") else 200)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def ntfy():
    Collector.posts = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", Collector.posts
    server.shutdown()


@pytest.fixture
def run(tmp_path, ntfy):
    """Invoke the guard in an isolated project, with ntfy pointed at the fake.

    HOME and LOCKIN_TOPIC_FILE are both redirected: a test that read the
    operator's real `~/.lockin-topic` would publish to their phone.
    """
    server, _ = ntfy
    topic_file = tmp_path / "topic"
    topic_file.write_text("test-topic-abcdef\n")

    def invoke(*args, topic=True, env=None):
        environment = {
            **os.environ,
            "HOME": str(tmp_path),
            "LOCKIN_NTFY_SERVER": server,
            "LOCKIN_TOPIC_FILE": str(topic_file if topic else tmp_path / "absent"),
            **(env or {}),
        }
        environment.pop("LOCKIN_NTFY_TOPIC", None)
        return subprocess.run(
            [str(GUARD), *args],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return invoke


def test_a_clean_run_says_nothing(run, tmp_path, ntfy):
    """Cron noise is how a working alert gets muted. Success is silent."""
    result = run("observe", "sh", "-c", "echo 25 weeks, 0 changed")

    assert result.returncode == 0
    assert ntfy[1] == []
    assert "25 weeks, 0 changed" in (tmp_path / "logs" / "observe.log").read_text()


def test_the_log_records_when_the_run_started_and_how_it_ended(run, tmp_path):
    """The old lines had no timestamps, so a four-day gap looked like nothing."""
    run("observe", "sh", "-c", "echo hello")

    log = (tmp_path / "logs" / "observe.log").read_text()
    assert log.startswith("===== observe  20")
    assert "exit 0 =====" in log


def test_a_failure_is_pushed_with_the_tail_of_the_output(run, ntfy):
    _, posts = ntfy
    result = run(
        "ingest",
        "sh",
        "-c",
        "echo Traceback; echo 'socket.gaierror: [Errno -3] Temporary failure'; exit 3",
    )

    assert result.returncode == 3, "cron must still see a failure as a failure"
    assert len(posts) == 1
    assert posts[0]["title"] == "lockin ingest failed"
    assert posts[0]["priority"] == "high"
    assert posts[0]["topic"] == "test-topic-abcdef"
    assert "exit 3" in posts[0]["body"]
    assert "Temporary failure" in posts[0]["body"]


def test_a_failure_is_still_logged(run, tmp_path):
    run("ingest", "sh", "-c", "echo boom >&2; exit 1")

    assert "boom" in (tmp_path / "logs" / "ingest.log").read_text()


def test_without_a_topic_it_stays_quiet_and_still_reports_the_status(run, ntfy):
    """Notifications are opt-in, the same contract lockin/notify.py holds."""
    result = run("observe", "sh", "-c", "exit 7", topic=False)

    assert result.returncode == 7
    assert ntfy[1] == []
    assert result.stderr == ""


def test_an_unreachable_ntfy_does_not_change_the_exit_status(run):
    """A broken notifier must not become a broken job."""
    result = run("observe", "sh", "-c", "exit 4", env={"LOCKIN_NTFY_SERVER": "http://127.0.0.1:1"})

    assert result.returncode == 4


# --- the outage case -----------------------------------------------------


def test_recovery_reports_the_gap_the_alerts_could_not(run, tmp_path, ntfy):
    """The four silent days, announced late rather than never.

    During a network outage the alert cannot be delivered, because the thing
    that broke the job also breaks the POST. The stamp is what closes that
    hole: the run that recovers notices how long it has been.
    """
    _, posts = ntfy
    run("observe", "sh", "-c", "true")
    stamp = tmp_path / "logs" / ".cron-guard" / "observe.ok"
    stamp.write_text(str(int(stamp.read_text().strip()) - 5 * 86400))
    posts.clear()

    result = run("observe", "sh", "-c", "true")

    assert result.returncode == 0
    assert len(posts) == 1
    assert posts[0]["title"] == "lockin observe recovered"
    assert "120 hours ago" in posts[0]["body"]


def test_an_ordinary_daily_run_is_not_a_recovery(run, tmp_path, ntfy):
    _, posts = ntfy
    run("observe", "sh", "-c", "true")
    stamp = tmp_path / "logs" / ".cron-guard" / "observe.ok"
    stamp.write_text(str(int(stamp.read_text().strip()) - 25 * 3600))
    posts.clear()

    run("observe", "sh", "-c", "true")

    assert posts == [], "a 25-hour gap is the normal daily cadence"


def test_a_corrupt_stamp_does_not_take_the_job_down(run, tmp_path, ntfy):
    run("observe", "sh", "-c", "true")
    (tmp_path / "logs" / ".cron-guard" / "observe.ok").write_text("not-a-number\n")

    result = run("observe", "sh", "-c", "true")

    assert result.returncode == 0
    assert ntfy[1] == []


# --- the plumbing --------------------------------------------------------


def test_the_topic_reaches_the_child(run, tmp_path):
    """`lockin digest --notify` reads it from the environment, so the guard
    must export it — that is what removes the secret from the crontab."""
    run("digest", "sh", "-c", "echo topic=$LOCKIN_NTFY_TOPIC")

    assert "topic=test-topic-abcdef" in (tmp_path / "logs" / "digest.log").read_text()


def test_the_state_directory_is_not_world_readable(run, tmp_path):
    """logs/ carries digest output; deployment.md §7 chmod 700s it."""
    run("observe", "sh", "-c", "true")

    assert (tmp_path / "logs").stat().st_mode & 0o077 == 0


def test_it_refuses_a_call_with_no_command(run):
    result = run("observe")

    assert result.returncode == 64
    assert "usage" in result.stderr


def test_the_log_file_is_not_world_readable(run, tmp_path):
    """logs/digest.log carries the lineup; the cron umask made these 644."""
    run("digest", "sh", "-c", "echo lineup")

    assert (tmp_path / "logs" / "digest.log").stat().st_mode & 0o077 == 0


# --- the guard reporting on itself ---------------------------------------
#
# Added after a test alert did not reach the operator's phone and the log could
# not say whether it had even left the Pi. Finding that out meant polling ntfy
# by hand — the silent-failure pattern this script exists to remove, reproduced
# inside the thing removing it.


def test_a_sent_alert_says_so_in_the_log(run, tmp_path):
    run("ingest", "sh", "-c", "exit 1")

    assert "alert: sent to" in (tmp_path / "logs" / "ingest.log").read_text()


def test_the_log_does_not_carry_the_whole_topic(run, tmp_path):
    """An ntfy topic is unauthenticated: the name is the whole of the secret,
    and this line lands in a file. Same six characters as notify.redacted()."""
    log = tmp_path / "logs" / "ingest.log"
    run("ingest", "sh", "-c", "exit 1")

    assert "test-topic-abcdef" not in log.read_text()
    assert "test-t..." in log.read_text()


def test_a_refused_alert_is_recorded_as_failed(run, tmp_path):
    """HTTP 500 from ntfy must not read the same as a delivered alert."""
    run("refuse", "sh", "-c", "exit 1")

    log = (tmp_path / "logs" / "refuse.log").read_text()
    assert "alert: FAILED, HTTP 500" in log


def test_an_unreachable_server_is_recorded_as_failed(run, tmp_path):
    run("observe", "sh", "-c", "exit 1", env={"LOCKIN_NTFY_SERVER": "http://127.0.0.1:1"})

    assert "alert: FAILED, no response" in (tmp_path / "logs" / "observe.log").read_text()


def test_alerting_being_off_is_distinguishable_from_alerting_failing(run, tmp_path):
    """ "disabled", "sent" and "failed" are three different problems."""
    run("observe", "sh", "-c", "exit 1", topic=False)

    log = (tmp_path / "logs" / "observe.log").read_text()
    assert "alert: disabled" in log
    assert "FAILED" not in log


def test_the_priority_and_tag_can_be_overridden(run, ntfy):
    """Which priority a given phone will actually surface is a property of that
    phone, so it is configuration rather than a constant."""
    _, posts = ntfy
    run(
        "observe",
        "sh",
        "-c",
        "exit 1",
        env={"CRON_GUARD_PRIORITY": "default", "CRON_GUARD_TAG": "basketball"},
    )

    assert posts[0]["priority"] == "default"
    assert posts[0]["tags"] == "basketball"
