"""`scripts/logrotate.conf` — the rule that keeps logs/ from growing forever.

Tested because the config has a trap in it that a reader will not see. logrotate
parses a bare relative path as a keyword, so `logs/*.log { ... }` is a syntax
error and the quoted form is not; and because this runs from cron, the only
symptom of getting it wrong would be logs that quietly never rotate.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

CONF = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "logrotate.conf"
LOGROTATE = shutil.which("logrotate") or "/usr/sbin/logrotate"

pytestmark = pytest.mark.skipif(
    not pathlib.Path(LOGROTATE).exists(), reason="logrotate is not installed"
)


def rotate(cwd, *, force=True):
    return subprocess.run(
        [
            LOGROTATE,
            *(["--force"] if force else []),
            "--state",
            "logs/.logrotate.state",
            str(CONF),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


def test_the_config_parses(logs, tmp_path):
    """The quoted relative path. Unquoted, this is a syntax error."""
    (logs / "observe.log").write_text("hello\n")

    result = rotate(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "not properly separated" not in result.stderr


def test_it_rotates_to_a_dated_archive(logs, tmp_path):
    (logs / "observe.log").write_text("x" * 5000)

    rotate(tmp_path)

    archives = list(logs.glob("observe.log-*.gz"))
    assert len(archives) == 1, f"expected one dated archive, got {list(logs.iterdir())}"
    assert (logs / "observe.log").read_text() == "", "a fresh log should be left in place"


def test_the_rotated_file_is_not_world_readable(logs, tmp_path):
    """logs/digest.log carries the lineup, and so does its archive."""
    (logs / "digest.log").write_text("lineup" * 500)
    (logs / "digest.log").chmod(0o600)

    rotate(tmp_path)

    for path in logs.glob("digest.log*"):
        assert path.stat().st_mode & 0o077 == 0, path


def test_an_archive_is_not_rotated_again(logs, tmp_path):
    """`*.log` must not match `observe.log-20260920.gz`, or history compounds."""
    (logs / "observe.log").write_text("x" * 5000)
    rotate(tmp_path)
    (logs / "observe.log").write_text("y" * 5000)

    rotate(tmp_path)

    assert not list(logs.glob("*.gz.gz")), "an archive was rotated a second time"


def test_a_missing_log_is_not_an_error(tmp_path):
    """A fresh deployment has no logs/advice.log until the job first runs."""
    (tmp_path / "logs").mkdir()

    result = rotate(tmp_path)

    assert result.returncode == 0, result.stderr


def test_an_empty_log_is_left_alone(logs, tmp_path):
    """notifempty — rotating nothing into an archive loses the dated name."""
    (logs / "observe.log").write_text("")

    rotate(tmp_path)

    assert list(logs.glob("observe.log-*")) == []
