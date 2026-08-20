"""Ticket #6: wiring test for the MCP `execute_code` interactive mode --
exercised against a real, local, headless SC2 game (Terran, reusing
`sdk.bot.VerifiedBotAI`/`Bot` directly -- see `sdk/mcp_server.py`'s module
docstring for why no new verification logic is introduced here) and a real
MCP client, per the spec's testing decision ("MCP ... entry points get thin
wiring tests only": a snippet can read/mutate state via bot/sdk exactly as
a direct call would, and the game is confirmed paused/stepped rather than
free-running during evaluation).

"Real MCP client" here means the official `mcp` SDK's own
`mcp.client.session.ClientSession`, performing real `initialize()`/
`list_tools()`/`call_tool()` JSON-RPC round trips against a real,
running `mcp.server.fastmcp.FastMCP` server (via
`mcp.shared.memory.create_connected_server_and_client_session` -- the same
in-memory-transport test harness the `mcp` SDK's own test suite uses) --
not a mocked or hand-simulated MCP interaction. The transport is in-memory
rather than a stdio subprocess specifically because what's under test is
this ticket's same-event-loop pause/step wiring (see `mcp_server.py`'s
module docstring) -- the transport framing itself is the well-tested
official SDK's problem, not this project's.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sc2.data import Difficulty, Race, Result

from sdk.mcp_server import DEFAULT_MAP, serve_execute_code

_SAFETY_TIME_LIMIT = 180  # in-game seconds; generous safety cap for the whole test.


def _payload(call_tool_result) -> dict:
    """Unwrap a CallToolResult's single TextContent block (see
    mcp_server.build_server's execute_code -- it returns a plain dict,
    which FastMCP serializes as one JSON TextContent block) into a dict."""
    assert not call_tool_result.isError, call_tool_result
    assert len(call_tool_result.content) == 1
    return json.loads(call_tool_result.content[0].text)


@pytest.mark.integration
def test_execute_code_against_real_game(sc2_install):
    """Runs the whole scripted exchange inside one asyncio.run() call so
    the real game (an asyncio.Task created by serve_execute_code) and the
    real MCP client/server exchange share one event loop -- exactly the
    same-loop architecture sdk/mcp_server.py's module docstring describes
    for production (stdio) use, just with an in-memory transport standing
    in for stdio piping."""
    asyncio.run(_run())


async def _run() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    async with create_connected_server_and_client_session(session.mcp) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["execute_code"]

        # -- read state via bot/sdk exactly as a direct call would --
        r = await client.call_tool("execute_code", {"code": "bot.observe().minerals"})
        payload = _payload(r)
        assert payload["ok"] is True
        assert payload["result"] == "50"  # starting minerals, confirming this IS live game state

        # -- wiring: confirm the game is paused/stepped, not free-running --
        #
        # A subtlety in what "game_time as of this call" actually reflects:
        # each execute_code call's own on_step -> client.step() advance
        # (see mcp_server.py's module docstring) becomes visible in the
        # *next* observation, not the current one -- the outer python-sc2
        # loop calls client.step() right after on_step returns, then only
        # fetches the following observation once the *next* call arrives.
        # So consecutive calls always differ by roughly one step
        # (client.game_step frames, ~0.179 in-game seconds by default),
        # even back-to-back with no wait -- that step is real and expected.
        # What must NOT happen is that delta growing with how long we wait
        # between calls: if the game were free-running/realtime, a 3-second
        # real-world gap would let it advance by several in-game seconds
        # (SC2 plays noticeably faster than 1:1 at the "faster" speed these
        # matches run at), not by the same ~one-step sliver a back-to-back
        # call pair shows.
        async def _game_time() -> float:
            r = await client.call_tool("execute_code", {"code": "bot.observe().game_time"})
            return float(_payload(r)["result"])

        t0 = await _game_time()
        t1 = await _game_time()  # back-to-back, no wait
        delta_immediate = t1 - t0

        t2 = await _game_time()
        await asyncio.sleep(3)
        t3 = await _game_time()
        delta_after_3s_wait = t3 - t2

        assert delta_after_3s_wait < 1.0, (
            f"game_time advanced by {delta_after_3s_wait:.3f}s during a 3s real-world gap with "
            "no execute_code call in flight -- consistent with the game running free/realtime "
            "instead of pausing between calls."
        )
        assert abs(delta_after_3s_wait - delta_immediate) < 0.2, (
            "the per-call game_time delta depended on how long we waited between calls "
            f"(delta_immediate={delta_immediate:.3f}s vs delta_after_3s_wait={delta_after_3s_wait:.3f}s) -- "
            "expected it to depend only on call count, not wall-clock time, if the game is "
            "truly paused between calls rather than free-running."
        )

        # -- wiring: confirm it's genuinely *stepped*, not just frozen forever --
        # Several more calls, issued back-to-back (fast, real wall-clock
        # time), should visibly advance game_time by several more times
        # that same per-call sliver -- proving time progresses with call
        # count, not with wall-clock time.
        t4 = t3
        for _ in range(5):
            t4 = await _game_time()
        assert t4 - t3 > delta_immediate, (
            "game_time did not advance proportionally after several more execute_code calls -- "
            "the game does not appear to be stepping at all."
        )

        # -- mutate state via bot.* exactly as a direct call would --
        r = await client.call_tool(
            "execute_code",
            {
                "code": (
                    "from sc2.ids.unit_typeid import UnitTypeId\n"
                    "await bot.train(UnitTypeId.SCV)"
                )
            },
        )
        payload = _payload(r)
        assert payload["ok"] is True
        assert "TrainOutcome" in payload["result"]
        assert "ok=True" in payload["result"]

        # -- mutate state via raw sdk.* passthrough, auto-awaited even though
        # the snippet's trailing expression didn't say `await` itself --
        r = await client.call_tool("execute_code", {"code": "sdk.client.debug_all_resources()"})
        payload = _payload(r)
        assert payload["ok"] is True

        r = await client.call_tool("execute_code", {"code": "bot.observe().minerals"})
        payload = _payload(r)
        assert payload["ok"] is True
        assert int(payload["result"]) > 50  # debug_all_resources() genuinely landed

        # -- errors come back structured (ok=False), not as a crashed session --
        r = await client.call_tool(
            "execute_code", {"code": "bot.observe().this_attribute_does_not_exist"}
        )
        payload = _payload(r)
        assert payload["ok"] is False
        assert payload["error"] is not None
        assert "AttributeError" in payload["error"]

        # -- print() output is captured and returned --
        r = await client.call_tool("execute_code", {"code": "print('hello from execute_code')"})
        payload = _payload(r)
        assert payload["ok"] is True
        assert "hello from execute_code" in payload["stdout"]

        # -- the session's own game_task is still the real, live game: it
        # keeps running (we never told it to end) until told to. Ending it
        # via a real sdk.* passthrough call (rather than cancelling the
        # task from outside) both (a) is itself one more demonstration of
        # "mutate game state via sdk exactly as a direct call would", and
        # (b) avoids fighting python-sc2's own client/protocol lifecycle --
        # cancelling _host_game's task mid-flight while it's awaiting a
        # pending websocket response is not something python-sc2's
        # protocol.py tolerates cleanly (observed firsthand: it hard-exits
        # the process via sys.exit(2) if the same in-flight request is
        # cancelled twice, which is exactly what both an explicit
        # `game_task.cancel()` here and asyncio.run()'s own end-of-run task
        # cleanup would each independently attempt). `client.leave()`
        # instead sets the client's own game-result flag synchronously
        # (see sc2/client.py's Client.leave), which _play_game_ai's own
        # outer loop (sc2/main.py) checks on its very next iteration and
        # responds to by calling on_end and returning normally -- a clean,
        # in-protocol way to end the match instead of an external cancel.
        assert not session.game_task.done()
        r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
        payload = _payload(r)
        assert payload["ok"] is True

    result = await asyncio.wait_for(session.game_task, timeout=30)
    assert result == Result.Defeat  # leave()/resign always scores as a loss for us
