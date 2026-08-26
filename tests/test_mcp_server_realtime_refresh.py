"""Fast, non-integration tests for ticket #21 (realtime state refresh on
`execute_code`/`start_task` dequeue) -- `ExecuteCodeBotAI.on_step`
(`sdk/mcp_server.py`), right after `request = await self._queue.get()` and
before `_eval_snippet` runs.

Why these don't reconstruct the real staleness effect
--------------------------------------------------------
Actually observing state visibly lagging by real wall-clock seconds needs a
live SC2 process free-running in realtime mode -- slow, and not something
worth doing twice given `tests/integration/test_execute_code_mcp.py` already
pays that cost for this module's other `on_step` machinery. See
`tests/integration/test_execute_code_mcp.py::test_realtime_refresh_reflects_wait_time_between_calls`
for the one real, end-to-end confirmation that the refresh actually closes
the gap against a live game.

What these tests cover instead, against a bare `ExecuteCodeBotAI` instance
with `on_start`'s bot/sdk wiring run but no real game underneath (`_eval_snippet`
snippets below touch neither `bot` nor `sdk`, so no live client is needed):

  1. The refresh is invoked when `self.realtime` is True, and skipped
     entirely when it's False -- non-realtime mode must see zero behavior
     change (see ticket #21's "Design decisions already made"). Observed via
     monkeypatching `sdk.mcp_server.refresh_realtime_state` (the name
     `on_step` actually calls, imported from `sdk.bot`) rather than a slow
     real wait.
  2. A refresh that times out logs a warning via this module's existing
     `loguru` `logger` and does NOT fail the call -- it proceeds into
     `_eval_snippet` with an `ok=True` result, exactly "today's pre-fix
     behavior for that one call" per the ticket's safety requirement. Faked
     by monkeypatching `refresh_realtime_state` to hang past its timeout
     (the same `asyncio.sleep(10**9)` "never returns, but cancels cleanly"
     pattern `test_execute_code_mcp.py` already uses for the snippet-timeout
     tests) and shrinking `REALTIME_STATE_REFRESH_TIMEOUT_SECONDS` so the
     test itself doesn't need to wait out the real default.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

import sdk.mcp_server as mcp_server
from sdk.mcp_server import ExecuteCodeBotAI, _PendingRequest


async def _make_bot_ai(*, realtime: bool) -> ExecuteCodeBotAI:
    """A bare ExecuteCodeBotAI with on_start's bot/sdk wiring done (so
    on_step's `{"bot": self.bot, "sdk": self.sdk}` globals exist) but no
    real game underneath -- fine for these tests since none of the snippets
    below touch `bot`/`sdk`."""
    ai = ExecuteCodeBotAI()
    await ai.on_start()
    ai.realtime = realtime  # normally set by python-sc2's own _prepare_start
    return ai


async def _run_one_call(ai: ExecuteCodeBotAI, code: str = "1 + 1"):
    future: "asyncio.Future" = asyncio.get_running_loop().create_future()
    await ai._queue.put(_PendingRequest(code=code, future=future, timeout_seconds=5.0))
    await ai.on_step(0)
    return await future


def test_realtime_refresh_is_invoked_when_game_is_realtime() -> None:
    asyncio.run(_run_realtime_refresh_is_invoked_when_game_is_realtime())


async def _run_realtime_refresh_is_invoked_when_game_is_realtime() -> None:
    calls: list[object] = []

    async def _fake_refresh(ai) -> None:
        calls.append(ai)

    ai = await _make_bot_ai(realtime=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp_server, "refresh_realtime_state", _fake_refresh)
        result = await _run_one_call(ai)

    assert len(calls) == 1, "refresh_realtime_state should be called exactly once per dequeued request"
    assert calls[0] is ai
    assert result.ok is True
    assert result.result == "2"


def test_realtime_refresh_is_skipped_when_game_is_not_realtime() -> None:
    asyncio.run(_run_realtime_refresh_is_skipped_when_game_is_not_realtime())


async def _run_realtime_refresh_is_skipped_when_game_is_not_realtime() -> None:
    calls: list[object] = []

    async def _fake_refresh(ai) -> None:
        calls.append(ai)

    ai = await _make_bot_ai(realtime=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp_server, "refresh_realtime_state", _fake_refresh)
        result = await _run_one_call(ai)

    assert calls == [], (
        "refresh_realtime_state must NOT be called in non-realtime mode -- ticket #21 requires "
        "zero behavior change there (nothing else advances the engine between calls, so state "
        "at on_step entry is already fully fresh)."
    )
    assert result.ok is True
    assert result.result == "2"


def test_realtime_refresh_timeout_logs_warning_and_does_not_fail_the_call() -> None:
    asyncio.run(_run_realtime_refresh_timeout_logs_warning_and_does_not_fail_the_call())


async def _run_realtime_refresh_timeout_logs_warning_and_does_not_fail_the_call() -> None:
    async def _hanging_refresh(ai) -> None:
        # Never returns on its own -- same "real await each iteration, cancels
        # cleanly" pattern test_execute_code_mcp.py's snippet-timeout tests use
        # for asyncio.wait_for to actually exercise cancellation against.
        await asyncio.sleep(10**9)

    warnings: list[str] = []
    sink_id = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")

    ai = await _make_bot_ai(realtime=True)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mcp_server, "refresh_realtime_state", _hanging_refresh)
            # Shrink the refresh timeout so this test doesn't wait out the
            # real ~2s default -- on_step reads this module attribute at
            # call time (an unqualified global lookup), so patching it here
            # affects the call inside on_step below.
            mp.setattr(mcp_server, "REALTIME_STATE_REFRESH_TIMEOUT_SECONDS", 0.05)
            result = await asyncio.wait_for(_run_one_call(ai), timeout=10)
    finally:
        logger.remove(sink_id)

    # The refresh's own timeout must NOT fail the caller's call -- it
    # degrades gracefully, proceeding into _eval_snippet with whatever state
    # is currently cached (here, the bare freshly-constructed instance).
    assert result.ok is True
    assert result.result == "2"

    assert any("realtime state refresh timed out" in w for w in warnings), (
        f"expected a warning about the refresh timeout to be logged via loguru's logger; got {warnings!r}"
    )
