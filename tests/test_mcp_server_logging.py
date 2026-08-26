"""Fast, non-integration test for `sdk.mcp_server`'s stdout-logging fix
(ticket #19: https://github.com/blokboy/sc2-sdk/issues/19) -- see that
module's "Keeping python-sc2's own logging off of stdout" section for the
full design.

Why this doesn't reconstruct the real two-process repro
-----------------------------------------------------------
The real bug only reproduces across two separate OS processes sharing a
real stdio pipe (a real `mcp.client.stdio` session reading a real
`sc2-sdk-mcp` subprocess's stdout) -- surfaced verifying ticket #17. The
in-process `mcp.shared.memory.create_connected_server_and_client_session`
harness this project's other MCP tests use (see `serve_execute_code`'s
docstring in `sdk.mcp_server`) does not share a real stdio pipe, so it
would not exercise this at all. Reconstructing the real two-process repro
in CI would be slow and platform-fragile for what is fundamentally a
one-line logging-configuration fact, so this instead asserts that fact
directly: after `sc2-sdk-mcp` startup's logging fix runs, `loguru`'s
shared `logger` singleton (see `sdk.mcp_server`'s module docstring on why
python-sc2's `sc2.main` import-time `logger.add(sys.stdout, ...)` is a
*global*, not per-importer, side effect) is no longer configured to write
to `sys.stdout` at all, and an emitted log line provably does not land on
stdout (the exact symptom ticket #19 describes).
"""

from __future__ import annotations

import sys

from loguru import logger

from sdk.mcp_server import _silence_python_sc2_stdout_logging


def test_silence_python_sc2_stdout_logging_removes_the_stdout_sink() -> None:
    # Put the logger back into the exact state python-sc2's own import-time
    # `logger.remove(); logger.add(sys.stdout, level="INFO")` (sc2/main.py)
    # leaves it in, so this test exercises the fix against the real
    # pre-fix configuration rather than whatever state an earlier test in
    # this session happened to leave `logger` in.
    logger.remove()
    logger.add(sys.stdout, level="INFO")

    _silence_python_sc2_stdout_logging()

    sink_streams = [getattr(handler._sink, "_stream", None) for handler in logger._core.handlers.values()]
    assert sys.stdout not in sink_streams, (
        f"logger still has a handler writing to sys.stdout after the fix: {sink_streams}"
    )


def test_silence_python_sc2_stdout_logging_keeps_log_lines_off_stdout(capsys) -> None:
    logger.remove()
    logger.add(sys.stdout, level="INFO")

    _silence_python_sc2_stdout_logging()
    logger.info("ticket-19-marker-line-should-not-reach-stdout")

    captured = capsys.readouterr()
    assert "ticket-19-marker-line-should-not-reach-stdout" not in captured.out
