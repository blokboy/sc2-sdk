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
import os
import time

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
        # Seven tools now, not one: this server also exposes new_game, the
        # standing-background-task trio start_task/task_status/cancel_task,
        # and the host_game/host_status pair (see sdk/mcp_server.py's
        # build_server) -- test_new_game_*/test_start_task_* below and
        # tests/integration/test_host_game_mcp.py cover those tools' own
        # behavior; this test's job is still just execute_code's wiring
        # against the first game.
        assert sorted(t.name for t in tools.tools) == [
            "cancel_task",
            "execute_code",
            "host_game",
            "host_status",
            "new_game",
            "start_task",
            "task_status",
        ]

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


@pytest.mark.integration
def test_new_game_starts_a_fresh_game_on_the_same_connection(sc2_install):
    """Ticket #6 follow-up: `new_game` lets a caller start another match
    without tearing down the stdio connection -- see `build_server`'s
    `new_game` docstring in `sdk/mcp_server.py` for the full design.

    Confirms two things a caller actually cares about:
      - after `new_game`, subsequent `execute_code` calls are routed to a
        *genuinely fresh* game (starting minerals back at 50, the same
        signal the first wiring test above uses to confirm it's reading
        live game state at all) -- not still talking to the first game.
      - the confirmation payload `new_game` returns describes the game it
        actually started, and correctly reports that a previous game was
        torn down first.
    """
    asyncio.run(_run_new_game_starts_fresh())


async def _run_new_game_starts_fresh() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )
    first_game_task = session.game_task

    async with create_connected_server_and_client_session(session.mcp) as client:
        tools = await client.list_tools()
        assert sorted(t.name for t in tools.tools) == [
            "cancel_task",
            "execute_code",
            "host_game",
            "host_status",
            "new_game",
            "start_task",
            "task_status",
        ]

        # -- spend minerals in the first game, so "back to 50" after
        # new_game can only be explained by a genuinely fresh game, not
        # coincidence --
        r = await client.call_tool("execute_code", {"code": "sdk.client.debug_all_resources()"})
        assert _payload(r)["ok"] is True
        r = await client.call_tool("execute_code", {"code": "bot.observe().minerals"})
        assert int(_payload(r)["result"]) > 50

        # -- new_game: no arguments, so it should fall back to this
        # session's own original launch configuration (map/races/
        # difficulty/realtime). Unlike execute_code, new_game's tool
        # return value IS the confirmation dict itself (see build_server's
        # new_game docstring) -- not an ExecuteCodeResult -- but it still
        # crosses the wire the same way (one JSON TextContent block), so
        # _payload's plain json.loads unwrap works for it too.
        r = await client.call_tool("new_game", {})
        new_game_payload = _payload(r)
        assert new_game_payload["map_name"] == DEFAULT_MAP
        assert new_game_payload["my_race"] == "Terran"
        assert new_game_payload["opponent_race"] == "Zerg"
        assert new_game_payload["difficulty"] == "Easy"
        assert new_game_payload["realtime"] is False
        assert new_game_payload["previous_game_torn_down"] is True

        # -- the OLD game_task was actually cancelled and torn down, not
        # left running in the background. It's already done() by the time
        # new_game's tool call above returned (new_game awaits it, wrapped
        # in contextlib.suppress(CancelledError), before returning -- see
        # build_server's new_game) -- don't re-await it here, since
        # awaiting an already-cancelled Task re-raises CancelledError.
        assert first_game_task.done()
        assert first_game_task.cancelled()

        # -- execute_code calls after new_game are routed to the NEW game:
        # starting minerals are back at 50, proving this is a fresh game
        # and not still-live state from the first one --
        r = await client.call_tool("execute_code", {"code": "bot.observe().minerals"})
        payload = _payload(r)
        assert payload["ok"] is True
        assert payload["result"] == "50"

        # -- and session.game_task (a property proxying the live
        # _ActiveGame -- see ExecuteCodeSession's docstring) now points at
        # the new game, not the old, already-cancelled one --
        assert session.game_task is not first_game_task
        assert not session.game_task.done()

        # -- clean shutdown of the second game, same in-protocol
        # sdk.client.leave() pattern the first wiring test above uses --
        r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
        assert _payload(r)["ok"] is True

    result = await asyncio.wait_for(session.game_task, timeout=30)
    assert result == Result.Defeat


@pytest.mark.integration
def test_execute_code_snippet_timeout_and_retry(sc2_install):
    """Ticket #6 follow-up: a per-call timeout on `execute_code` snippet
    execution, with the user's own retry policy verbatim -- "Add the
    per-call timeout to a runaway snippet so we don't wedge the session,
    but always move a timed out call to the end of the list and retry it
    once. If it fails again make sure to mention it to the user so they
    know what failed." -- see `sdk/mcp_server.py`'s module docstring,
    "Per-call timeout and single automatic retry" section, for the full
    mechanism (`asyncio.wait_for` around `_eval_snippet` inside `on_step`,
    an attempt counter carried on the requeued `_PendingRequest`).

    This is the same kind of incident `test_new_game_recovers_a_snippet_
    that_never_returns` (above) exists for: a snippet with a genuine bug
    (`while scvs_needed > 0: ... else: await bot._advance(22)`, where the
    `else` was unreachable due to a missing supply-cap check) looped
    forever, each iteration doing a real `await`, and wedged `on_step` --
    and therefore the whole game -- permanently. `new_game` can recover
    *after the fact*, once someone notices the session is stuck and calls
    it; this timeout/retry is what's meant to catch it *as it happens*,
    automatically, without needing that manual intervention at all.

    Uses a short `timeout_seconds` per-call override (not the real
    ~tens-of-seconds server default -- see `DEFAULT_SNIPPET_TIMEOUT_SECONDS`'s
    reasoning for why the *default* is deliberately generous) so this test
    doesn't itself need to wait anywhere near that long -- the same
    "`await asyncio.sleep(10**9)` simulates 'never returns' cleanly, and a
    real `await` each iteration rather than a tight CPU spin" pattern
    `test_new_game_recovers_a_snippet_that_never_returns` above uses.

    Three scenarios share ONE real game/session (rather than three separate
    real-SC2-client boots, which would just multiply this test's real
    wall-clock cost for no additional coverage):

      1. **baseline** -- a fast snippet, well under the timeout, is
         unaffected -- a plain regression check that the timeout wiring
         doesn't get in the way of an ordinary call.
      2. **retry-then-succeed** -- a snippet that's slow on its first
         attempt and fast on the automatic retry succeeds, and the caller
         (this `call_tool` await) never sees a timeout error at all --
         only the final, successful result. "Slow on attempt 1, fast on
         attempt 2" is tracked via an `os.environ` counter the snippet
         itself increments: `_eval_snippet` execs the snippet's code with
         `{"bot": ..., "sdk": ...}` as its only globals (see
         `mcp_server.py`'s module docstring), so there's no channel for a
         bare `code: str` argument to signal "which attempt is this" other
         than mutating some process-wide state it can see -- and since the
         snippet runs in-process (no subprocess, no IPC -- see the module
         docstring's concurrency design section), `os.environ` works for
         exactly the same reason `bot`/`sdk` themselves are the live
         objects rather than a proxy: it's the same interpreter.
      3. **retry-then-fail** -- a snippet that's always slow exceeds the
         timeout on both the original attempt and the retry (confirmed via
         the same counter: exactly 2 attempts, not more) and comes back as
         a clear, structured `ok=False` result whose `error` names the
         timeout used and includes the snippet's own code verbatim --
         exactly what "mention it to the user so they know what failed"
         means in practice, since this `ExecuteCodeResult` is what
         surfaces straight back to the calling agent.
    """
    asyncio.run(_run_snippet_timeout_and_retry())


async def _run_snippet_timeout_and_retry() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    # Short enough that a snippet timing out twice (original + retry) still
    # only costs this test a couple of real seconds, not tens of seconds --
    # this is a per-call override (execute_code's timeout_seconds argument),
    # not the real server default, which is deliberately much more generous
    # (see DEFAULT_SNIPPET_TIMEOUT_SECONDS's reasoning in mcp_server.py).
    _TIMEOUT = 1.0
    _COUNTER_ENV_VAR = "SC2_SDK_TEST_EXECUTE_CODE_ATTEMPT_COUNT"

    async with create_connected_server_and_client_session(session.mcp) as client:
        try:
            # -- 1. baseline: a fast snippet, well under the timeout, is
            # unaffected --
            r = await client.call_tool("execute_code", {"code": "1 + 1", "timeout_seconds": _TIMEOUT})
            payload = _payload(r)
            assert payload["ok"] is True
            assert payload["result"] == "2"

            # -- 2. retry-then-succeed: slow (sleeps effectively forever)
            # on attempt 1, fast on attempt 2 -- see this test's own
            # docstring for why an os.environ counter is the signaling
            # channel here.
            os.environ[_COUNTER_ENV_VAR] = "0"
            code_retry_then_succeed = (
                "import asyncio, os\n"
                f"n = int(os.environ.get({_COUNTER_ENV_VAR!r}, '0')) + 1\n"
                f"os.environ[{_COUNTER_ENV_VAR!r}] = str(n)\n"
                "if n == 1:\n"
                "    await asyncio.sleep(10**9)\n"
                "n\n"
            )
            r = await asyncio.wait_for(
                client.call_tool(
                    "execute_code", {"code": code_retry_then_succeed, "timeout_seconds": _TIMEOUT}
                ),
                timeout=30,
            )
            payload = _payload(r)
            assert payload["ok"] is True
            # result == "2": the snippet's OWN attempt counter reached 2
            # before it returned successfully -- direct proof the retry
            # actually ran, not just that some result came back ok.
            assert payload["result"] == "2"
            assert os.environ[_COUNTER_ENV_VAR] == "2"

            # -- 3. retry-then-fail: always slow, so it exceeds the
            # timeout on both the original attempt and the automatic
            # retry -- exactly 2 attempts total, not more -- and comes
            # back as a clear, structured failure naming the timeout and
            # including the snippet's own code.
            os.environ[_COUNTER_ENV_VAR] = "0"
            code_always_slow = (
                "import asyncio, os\n"
                f"n = int(os.environ.get({_COUNTER_ENV_VAR!r}, '0')) + 1\n"
                f"os.environ[{_COUNTER_ENV_VAR!r}] = str(n)\n"
                "await asyncio.sleep(10**9)\n"
            )
            r = await asyncio.wait_for(
                client.call_tool("execute_code", {"code": code_always_slow, "timeout_seconds": _TIMEOUT}),
                timeout=30,
            )
            payload = _payload(r)
            assert payload["ok"] is False
            assert payload["error"] is not None
            assert "timed out" in payload["error"]
            assert "original attempt and one automatic retry" in payload["error"]
            assert f"{_TIMEOUT:g}s" in payload["error"]
            assert code_always_slow in payload["error"]  # the failing snippet's own code, verbatim
            # Exactly 2 attempts (original + 1 retry) -- not a third,
            # unbounded retry.
            assert os.environ[_COUNTER_ENV_VAR] == "2"
        finally:
            os.environ.pop(_COUNTER_ENV_VAR, None)

        # -- clean shutdown, same in-protocol sdk.client.leave() pattern
        # every other test in this file uses --
        r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
        assert _payload(r)["ok"] is True

    result = await asyncio.wait_for(session.game_task, timeout=30)
    assert result == Result.Defeat


@pytest.mark.integration
def test_execute_code_timeout_retry_moves_to_end_of_queue(sc2_install):
    """Ticket #6 follow-up, the "always move a timed out call to the end
    of the list" half of the retry policy specifically (see
    `test_execute_code_snippet_timeout_and_retry` above for the rest):
    proves a call queued *behind* a call that's about to time out gets
    processed before the timed-out call's retry, not after it -- i.e. the
    retry is genuinely `await self._queue.put(request)`-ed onto the END of
    `self._queue`, not re-run immediately in place ahead of anything
    already waiting its turn.
    """
    asyncio.run(_run_timeout_retry_queue_ordering())


async def _run_timeout_retry_queue_ordering() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )
    _TIMEOUT = 1.0

    async with create_connected_server_and_client_session(session.mcp) as client:
        # Submit the soon-to-time-out call first, as a background task --
        # same "don't await it directly, it's the stuck one under test"
        # shape test_new_game_recovers_a_snippet_that_never_returns above
        # uses.
        wedged_call = asyncio.create_task(
            client.call_tool(
                "execute_code",
                {"code": "import asyncio\nawait asyncio.sleep(10**9)", "timeout_seconds": _TIMEOUT},
            )
        )

        # Give it a moment to actually be the thing on_step is awaiting
        # (not still sitting in the queue) before queuing a second call
        # behind it -- same reasoning as the wedged-snippet recovery test.
        await asyncio.sleep(0.3)
        assert not wedged_call.done(), "the wedged snippet call finished on its own -- test setup is wrong"

        queued_call = asyncio.create_task(client.call_tool("execute_code", {"code": "'queued'"}))

        # wedged_call's first attempt times out at ~1.0s and gets requeued
        # onto the END of the queue -- behind queued_call, which was
        # already waiting there -- so queued_call should resolve well
        # before wedged_call's retry (attempt 2) has had its own ~1.0s
        # chance to time out.
        queued_result = await asyncio.wait_for(queued_call, timeout=10)
        assert _payload(queued_result)["result"] == "'queued'"
        assert not wedged_call.done(), (
            "wedged_call resolved before (or at the same time as) the call queued behind it -- "
            "expected the queued call to be processed first, proving a timed-out request is "
            "moved to the end of the queue rather than retried immediately ahead of anything "
            "already waiting."
        )

        wedged_result = await asyncio.wait_for(wedged_call, timeout=10)
        assert _payload(wedged_result)["ok"] is False

        r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
        assert _payload(r)["ok"] is True

    result = await asyncio.wait_for(session.game_task, timeout=30)
    assert result == Result.Defeat


@pytest.mark.integration
def test_new_game_recovers_a_snippet_that_never_returns(sc2_install):
    """Ticket #6 follow-up: the other half of `new_game`'s job -- recovering
    a session permanently wedged inside a runaway `execute_code` snippet
    that never returns. `on_step` awaits `_eval_snippet`, which awaits the
    snippet's own coroutine directly (see `mcp_server.py`'s module
    docstring) -- `await asyncio.sleep(10**9)` simulates "never returns"
    cleanly (a real `await` each "iteration", not a tight CPU spin) without
    the test itself needing to wait anywhere near that long, since
    `new_game`'s `game_task.cancel()` is what's actually expected to unwind
    it, not the sleep ever completing on its own.

    Before this ticket, the only recovery from this state was killing the
    whole `sc2-sdk-mcp` process; this test's whole point is proving
    `new_game` recovers it instead, while the connection stays up.
    """
    asyncio.run(_run_new_game_recovers_wedged_snippet())


async def _run_new_game_recovers_wedged_snippet() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )
    first_game_task = session.game_task

    async with create_connected_server_and_client_session(session.mcp) as client:
        # Submit a snippet that will never return, and deliberately do NOT
        # await this call directly -- it's the "wedged forever" call under
        # test, and awaiting it here would just hang the test too. Instead
        # fire it as a background task so this coroutine can move on to
        # calling new_game while it's still stuck.
        wedged_call = asyncio.create_task(
            client.call_tool("execute_code", {"code": "import asyncio\nawait asyncio.sleep(10**9)"})
        )

        # Give the wedged snippet a moment to actually be the thing on_step
        # is awaiting (not still sitting in the queue) before we try to
        # recover from it -- on_step's queue.get() -> _eval_snippet ->
        # await asyncio.sleep(...) chain needs at least one event-loop
        # round trip to get there.
        await asyncio.sleep(1)
        assert not wedged_call.done(), "the wedged snippet call finished on its own -- test setup is wrong"

        # -- the actual recovery under test: new_game while the previous
        # game is wedged forever inside that snippet --
        r = await asyncio.wait_for(client.call_tool("new_game", {}), timeout=60)
        assert not r.isError, r
        new_game_payload = json.loads(r.content[0].text)
        assert new_game_payload["previous_game_torn_down"] is True

        # -- the old game_task is now cancelled (this is what unwound the
        # stuck asyncio.sleep via CancelledError propagation) --
        assert first_game_task.cancelled()

        # -- the wedged call itself resolves too (submit() races its own
        # future against game_task -- see ExecuteCodeBotAI.submit's
        # docstring) instead of hanging forever now that game_task is done --
        wedged_result = await asyncio.wait_for(wedged_call, timeout=30)
        wedged_payload = _payload(wedged_result)
        assert wedged_payload["ok"] is False
        assert "Match ended" in wedged_payload["error"]

        # -- and the new game actually works: execute_code is routed to it,
        # proving the session recovered rather than merely not crashing --
        r = await client.call_tool("execute_code", {"code": "bot.observe().minerals"})
        payload = _payload(r)
        assert payload["ok"] is True
        assert payload["result"] == "50"

        r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
        assert _payload(r)["ok"] is True

    result = await asyncio.wait_for(session.game_task, timeout=30)
    assert result == Result.Defeat


@pytest.mark.integration
def test_start_task_runs_in_background_without_blocking_other_calls(sc2_install):
    """Covers the standing-background-task mechanism's core promises (see
    `sdk/mcp_server.py`'s module docstring, "Standing background tasks"
    section) that don't need real bot/sdk game state to demonstrate --
    `test_start_task_supply_depot_scenario_completes` below covers the
    "actually drives real game state to a goal" half separately, using the
    user's own worked example. Four scenarios share ONE real game/session
    (same economizing as `test_execute_code_snippet_timeout_and_retry`
    above), since none of them need to observe/mutate real game state:

      1. **`start_task` returns immediately.** Its step code sleeps a
         fixed, real amount of wall-clock time per turn and needs several
         turns to finish -- `start_task` itself must return in a small
         fraction of that total, proving it doesn't wait for the task (or
         even its first turn) to run at all.
      2. **An ordinary `execute_code` call submitted right after stays
         responsive.** It must resolve well before the background task's
         total runtime -- proving the shared `_queue`/`on_step` loop is
         interleaving fairly (FIFO), not starving ordinary calls behind an
         entire in-progress task.
      3. **`task_status` reflects incremental progress while running.**
         Polled repeatedly, its `iterations` count strictly increases and
         its `log` grows before the task reaches `"done"` -- not just a
         single jump from 0 to final. `task_status()` with no `task_id`
         also lists this task, covering the "list all known tasks"
         nice-to-have.
      4. **A task whose step code always raises is marked `"failed"`
         immediately** (this project's chosen behavior for a plain
         exception -- see `_finish_task_turn`'s docstring -- as opposed to
         the timeout/retry path, which only applies to a turn that never
         returns at all), and **`cancel_task` stops further turns** from
         being scheduled for an otherwise-endless task, without needing to
         interrupt whichever single turn is already in flight or queued
         when the cancellation is requested.
    """
    asyncio.run(_run_start_task_background_and_responsive())


async def _run_start_task_background_and_responsive() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    # A real (small) per-turn wall-clock delay, not real game/engine timing
    # -- deterministic and environment-independent, unlike relying on how
    # fast the actual SC2 client happens to process a stepped simulation
    # tick on whatever machine this test runs on.
    _PER_TURN_SLEEP = 0.3
    _TURNS_TO_COMPLETE = 4
    _TOTAL_TASK_SECONDS = _PER_TURN_SLEEP * _TURNS_TO_COMPLETE

    async def _poll_until_finished(client, task_id: str, *, max_polls: int = 100, poll_interval: float = 0.05):
        status = None
        iterations_seen: set[int] = set()
        for _ in range(max_polls):
            r = await client.call_tool("task_status", {"task_id": task_id})
            status = _payload(r)
            assert status["ok"] is True, status
            iterations_seen.add(status["iterations"])
            if status["status"] != "running":
                break
            await asyncio.sleep(poll_interval)
        return status, iterations_seen

    _COUNTER_ENV_VAR = "SC2_SDK_TEST_START_TASK_TURN_COUNT"

    async with create_connected_server_and_client_session(session.mcp) as client:
        try:
            # -- 1 & 2: start_task returns immediately, and an ordinary
            # execute_code call submitted right after isn't stuck waiting
            # behind the whole task -- each turn sleeps a fixed, real
            # amount of wall-clock time, then reports "done" once it's
            # been run exactly _TURNS_TO_COMPLETE times. Turn count is
            # tracked via an os.environ counter (the same pattern
            # test_execute_code_snippet_timeout_and_retry above uses)
            # rather than a wall-clock deadline computed up front: each
            # turn's own `_eval_snippet` call, `on_step`'s queue
            # scheduling, and the real game's own step round trip between
            # turns all add real overhead on top of this task's own
            # `asyncio.sleep`, so a deadline set before the first turn
            # even starts can't reliably predict which turn will be
            # running once wall-clock time catches up to it -- only an
            # explicit, turn-incrementing counter guarantees exactly
            # _TURNS_TO_COMPLETE turns run, regardless of how much
            # overhead each turn's round trip actually takes.
            os.environ[_COUNTER_ENV_VAR] = "0"
            task_code = (
                "import asyncio, os\n"
                f"await asyncio.sleep({_PER_TURN_SLEEP})\n"
                f"n = int(os.environ.get({_COUNTER_ENV_VAR!r}, '0')) + 1\n"
                f"os.environ[{_COUNTER_ENV_VAR!r}] = str(n)\n"
                f"return n >= {_TURNS_TO_COMPLETE}\n"
            )

            t0 = time.monotonic()
            r = await client.call_tool(
                "start_task",
                {
                    "code": task_code,
                    "description": f"sleep {_PER_TURN_SLEEP}s per turn, {_TURNS_TO_COMPLETE} turns",
                },
            )
            start_task_elapsed = time.monotonic() - t0
            payload = _payload(r)
            task_id = payload["task_id"]
            assert isinstance(task_id, str) and task_id

            assert start_task_elapsed < _TOTAL_TASK_SECONDS / 2, (
                f"start_task took {start_task_elapsed:.3f}s to return -- expected it to return "
                f"almost immediately, well under the ~{_TOTAL_TASK_SECONDS:.1f}s the whole "
                "background task needs to run all its turns, since start_task must not block "
                "waiting for the task (or even its first turn) to run."
            )

            t1 = time.monotonic()
            r = await client.call_tool("execute_code", {"code": "1 + 1"})
            ordinary_call_elapsed = time.monotonic() - t1
            assert _payload(r)["result"] == "2"
            assert ordinary_call_elapsed < _TOTAL_TASK_SECONDS, (
                f"an ordinary execute_code call submitted right after start_task took "
                f"{ordinary_call_elapsed:.3f}s -- expected it to be serviced well before the "
                f"background task's total ~{_TOTAL_TASK_SECONDS:.1f}s runtime, proving it "
                "wasn't stuck waiting behind the whole task rather than just interleaved "
                "fairly with it."
            )

            # -- 3: task_status reflects incremental progress while running --
            status, iterations_seen = await _poll_until_finished(client, task_id)
            assert status["status"] == "done", status
            assert status["result"] == "True"
            assert status["iterations"] == _TURNS_TO_COMPLETE
            assert len(iterations_seen) > 1, (
                "expected to observe the iteration count increase across multiple polls "
                f"while the task was still running, not just jump straight to its final "
                f"value; saw {iterations_seen}"
            )
            assert len(status["log"]) == _TURNS_TO_COMPLETE
            assert all(f"[turn {i}]" in status["log"][i - 1] for i in range(1, _TURNS_TO_COMPLETE + 1))

            # -- task_status() with no task_id lists every known task --
            r = await client.call_tool("task_status", {})
            listing = _payload(r)
            assert any(t["task_id"] == task_id for t in listing["tasks"])

            # -- 4a: a task whose step code always raises is marked
            # "failed" immediately (one turn, not retried -- a plain
            # exception is not the timeout/hang case the retry machinery
            # exists for) --
            r = await client.call_tool(
                "start_task",
                {"code": "raise ValueError('deliberate failure for this test')", "description": "always errors"},
            )
            erroring_task_id = _payload(r)["task_id"]
            erroring_status, _ = await _poll_until_finished(client, erroring_task_id)
            assert erroring_status["status"] == "failed", erroring_status
            assert erroring_status["iterations"] == 1
            assert erroring_status["error"] is not None
            assert "ValueError" in erroring_status["error"]
            assert "deliberate failure for this test" in erroring_status["error"]

            # -- 4b: cancel_task stops further turns from being scheduled --
            r = await client.call_tool(
                "start_task", {"code": "return False", "description": "never completes on its own"}
            )
            endless_task_id = _payload(r)["task_id"]

            # Let a handful of turns actually run (this step code does no
            # awaiting at all, so turns can complete very quickly -- give
            # it a brief, real head start) before requesting cancellation.
            await asyncio.sleep(0.2)
            r = await client.call_tool("cancel_task", {"task_id": endless_task_id})
            cancel_payload = _payload(r)
            assert cancel_payload["ok"] is True
            assert cancel_payload["task_id"] == endless_task_id

            cancelled_status, _ = await _poll_until_finished(client, endless_task_id)
            assert cancelled_status["status"] == "cancelled", cancelled_status
            iterations_at_cancellation = cancelled_status["iterations"]

            # -- no further turns get scheduled: waiting longer doesn't
            # grow the iteration count any further --
            await asyncio.sleep(0.3)
            r = await client.call_tool("task_status", {"task_id": endless_task_id})
            status_later = _payload(r)
            assert status_later["status"] == "cancelled"
            assert status_later["iterations"] == iterations_at_cancellation, (
                "iteration count grew after cancel_task -- a further turn was scheduled "
                "despite the task having been cancelled."
            )

            # -- cancelling an already-finished task reports a clear
            # failure, not a silent no-op --
            r = await client.call_tool("cancel_task", {"task_id": endless_task_id})
            assert _payload(r)["ok"] is False

            # -- an unknown task_id is reported clearly by both tools --
            r = await client.call_tool("task_status", {"task_id": "not-a-real-task-id"})
            assert _payload(r)["ok"] is False
            r = await client.call_tool("cancel_task", {"task_id": "not-a-real-task-id"})
            assert _payload(r)["ok"] is False
        finally:
            os.environ.pop(_COUNTER_ENV_VAR, None)

        r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
        assert _payload(r)["ok"] is True

    result = await asyncio.wait_for(session.game_task, timeout=30)
    assert result == Result.Defeat


@pytest.mark.integration
def test_start_task_supply_depot_scenario_completes(sc2_install):
    """The user's own motivating scenario, end to end: "have an SCV create
    Supply Depots until we have 30" -- exercised here with a small target
    (2, not 30 -- see this project's testing brief: keep the real
    end-to-end test's runtime reasonable) so the test itself stays fast
    while still proving the whole mechanism against real, live game state,
    not a simulated stand-in for it.

    Each turn: if we already have enough depots, report done (`True`);
    otherwise cheat resources (`debug_all_resources`, the same pattern
    `test_verified_bot_actions.py` and this module's own new_game tests
    already use for fast, deterministic tests that don't want to wait on
    real income) and start one more depot at a placement offset by however
    many depots already exist (so successive depots don't collide), then
    report "keep going" (`False`). See `sdk/mcp_server.py`'s module
    docstring, "Standing background tasks" section, for this exact
    scenario written out as `start_task`'s worked example.
    """
    asyncio.run(_run_start_task_supply_depot_scenario())


async def _run_start_task_supply_depot_scenario() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )
    _TARGET_DEPOTS = 2

    task_code = (
        "from sc2.ids.unit_typeid import UnitTypeId\n"
        "depot_count = len(sdk.structures(UnitTypeId.SUPPLYDEPOT))\n"
        f"if depot_count >= {_TARGET_DEPOTS}:\n"
        "    return True\n"
        "await sdk.client.debug_all_resources()\n"
        "depot_point = sdk.townhalls.first.position.towards(sdk.game_info.map_center, 6)\n"
        "depot_point = depot_point.offset((depot_count * 3, 0))\n"
        "await bot.build(UnitTypeId.SUPPLYDEPOT, near=depot_point)\n"
        "return False\n"
    )

    async with create_connected_server_and_client_session(session.mcp) as client:
        r = await client.call_tool(
            "start_task",
            {
                "code": task_code,
                "description": f"SCVs build Supply Depots until we have {_TARGET_DEPOTS}",
                "max_iterations": 30,
            },
        )
        task_id = _payload(r)["task_id"]

        status = None
        for _ in range(200):
            r = await client.call_tool("task_status", {"task_id": task_id})
            status = _payload(r)
            assert status["ok"] is True, status
            if status["status"] != "running":
                break
            await asyncio.sleep(0.25)

        assert status["status"] == "done", status
        assert status["result"] == "True"
        assert status["iterations"] >= _TARGET_DEPOTS

        # -- confirm this reflects genuinely live game state, not just the
        # task's own internal bookkeeping --
        r = await client.call_tool(
            "execute_code",
            {"code": "from sc2.ids.unit_typeid import UnitTypeId\nlen(sdk.structures(UnitTypeId.SUPPLYDEPOT))"},
        )
        payload = _payload(r)
        assert payload["ok"] is True
        assert int(payload["result"]) >= _TARGET_DEPOTS

        r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
        assert _payload(r)["ok"] is True

    result = await asyncio.wait_for(session.game_task, timeout=30)
    assert result == Result.Defeat


@pytest.mark.integration
def test_task_does_not_survive_new_game(sc2_install):
    """Ticket follow-up to the standing-background-task mechanism: a task
    belongs to whichever `ExecuteCodeBotAI` was active when `start_task`
    registered it (see `sdk/mcp_server.py`'s `start_task`/`new_game`
    docstrings, "Lifecycle" sections) -- once `new_game` replaces that
    game with a fresh one, the task's `task_id` becomes unresolvable,
    exactly like a `task_id` that was never registered would be, with no
    special cleanup required. This is a real, exercised confirmation of
    that claim, not just a read of the source -- see the module docstring
    for why the underlying mechanism (the old `ExecuteCodeBotAI`, and
    therefore its `_tasks` dict, simply becomes unreachable once
    `active.bot_ai` is reassigned) requires no special-casing beyond what
    `new_game` already does for an ordinary in-flight `execute_code` call.
    """
    asyncio.run(_run_task_new_game_lifecycle())


async def _run_task_new_game_lifecycle() -> None:
    session = await serve_execute_code(
        map_name=DEFAULT_MAP,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    async with create_connected_server_and_client_session(session.mcp) as client:
        r = await client.call_tool(
            "start_task", {"code": "return False", "description": "belongs to the first game only"}
        )
        task_id = _payload(r)["task_id"]

        # -- sanity: it's known right now, against the first game --
        r = await client.call_tool("task_status", {"task_id": task_id})
        assert _payload(r)["ok"] is True

        r = await client.call_tool("new_game", {})
        assert _payload(r)["previous_game_torn_down"] is True

        # -- the task belonged to the OLD game; it does not survive new_game --
        r = await client.call_tool("task_status", {"task_id": task_id})
        after_new_game = _payload(r)
        assert after_new_game["ok"] is False
        assert "Unknown task_id" in after_new_game["error"]

        r = await client.call_tool("cancel_task", {"task_id": task_id})
        assert _payload(r)["ok"] is False

        r = await client.call_tool("execute_code", {"code": "await sdk.client.leave()"})
        assert _payload(r)["ok"] is True

    result = await asyncio.wait_for(session.game_task, timeout=30)
    assert result == Result.Defeat
