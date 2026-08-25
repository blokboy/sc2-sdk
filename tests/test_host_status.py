"""Fast, non-integration tests for `sdk.mcp_server`'s hosted-game status
derivation (`_host_status`) -- see that module's "Hosting a two-player
match" section for the full design. These exercise `_host_status`'s pure
state-derivation logic directly against fake `bot_ai`/`game_task`
stand-ins (no real SC2 client, no real `_run_host_role` scheduling) --
the real end-to-end `host_game`/`host_status`/`sc2-sdk-join` flow is
covered separately by `tests/integration/test_host_game_mcp.py`, which
needs a real local SC2 install and is therefore skipped where one isn't
available.

A bare `asyncio.Future` (not a `Task`) stands in for `game_task` in every
test below: `_host_status` only ever calls `.done()`/`.cancelled()`/
`.exception()` on it, all of which a plain `Future` supports identically,
and (unlike `Task.cancel()`, which only *requests* cancellation for the
next event-loop iteration to deliver) `Future.cancel()` takes effect
synchronously -- convenient for a test that wants a definitively-cancelled
object without actually running a coroutine through cancellation.
"""

from __future__ import annotations

import asyncio

from sdk.mcp_server import _HostGameState, _host_status


class _FakeBotAI:
    """The two attributes `_host_status` actually reads off `bot_ai` --
    see `ExecuteCodeBotAI.host_state`/`.ready` -- without needing to
    construct a real `ExecuteCodeBotAI` (and therefore a real `BotAI`)."""

    def __init__(self, *, host_state: "_HostGameState | None", ready: bool = False) -> None:
        self.host_state = host_state
        self.ready = asyncio.Event()
        if ready:
            self.ready.set()


def test_host_status_no_hosted_game() -> None:
    asyncio.run(_check_no_hosted_game())


async def _check_no_hosted_game() -> None:
    bot_ai = _FakeBotAI(host_state=None)
    game_task = asyncio.get_running_loop().create_future()
    assert _host_status(bot_ai, game_task) == {
        "ok": False,
        "error": "No hosted game is active on the current game session. Call host_game first.",
    }


def test_host_status_waiting_before_ready_and_before_game_task_done() -> None:
    asyncio.run(_check_waiting())


async def _check_waiting() -> None:
    state = _HostGameState(match_code="fake-code")
    bot_ai = _FakeBotAI(host_state=state, ready=False)
    game_task = asyncio.get_running_loop().create_future()  # left pending -- no peer yet
    assert _host_status(bot_ai, game_task) == {
        "ok": True,
        "match_code": "fake-code",
        "status": "waiting",
        "error": None,
    }


def test_host_status_joined_once_ready_is_set() -> None:
    asyncio.run(_check_joined())


async def _check_joined() -> None:
    state = _HostGameState(match_code="fake-code")
    bot_ai = _FakeBotAI(host_state=state, ready=True)
    # Still pending -- the match is ongoing, not the game_task finishing,
    # that flips status to "joined" (see _host_status's docstring: ready
    # is checked before game_task.done()).
    game_task = asyncio.get_running_loop().create_future()
    assert _host_status(bot_ai, game_task) == {
        "ok": True,
        "match_code": "fake-code",
        "status": "joined",
        "error": None,
    }


def test_host_status_reports_join_timeout_distinctly() -> None:
    asyncio.run(_check_timed_out())


async def _check_timed_out() -> None:
    # Mirrors what _launch_hosted_game's own task wrapper actually does on
    # a JoinTimeoutError: mark host_state.timed_out, then let the
    # exception propagate out of the task.
    state = _HostGameState(match_code="fake-code", timed_out=True)
    bot_ai = _FakeBotAI(host_state=state, ready=False)
    game_task = asyncio.get_running_loop().create_future()
    game_task.set_exception(RuntimeError("standing in for JoinTimeoutError"))
    assert _host_status(bot_ai, game_task) == {
        "ok": True,
        "match_code": "fake-code",
        "status": "failed",
        "error": "No peer joined within the configured join_timeout.",
    }


def test_host_status_reports_cancellation_distinctly() -> None:
    asyncio.run(_check_cancelled())


async def _check_cancelled() -> None:
    # e.g. new_game (or another host_game) tearing this hosted game down
    # via _teardown_active_game before a peer ever joined.
    state = _HostGameState(match_code="fake-code")
    bot_ai = _FakeBotAI(host_state=state, ready=False)
    game_task = asyncio.get_running_loop().create_future()
    game_task.cancel()
    assert _host_status(bot_ai, game_task) == {
        "ok": True,
        "match_code": "fake-code",
        "status": "failed",
        "error": "The hosted game was cancelled (e.g. by a new_game call) before a peer joined.",
    }


def test_host_status_reports_other_failures_generically() -> None:
    asyncio.run(_check_other_failure())


async def _check_other_failure() -> None:
    state = _HostGameState(match_code="fake-code")
    bot_ai = _FakeBotAI(host_state=state, ready=False)
    game_task = asyncio.get_running_loop().create_future()
    game_task.set_exception(ValueError("something else went wrong"))
    result = _host_status(bot_ai, game_task)
    assert result["ok"] is True
    assert result["status"] == "failed"
    assert result["error"] == "ValueError: something else went wrong"
