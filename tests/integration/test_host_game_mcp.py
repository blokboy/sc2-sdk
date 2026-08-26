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

#: Ticket #18's own fix -- a short join_timeout so this test doesn't spend
#: minutes actually waiting it out, plus generous wall-clock headroom
#: around it (a real local SC2 client's own launch takes several seconds
#: before the join_timeout countdown even starts -- see
#: sdk.matchcode.wait_for_joiner's docstring) and a hard outer bound (via
#: asyncio.wait_for below) so a regression back to the pre-fix hang fails
#: this test loudly within a couple of minutes instead of hanging CI.
_NO_JOINER_JOIN_TIMEOUT_S = 10.0
_NO_JOINER_POLL_TIMEOUT_S = 60.0
_NO_JOINER_POLL_INTERVAL_S = 1.0
_NO_JOINER_OUTER_BOUND_S = 150.0

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


@pytest.mark.integration
def test_host_game_reports_failed_status_when_no_peer_ever_joins(sc2_install):
    """Ticket #18 (https://github.com/blokboy/sc2-sdk/issues/18): a real
    end-to-end "nobody ever joins" repro -- host_game -> join_timeout
    elapses -> host_status reports a terminal "failed" -- previously
    deliberately left out of this file because it would hang: against a
    real local SC2 client sitting in an empty lobby,
    `sdk.join._run_host_role`'s `join_timeout` did not unblock at all
    (`wait_for_joiner`'s `asyncio.wait_for` around `_connect()` never
    returned even 15-16x past a configured 15s timeout, observed hanging
    4+ minutes). Root cause (see `sdk.matchcode.wait_for_joiner`'s
    docstring): python-sc2's own `Protocol.__request` swallows the first
    `CancelledError` `wait_for_joiner`'s timeout delivers and blocks again
    on a second raw response read that a real peer's absence means never
    resolves -- fixed by driving the connect attempt as its own `Task` and
    force-closing the underlying websocket on timeout instead of relying
    on cooperative cancellation alone.

    Wrapped in an outer `asyncio.wait_for` (not just relying on
    `_NO_JOINER_POLL_TIMEOUT_S`'s own polling loop bound) so a regression
    back to the pre-fix hang fails this test loudly within
    `_NO_JOINER_OUTER_BOUND_S` instead of hanging CI the way the removed
    NOTE below this test used to warn about.
    """
    asyncio.run(asyncio.wait_for(_run_no_joiner(), timeout=_NO_JOINER_OUTER_BOUND_S))


async def _run_no_joiner() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    async with create_connected_server_and_client_session(session.mcp) as client:
        r = await client.call_tool(
            "host_game",
            {
                "host_ip": "127.0.0.1",
                "join_timeout": _NO_JOINER_JOIN_TIMEOUT_S,
                "game_time_limit": _SAFETY_TIME_LIMIT,
            },
        )
        host_payload = _payload(r)
        match_code = host_payload["match_code"]
        assert host_payload["join_timeout"] == _NO_JOINER_JOIN_TIMEOUT_S

        # -- host_status flips to "failed" once join_timeout elapses,
        # without ever needing a peer --
        deadline = time.monotonic() + _NO_JOINER_POLL_TIMEOUT_S
        status_payload = None
        while time.monotonic() < deadline:
            r = await client.call_tool("host_status", {})
            status_payload = _payload(r)
            assert status_payload["ok"] is True, status_payload
            if status_payload["status"] == "failed":
                break
            assert status_payload["status"] == "waiting", (
                f"expected status to stay 'waiting' until it flips to 'failed', got: {status_payload}"
            )
            await asyncio.sleep(_NO_JOINER_POLL_INTERVAL_S)

        assert status_payload is not None and status_payload["status"] == "failed", (
            f"host_status never reported 'failed' within {_NO_JOINER_POLL_TIMEOUT_S}s of a "
            f"{_NO_JOINER_JOIN_TIMEOUT_S}s join_timeout -- last status: {status_payload}"
        )
        assert status_payload["match_code"] == match_code
        assert status_payload["error"] == "No peer joined within the configured join_timeout."
