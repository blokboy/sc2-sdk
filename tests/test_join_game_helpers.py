"""Fast, non-integration tests for `sdk.mcp_server`'s join-side helpers
(`_await_connected_or_failure`) -- see that module's "Joining a two-player
match" section for the full design. These exercise the pure state-derivation/
blocking-wait logic directly against fake `bot_ai`/`game_task` stand-ins (no
real SC2 client, no real `_run_join_role` scheduling) -- the real end-to-end
`join_game`/`sc2-sdk-host` flow is covered separately by
`tests/integration/test_join_game_mcp.py`, which needs a real local SC2
install and is therefore skipped where one isn't available.

A real `asyncio.Future` (not a `Task`) stands in for `game_task` in every
test below -- see `tests/test_host_status.py`'s module docstring for why
that's a safe, synchronous-cancellation-friendly stand-in for the
`.done()`/`.exception()` reads `_await_connected_or_failure` performs.
"""

from __future__ import annotations

import asyncio

import pytest

from sdk.mcp_server import _await_connected_or_failure


class _FakeBotAI:
    """The one attribute `_await_connected_or_failure` actually reads off
    `bot_ai` -- see `ExecuteCodeBotAI.ready` -- without needing to construct
    a real `ExecuteCodeBotAI`."""

    def __init__(self) -> None:
        self.ready: asyncio.Event = asyncio.Event()


def test_returns_once_ready_is_set_even_if_game_task_is_still_running() -> None:
    asyncio.run(_check_returns_when_ready())


async def _check_returns_when_ready() -> None:
    bot_ai = _FakeBotAI()
    bot_ai.ready.set()  # already connected -- e.g. the join succeeded before this was even awaited
    game_task = asyncio.get_running_loop().create_future()  # match still ongoing
    await asyncio.wait_for(_await_connected_or_failure(bot_ai, game_task), timeout=5)
    assert not game_task.done()  # left running -- this helper must not touch it on the happy path


def test_raises_game_tasks_own_exception_when_it_finishes_before_ready() -> None:
    asyncio.run(_check_raises_game_task_exception())


async def _check_raises_game_task_exception() -> None:
    bot_ai = _FakeBotAI()
    game_task = asyncio.get_running_loop().create_future()
    game_task.set_exception(RuntimeError("standing in for JoinTimeoutError"))
    with pytest.raises(RuntimeError, match="standing in for JoinTimeoutError"):
        await asyncio.wait_for(_await_connected_or_failure(bot_ai, game_task), timeout=5)


def test_raises_generic_error_if_game_task_finishes_without_exception_or_ready() -> None:
    asyncio.run(_check_raises_generic_error())


async def _check_raises_generic_error() -> None:
    # Edge case: the connection coroutine returned a Result without ever
    # setting `ready` (on_start never fired) -- shouldn't happen in
    # practice, but _await_connected_or_failure must not hang or crash
    # confusingly if it somehow did.
    bot_ai = _FakeBotAI()
    game_task = asyncio.get_running_loop().create_future()
    game_task.set_result("some-result-standing-in-for-a-real-Result")
    with pytest.raises(RuntimeError, match="connection ended before the game started"):
        await asyncio.wait_for(_await_connected_or_failure(bot_ai, game_task), timeout=5)
