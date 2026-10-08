"""The real process, started and stopped the way Docker and systemd do it.

A webhook-only deployment needs no platform token, so the whole lifecycle runs
here: start, serve, SIGTERM, and the shutdown that must close the HTTP client
and the database (which is what merges SQLite's WAL back into the main file).
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _start(tmp_path: Path, *, api: bool, api_port: int) -> subprocess.Popen[str]:
    (tmp_path / "webhooks.yaml").write_text(
        "destinations:\n  hook:\n    url: http://127.0.0.1:9/hook\n", encoding="utf-8"
    )
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("DISCORD_", "TELEGRAM_", "API_"))
    }
    env.update(
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'newsflow.db'}",
        WEBHOOKS_CONFIG_PATH=str(tmp_path / "webhooks.yaml"),
        SOURCES_CONFIG_PATH=str(tmp_path / "sources.yaml"),
        API_ENABLED="true" if api else "false",
        API_PORT=str(api_port),
        API_KEY="k",
    )
    return subprocess.Popen(
        [sys.executable, "-m", "newsflow.main"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_first_round(tmp_path: Path, proc: subprocess.Popen[str]) -> None:
    heartbeat = tmp_path / "heartbeat" / "dispatch"
    deadline = time.monotonic() + 30
    while not heartbeat.exists():
        assert proc.poll() is None, "the process exited before its first round"
        assert time.monotonic() < deadline, "no dispatch round within 30 s"
        time.sleep(0.1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is TerminateProcess on Windows")
@pytest.mark.parametrize("api", [False, True], ids=["api-off", "api-on"])
def test_sigterm_stops_everything_and_closes_the_database(tmp_path: Path, api: bool):
    proc = _start(tmp_path, api=api, api_port=_free_port())
    _wait_for_first_round(tmp_path, proc)

    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=10)

    assert proc.returncode == 0, out
    assert "Shutdown complete" in out
    assert "Unclosed" not in out
    assert "Traceback" not in out
    assert not (tmp_path / "newsflow.db-wal").exists()


def test_a_failing_service_exits_through_the_same_shutdown(tmp_path: Path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        proc = _start(tmp_path, api=True, api_port=taken.getsockname()[1])
        out, _ = proc.communicate(timeout=30)

    assert proc.returncode == 1
    assert "Fatal: api failed" in out
    assert "Shutdown complete" in out
    assert "Unclosed" not in out
