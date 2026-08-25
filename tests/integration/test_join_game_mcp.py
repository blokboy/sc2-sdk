"""Ticket #16 (https://github.com/blokboy/sc2-sdk/issues/16): the `join_game`
MCP tool, verified end to end against the existing, already-proven
`sc2-sdk-host` CLI as the counterpart -- see `sdk/mcp_server.py`'s module
docstring, "Joining a two-player match" section, for the full design.

Real, non-mocked per this project's one testing seam (see
`tests/conftest.py`'s module docstring): no local SC2 install means this
SKIPs via the shared `sc2_install` fixture, same as every other integration
test. The joining side (`sc2-sdk-mcp`, via `join_game`) runs in-process, in
the same event loop as the real MCP client exchange -- the same
architecture `test_host_game_mcp.py` already uses for the hosting side --
while the hosting side is a real, separate `sc2-sdk-host` OS process (must
be a genuinely separate process, not a background thread in this one -- see
`sdk/join.py`'s module docstring on why `SC2Process`'s SIGINT handler
registration requires the interpreter's main thread).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sc2.data import Race, Result

from sdk.mcp_server import DEFAULT_MAP, serve_execute_code

_SAFETY_TIME_LIMIT = 20 * 60  # in-game seconds
_HOST_JOIN_TIMEOUT = 90.0  # host CLI's own --timeout (seconds to wait for a peer)
_MATCH_CODE_WAIT_TIMEOUT_S = 60.0

_BIN_DIR = Path(sys.executable).parent
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _payload(call_tool_result) -> dict:
    assert not call_tool_result.isError, call_tool_result
    assert len(call_tool_result.content) == 1
    return json.loads(call_tool_result.content[0].text)


async def _read_match_code(host_proc: "subprocess.Popen[str]", timeout: float) -> str:
    """Reads the host CLI's stdout line by line (in a thread, since
    `Popen.stdout.readline` is blocking) until the `MATCH CODE: ...` line
    `sdk.host_join.main_host` prints appears -- the host process then keeps
    running, waiting for a joiner, so this must not wait for it to exit."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = await asyncio.wait_for(asyncio.to_thread(host_proc.stdout.readline), timeout=deadline - time.monotonic())
        if not line:
            raise RuntimeError(f"host CLI exited before printing a match code (returncode={host_proc.poll()})")
        if line.startswith("MATCH CODE: "):
            return line.removeprefix("MATCH CODE: ").strip()
    raise TimeoutError(f"host CLI never printed a match code within {timeout}s")


@pytest.mark.integration
def test_join_game_connects_to_a_real_hosted_match(sc2_install):
    asyncio.run(_run())


async def _run() -> None:
    host_argv = [
        str(_BIN_DIR / "sc2-sdk-host"),
        "bots/idle_example.py",
        "--race",
        "terran",
        "--host-ip",
        "127.0.0.1",
        "--timeout",
        str(_HOST_JOIN_TIMEOUT),
        "--realtime",
        "--time-limit",
        str(_SAFETY_TIME_LIMIT),
    ]
    host_proc = subprocess.Popen(
        host_argv, cwd=_REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    try:
        match_code = await _read_match_code(host_proc, timeout=_MATCH_CODE_WAIT_TIMEOUT_S)
        assert isinstance(match_code, str) and match_code

        session = await serve_execute_code(
            map_name=DEFAULT_MAP,
            my_race=Race.Terran,
            game_time_limit=_SAFETY_TIME_LIMIT,
        )
        async with create_connected_server_and_client_session(session.mcp) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools.tools}
            assert "join_game" in tool_names

            # -- join_game blocks until connected, then returns --
            t0 = time.monotonic()
            r = await client.call_tool(
                "join_game", {"code": match_code, "race": "zerg", "join_timeout": _HOST_JOIN_TIMEOUT}
            )
            elapsed = time.monotonic() - t0
            join_payload = _payload(r)
            assert join_payload["host_ip"] == "127.0.0.1"
            assert join_payload["map_name"] == DEFAULT_MAP
            assert join_payload["my_race"] == "Zerg"
            # This server's very first game (the solo-vs-built-in-AI one
            # serve_execute_code started) was still active -- join_game
            # must have torn it down the same way new_game/host_game would.
            assert join_payload["previous_game_torn_down"] is True
            assert elapsed < _HOST_JOIN_TIMEOUT, (
                f"join_game took {elapsed:.1f}s -- expected it to return once connected, "
                "well before its own join_timeout."
            )

            # -- the match is controllable via execute_code exactly like
            # any other game this server serves, now that the join succeeded --
            r = await client.call_tool("execute_code", {"code": "bot.observe().minerals"})
            exec_payload = _payload(r)
            assert exec_payload["ok"] is True
            assert int(exec_payload["result"]) >= 0

            # -- clean shutdown, same in-protocol sdk.client.leave()
            # pattern the rest of this project's MCP tests use --
            r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
            assert _payload(r)["ok"] is True

        result = await asyncio.wait_for(session.game_task, timeout=60)
        assert result in (Result.Victory, Result.Defeat, Result.Tie)
    finally:
        try:
            host_proc.wait(timeout=120)
        finally:
            if host_proc.poll() is None:
                host_proc.kill()
                host_proc.wait()


def test_join_game_fails_promptly_on_an_undecodable_code():
    from sdk.mcp_server import decode_match_code

    with pytest.raises(Exception):
        decode_match_code("not-a-real-match-code")


# NOTE: a real end-to-end "no host is actually listening -> join_game raises
# within join_timeout instead of hanging" test is deliberately NOT included
# here. Probing it manually against a real local SC2 client on this platform
# reproduced the same defect tests/integration/test_host_game_mcp.py already
# documented for the hosting role (see its own NOTE, and issue #18): both
# `_run_host_role` and `_run_join_role`'s "wait for the peer" step bottom out
# in the exact same `sdk.matchcode.wait_for_joiner(_join_game_at(...), ...)`
# call (see `sdk/join.py`) -- `_run_join_role`'s own `_connect()` closure IS
# just `_join_game_at`, the same call `_run_host_role`'s `_connect()` makes
# after `create_game`. So this is not a second, join-side-specific defect:
# it's the same #18 defect, now confirmed reachable from both roles, not
# something `join_game`'s own code (`_launch_joined_game`/
# `_await_connected_or_failure`) can fix without touching `sdk.join`/
# `sdk.matchcode` -- both explicitly "kept separate/untouched" per their own
# docstrings, and out of this ticket's scope. `_await_connected_or_failure`'s
# own logic (raising game_task's exception once it actually finishes) is
# covered without a real client by the unit tests in
# tests/test_join_game_helpers.py.
