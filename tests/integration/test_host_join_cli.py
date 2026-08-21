"""Ticket #12: prove the actual `sc2-sdk-host`/`sc2-sdk-join <code>` console
scripts play a real match end to end -- not just the underlying `sdk.join`
primitive #11 already proved, but the full user-facing flow: print a code,
paste it into the join side, and reach a real result on both.

Real, non-mocked per this project's one testing seam (see
`tests/conftest.py`'s module docstring): no local SC2 install means this
SKIPs via the shared `sc2_install` fixture, same as every other integration
test.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest
from sc2.data import Result

_SAFETY_TIME_LIMIT = 20 * 60
_MATCH_CODE_WAIT_S = 60.0

_BIN_DIR = Path(sys.executable).parent


def _drain_lines(pipe, into: list[str]) -> None:
    for line in pipe:
        into.append(line)
    pipe.close()


def _wait_for_match_code(lines: list[str], timeout: float) -> str:
    event = threading.Event()

    def _poll() -> None:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in lines:
                if line.startswith("MATCH CODE: "):
                    event.set()
                    return
            time.sleep(0.1)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()
    poller.join(timeout=timeout + 1)
    if not event.is_set():
        raise AssertionError(f"Host never printed a MATCH CODE line within {timeout}s. Output so far: {lines}")
    for line in lines:
        if line.startswith("MATCH CODE: "):
            return line.removeprefix("MATCH CODE: ").strip()
    raise AssertionError("unreachable")


def _last_result(lines: list[str]) -> Result:
    for line in reversed(lines):
        if line.startswith("RESULT: "):
            return Result[line.removeprefix("RESULT: ").strip()]
    raise AssertionError(f"Process never printed a RESULT line. Output: {lines}")


@pytest.mark.integration
def test_host_and_join_cli_play_a_real_match(sc2_install):
    host_argv = [
        str(_BIN_DIR / "sc2-sdk-host"),
        "bots/idle_example.py",
        "--race",
        "terran",
        "--host-ip",
        "127.0.0.1",
        "--timeout",
        "120",
        "--time-limit",
        str(_SAFETY_TIME_LIMIT),
    ]
    host_proc = subprocess.Popen(
        host_argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    host_lines: list[str] = []
    host_drain = threading.Thread(target=_drain_lines, args=(host_proc.stdout, host_lines), daemon=True)
    host_drain.start()
    join_proc: subprocess.Popen | None = None

    try:
        code = _wait_for_match_code(host_lines, timeout=_MATCH_CODE_WAIT_S)

        join_argv = [
            str(_BIN_DIR / "sc2-sdk-join"),
            code,
            "bots/idle_example.py",
            "--race",
            "zerg",
            "--time-limit",
            str(_SAFETY_TIME_LIMIT),
        ]
        join_proc = subprocess.Popen(
            join_argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        join_lines: list[str] = []
        join_drain = threading.Thread(target=_drain_lines, args=(join_proc.stdout, join_lines), daemon=True)
        join_drain.start()

        host_proc.wait(timeout=600)
        join_proc.wait(timeout=600)
        host_drain.join(timeout=5)
        join_drain.join(timeout=5)
    finally:
        for proc in (host_proc, join_proc):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()

    host_result = _last_result(host_lines)
    join_result = _last_result(join_lines)

    for result in (host_result, join_result):
        assert result in (Result.Victory, Result.Defeat, Result.Tie)

    if Result.Tie in (host_result, join_result):
        assert host_result == join_result == Result.Tie
    else:
        assert {host_result, join_result} == {Result.Victory, Result.Defeat}
