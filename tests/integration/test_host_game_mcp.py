"""Ticket #15 (https://github.com/blokboy/sc2-sdk/issues/15): the `host_game`/
`host_status` MCP tools, verified end to end against the existing, already-
proven `sc2-sdk-join` CLI as the counterpart -- see `sdk/mcp_server.py`'s
module docstring, "Hosting a two-player match" section, for the full design.

Real, non-mocked per this project's one testing seam (see
`tests/conftest.py`'s module docstring): no local SC2 install means this
SKIPs via the shared `sc2_install` fixture, same as every other integration
test. The `sc2-sdk-mcp` side (host) runs in-process, in the same event loop
as the real MCP client exchange -- the same architecture
`test_execute_code_mcp.py` already uses -- while the joining side is a real,
separate `sc2-sdk-join` OS process (must be a genuinely separate process,
not a background thread in this one -- see `sdk/join.py`'s module docstring
on why `SC2Process`'s SIGINT handler registration requires the interpreter's
main thread), the same shape `tests/integration/test_host_join_cli.py`
already uses for the standalone CLI pair.
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
_JOIN_POLL_TIMEOUT_S = 90.0
_JOIN_POLL_INTERVAL_S = 1.0

_BIN_DIR = Path(sys.executable).parent
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _payload(call_tool_result) -> dict:
    assert not call_tool_result.isError, call_tool_result
    assert len(call_tool_result.content) == 1
    return json.loads(call_tool_result.content[0].text)


@pytest.mark.integration
def test_host_game_returns_immediately_and_join_cli_connects(sc2_install):
    asyncio.run(_run())


async def _run() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    join_proc: "subprocess.Popen | None" = None
    try:
        async with create_connected_server_and_client_session(session.mcp) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools.tools}
            assert "host_game" in tool_names
            assert "host_status" in tool_names

            # -- host_game returns immediately, without blocking on a peer --
            t0 = time.monotonic()
            r = await client.call_tool(
                "host_game", {"host_ip": "127.0.0.1", "join_timeout": 60, "game_time_limit": _SAFETY_TIME_LIMIT}
            )
            elapsed = time.monotonic() - t0
            host_payload = _payload(r)
            match_code = host_payload["match_code"]
            assert isinstance(match_code, str) and match_code
            assert host_payload["host_ip"] == "127.0.0.1"
            # This server's very first game (the solo-vs-built-in-AI one
            # serve_execute_code started) was still active -- host_game
            # must have torn it down the same way new_game would.
            assert host_payload["previous_game_torn_down"] is True
            assert elapsed < 30, (
                f"host_game took {elapsed:.1f}s to return -- expected an almost-immediate "
                "return, not a wait for a peer to connect."
            )

            # -- host_status reports "waiting" before anyone has joined --
            r = await client.call_tool("host_status", {})
            status_payload = _payload(r)
            assert status_payload == {
                "ok": True,
                "match_code": match_code,
                "status": "waiting",
                "error": None,
            }

            # -- connect the existing, already-proven sc2-sdk-join CLI as
            # the real peer, exactly the counterpart this ticket's own
            # acceptance criteria calls for --
            join_argv = [
                str(_BIN_DIR / "sc2-sdk-join"),
                match_code,
                "bots/idle_example.py",
                "--race",
                "zerg",
                "--realtime",
                "--time-limit",
                str(_SAFETY_TIME_LIMIT),
            ]
            join_proc = subprocess.Popen(
                join_argv, cwd=_REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )

            # -- host_status flips to "joined" once the peer connects --
            deadline = time.monotonic() + _JOIN_POLL_TIMEOUT_S
            status_payload = None
            while time.monotonic() < deadline:
                r = await client.call_tool("host_status", {})
                status_payload = _payload(r)
                assert status_payload["ok"] is True, status_payload
                if status_payload["status"] == "joined":
                    break
                assert status_payload["status"] == "waiting", (
                    f"expected status to stay 'waiting' until it flips to 'joined', got: {status_payload}"
                )
                await asyncio.sleep(_JOIN_POLL_INTERVAL_S)
            assert status_payload is not None and status_payload["status"] == "joined", (
                f"host_status never reported 'joined' within {_JOIN_POLL_TIMEOUT_S}s -- "
                f"last status: {status_payload}"
            )
            assert status_payload["match_code"] == match_code

            # -- the match is controllable via execute_code exactly like
            # any other game this server serves, now that a peer connected --
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
        if join_proc is not None:
            try:
                join_proc.wait(timeout=120)
            finally:
                if join_proc.poll() is None:
                    join_proc.kill()
                    join_proc.wait()


# NOTE: a real end-to-end "nobody ever joins" test (host_game -> join_timeout
# elapses -> host_status reports a terminal "failed") is deliberately NOT
# included here. Verifying it against a real local SC2 client on this
# platform surfaced that sdk.join._run_host_role's join_timeout does not
# reliably unblock at all in that exact scenario: `wait_for_joiner`'s
# `asyncio.wait_for` around `_connect()` never returned even 15-16x past a
# configured 15s timeout (observed hanging 4+ minutes, in both realtime=True
# and realtime=False, against a real, non-headless macOS SC2 client with no
# peer) -- the pending `client._execute(join_game=request)` call appears not
# to be cleanly cancellable once the local engine is sitting in an empty
# multiplayer lobby. This is a property of `sdk.join`/`sdk.matchcode`
# (tickets #11/#12's already-shipped, deliberately untouched primitive --
# see those modules' own "kept separate/untouched" docstrings), not
# something introduced by `host_game`/`host_status` here: `_launch_hosted_
# game`'s wrapper and `_host_status`'s state derivation both handle a
# JoinTimeoutError correctly *if* `_run_host_role` ever raises one -- see
# tests/test_host_status.py's test_host_status_reports_join_timeout_
# distinctly, which exercises exactly that handling without needing a real
# client at all. Confirming whether wait_for_joiner itself is reliable
# end-to-end against a real (as opposed to headless-Linux) SC2 client is
# flagged as follow-up work on tickets #11/#12's primitive, out of this
# ticket's scope.
