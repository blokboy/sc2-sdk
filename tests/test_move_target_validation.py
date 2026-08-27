"""Regression tests for the invalid-move-target match-killer.

A malformed `move`/`attack_move` target (`bot.move(units, (145, 114))` --
a bare tuple where a `Point2` was required) used to strand an
unserializable command in python-sc2's pending action queue. python-sc2's
`_after_step` only clears `ai.actions` *after* `_do_actions` succeeds::

    if self.actions:
        await self._do_actions(self.actions)
        self.actions.clear()

so the rejected command stayed queued. `_eval_snippet` caught the first
failure and reported it as a structured error, but python-sc2's own outer
`_after_step()` -- which runs after `on_step` returns, where nothing can
catch it -- then hit the same command again. That exception unwound the
game task, `SC2Process.__aexit__` closed the websocket and killed the SC2
client, and the opposing side of a two-player match saw its peer drop.

Three layers are covered here, one per contributing defect:

  1. `sdk.bot._normalize_move_target` -- targets are validated (and a bare
     `(x, y)` pair normalized) BEFORE any command is built or queued.
  2. `Bot._move_or_attack` -- an invalid target returns `ok=False` having
     queued NOTHING, so no later flush has anything to choke on.
  3. `ExecuteCodeBotAI.on_step` -- for a raw `sdk.*` command that bypasses
     (1) and (2) entirely, malformed commands are rejected by local
     serialization preflight and reported as an ordinary failed result,
     leaving `actions` empty so the outer `_after_step()` cannot re-raise.
     That last one is the "an invalid move cannot terminate game_task"
     proof this file exists for.

None of these launch SC2. Layers (1) and (2) are pure input validation
that never reaches a client, and (3) needs only a bare `ExecuteCodeBotAI`
-- the same "no real game underneath" approach
`tests/test_mcp_server_realtime_refresh.py` documents.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sc2.action import combine_actions
from sc2.position import Point2

from sdk.bot import Bot, _normalize_move_target
from sdk.mcp_server import ExecuteCodeBotAI, _PendingRequest


# -- fakes -------------------------------------------------------------------
#
# Deliberately minimal: _move_or_attack only ever touches ai.client.in_game
# (via _require_live), ai.realtime (via _resolve_max_wait_steps),
# ai.all_own_units.find_by_tag (via _resolve_units) and ai.do -- so a real
# BotAI, and therefore a real game, isn't needed to prove what gets queued.


class _FakeClient:
    in_game = True


class _RecordingActionClient:
    in_game = True

    def __init__(self) -> None:
        self.flushed: list[list] = []

    async def actions(self, actions) -> None:
        list(combine_actions(actions))
        self.flushed.append(list(actions))

    async def _send_debug(self) -> None:
        return None


class _FakeUnit:
    """Stand-in for `sc2.unit.Unit`. Not a real `Unit` subclass on purpose:
    `_resolve_units` selects by tag through `all_own_units.find_by_tag`, so
    tags are all that's needed, and the `move`/`attack` methods here record
    the target they were handed instead of building a real `UnitCommand`."""

    def __init__(self, tag: int) -> None:
        self.tag = tag

    def move(self, target, queue: bool = False):
        return ("move", self.tag, target, queue)

    def attack(self, target, queue: bool = False):
        return ("attack", self.tag, target, queue)


class _FakeUnits:
    def __init__(self, units: list[_FakeUnit]) -> None:
        self._by_tag = {u.tag: u for u in units}

    def find_by_tag(self, tag: int):
        return self._by_tag.get(tag)


class _FakeAI:
    realtime = False

    def __init__(self, units: list[_FakeUnit]) -> None:
        self.client = _FakeClient()
        self.all_own_units = _FakeUnits(units)
        self.actions: list = []
        self.unit_tags_received_action: set[int] = set()

    def do(self, command, ignore_warning: bool = False) -> bool:
        self.actions.append(command)
        return True


def _make_bot(tags: tuple[int, ...] = (7,)) -> tuple[Bot, _FakeAI]:
    ai = _FakeAI([_FakeUnit(t) for t in tags])
    return Bot(ai), ai


# -- layer 1: target normalization -------------------------------------------


def test_point2_target_passes_through_unchanged() -> None:
    target = Point2((145, 114))
    resolved, error = _normalize_move_target(target)
    assert error is None
    assert resolved is target, "a Point2 must not be re-wrapped (Point2 subclasses tuple)"


def test_number_pair_is_normalized_to_point2() -> None:
    for raw in ((145, 114), [145, 114], (145.5, 114.25)):
        resolved, error = _normalize_move_target(raw)
        assert error is None, f"{raw!r} should be accepted"
        assert isinstance(resolved, Point2)
        assert (resolved.x, resolved.y) == (float(raw[0]), float(raw[1]))


def test_non_coordinate_targets_are_rejected_with_an_actionable_message() -> None:
    for raw in ("145,114", None, {"x": 1}, (1, 2, 3), (1,), ("a", "b"), (True, False)):
        resolved, error = _normalize_move_target(raw)
        assert resolved is None, f"{raw!r} must not be accepted as a target"
        assert error is not None
        assert "Point2" in error, "the error must name the type the caller should have used"


# -- layer 2: nothing is queued for an invalid target ------------------------


def test_invalid_move_target_queues_nothing() -> None:
    asyncio.run(_run_invalid_move_target_queues_nothing())


async def _run_invalid_move_target_queues_nothing() -> None:
    bot, ai = _make_bot()

    outcome = await bot.move(7, "not-a-point", max_wait_steps=0)

    assert outcome.ok is False
    assert outcome.effect_confirmed is False
    assert outcome.error is not None and "Point2" in outcome.error
    assert outcome.requested_tags == (7,)
    assert outcome.confirmed_tags == ()
    # The whole point: an unserializable command must never reach the queue,
    # because python-sc2 would not clear it again after the flush it breaks.
    assert ai.actions == [], "an invalid target must not queue a command"


def test_invalid_attack_move_target_queues_nothing() -> None:
    asyncio.run(_run_invalid_attack_move_target_queues_nothing())


async def _run_invalid_attack_move_target_queues_nothing() -> None:
    bot, ai = _make_bot()

    outcome = await bot.attack_move(7, object(), max_wait_steps=0)

    assert outcome.ok is False
    assert outcome.mode == "attack_move"
    assert ai.actions == []


def test_number_pair_target_is_dispatched_as_a_point2() -> None:
    asyncio.run(_run_number_pair_target_is_dispatched_as_a_point2())


async def _run_number_pair_target_is_dispatched_as_a_point2() -> None:
    bot, ai = _make_bot(tags=(7, 9))

    # The exact call shape that killed the match, now accepted.
    outcome = await bot.move([7, 9], (145, 114), max_wait_steps=0)

    assert outcome.ok is True
    assert len(ai.actions) == 2, "both units should have been given a move order"
    for mode, _tag, target, _queue in ai.actions:
        assert mode == "move"
        assert isinstance(target, Point2), "the tuple must be converted before it is queued"
        assert (target.x, target.y) == (145.0, 114.0)


# -- layer 3: a failed flush cannot escape on_step ---------------------------


async def _make_execute_code_ai() -> ExecuteCodeBotAI:
    """A bare ExecuteCodeBotAI with on_start's bot/sdk wiring done but no
    real game underneath -- same approach as
    tests/test_mcp_server_realtime_refresh.py, see that module's docstring."""
    ai = ExecuteCodeBotAI()
    await ai.on_start()
    ai.realtime = False  # normally set by python-sc2's own _prepare_start
    return ai


async def _run_one_call(ai: ExecuteCodeBotAI, code: str):
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    await ai._queue.put(_PendingRequest(code=code, future=future, timeout_seconds=5.0))
    await ai.on_step(0)
    return await future


def test_unflushable_raw_action_cannot_terminate_the_game_task() -> None:
    asyncio.run(_run_unflushable_raw_action_cannot_terminate_the_game_task())


async def _run_unflushable_raw_action_cannot_terminate_the_game_task() -> None:
    ai = await _make_execute_code_ai()
    client = _RecordingActionClient()
    ai.client = client
    ai.state = SimpleNamespace(game_loop=1)

    # Build the same real python-sc2 UnitCommand shape that killed the live
    # match. UnitCommand accepts the tuple; combine_actions rejects it later.
    code = (
        "from sc2.ids.ability_id import AbilityId\n"
        "from sc2.unit_command import UnitCommand\n"
        "unit = type('Unit', (), {'tag': 7, 'orders': []})()\n"
        "sdk.actions.append(UnitCommand(AbilityId.MOVE, unit, (145, 114)))"
    )
    result = await asyncio.wait_for(_run_one_call(ai, code=code), timeout=1.0)

    # The regression itself: the queue must be empty once on_step returns.
    assert ai.actions == [], "a rejected command must not stay queued for the outer flush to re-raise"

    # python-sc2's own post-on_step flush guards entirely on `if self.actions:`
    # (bot_ai_internal._after_step), so replaying that guard here is a faithful
    # stand-in for it: with the queue drained it never calls the raising
    # _do_actions at all, and the game task survives.
    if ai.actions:  # pragma: no cover -- asserted empty above; this is the proof
        await ai._do_actions(ai.actions)

    # ...and the caller is told what happened rather than it being swallowed.
    assert result.ok is False
    assert result.error is not None
    assert "discarded" in result.error
    assert "Point2" in result.error, "the error should point at the usual cause"

    await ai._after_step()
    assert client.flushed == [], "the actual outer flush must find no poisoned action"


def test_successful_raw_action_is_left_for_outer_flush_and_reports_ok() -> None:
    asyncio.run(_run_successful_raw_action_is_left_for_outer_flush_and_reports_ok())


async def _run_successful_raw_action_is_left_for_outer_flush_and_reports_ok() -> None:
    """Preflight validates only; python-sc2's outer flush still dispatches."""
    ai = await _make_execute_code_ai()
    client = _RecordingActionClient()
    ai.client = client
    ai.state = SimpleNamespace(game_loop=1)

    code = (
        "from sc2.ids.ability_id import AbilityId\n"
        "from sc2.unit_command import UnitCommand\n"
        "unit = type('Unit', (), {'tag': 7, 'orders': []})()\n"
        "sdk.actions.append(UnitCommand(AbilityId.STOP, unit))\n"
        "41 + 1"
    )
    result = await _run_one_call(ai, code=code)

    assert client.flushed == [], "on_step preflight must not perform transport I/O"
    assert len(ai.actions) == 1
    assert result.ok is True
    assert result.result == "42"
    assert result.error is None

    await ai._after_step()
    assert len(client.flushed) == 1
    assert len(client.flushed[0]) == 1
    assert ai.actions == []


def test_timed_out_snippet_discards_unsent_raw_actions_before_retry() -> None:
    asyncio.run(_run_timed_out_snippet_discards_unsent_raw_actions_before_retry())


async def _run_timed_out_snippet_discards_unsent_raw_actions_before_retry() -> None:
    ai = await _make_execute_code_ai()
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    code = (
        "import asyncio\n"
        "from sc2.ids.ability_id import AbilityId\n"
        "from sc2.unit_command import UnitCommand\n"
        "unit = type('Unit', (), {'tag': 7, 'orders': []})()\n"
        "sdk.actions.append(UnitCommand(AbilityId.STOP, unit))\n"
        "await asyncio.sleep(10**9)"
    )
    request = _PendingRequest(code=code, future=future, timeout_seconds=0.01)
    await ai._queue.put(request)

    await ai.on_step(0)

    assert ai.actions == [], "a cancelled attempt must not dispatch pending raw actions later"
    assert ai.unit_tags_received_action == set()
    assert request.attempt == 2
    assert ai._queue.qsize() == 1, "the existing one-retry policy should remain intact"
