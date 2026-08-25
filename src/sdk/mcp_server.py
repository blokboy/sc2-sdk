"""MCP `execute_code` interactive mode -- ticket #6
(https://github.com/blokboy/sc2-sdk/issues/6): an MCP server exposing an
`execute_code` tool that evaluates a Python snippet against live `bot`/
`sdk` globals bound to a running game, and a `new_game` tool that starts a
fresh game on the same stdio connection instead of requiring a full MCP
reconnect for every match (see `build_server`'s `new_game` docstring for
the design: a small mutable `_ActiveGame` holder both tools route through,
and why `new_game` cancels the previous `game_task` outright rather than
waiting for it to finish -- that same cancellation is also what recovers a
session wedged forever inside a runaway snippet that never returns). By
default the game is paused between calls rather than advancing on
wall-clock time; pass `realtime=True` (`--realtime` on the CLI, or
`new_game`'s `realtime` argument) to run it at wall-clock speed instead,
e.g. for a human spectating the rendered client live -- see
`_build_session`'s docstring for what that does and doesn't change.

Concurrency/communication design
---------------------------------
An `execute_code` call is a request arriving from outside the current call
stack (an MCP client, over stdio or an in-memory transport) asking to run
code against a game that's already in progress. The design chosen here is
"same process, same asyncio event loop, no IPC":

  - `ExecuteCodeBotAI` (below) is a `VerifiedBotAI` subclass (see `bot.py`)
    whose `on_step` does not run a scripted sequence like every prior
    ticket's bots -- instead it does exactly one thing: `await` a get from
    an `asyncio.Queue`, run whatever snippet arrives against `self.bot`/
    `self.sdk`, hand the result back via a per-call `asyncio.Future`, and
    return.
  - `sc2.main._play_game_ai`'s own outer loop (see `sc2/main.py` in the
    installed `python-sc2` package) is what actually drives the game
    forward in non-realtime mode: each iteration it awaits `ai.on_step(...)`
    to *return* before it calls `client.step()` to advance the simulation
    and fetch the next observation. So as long as `on_step` is blocked
    awaiting the next queued snippet, `client.step()` is never called and
    the game sits still -- for however long it takes an MCP client (an LLM
    composing a call) to make the next `execute_code` call. This is exactly
    what gives "pausing for each call rather than advancing on wall-clock
    time" for free, without needing to touch python-sc2's stepping logic at
    all: the mechanism already exists, we're just making `on_step`'s
    *content* be "wait for external input" instead of "run scripted code".
  - The MCP server (a `FastMCP` instance, from the official `mcp` SDK) and
    the running game share the *same* asyncio event loop as ordinary
    concurrent tasks: `serve_execute_code()` below creates the game as an
    `asyncio.Task` (see the note on `sc2.main._host_game` below) and hands
    back a `FastMCP` instance whose `execute_code` tool pushes onto that
    same bot instance's queue and awaits the per-call future. No IPC, no
    subprocess boundary, no serialization of `bot`/`sdk` themselves --
    `execute_code`'s snippet runs with the literal live `Bot`/`BotAI`
    Python objects as its globals, "exactly as a direct call would" (the
    acceptance criterion's own words), because it's *the same objects in
    the same process*, not a proxy across a wire.

Why `sc2.main._host_game` instead of the public `sc2.main.run_game`
---------------------------------------------------------------------
`run_game()` (used by `runtime.run_bot_vs_builtin_ai`, which this module
deliberately does NOT reuse) is a synchronous function that wraps its own
`asyncio.run(_host_game(...))` internally. `asyncio.run()` cannot be called
from inside an already-running event loop -- and this module needs the game
and the MCP server to run as two tasks on *one* already-running loop (see
above), not two separate loops requiring cross-loop synchronization. So
`serve_execute_code()` below calls `sc2.main._host_game` -- the exact same
coroutine `run_game()` itself awaits -- directly, and schedules it with
`asyncio.create_task()` instead. This mirrors the project's existing,
documented precedent for reaching into a private python-sc2 name for a
structural reason a public API doesn't support: see `bot.py`'s module
docstring on `BotAI._advance_steps`/`_after_step`. `_host_game` is not
`@final`/publicly documented the way `_advance_steps` is, so this is called
out explicitly here as a considered choice, not copied blindly: it is the
same call `run_game()`'s own single-non-computer-opponent branch makes,
unwrapped from its private `asyncio.run()`, doing nothing `run_game()`
doesn't already do.

Snippet evaluation semantics
-----------------------------
`_eval_snippet` treats the snippet like a single REPL cell: it's parsed,
wrapped in a synthetic `async def`, and if the snippet's last top-level
statement is a bare expression, that statement becomes `return <expr>` so
its value comes back as the call's `result` (auto-`await`-ed if the
expression itself evaluates to a coroutine, so `bot.train(...)` works
whether or not the snippet remembers to write `await`). Anything printed
via `print()` is captured and returned as `stdout`. Any exception raised
while compiling or running the snippet is caught and reported as a
structured `ok=False`/`error` result (mirroring this project's existing
outcomes.py convention) rather than propagating out of `on_step` and
crashing the running game.

Per-call timeout and single automatic retry
---------------------------------------------
The above except-`Exception` clause does NOT protect against a snippet
that simply never finishes -- and that's not hypothetical: a snippet with
a genuine bug (`while scvs_needed > 0: ... else: await bot._advance(22)`,
where the `else` was unreachable because of a missing supply-cap check)
looped forever, each iteration doing a real `await`, and wedged `on_step`
-- and therefore the whole game -- permanently, with no recovery short of
killing the whole `sc2-sdk-mcp` process. `new_game` (above) can recover
*after the fact* by cancelling the wedged game outright, but nothing
prevented getting wedged in the first place, and a merely-slow (not
actually buggy) call had no chance to just be retried.

`ExecuteCodeBotAI.on_step` now wraps the `_eval_snippet` call in
`asyncio.wait_for(..., timeout=request.timeout_seconds)` -- the same
cancellation mechanism `new_game` already relies on (see its docstring in
`build_server`): `wait_for` cancels its inner task on timeout, which
propagates an ordinary `asyncio.CancelledError` into whatever the
snippet's `await` chain is currently suspended on. `_eval_snippet`'s own
`except Exception` (not `BaseException`) does NOT swallow that
`CancelledError` -- `CancelledError` is a `BaseException` subclass, not an
`Exception` subclass, on the Python version this project targets -- so
the cancellation unwinds cleanly out of `wait_for` as a `TimeoutError`,
exactly as intended, rather than being silently caught and reported as an
ordinary snippet exception.

The retry policy, in `on_step`:

  1. **First timeout**: the caller's future is deliberately left unresolved
     (`execute_code`'s MCP call stays pending). The same `_PendingRequest`
     is mutated (`attempt` incremented from 1 to 2) and re-enqueued onto
     the *end* of `self._queue` via `await self._queue.put(request)` -- so
     any other calls already queued behind it get their turn first, and
     this one gets exactly one more shot after them.
  2. **Second timeout** (the retry also times out): the future IS resolved
     now, with `ExecuteCodeResult(ok=False, ...)` whose `error` spells out
     that the snippet timed out on both the original attempt and the
     automatic retry, the timeout duration used, and the snippet's own
     code -- so whoever reads the result (the MCP tool's return value,
     surfaced straight back to the calling agent) knows unambiguously what
     failed and why, without needing to go dig through server logs.
  3. A snippet that finishes within its timeout on either attempt resolves
     normally -- the caller never sees a timeout error at all in that case,
     only (if it happened) a brief delay while any calls queued behind the
     slow first attempt got processed first.

Timeout value: configurable, not one hardcoded constant, because this
project's `--realtime` mode has *legitimately* slow snippets that are not
bugs -- e.g. `while not sdk.can_afford(X): await bot._advance(22)` waiting
for minerals to accumulate can genuinely take a while in real time if
income is slow. See `DEFAULT_SNIPPET_TIMEOUT_SECONDS` for the default and
its reasoning, and `execute_code`'s `timeout_seconds` argument for the
per-call override a caller who knows a specific snippet will legitimately
run long can use instead of raising the server-wide default (which would
make every *other* call wait just as long before a real bug is ever
detected). The effective timeout for a given call -- server default or
per-call override -- is resolved once, in `submit()`, and carried on the
`_PendingRequest` itself, so a requeued retry reuses the exact same
timeout its first attempt used rather than silently re-resolving against
a `new_game`-updated server default mid-flight.

Interaction with `new_game`: a timed-out-and-requeued request lives in a
specific `ExecuteCodeBotAI` instance's `self._queue`, tied to that
instance's `on_step` loop. If `new_game` cancels that game entirely while
a retry is still queued (or in flight), the existing `submit()`/
`game_task`-racing logic (see `submit()`'s docstring) already produces a
clean "match ended" result for it -- exactly the same path any other
in-flight call takes during a `new_game`-triggered cancellation, since
`game_task.cancel()` only ever propagates a plain `CancelledError`
(distinct from the `TimeoutError` `wait_for` raises on an ordinary
timeout) out through `on_step`, past the retry logic entirely, and into
`_host_game`'s own cleanup. Nothing extra was needed for this case.

Standing background tasks: `start_task`/`task_status`/`cancel_task`
----------------------------------------------------------------------
The problem this solves: an open-ended, goal-directed instruction like
"have an SCV build Supply Depots until we have 30" can legitimately take
many real minutes (waiting on mineral income, one depot at a time) --
which is exactly the kind of "legitimately slow, not a bug" case the
per-call timeout above is deliberately generous about, but a *single*
`execute_code` call still isn't the right shape for it: either the
snippet loops internally until the goal is met (in which case it looks
identical to a runaway snippet from `on_step`'s perspective, and either
gets killed by the timeout or -- worse -- requires the caller to pass a
huge `timeout_seconds` override for this one goal, which the project
explicitly rejected: "arbitrary flagging of tasks with different cooldown
is very imprecise"), or the calling agent has to re-issue `execute_code`
itself in a polling loop, which blocks that agent's own turn for just as
long. Neither lets other `execute_code` calls (e.g. a human checking in
on a completely different part of the game) get serviced while the goal
is pending.

The fix implemented here is NOT a second, concurrently-scheduled
execution path -- see this module's "Concurrency/communication design"
section above for why that would be unsafe (two coroutines calling into
`self.bot`/`self.sdk` at genuinely overlapping times, racing on
python-sc2's unsynchronized internal state). Instead, a background task is
just a different *kind* of item on the exact same `self._queue` `on_step`
already drains one at a time:

  - `start_task(code, description, max_iterations)` registers a
    `_TaskState` (in `self._tasks`, keyed by a short incrementing
    `task_id`) and immediately enqueues ONE turn for it -- a
    `_PendingRequest` whose `task_id` is set (see `_PendingRequest`'s
    docstring) instead of carrying a caller-facing `future` -- via
    `self._queue.put_nowait(...)`. `put_nowait` never blocks (this
    module's queues are unbounded, same as `submit()`'s `await
    self._queue.put(...)`, which also never actually suspends), so
    `start_task` returns to its caller the instant the first turn is
    queued, without waiting for that turn -- let alone the whole goal --
    to run. This is what makes it "not just `execute_code` with a bigger
    timeout": the goal can take as many real-world turns as it needs,
    each one bounded by the ordinary per-call timeout, while the
    `start_task` call itself returns in microseconds.
  - Each turn is `code` -- ONE snippet, evaluated repeatedly, doing ONE
    bounded chunk of work per turn (check progress, do a small amount of
    work if the goal isn't met yet, or wait a tick if not yet
    affordable), then signal "keep going" or "goal met" via its return
    value -- reusing `_eval_snippet`'s *existing* "trailing expression (or
    an explicit `return`, since the snippet's body literally becomes a
    real `async def` function body -- nothing new needed for that to
    work) becomes the result" convention verbatim. No new
    snippet-evaluation semantics: the only new thing is a *convention* for
    what a task's return value means -- `True` (or any truthy value) ends
    the task successfully; `False`/falsy keeps it going. Concretely, the
    user's own supply-depot scenario as one task's `code`:

        from sc2.ids.unit_typeid import UnitTypeId

        depot_count = len(sdk.structures(UnitTypeId.SUPPLYDEPOT))
        if depot_count >= 30:
            return True
        if sdk.can_afford(UnitTypeId.SUPPLYDEPOT):
            depot_point = sdk.townhalls.first.position.towards(sdk.game_info.map_center, 6)
            depot_point = depot_point.offset((depot_count * 3, 0))
            await bot.build(UnitTypeId.SUPPLYDEPOT, near=depot_point)
        else:
            await bot._advance(22)
        return False

    registered via `start_task(code=..., description="SCVs build Supply
    Depots until we have 30")`. Each turn either builds one more depot (if
    affordable) or waits one tick for minerals, then reports `False` to
    request another turn -- until `depot_count >= 30`, when a turn
    finally returns `True` and the task is marked done. Every turn is
    independently subject to the same `asyncio.wait_for` timeout + single
    retry ordinary `execute_code` calls get (see the section above) --
    reused, not reimplemented (see `on_step` below) -- so a turn that
    itself hangs (e.g. a bug in the task's own code) is caught exactly
    like a hung ordinary snippet would be, it just marks the *task* failed
    afterward instead of resolving a caller's future.
  - `on_step` doesn't care whether the `_PendingRequest` it just dequeued
    came from `submit()` (ordinary `execute_code`) or `start_task`
    (a task turn) -- it runs `_eval_snippet` under the identical
    `asyncio.wait_for(..., timeout=request.timeout_seconds)` either way.
    Only what happens *after* that differs, and it's a single `if
    request.task_id is not None: ... else: ...` branch at each of the two
    existing resolution points (the timeout-exhausted branch and the
    normal-completion branch) -- see `_finish_task_turn`. This is what
    "reuses the existing timeout/retry machinery rather than forking a
    parallel implementation" means concretely: there is exactly one
    `asyncio.wait_for(_eval_snippet(...))` call site in this whole module,
    used by both kinds of request.
  - `_finish_task_turn` (called from `on_step` once a turn's outcome --
    success, ordinary failure, or both-attempts-timed-out -- is known)
    updates the task's bookkeeping and decides what happens next: a
    truthy result marks the task `"done"`; an `ok=False` result (an
    exception, or a timeout that exhausted its retry) marks it `"failed"`
    and stops -- a broken task does not spin forever; a falsy-but-`ok`
    result means "keep going", which either re-enqueues one more turn (if
    a `cancel_task` call hasn't set the task's `cancel_requested` flag,
    and `max_iterations` hasn't been reached) or ends the task as
    `"cancelled"`/`"failed"` (max-iterations-exhausted) instead. Because
    at most one turn per task is EVER sitting in `self._queue` or actively
    running at a time (a task's next turn is only enqueued from inside
    `_finish_task_turn`, i.e. strictly after the previous turn's result is
    already known), `cancel_task` never needs to interrupt an in-flight
    turn -- it just flips `cancel_requested`, and the in-flight (or
    already-queued) turn's own eventual `_finish_task_turn` call is
    guaranteed to be the next -- and only -- place that flag is ever
    consulted for that task.
  - Progress is pull-based: `task_status(task_id)` reports the task's
    current `status`/`iterations`/a capped recent-activity `log` (see
    `_TASK_LOG_MAX_ENTRIES`) /`result`/`error` on demand. Nothing pushes
    updates to a caller -- MCP tools are request/response, so there is no
    server-initiated notification channel to build here; a caller
    interested in a long-running task's progress polls `task_status`
    instead, the same way a human might glance at a build order in
    progress.
  - Lifecycle: tasks live in `self._tasks` on a specific `ExecuteCodeBotAI`
    instance, exactly like `self._queue` already does. `new_game` (see
    `build_server`) replaces `active.bot_ai` with a brand new
    `ExecuteCodeBotAI` (a fresh, empty `self._tasks`) and only swaps that
    reference in after the OLD game's `game_task` has been cancelled and
    fully awaited -- so by the time any tool call could observe the new
    `active.bot_ai`, the old one's `on_step` loop is provably not running
    anymore, and never will again (see `new_game`'s docstring in
    `build_server` for why cancellation propagates a plain
    `CancelledError` straight out of `on_step`, bypassing every branch
    discussed above -- `_finish_task_turn` is never reached for a turn
    caught mid-flight by a `new_game`-triggered cancellation, so that
    turn, and the task it belonged to, simply stop existing along with
    the old `ExecuteCodeBotAI` object itself, with nothing further to
    clean up). Concretely: a task does NOT survive `new_game` -- its
    `task_id` becomes unresolvable (`task_status` reports "unknown
    task_id", the same way it would for a `task_id` that was never
    registered at all) the moment a new game replaces the one the task
    belonged to, with no special-casing required anywhere in this
    mechanism beyond what `new_game`/`_ActiveGame` already do for an
    ordinary in-flight `execute_code` call.

Hosting a two-player match: `host_game`/`host_status`
-------------------------------------------------------
Ticket #15 (https://github.com/blokboy/sc2-sdk/issues/15): lets an LLM host
a real two-player match from inside an interactive `sc2-sdk-mcp` session --
another `sc2-sdk-mcp` session (or the standalone `sc2-sdk-join` CLI) can then
join it -- instead of only via the standalone `sc2-sdk-host` CLI script
(`sdk.host_join`), which requires a whole separate bot script and never
exposes the live `execute_code`/`start_task` tools this module already has.

Built on the already-proven pieces, not new protocol work: `sdk.join`'s
`_run_host_role` (ticket #11's proven host_ip-aware join primitive, already
extended with an optional `join_timeout` for ticket #12) and `sdk.matchcode`
(the shareable code format). Neither of those modules is touched here --
both carry their own "kept separate/untouched" ground rules (see their
docstrings), and nothing this feature needs requires changing either:

  - **Defaulting `host_ip` to loopback, not Tailscale.** `sdk.host_join`'s
    CLI auto-detects (or offers to install) Tailscale by default, because
    genuinely separate machines is its whole point. This feature's current
    scope (see the project's tracked tickets) is same-machine, two-LLM
    play -- reaching for Tailscale auto-detection here would risk an
    unwanted install prompt for a match that never leaves the host machine.
    `host_game`'s `host_ip` defaults to `"127.0.0.1"` instead, plainly, with
    no `install.tailscale` involvement at all; a caller who already knows a
    real routable address (LAN, Tailscale, whatever) can still pass it
    explicitly -- `host_ip` is just a string, unchanged from `_run_host_role`'s
    own signature.
  - **Realtime is not a `host_game` option -- it's forced on.** Single-player
    `execute_code` mode gets its "paused between calls" feel for free
    because there's only one client to hold still (see this module's
    top-level docstring). Once a second, independently-paced client is
    synchronized into the same match, there is no shared pause to hold: the
    SC2 engine's stepping is a per-connection request, but the match
    simulation itself is shared. So `_launch_hosted_game` (below) always
    passes `realtime=True` to `_run_host_role`, unconditionally -- this is
    not a caller-configurable default the way `new_game`'s `realtime`
    argument is, because "stepped" was never a real option here to begin
    with, not merely a worse one.
  - **`host_game` returns immediately; `host_status` is polled** -- the
    exact same shape `start_task`/`task_status` already established above
    for "kick off something that may take a while, then check back": a
    human relaying a match code between two chat sessions is exactly the
    kind of open-ended, human-timescale wait `start_task` was built for, so
    this reuses that shape rather than inventing a second one. Concretely:
    `_launch_hosted_game` schedules `_run_host_role`'s coroutine (which
    itself blocks on `create_game`+`join_game` until a peer connects, then
    runs the whole match to completion) as an `asyncio.Task` via
    `asyncio.create_task`, exactly the way `_launch_game` already schedules
    `_host_game`'s coroutine for a solo game against the built-in AI -- and
    returns the match code to the caller without awaiting that task at all.
    `host_status` reports one of three states, derived from state this
    module already tracks for other reasons rather than a parallel state
    machine: `"waiting"` (the peer hasn't connected yet: `bot_ai.ready` --
    the same event `execute_code` already awaits per-call -- isn't set, and
    the task hasn't finished either); `"joined"` (`bot_ai.ready` is set --
    once the peer's engine handshake resolves, `_play_game_ai` starts
    driving `ExecuteCodeBotAI.on_step`, which sets `ready` from `on_start`
    exactly as it does for a solo game, so this needs no new signal); or
    `"failed"` (the task finished without ever setting `ready` -- either a
    join timeout, surfaced by `_run_host_role` raising `sdk.matchcode
    .JoinTimeoutError`, which `_launch_hosted_game`'s own task wrapper
    catches and records onto `_HostGameState.timed_out` before re-raising,
    or some other launch failure). There is no `"timed_out"`-vs-`"failed"`
    split in `host_status`'s public `status` field -- `_HostGameState.
    timed_out` distinguishes them internally (a clearer `error` message for
    the timeout case specifically), but both are simply "this host never
    got matched" from a caller's point of view, and both are recovered from
    identically: call `new_game` (see below) and try again.
  - **An abandoned/unmatched host is torn down via `new_game`, not a new
    cancel tool.** `new_game`'s existing contract is already "end whatever's
    running outright, cancelling it, not waiting" -- exactly the semantics
    needed to give up on a host nobody has joined yet, or to bail out before
    the timeout elapses because the map/race was wrong. `new_game` and
    `host_game` now share that teardown step (`_teardown_active_game`,
    factored out of `new_game`'s body below) so there is exactly one place
    "cancel whatever game is currently active" is implemented, used by every
    way of starting a new one.
  - **Race pins carry through the match code unchanged.** `host_game`'s
    `opponent_race_pin` argument is embedded into the match code exactly
    like `sc2-sdk-host --opponent-race-pin` already does (see
    `sdk.matchcode.encode_match_code`/`resolve_race`) -- nothing on the
    hosting side "resolves" this pin itself; it's informational for whoever
    calls the *joining* side's own tool later (a separate ticket), the same
    way it already is for the standalone `sc2-sdk-join` CLI today.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import atexit
import contextlib
import dataclasses
import io
import json
import os
import secrets
import tempfile
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil
from loguru import logger

from sc2 import maps
from sc2.data import Difficulty, Race, Result
from sc2.main import _host_game  # noqa: SLF001 -- see module docstring
from sc2.player import Bot as Sc2BotPlayer
from sc2.player import Computer
from sc2.portconfig import Portconfig
from sc2.sc2process import SC2Process  # noqa: SLF001 -- see _patched_sc2process_launch's docstring below

from mcp.server.fastmcp import FastMCP

from install.paths import BINARY_NAME, platform_name
from sdk.bot import VerifiedBotAI
from sdk.host_join import DEFAULT_JOIN_TIMEOUT
from sdk.join import _run_host_role  # noqa: SLF001 -- see this module's "Hosting a two-player match" section
from sdk.matchcode import JoinTimeoutError, encode_match_code

#: Same fixed test map the rest of the project's harnesses default to.
DEFAULT_MAP = "AutomatonLE"

#: Default per-`execute_code`-call timeout (in seconds), used by `on_step`'s
#: `asyncio.wait_for` around `_eval_snippet` -- see the module docstring's
#: "Per-call timeout and single automatic retry" section for the full
#: mechanism this guards. 45 seconds: generous enough (tens of seconds, not
#: single-digit seconds) that an ordinary realtime resource-wait loop --
#: e.g. `while not sdk.can_afford(X): await bot._advance(22)` waiting for
#: minerals to accumulate under normal income -- comfortably finishes well
#: within it, while still bounding a genuinely wedged snippet (like the
#: real incident this feature was built for: a supply-cap bug that turned
#: an `await bot._advance(22)` loop into an infinite one) to under a
#: minute times two attempts, instead of hanging `on_step` -- and therefore
#: the whole game -- forever. A snippet known in advance to legitimately
#: need longer than this (e.g. a genuinely slow-income minerals wait that
#: can take upwards of a minute) should pass its own `timeout_seconds` to
#: `execute_code` rather than raising this server-wide default, which
#: would make every *other* call wait just as long before a real bug is
#: ever detected. See `_GameConfig.snippet_timeout_seconds` for how this
#: default is overridden per-server (`--snippet-timeout`) and persists
#: across `new_game` calls.
DEFAULT_SNIPPET_TIMEOUT_SECONDS = 45.0

#: Default `start_task` `max_iterations` -- see the module docstring's
#: "Standing background tasks" section for the overall mechanism. This
#: bounds how many turns a background task will run before giving up as
#: `"failed"` (exhausted) if its step code's trailing return value never
#: evaluates truthy -- a backstop against a task whose goal is simply
#: never reachable (e.g. a step-code bug that always reports "keep going",
#: or a genuinely unreachable target like more depots than the map's
#: supply cap allows) spinning forever, consuming one queued turn after
#: another indefinitely. 1000 is deliberately generous: even a slow
#: resource-bound goal like "Supply Depots until 30" (the module
#: docstring's worked example) needs at most a few dozen turns, so 1000
#: leaves ample headroom for legitimately larger goals without meaningfully
#: risking an actually-stuck task running "forever" in practice -- a
#: caller with a goal it knows needs more turns than this can simply pass
#: a larger `max_iterations` to `start_task` explicitly.
DEFAULT_TASK_MAX_ITERATIONS = 1000

#: Cap on how many recent per-turn log entries `task_status` accumulates
#: per task (see `_TaskState.log` and `_finish_task_turn`) -- kept as a
#: bounded `collections.deque(maxlen=...)`, not an ever-growing list, so
#: polling `task_status` on a long-running task (up to
#: `DEFAULT_TASK_MAX_ITERATIONS` turns) never accumulates unbounded memory
#: for a log a caller only ever wants "the recent activity" from, not a
#: full history back to turn 1. 50 entries: generous enough that a caller
#: polling every so often sees a meaningful, multi-turn window of recent
#: progress (not just the single most recent turn), while staying small
#: enough that even a task that runs all the way to `max_iterations` keeps
#: a `task_status` response small and fast to read -- a count of entries
#: (not a character budget) was chosen because each entry is already a
#: short, single-line summary (see `_format_task_log_entry`), so bounding
#: by count already bounds total size to a predictable, small multiple of
#: one line.
_TASK_LOG_MAX_ENTRIES = 50

#: Same name->enum lookup convention sdk.play._parse_args uses, for the same
#: --race/--opponent-race/--difficulty CLI flags below.
_RACE_BY_NAME = {r.name.lower(): r for r in Race}
_DIFFICULTY_BY_NAME = {d.name.lower(): d for d in Difficulty}


# ---------------------------------------------------------------------------
# Single-instance guard + explicit SC2-client PID tracking
#
# Real, observed incident: over one long session involving several `/mcp`
# reconnects, THREE separate `sc2-sdk-mcp` python processes and TWO separate
# `SC2.app` client processes ended up running simultaneously, none of which
# had exited on their own. Every `/mcp` reconnect risks leaving the previous
# instance's process (and its own spawned SC2 client) running forever in the
# background. Worse, if a client's stdio pipe were ever reattached to the
# wrong instance, tool calls could get serviced by the wrong game entirely.
# Everything below is the defense-in-depth fix: guarantee at most one
# sc2-sdk-mcp instance (and its SC2 client(s)) is ever alive, regardless of
# whether the reconnect mechanics above are ever fixed on the harness side.
#
# Root cause investigation
# --------------------------
# 1. stdio EOF handling: `mcp.server.stdio.stdio_server()`'s `stdin_reader()`
#    (installed under `.venv/.../mcp/server/stdio.py`) does `async for line
#    in stdin:` and, on a genuine EOF (the write end of stdin actually
#    closed), that loop ends *normally* -- no exception, no special handling
#    needed. That closes `read_stream_writer`, which anyio's memory-object-
#    stream machinery propagates as end-of-stream to
#    `BaseSession._receive_loop` (`.venv/.../mcp/shared/session.py`), whose
#    own `async with (self._read_stream, self._write_stream):` block then
#    closes both streams on its way out, which ends `Server.run()`'s
#    `async for message in session.incoming_messages`
#    (`.venv/.../mcp/server/lowlevel/server.py`), which makes
#    `run_stdio_async()` return, which makes `_main_async` return, which
#    lets `asyncio.run()` in `main()` reach its own built-in cleanup
#    (cancelling any tasks still on the loop -- including `game_task` --
#    before closing the loop). In other words: **the installed `mcp`
#    library already shuts down cleanly on a real stdin EOF; nothing in this
#    module needs to be added to make that specific path work.**
#
#    That means the orphaned-process incident this section fixes is not
#    explained by a bug in stdio EOF handling *within this process*. The far
#    more likely explanation -- outside this repo's control, and not
#    fixable from here -- is that Claude Code's own harness, on a `/mcp`
#    reconnect, does not actually close (EOF) the old subprocess's stdin; it
#    just stops talking to the old process and spawns a new one, leaving the
#    old subprocess blocked forever on a read that will never see EOF and
#    never receives a signal either. If that is what's happening, no amount
#    of stdio-handling code in this module can detect or react to it,
#    because nothing distinguishing "abandoned" from "still my client, just
#    quiet for a while" ever crosses this process's stdin. Noted here for
#    visibility, not "fixed" here -- the single-instance guard below is the
#    fix that doesn't depend on the harness ever telling this process
#    anything.
#
# 2. SIGINT vs SIGTERM in `SC2Process` (`.venv/.../sc2/sc2process.py`):
#    `SC2Process.__aenter__` registers a cleanup handler with
#    `signal.signal(signal.SIGINT, signal_handler)` ONLY -- SIGINT, not
#    SIGTERM. That handler calls `KillSwitch.kill_all()`, which calls
#    `_clean()` on every live `SC2Process`, which is what actually
#    terminates the SC2 client subprocess. SIGTERM has no handler registered
#    anywhere in `python-sc2` or this project, so it falls back to Python's
#    default disposition for SIGTERM: the interpreter is torn down
#    immediately, WITHOUT running `finally` blocks, `atexit` hooks, or
#    (therefore) `KillSwitch.kill_all()`. Concretely: **sending SIGTERM to a
#    stale `sc2-sdk-mcp` process -- the natural first thing a "kill the
#    stale instance" implementation would try -- does NOT clean up that
#    process's SC2 client child.** The child is orphaned (re-parented to
#    launchd/init) instead. This is exactly why the guard below tracks and
#    terminates SC2 client PIDs explicitly and independently, rather than
#    assuming "kill the parent, the child follows."
#
# Explicit ownership tracking, not pattern matching
# ----------------------------------------------------
# A user who plays one game via `sc2-sdk-mcp` and *separately* opens their
# own SC2 client by hand (Battle.net, double-clicking SC2.app, etc.) must
# never have that manual client touched by this guard, even though it is a
# real, live process whose executable path genuinely matches
# `install.paths.BINARY_NAME`. "Looks like an SC2 client" is therefore NOT
# sufficient grounds for termination anywhere in this section -- only "was
# explicitly recorded, at the moment this exact `sc2-sdk-mcp` process
# launched it, as a PID this process spawned" is. Concretely:
#
#   - `_patched_sc2process_launch` (below) intercepts the literal
#     `subprocess.Popen` object `SC2Process._launch()` creates -- not a PID
#     rediscovered later via a PPID scan -- and reports its `.pid` straight
#     from that object, once, at spawn time. `_host_game` (used via
#     `sc2.main._host_game`, see the module docstring's "Why
#     `sc2.main._host_game`..." section) fully encapsulates its
#     `SC2Process`/`Controller` and never exposes it back to this module --
#     there is no supported way to get the real `Popen` object out of
#     `_host_game` short of this kind of interception, and monkeypatching a
#     private `python-sc2` method at runtime (not editing the installed
#     package) is the same category of "reach into an internal for a
#     structural reason a public API doesn't support" this module already
#     does for `_host_game` itself.
#   - That PID is recorded into `_owned_sc2_pids` (this process's own
#     in-memory bookkeeping) and persisted into the lockfile -- see
#     `_track_sc2_pid`/`_write_lockfile`.
#   - `_terminate_stale_instance` (below), when it finds a stale prior
#     `sc2-sdk-mcp` instance, terminates ONLY the PIDs that instance's OWN
#     lockfile explicitly recorded as its SC2 client(s). It never scans
#     `ps`/`psutil` for "children of the stale PID" or "any process whose
#     path contains SC2.app/StarCraftII" as a way of *discovering*
#     candidates -- the identity check against `BINARY_NAME` (via
#     `_looks_like_sc2_client_process`) is used only as a defensive second
#     check against an *already-recorded* PID (guarding against that exact
#     PID having been reused by an unrelated process in the time since it
#     was recorded), never as the primary discovery mechanism. A manually-
#     started client's PID is never written to any `sc2-sdk-mcp` lockfile in
#     the first place, so it is structurally never a candidate here,
#     regardless of what it looks like from the outside.
#
# Why a lockfile, not a process/psutil scan, for the sc2-sdk-mcp half too
# -------------------------------------------------------------------------
# `psutil` is already an installed dependency (see pyproject.toml's
# `dependencies` for why it's now listed explicitly rather than only relied
# on transitively via `portpicker`, which `burnysc2` already depends on)
# and IS used below -- but only for liveness/identity checks on PIDs already
# named by the lockfile, not for discovery-by-scanning. Once the SC2-client
# half of this problem structurally requires explicit, recorded state (see
# above -- there is no live signal to scan for that would be both sufficient
# and safe), using that same lockfile mechanism for the sc2-sdk-mcp process
# itself is simpler and more uniform than mixing "scan for one, lockfile for
# the other."
# ---------------------------------------------------------------------------

#: Where this process records "I am the current sc2-sdk-mcp instance, and
#: here are the SC2 client PID(s) I explicitly spawned" -- read by the next
#: sc2-sdk-mcp startup's single-instance guard to find and clean up a stale
#: prior instance. A namespaced file under the system temp dir: this project
#: has no existing convention for its own local/runtime state
#: (`install.paths` is about locating the SC2 *client* install, a different
#: concern -- see this section's docstring above), so this is a reasonable
#: default rather than inventing a project-specific state directory for one
#: small file.
DEFAULT_LOCKFILE_PATH = Path(tempfile.gettempdir()) / "sc2-sdk-mcp.lock"

#: Substrings checked (via `in`) against a candidate process's `cmdline()`,
#: joined with spaces, to decide "this is an sc2-sdk-mcp process" -- see
#: `_is_sc2_sdk_mcp_process`. Covers both ways this project's entrypoint is
#: documented to be launched (README.md "Play interactively via MCP"): the
#: installed `sc2-sdk-mcp` console script (pyproject.toml
#: `[project.scripts]`), whose own script file path contains "sc2-sdk-mcp"
#: and is what a POSIX shebang re-exec leaves in `cmdline()`, and the
#: `python -m sdk.mcp_server` form, whose cmdline contains "sdk.mcp_server"
#: instead.
_SC2_SDK_MCP_CMDLINE_MARKERS = ("sc2-sdk-mcp", "sdk.mcp_server")

#: Bounded wait after SIGTERM before escalating to SIGKILL, and after
#: SIGKILL before giving up on confirming the process is gone -- see
#: `_terminate_pid`. Short on purpose: this runs at `sc2-sdk-mcp` startup,
#: before serving anything, and a stale process is either already
#: unresponsive (waiting longer only delays every future startup for no
#: benefit) or exits almost immediately on SIGTERM (the common case, since
#: neither this project's own code nor python-sc2 installs a SIGTERM
#: handler that could meaningfully prolong shutdown -- see root-cause note
#: 2 above).
_TERMINATE_WAIT_SECONDS = 2.0
_KILL_WAIT_SECONDS = 1.0

#: Set by `_launch_game`, immediately before it schedules the `_host_game`
#: task that will (eventually, once that task actually runs) call
#: `SC2Process._launch()` -- read exactly once by
#: `_patched_sc2process_launch` below when that happens, to report the new
#: SC2 client's PID back to the specific `ExecuteCodeBotAI` that asked for
#: it.
#:
#: A single module-level slot, not a per-call queue/dict, is deliberately
#: sufficient here: `_launch_game` is only ever called (a) once at server
#: startup, before any game exists, and (b) from `new_game` (see
#: `build_server`), which holds `active.lock` for its entire body and --
#: critically -- fully awaits the OLD `game_task`'s cancellation (running
#: `SC2Process.__aexit__`'s cleanup to completion) before calling
#: `_launch_game` again for the new one. So by construction there is never
#: more than one `_launch_game` call whose `SC2Process._launch()` hasn't
#: fired yet, and this slot is only ever overwritten by a *new*
#: `_launch_game` call after the previous one's callback has already run
#: (spawning the OS process is one of the very first things
#: `SC2Process.__aenter__` does, long before the up-to-180-second
#: `_connect()` wait this module's own docstring discusses elsewhere) --
#: there is no window in which two callbacks could race for this one slot.
_pending_sc2_pid_capture: "Callable[[int], None] | None" = None


def _patched_sc2process_launch(self: SC2Process):
    """Monkeypatch installed over `SC2Process._launch` at import time (see
    the assignment right below this function, and this section's "Explicit
    ownership tracking, not pattern matching" docstring above for why this
    exists at all): calls straight through to the original implementation,
    then reports the resulting `subprocess.Popen`'s real OS `.pid` to
    whichever launch is currently pending capture (see `_launch_game`'s use
    of `_pending_sc2_pid_capture`) before returning it unchanged.

    Safe to install exactly once at import time (this module only ever
    performs the assignment below once, at module load): `SC2Process._launch`
    is a synchronous, ordinary instance method with no `async`/awaiting
    inside it, so this wrapper adds no concurrency behavior of its own -- it
    is a pure interception point, not a scheduling change.
    """
    process = _ORIGINAL_SC2PROCESS_LAUNCH(self)
    if _pending_sc2_pid_capture is not None:
        _pending_sc2_pid_capture(process.pid)
    return process


_ORIGINAL_SC2PROCESS_LAUNCH = SC2Process._launch
SC2Process._launch = _patched_sc2process_launch  # noqa: SLF001 -- see _patched_sc2process_launch's docstring


def _read_lockfile(lockfile_path: Path) -> dict[str, object] | None:
    """Best-effort parse of the lockfile at `lockfile_path` -- returns
    `None` (never raises) if it doesn't exist, isn't valid JSON, or doesn't
    have the shape this module writes (e.g. a leftover file from something
    else entirely, or a torn write from a process that crashed mid-write).
    A missing/unreadable lockfile is treated identically to "no prior
    instance" by `_terminate_stale_instance` -- there is nothing to clean up
    either way."""
    try:
        raw = lockfile_path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "mcp_pid" not in data:
        return None
    return data


def _write_lockfile(lockfile_path: Path, mcp_pid: int, sc2_pids: "set[int]") -> None:
    """Write `{"mcp_pid": ..., "sc2_pids": [...]}` to `lockfile_path`,
    atomically (write to a sibling temp file, then `os.replace` it over the
    real path) so a concurrent reader (the next `sc2-sdk-mcp` startup's
    guard) never observes a partially-written file. Called once at startup
    (claiming the lockfile for this process, with an empty `sc2_pids`) and
    again every time `_track_sc2_pid`/`_untrack_sc2_pid` changes what this
    process believes it owns, so the lockfile is always an accurate,
    up-to-date record of "this process, and the SC2 client(s) it has
    actually spawned and not yet torn down" -- not just a one-time snapshot
    from startup."""
    tmp_path = lockfile_path.parent / f"{lockfile_path.name}.{os.getpid()}.tmp"
    tmp_path.write_text(json.dumps({"mcp_pid": mcp_pid, "sc2_pids": sorted(sc2_pids)}))
    os.replace(tmp_path, lockfile_path)


#: This process's own bookkeeping of "SC2 client PIDs I have spawned and not
#: yet torn down" -- the in-memory source of truth `_write_lockfile`
#: persists on every change via `_track_sc2_pid`/`_untrack_sc2_pid`. Kept in
#: memory (not re-read from the lockfile each time) because this process is
#: the sole writer of its own lockfile entry for its entire lifetime;
#: re-reading would only reintroduce a TOCTOU window against itself for no
#: benefit.
_owned_sc2_pids: "set[int]" = set()


def _track_sc2_pid(pid: int, *, lockfile_path: Path) -> None:
    """Record `pid` as an SC2 client this process spawned, and persist that
    to the lockfile immediately -- called from `_patched_sc2process_launch`
    (via the closure `_launch_game` installs into
    `_pending_sc2_pid_capture`) the moment a new SC2 client's real PID is
    known, so a concurrently-starting `sc2-sdk-mcp` instance's guard sees it
    as early as possible rather than only once some later, unrelated
    lockfile write happens to occur."""
    _owned_sc2_pids.add(pid)
    _write_lockfile(lockfile_path, os.getpid(), _owned_sc2_pids)


def _untrack_sc2_pid(pid: "int | None", *, lockfile_path: Path) -> None:
    """Remove `pid` from this process's own "SC2 clients I own" bookkeeping
    and persist that -- called once a game's `SC2Process.__aexit__` cleanup
    has actually run and torn that client down normally (see `new_game`'s
    use of this, in `build_server`), so the lockfile doesn't keep
    advertising a PID this process already cleaned up itself. Not
    load-bearing for correctness (`_terminate_stale_instance` always
    re-verifies liveness and identity before acting on anything a lockfile
    records -- see its docstring), just keeps the lockfile's contents honest
    and current. A no-op if `pid` is `None` (the game never got far enough
    for its SC2 client's PID to be captured -- see `ExecuteCodeBotAI.sc2_pid`)
    or wasn't tracked."""
    if pid is None:
        return
    _owned_sc2_pids.discard(pid)
    _write_lockfile(lockfile_path, os.getpid(), _owned_sc2_pids)


def _process_cmdline(pid: int) -> "str | None":
    """`" ".join(psutil.Process(pid).cmdline())`, or `None` if `pid` isn't a
    live process this user can inspect -- the shared plumbing
    `_is_sc2_sdk_mcp_process`/`_looks_like_sc2_client_process` both build on.
    Collapses `psutil.NoSuchProcess` (the PID is dead, or was reused by
    something that has since exited too -- this also covers
    `psutil.ZombieProcess`, a `NoSuchProcess` subclass) and
    `psutil.AccessDenied` (platform/permission edge case, e.g. sandboxing) to
    the same `None` -- every caller here treats both identically: cannot
    confirm this is what we think it is, so don't touch it."""
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _is_sc2_sdk_mcp_process(pid: int) -> bool:
    """True if `pid` is a live process whose command line identifies it as
    an `sc2-sdk-mcp` instance (see `_SC2_SDK_MCP_CMDLINE_MARKERS`) -- used by
    `_terminate_stale_instance` to guard against PID reuse: a lockfile can
    outlive the process it describes (e.g. that process crashed without
    cleaning up after itself), and by the time a later guard reads it, the
    OS may have handed that same PID to a completely unrelated process.
    Checking liveness alone is not enough; this also confirms the live
    process at that PID is still plausibly the one the lockfile described
    before anything gets terminated."""
    cmdline = _process_cmdline(pid)
    return cmdline is not None and any(marker in cmdline for marker in _SC2_SDK_MCP_CMDLINE_MARKERS)


def _looks_like_sc2_client_process(pid: int) -> bool:
    """True if `pid` is a live process whose command line contains the
    current platform's SC2 client binary marker (`install.paths.BINARY_NAME`
    -- the same name `install.paths.has_valid_install` already matches
    against an install directory, reused here rather than inventing a second
    SC2-executable-name convention).

    Deliberately NOT used anywhere in this module as a way to *discover*
    candidate PIDs to terminate (e.g. by scanning all running processes, or
    all children of some PID, for this marker) -- see this section's
    "Explicit ownership tracking, not pattern matching" docstring above for
    why that would be unsafe (it would risk matching a user's own, manually-
    started SC2 client). This is used only as a defensive second check
    against a PID `_terminate_stale_instance` already has *explicit,
    recorded* grounds to believe is one of this project's own spawned
    clients (from a lockfile's `sc2_pids`) -- confirming the identity of an
    already-targeted PID, never selecting the target."""
    cmdline = _process_cmdline(pid)
    if cmdline is None:
        return False
    marker = Path(BINARY_NAME.get(platform_name(), "")).name
    return bool(marker) and marker in cmdline


def _terminate_pid(
    pid: int,
    *,
    terminate_wait_seconds: float = _TERMINATE_WAIT_SECONDS,
    kill_wait_seconds: float = _KILL_WAIT_SECONDS,
) -> bool:
    """SIGTERM `pid`, wait up to `terminate_wait_seconds` for it to exit,
    and if it hasn't, escalate to SIGKILL and wait up to `kill_wait_seconds`
    more. Returns whether the process is confirmed gone by the time this
    returns.

    SIGTERM first, not SIGKILL immediately: gives a stale `sc2-sdk-mcp`
    process every chance to run its own cleanup (in case it somehow *does*
    have a working shutdown path -- e.g. it's blocked in the stdio EOF path
    this section's root-cause notes above describe as normally working,
    just stuck on a harness that never delivers the EOF) before this guard
    forces the issue. Bounded, short waits at each stage (not an unbounded
    `Process.wait()`) so a stuck stale instance can't delay this process's
    own startup indefinitely -- see `_TERMINATE_WAIT_SECONDS`/
    `_KILL_WAIT_SECONDS`'s docstrings for why these are short by design.

    Swallows `psutil.NoSuchProcess` at every step (the process exiting
    exactly while this function is acting on it is success, not failure) and
    returns `True` in that case."""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True

    with contextlib.suppress(psutil.NoSuchProcess):
        proc.terminate()
    with contextlib.suppress(psutil.NoSuchProcess, psutil.TimeoutExpired):
        proc.wait(timeout=terminate_wait_seconds)
    if not proc.is_running():
        return True

    with contextlib.suppress(psutil.NoSuchProcess):
        proc.kill()
    with contextlib.suppress(psutil.NoSuchProcess, psutil.TimeoutExpired):
        proc.wait(timeout=kill_wait_seconds)
    return not proc.is_running()


def _terminate_stale_instance(
    lockfile_path: Path = DEFAULT_LOCKFILE_PATH,
    *,
    own_pid: "int | None" = None,
    terminate_wait_seconds: float = _TERMINATE_WAIT_SECONDS,
    kill_wait_seconds: float = _KILL_WAIT_SECONDS,
) -> dict[str, object]:
    """The single-instance guard's whole detection+cleanup mechanism,
    extracted into one small, independently-testable function (see
    `tests/test_mcp_server_single_instance.py` -- deliberately not
    exercising this through a real pair of `sc2-sdk-mcp` subprocesses, which
    would be slow/flaky; see that test module's own docstring). Called once,
    at startup, before this process serves anything -- see
    `_run_single_instance_guard`.

    Mechanism (lockfile, not a live process/PID scan): reads `lockfile_path`
    (see `_read_lockfile`) for a previously-recorded `{"mcp_pid": ...,
    "sc2_pids": [...]}`. A lockfile approach was chosen over scanning
    `ps`/`psutil` for "anything that looks like sc2-sdk-mcp" because the
    SC2-client half of the problem has no safe scanning option at all (see
    this section's "Explicit ownership tracking, not pattern matching"
    docstring) -- once one half of the mechanism has to be explicit,
    recorded state, using the same mechanism for both halves (the mcp
    process AND its SC2 client(s)) is simpler than mixing a scan for one and
    a lockfile for the other. If nothing is recorded, or what's recorded is
    no longer a live, still-identifiable `sc2-sdk-mcp` process (see
    `_is_sc2_sdk_mcp_process` -- also guards against a dead lockfile entry's
    PID having been reused by something unrelated since), this is a no-op:
    there is no stale instance to clean up.

    If a live, verified-stale `sc2-sdk-mcp` process IS found (and it is not
    `own_pid` -- this process's own PID should never appear as "stale"
    relative to itself, but the check is explicit rather than assumed):
    terminate it (`_terminate_pid` -- SIGTERM, bounded wait, escalate to
    SIGKILL). Then, independently, for each PID the lockfile recorded under
    `sc2_pids`: only if it is BOTH still alive AND still identifiable as an
    SC2 client (`_looks_like_sc2_client_process`) is it terminated the same
    way. Both checks matter and neither is skipped as an optimization: a PID
    that's already dead needs no action, and a live PID that no longer looks
    like an SC2 client (reused by something else in the meantime) must never
    be touched, no matter what the lockfile once said about it -- this is
    what makes it safe to act on `sc2_pids` at all, given they were recorded
    at *launch* time, possibly a while before this guard runs. This check is
    deliberately independent of whether the stale mcp process itself was
    found alive: even if the mcp process already died on its own (crashed,
    or somehow left a stale lockfile behind), its SC2 client(s) are still
    checked and cleaned up if the lockfile still lists them and they still
    verify -- because the empirical finding above (SIGTERM does not trigger
    `SC2Process`'s own SIGINT-only cleanup, and a crash triggers no cleanup
    at all) means the mcp process's own fate is no evidence at all about its
    child's.

    Returns a small report dict for logging/tests: `{"stale_mcp_pid": int |
    None, "stale_mcp_terminated": bool, "sc2_pids_terminated": list[int],
    "sc2_pids_skipped": list[int]}` -- "skipped" meaning "listed in the
    lockfile but not touched" (already dead, or failed the identity check),
    reported separately from "terminated" so a caller/test can assert on
    both outcomes precisely.
    """
    own_pid = own_pid if own_pid is not None else os.getpid()
    report: dict[str, object] = {
        "stale_mcp_pid": None,
        "stale_mcp_terminated": False,
        "sc2_pids_terminated": [],
        "sc2_pids_skipped": [],
    }

    lock = _read_lockfile(lockfile_path)
    if lock is None:
        return report

    stale_mcp_pid = lock.get("mcp_pid")
    recorded_sc2_pids = lock.get("sc2_pids") or []

    if isinstance(stale_mcp_pid, int) and stale_mcp_pid != own_pid and _is_sc2_sdk_mcp_process(stale_mcp_pid):
        logger.warning(
            f"sc2-sdk-mcp: found a stale prior instance (pid={stale_mcp_pid}) still running "
            f"from lockfile {lockfile_path} -- terminating it before starting."
        )
        report["stale_mcp_pid"] = stale_mcp_pid
        report["stale_mcp_terminated"] = _terminate_pid(
            stale_mcp_pid,
            terminate_wait_seconds=terminate_wait_seconds,
            kill_wait_seconds=kill_wait_seconds,
        )
        if report["stale_mcp_terminated"]:
            logger.info(f"sc2-sdk-mcp: stale instance (pid={stale_mcp_pid}) terminated.")
        else:
            logger.error(f"sc2-sdk-mcp: stale instance (pid={stale_mcp_pid}) did not exit even after SIGKILL.")

    for sc2_pid in recorded_sc2_pids:
        if not isinstance(sc2_pid, int):
            continue
        if psutil.pid_exists(sc2_pid) and _looks_like_sc2_client_process(sc2_pid):
            logger.warning(
                f"sc2-sdk-mcp: terminating stale SC2 client (pid={sc2_pid}) recorded for the "
                "prior instance above -- its own SIGINT-based cleanup does not run on SIGTERM "
                "(see this module's root-cause notes), so this is done explicitly rather than "
                "assumed."
            )
            if _terminate_pid(
                sc2_pid, terminate_wait_seconds=terminate_wait_seconds, kill_wait_seconds=kill_wait_seconds
            ):
                report["sc2_pids_terminated"].append(sc2_pid)
                logger.info(f"sc2-sdk-mcp: stale SC2 client (pid={sc2_pid}) terminated.")
            else:
                report["sc2_pids_skipped"].append(sc2_pid)
                logger.error(f"sc2-sdk-mcp: stale SC2 client (pid={sc2_pid}) did not exit even after SIGKILL.")
        else:
            report["sc2_pids_skipped"].append(sc2_pid)

    return report


def _run_single_instance_guard(lockfile_path: Path = DEFAULT_LOCKFILE_PATH) -> None:
    """Called once, at the very top of `main()`, before this process builds
    a game or serves any MCP traffic: cleans up a stale prior `sc2-sdk-mcp`
    instance if one is found (`_terminate_stale_instance`), then claims the
    lockfile for this process (an empty `sc2_pids` -- none have been spawned
    yet at this point in startup) and registers a best-effort `atexit` hook
    to release it again on a normal exit, so a *clean* shutdown doesn't
    leave the next startup thinking there's a stale instance to clean up
    (harmless if it does -- `_terminate_stale_instance` would just find the
    recorded PID already dead and skip it -- but there is no reason to leave
    stale-looking state behind when exiting normally)."""
    _terminate_stale_instance(lockfile_path, own_pid=os.getpid())
    _owned_sc2_pids.clear()
    _write_lockfile(lockfile_path, os.getpid(), _owned_sc2_pids)

    def _release_lockfile_if_ours() -> None:
        lock = _read_lockfile(lockfile_path)
        if lock is not None and lock.get("mcp_pid") == os.getpid():
            with contextlib.suppress(OSError):
                lockfile_path.unlink()

    atexit.register(_release_lockfile_if_ours)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GameConfig:
    """The six parameters that fully describe "what kind of game to start"
    -- exactly `_build_session`'s (and therefore `_parse_args`'/`main()`'s)
    own parameter list, bundled into one value instead of threaded through
    as six loose arguments everywhere a game gets constructed.

    Two call sites build one of these:
      - `_build_session`, from whatever `main()` parsed off the CLI (or
        whatever a test/caller passed to `serve_execute_code` directly) --
        this becomes `build_server`'s `defaults`, i.e. "the kind of game
        `sc2-sdk-mcp` was originally launched with".
      - the `new_game` MCP tool (see `build_server`), which overlays
        whichever of its own optional per-call arguments a caller actually
        passed on top of that same `defaults` value, so any argument the
        caller omitted falls back to the original launch configuration
        rather than to a second, independently-hardcoded set of "sensible
        defaults" that could silently drift from `_build_session`'s own.
    """

    map_name: str = DEFAULT_MAP
    my_race: Race = Race.Terran
    opponent_race: Race = Race.Random
    difficulty: Difficulty = Difficulty.Easy
    game_time_limit: int | None = None
    realtime: bool = False
    #: Server-wide default passed to each new ExecuteCodeBotAI's
    #: `default_snippet_timeout_seconds` -- see DEFAULT_SNIPPET_TIMEOUT_SECONDS
    #: for the value's reasoning and the module docstring's "Per-call
    #: timeout and single automatic retry" section for the mechanism. Kept
    #: on _GameConfig, not passed to ExecuteCodeBotAI.__init__ separately,
    #: for the same reason as every other field here: so `new_game` (which
    #: falls back to `defaults` -- see build_server's new_game docstring)
    #: reproduces "the same timeout sc2-sdk-mcp was launched with" unless a
    #: caller explicitly overrides it, without needing a second,
    #: independently-hardcoded default that could drift from this one.
    snippet_timeout_seconds: float = DEFAULT_SNIPPET_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ExecuteCodeResult:
    """What one `execute_code` call reports back.

    Mirrors the `ok`/`error` shape `outcomes.py` already established for
    `bot.*` actions, extended with `stdout` (anything the snippet printed)
    and `result` (`repr()` of the snippet's trailing-expression value, if
    any -- see module docstring). `result`/`stdout`/`error` are kept as
    plain strings rather than raw Python objects because this is what
    crosses the MCP tool boundary as the JSON response body.
    """

    ok: bool
    result: str | None
    stdout: str
    error: str | None
    traceback: str | None


async def _eval_snippet(code: str, global_vars: dict[str, object]) -> ExecuteCodeResult:
    """Evaluate `code` against `global_vars` (`{"bot": ..., "sdk": ...}`)
    exactly like one REPL cell -- see module docstring for the exact
    semantics. Never raises: any failure (a syntax error, an exception
    raised while running) comes back as `ok=False` with `error`/`traceback`
    populated instead."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return ExecuteCodeResult(ok=False, result=None, stdout="", error=f"SyntaxError: {exc}", traceback=None)

    # If the snippet's last statement is a bare expression (e.g. the final
    # line of `x = 1; x + 1`), capture its value the way a REPL would,
    # instead of silently discarding it.
    injected_return = False
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body[-1]
        tree.body[-1] = ast.copy_location(ast.Return(value=last.value), last)
        injected_return = True

    wrapper_name = "__execute_code_snippet__"
    wrapper = ast.AsyncFunctionDef(
        name=wrapper_name,
        args=ast.arguments(
            posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]
        ),
        body=tree.body if tree.body else [ast.Pass()],
        decorator_list=[],
        returns=None,
        lineno=1,
        col_offset=0,
    )
    module = ast.Module(body=[wrapper], type_ignores=[])
    ast.fix_missing_locations(module)

    stdout = io.StringIO()
    try:
        code_obj = compile(module, "<execute_code>", "exec")
        local_ns: dict[str, object] = {}
        with contextlib.redirect_stdout(stdout):
            exec(code_obj, global_vars, local_ns)  # noqa: S102 -- this IS the feature: execute agent-authored code
            value = await local_ns[wrapper_name]()
            if injected_return and asyncio.iscoroutine(value):
                # The snippet's trailing expression evaluated to a
                # coroutine it forgot to `await` (e.g. `bot.train(...)`
                # with no `await`) -- await it rather than handing the
                # caller an opaque coroutine object it can't do anything
                # useful with.
                value = await value
    except Exception as exc:  # noqa: BLE001 -- any snippet failure is reported, not a crash; see module docstring
        return ExecuteCodeResult(
            ok=False,
            result=None,
            stdout=stdout.getvalue(),
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
        )

    return ExecuteCodeResult(
        ok=True,
        result=None if value is None else repr(value),
        stdout=stdout.getvalue(),
        error=None,
        traceback=None,
    )


def _snippet_result_is_truthy(result: ExecuteCodeResult) -> bool:
    """Interprets a background task turn's `ExecuteCodeResult` per the
    "goal met?" convention documented in the module docstring's "Standing
    background tasks" section: `True` (goal met, stop) if the snippet's
    returned value, reconstructed from `result.result`'s `repr()` string
    via `ast.literal_eval`, is itself truthy; `False` (keep going) for
    everything else -- including `result.ok is False` (an exception, or a
    both-attempts-timed-out turn; see `_finish_task_turn`, which never
    even calls this for that case, but this function treats it
    conservatively the same way if it ever were), `result.result is None`
    (the snippet's trailing value was `None`, itself falsy), and a
    `result.result` that doesn't round-trip through `ast.literal_eval` at
    all (e.g. a snippet that returned a live object like a `TrainOutcome`
    dataclass instance, whose `repr()` isn't a Python literal) -- the
    latter deliberately errs toward "keep going" rather than raising or
    guessing, since only a clean, unambiguous literal (in practice: a bare
    `True`) is meant to signal task completion under this convention.

    Deliberately does NOT change `_eval_snippet`'s return contract to
    carry the raw Python value alongside its `repr()` string -- that value
    already crosses back out to a real MCP client as `result.result`
    (`dataclasses.asdict(result)`, see `execute_code`), and stashing the
    live object on `ExecuteCodeResult` just for this internal purpose
    would either leak an extra field into every ordinary `execute_code`
    response or require carving out a wire-incompatible "internal" variant
    of the dataclass -- both more invasive than reconstructing the literal
    from the same string representation `execute_code` callers already
    receive and rely on."""
    if not result.ok or result.result is None:
        return False
    try:
        value = ast.literal_eval(result.result)
    except (ValueError, SyntaxError):
        return False
    return bool(value)


@dataclass
class _PendingRequest:
    """One `execute_code` call, or one background-task turn (see the
    module docstring's "Standing background tasks" section), queued for
    the next `on_step` to run -- both kinds flow through the exact same
    `self._queue`/`on_step` consumption path (see `on_step`'s own
    docstring for why sharing that path, rather than adding a second one,
    is what keeps at most one snippet ever running against `self.bot`/
    `self.sdk` at a time).

    `timeout_seconds` is the *effective* per-call timeout -- the caller's
    own `timeout_seconds` override if it passed one, else the server's
    `default_snippet_timeout_seconds` -- resolved once, in `submit()` (for
    an ordinary call) or `ExecuteCodeBotAI._enqueue_task_turn` (for a task
    turn, which always uses `default_snippet_timeout_seconds` -- a task's
    step code is not expected to need a different timeout per turn than an
    ordinary snippet would), at enqueue time. It is carried on the request
    itself (not re-resolved inside `on_step`) so that a retry (see
    `attempt` below) reuses exactly the same timeout its first attempt
    used, even if the server default could in principle change out from
    under it (it can't today -- the default is fixed for an
    `ExecuteCodeBotAI`'s whole lifetime -- but resolving once here keeps
    that invariant obviously true rather than incidentally true).

    `attempt` starts at 1 (the original try) and is bumped to 2 when
    `on_step` requeues this exact request after a first timeout -- see the
    module docstring's "Per-call timeout and single automatic retry"
    section. `on_step` only ever retries once (`attempt < 2`), so this
    never exceeds 2 in practice; it's an `int` rather than a `bool`
    ("already retried?") mainly so the timeout-exhausted error message
    below can report "1 original attempt + 1 retry" precisely rather than
    reconstructing that from a flag.

    Exactly one of `future`/`task_id` is meaningful for a given request,
    and `on_step` branches on `task_id is not None` to decide which:
      - An ordinary `execute_code` call (built by `submit()`) sets
        `future` to the caller's own result future (see `submit()`) and
        leaves `task_id` as `None` -- `on_step` resolves `future` directly
        with the outcome, exactly as before background tasks existed.
      - A background-task turn (built by `_enqueue_task_turn`) sets
        `task_id` to its owning `_TaskState.task_id` and leaves `future`
        as `None` -- nobody is synchronously blocked on a task turn (that
        is the entire point of `start_task` returning immediately), so
        there is no future to resolve; `on_step` instead hands the
        outcome to `_finish_task_turn`, which updates that `_TaskState`
        and decides whether to enqueue another turn.
    A single dataclass with an optional field was chosen over two
    unrelated request types sharing one `asyncio.Queue` (e.g. a `Union`)
    because the two kinds already share every field but this one
    (`code`/`timeout_seconds`/`attempt`), including the entire
    timeout/retry state machine in `on_step` -- introducing a second class
    would just mean threading an `isinstance` check through both of
    `on_step`'s existing branches anyway, for no less code.
    """

    code: str
    future: "asyncio.Future[ExecuteCodeResult] | None"
    timeout_seconds: float
    attempt: int = 1
    #: `None` for an ordinary `execute_code` call; the owning task's
    #: `task_id` for a background-task turn -- see this dataclass's
    #: docstring above.
    task_id: "str | None" = None


def _format_task_log_entry(turn_number: int, result: ExecuteCodeResult) -> str:
    """One line summarizing a completed background-task turn, appended to
    that task's bounded `_TaskState.log` by `_finish_task_turn` -- what a
    caller polling `task_status` mid-task actually reads to see recent
    progress (see the module docstring's "Standing background tasks"
    section on why this is pull-based rather than pushed). Kept to a
    single line per turn (not the full `stdout`/`traceback`, which can be
    arbitrarily long) so `_TASK_LOG_MAX_ENTRIES` bounds total log size
    predictably -- see that constant's docstring."""
    if not result.ok:
        return f"[turn {turn_number}] FAILED: {result.error}"
    stdout_suffix = f" stdout={result.stdout!r}" if result.stdout else ""
    return f"[turn {turn_number}] result={result.result}{stdout_suffix}"


@dataclass
class _TaskState:
    """Bookkeeping for one `start_task`-registered background task, stored
    in `ExecuteCodeBotAI._tasks` keyed by `task_id` -- see the module
    docstring's "Standing background tasks" section for the overall
    mechanism this backs.

    `status` is one of `"running"`, `"done"`, `"failed"`, or `"cancelled"`
    -- the last of these is a deliberate, documented extension beyond the
    three-value `"running"|"done"|"failed"` `task_status` was originally
    scoped to: collapsing "the caller asked to stop this" into `"failed"`
    would make `task_status`'s `error` field ambiguous between "something
    actually went wrong" and "this was cancelled on purpose", which seemed
    more likely to confuse a calling agent checking in on a task than a
    fourth, self-explanatory status value would.

    `cancel_requested` is set by `cancel_task` and consulted exactly once
    per turn, inside `_finish_task_turn`, right after that turn's outcome
    is known -- never mid-turn (see `_finish_task_turn`'s docstring for
    why at most one turn per task is ever in flight or queued at a time,
    which is what makes "check the flag between turns" sufficient instead
    of needing to interrupt a turn already running).

    `log` is a `collections.deque(maxlen=_TASK_LOG_MAX_ENTRIES)` -- see
    that constant's docstring for the size/why-a-count-not-a-char-budget
    reasoning -- of `_format_task_log_entry` lines, oldest evicted first,
    so `task_status` always reflects the most *recent* activity rather
    than growing without bound over a task's whole lifetime.

    `result`/`error` are populated (mutually exclusive) once `status` is
    no longer `"running"`: `result` is the final truthy turn's
    `ExecuteCodeResult.result` (the same `repr()` string an ordinary
    `execute_code` caller would see) when `status == "done"`; `error`
    names why otherwise (an exception, a both-attempts-timed-out turn, or
    `max_iterations` exhausted) when `status == "failed"`.
    """

    task_id: str
    code: str
    description: str
    max_iterations: int
    status: str = "running"
    iterations: int = 0
    cancel_requested: bool = False
    result: "str | None" = None
    error: "str | None" = None
    log: "deque[str]" = dataclasses.field(default_factory=lambda: deque(maxlen=_TASK_LOG_MAX_ENTRIES))


@dataclass
class _HostGameState:
    """Bookkeeping for one `host_game`-started hosted match, stored on the
    `ExecuteCodeBotAI` it belongs to (`ExecuteCodeBotAI.host_state`) -- see
    the module docstring's "Hosting a two-player match" section for the
    overall design.

    `match_code` is recorded here (rather than only ever handed back once,
    in `host_game`'s own return value) so `host_status` can report it again
    on every poll without the caller needing to have saved it separately.

    `timed_out` starts `False` and is set exactly once, by the `game_task`
    wrapper `_launch_hosted_game` builds, if `_run_host_role` raises
    `sdk.matchcode.JoinTimeoutError` (no peer connected within the
    configured `join_timeout`) -- see `_host_status` for how this
    distinguishes "nobody ever joined" from any other way a hosted match's
    `game_task` could end without `bot_ai.ready` ever having been set.
    """

    match_code: str
    timed_out: bool = False


class ExecuteCodeBotAI(VerifiedBotAI):
    """A `VerifiedBotAI` (see `bot.py`) whose `on_step` blocks on an
    external snippet queue instead of running a fixed scripted sequence --
    see module docstring for why this is what makes the game "pause"
    between `execute_code` calls.

    `game_task` is set by `serve_execute_code()` once it has created the
    task driving this bot through `_host_game` -- `submit()` races the
    caller's own future against it so a snippet submitted after the match
    has already ended reports a clear error instead of hanging forever
    waiting for an `on_step` call that will never come again.
    """

    def __init__(self, default_snippet_timeout_seconds: float = DEFAULT_SNIPPET_TIMEOUT_SECONDS) -> None:
        super().__init__()
        #: Set once on_start has wired up self.bot/self.sdk -- execute_code
        #: callers should wait on this before submitting a snippet.
        self.ready: asyncio.Event = asyncio.Event()
        self._queue: "asyncio.Queue[_PendingRequest]" = asyncio.Queue()
        #: Set from outside, after this instance has been handed to
        #: asyncio.create_task(_host_game(...)) -- see serve_execute_code.
        self.game_task: "asyncio.Task[Result] | None" = None
        #: The real OS pid of the SC2 client process `_host_game` launches
        #: for this game, once known -- populated asynchronously (typically
        #: within a fraction of a second, well before the game is `ready`)
        #: by `_launch_game`'s `_pending_sc2_pid_capture` closure, which
        #: `_patched_sc2process_launch` calls the moment the real
        #: `subprocess.Popen` exists. `None` until then, and also `None`
        #: forever if that launch never actually spawned a process (e.g. an
        #: immediate `_launch()` failure). See this module's "Single-instance
        #: guard + explicit SC2-client PID tracking" section for why this is
        #: captured this way instead of via a PPID scan, and `new_game`'s use
        #: of this field (in `build_server`) for how it's untracked once the
        #: game it belongs to is torn down.
        self.sc2_pid: "int | None" = None
        #: Server-wide default for a submit() call that doesn't pass its
        #: own timeout_seconds override -- see DEFAULT_SNIPPET_TIMEOUT_SECONDS
        #: and _GameConfig.snippet_timeout_seconds for where this value
        #: comes from (threaded in by _launch_game) and why it persists
        #: across new_game calls.
        self.default_snippet_timeout_seconds = default_snippet_timeout_seconds
        #: Every background task registered against THIS game instance via
        #: start_task, keyed by task_id -- see the module docstring's
        #: "Standing background tasks" section. A fresh, empty dict on
        #: every new ExecuteCodeBotAI, which is what makes a task not
        #: survive new_game "for free": new_game (see build_server) swaps
        #: active.bot_ai to a brand new instance of this class, and the OLD
        #: instance's _tasks dict -- along with everything referencing it --
        #: simply stops being reachable from any tool call, the same way
        #: its _queue does today for an ordinary in-flight execute_code
        #: call.
        self._tasks: "dict[str, _TaskState]" = {}
        #: Source of task_id values handed out by start_task -- a short,
        #: per-instance incrementing counter (formatted as "task-<n>" by
        #: start_task) rather than a UUID: task_ids are only ever meaningful
        #: within one ExecuteCodeBotAI's lifetime (see _tasks above --
        #: they don't survive new_game, and there is no cross-process or
        #: cross-game identity concern to defend against), so a short,
        #: human-readable, easy-to-read-back-in-conversation id is more
        #: useful here than a UUID's collision-resistance, which isn't
        #: needed at this scope.
        self._next_task_id: int = 1
        #: Set by `_launch_hosted_game` (see the module docstring's
        #: "Hosting a two-player match" section) for a game started via the
        #: `host_game` MCP tool; `None` for every other kind of game
        #: (solo-vs-built-in-AI via `new_game`/`_launch_game`) -- what
        #: `host_status` checks to report a clear error instead of "waiting"
        #: forever if it's called against a non-hosted game.
        self.host_state: "_HostGameState | None" = None

    async def on_start(self) -> None:
        await super().on_start()
        self.ready.set()

    async def on_step(self, iteration: int) -> None:
        # This blocking get() -- not any change to python-sc2's own
        # stepping code -- is the entire mechanism behind "the game runs in
        # non-realtime/stepped mode... pausing for each call": see module
        # docstring. request may be an ordinary execute_code call OR one
        # turn of a background task (see _PendingRequest's docstring) --
        # both are drained from this SAME queue, one at a time, by this
        # SAME await, which is exactly what guarantees at most one snippet
        # is ever running against self.bot/self.sdk at once regardless of
        # how many ordinary calls and/or background tasks are currently
        # pending: there is only ever one on_step "in progress" per game
        # (python-sc2 itself only calls on_step once at a time, awaiting
        # each call to return before the next), and this is its only body.
        request = await self._queue.get()
        try:
            result = await asyncio.wait_for(
                _eval_snippet(request.code, {"bot": self.bot, "sdk": self.sdk}),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError:
            # See the module docstring's "Per-call timeout and single
            # automatic retry" section. wait_for already cancelled
            # _eval_snippet's task -- an ordinary asyncio.CancelledError
            # propagation, the same mechanism new_game's game_task.cancel()
            # relies on -- so nothing further is needed to stop the
            # runaway snippet itself; what's left is only the retry policy.
            # This applies identically to a background-task turn: a turn
            # that hangs gets the same one-retry treatment an ordinary
            # snippet does, reusing this exact branch rather than a
            # parallel implementation (see the module docstring's
            # "Standing background tasks" section).
            if request.attempt < 2:
                # First timeout: leave the caller's future unresolved (its
                # execute_code MCP call stays pending) and move this exact
                # request to the END of the queue for one more shot, after
                # anything already queued behind it gets processed first.
                # For a task turn (no future to leave pending), this is
                # simply "try this turn again" -- nothing else observes a
                # task between turns except task_status, which will just
                # keep reporting "running" with today's iteration count
                # until the retry resolves one way or the other.
                request.attempt += 1
                await self._queue.put(request)
                return

            # Second timeout (the retry also timed out): give up and
            # report a clear, structured failure -- this is what "mention
            # it to the user so they know what failed" means in practice,
            # since this ExecuteCodeResult is exactly what crosses back
            # over the MCP tool boundary to the caller (see build_server's
            # execute_code), or -- for a task turn -- becomes that task's
            # recorded failure (see _finish_task_turn).
            timeout_result = ExecuteCodeResult(
                ok=False,
                result=None,
                stdout="",
                error=(
                    f"Snippet timed out after {request.timeout_seconds:g}s on both the "
                    "original attempt and one automatic retry (2 attempts total). If this "
                    "snippet is expected to legitimately run this long (e.g. waiting on "
                    "slow resource income), pass a larger timeout_seconds to execute_code. "
                    f"Code that timed out:\n{request.code}"
                ),
                traceback=None,
            )
            if request.task_id is not None:
                self._finish_task_turn(request.task_id, timeout_result)
            elif request.future is not None and not request.future.done():
                request.future.set_result(timeout_result)
            return

        if request.task_id is not None:
            self._finish_task_turn(request.task_id, result)
        elif request.future is not None and not request.future.done():
            request.future.set_result(result)

    def _enqueue_task_turn(self, state: "_TaskState") -> None:
        """Puts one more turn of `state`'s step code onto `self._queue`,
        for `on_step` to eventually run -- called once by `start_task` (the
        task's first turn) and again by `_finish_task_turn` each time a
        turn completes with a falsy, non-terminal result. `put_nowait`
        (never blocks, since `self._queue` is unbounded -- see
        `asyncio.Queue()` in `__init__`) rather than `await ...put(...)`
        because this can be called from a plain, non-async method
        (`start_task`) as well as from `on_step`'s own coroutine, and
        never needs to suspend either way."""
        self._queue.put_nowait(
            _PendingRequest(
                code=state.code,
                future=None,
                timeout_seconds=self.default_snippet_timeout_seconds,
                task_id=state.task_id,
            )
        )

    def _finish_task_turn(self, task_id: str, result: ExecuteCodeResult) -> None:
        """Called from `on_step` once a background-task turn's outcome is
        known -- either an ordinary `_eval_snippet` result (success or a
        caught exception) or the synthetic timeout-exhausted
        `ExecuteCodeResult` `on_step` builds after both attempts time out.
        Updates `self._tasks[task_id]`'s bookkeeping and decides what
        happens next: end the task (`"done"`/`"failed"`/`"cancelled"`) or
        enqueue one more turn (`_enqueue_task_turn`) -- see the module
        docstring's "Standing background tasks" section and `_TaskState`'s
        docstring for the full state machine this implements.

        Guaranteed to be the only place that ever mutates a given task's
        `_TaskState` after it's created (`start_task` only constructs and
        registers it) -- and, by construction, is only ever invoked for
        AT MOST one turn of a given task at a time, since a task's next
        turn is enqueued from inside this very method, strictly after the
        current turn's outcome is already known. That's what makes reading
        `state.cancel_requested` here (set asynchronously by `cancel_task`,
        possibly while this exact turn was in flight) safe without any
        additional locking: there is no concurrent writer to race against,
        only a flag that may have flipped to `True` sometime before this
        specific read.

        A missing or already-finished `state` (looked up by `task_id`) is
        a silent no-op, not an error -- defensive only: by the invariant
        above this should never actually happen for a task turn drained
        from `self._queue` in the ordinary course of events, but a
        `new_game`-triggered cancellation could in principle interleave
        oddly with a bug elsewhere, and silently dropping a stale result
        is safer than resurrecting a task a caller has already been told
        is finished."""
        state = self._tasks.get(task_id)
        if state is None or state.status != "running":
            return

        state.iterations += 1
        state.log.append(_format_task_log_entry(state.iterations, result))

        if not result.ok:
            state.status = "failed"
            state.error = result.error
            return

        if _snippet_result_is_truthy(result):
            state.status = "done"
            state.result = result.result
            return

        if state.cancel_requested:
            state.status = "cancelled"
            return

        if state.iterations >= state.max_iterations:
            state.status = "failed"
            state.error = (
                f"Exhausted max_iterations ({state.max_iterations}) without the step code's "
                "return value ever evaluating truthy -- see start_task's docstring for the "
                "goal-completion convention this relies on. If this task's goal is real but "
                "just needs more turns, start a new task with a larger max_iterations."
            )
            return

        self._enqueue_task_turn(state)

    def start_task(self, code: str, description: str, max_iterations: int = DEFAULT_TASK_MAX_ITERATIONS) -> str:
        """Registers a new background task and enqueues its first turn --
        see the `start_task` MCP tool (in `build_server`) for the
        caller-facing contract, and the module docstring's "Standing
        background tasks" section for the overall mechanism. Synchronous
        and non-blocking (no `await` anywhere in this method): the queue
        put is a `put_nowait` (see `_enqueue_task_turn`), so this returns
        to its caller -- the `start_task` MCP tool, which does nothing
        else after this call but package the `task_id` into its response
        -- before the first turn has even had a chance to run, let alone
        the whole goal complete."""
        task_id = f"task-{self._next_task_id}"
        self._next_task_id += 1
        state = _TaskState(task_id=task_id, code=code, description=description, max_iterations=max_iterations)
        self._tasks[task_id] = state
        self._enqueue_task_turn(state)
        return task_id

    def cancel_task(self, task_id: str) -> dict[str, object]:
        """Requests that `task_id` stop scheduling further turns -- see the
        `cancel_task` MCP tool (in `build_server`) for the caller-facing
        contract. Only ever sets a flag (`_TaskState.cancel_requested`);
        never touches `self._queue` or attempts to interrupt a turn
        that's already running or already queued -- see `_finish_task_turn`'s
        docstring for why checking the flag between turns is sufficient
        and correct, and the module docstring's "Standing background
        tasks" section for why interrupting an in-flight turn is
        deliberately out of scope."""
        state = self._tasks.get(task_id)
        if state is None:
            return {"ok": False, "task_id": task_id, "error": f"Unknown task_id: {task_id!r}"}
        if state.status != "running":
            return {
                "ok": False,
                "task_id": task_id,
                "error": f"Task {task_id!r} is already {state.status!r}; nothing to cancel.",
            }
        state.cancel_requested = True
        return {"ok": True, "task_id": task_id, "status": state.status}

    def list_task_ids(self) -> "list[str]":
        """Every `task_id` currently registered against this game instance,
        in registration order -- backs `task_status()`'s no-argument
        "list everything" form (see `build_server`). A thin, read-only view
        over `self._tasks`'s keys (a `dict`, so insertion order is already
        preserved) rather than exposing `self._tasks` itself, keeping this
        class's internal bookkeeping structure private to it."""
        return list(self._tasks)

    def task_status(self, task_id: str) -> dict[str, object]:
        """Snapshots `task_id`'s current bookkeeping into a plain dict --
        see the `task_status` MCP tool (in `build_server`) for the
        caller-facing contract. Called fresh on every invocation (no
        caching): `self._tasks[task_id]` is mutated in place by
        `_finish_task_turn` as each turn completes, so this always reflects
        genuinely current progress, including while the task is still
        `"running"` -- not just a snapshot taken once at `start_task` time
        or only once the task finishes."""
        state = self._tasks.get(task_id)
        if state is None:
            return {"ok": False, "task_id": task_id, "error": f"Unknown task_id: {task_id!r}"}
        return {
            "ok": True,
            "task_id": state.task_id,
            "description": state.description,
            "status": state.status,
            "iterations": state.iterations,
            "max_iterations": state.max_iterations,
            "log": list(state.log),
            "result": state.result,
            "error": state.error,
        }

    async def submit(self, code: str, timeout_seconds: float | None = None) -> ExecuteCodeResult:
        """Called by the `execute_code` MCP tool handler: enqueue `code`
        for the next `on_step` to run, and wait for its result -- or for a
        clear error if the match ends before that happens.

        `timeout_seconds`, if given, overrides `self.default_snippet_timeout_seconds`
        for this call only -- resolved to a concrete float right here, once,
        and carried on the `_PendingRequest` (see its docstring for why),
        so a first-timeout retry (see `on_step`) reuses this same call's
        chosen timeout rather than re-resolving against the server default."""
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else self.default_snippet_timeout_seconds
        )
        future: "asyncio.Future[ExecuteCodeResult]" = asyncio.get_running_loop().create_future()
        await self._queue.put(_PendingRequest(code=code, future=future, timeout_seconds=effective_timeout))

        if self.game_task is None:
            return await future

        done, _pending = await asyncio.wait({future, self.game_task}, return_when=asyncio.FIRST_COMPLETED)
        if future in done:
            return future.result()

        # game_task finished first: the match ended before our snippet's
        # turn came up (e.g. it was queued right as the game concluded).
        if self.game_task.cancelled():
            match_report = "the game task was cancelled"
        elif self.game_task.exception() is not None:
            match_report = f"the game task raised {self.game_task.exception()!r}"
        else:
            match_report = f"match_result={self.game_task.result()!r}"
        return ExecuteCodeResult(
            ok=False,
            result=None,
            stdout="",
            error=f"Match ended before this snippet could run ({match_report}). Call bot.observe() instead.",
            traceback=None,
        )


@dataclass
class _ActiveGame:
    """Mutable indirection layer between the `execute_code`/`new_game` MCP
    tool closures (each defined exactly once, inside `build_server`, for
    the life of the `FastMCP` server/stdio connection) and "whichever
    `ExecuteCodeBotAI`/`game_task` is the current game" -- `new_game`'s
    entire reason for existing (see `build_server`'s docstring).

    Design note -- why a mutable holder instead of a plain closure
    variable: the original ticket #6 `build_server(bot_ai)` took a single
    `bot_ai` parameter and the `execute_code` tool closed over it directly,
    permanently. That's fine as long as there is exactly one game for the
    life of the process -- but it means there is *no* name a later
    `new_game` call could rebind to make subsequent `execute_code` calls
    see a different game, short of tearing down and rebuilding the whole
    `FastMCP` server (which is exactly the stdio-reconnect pain this
    ticket exists to remove). Routing both tools through one shared,
    mutable `_ActiveGame` instead means "swap the current game" is just
    two attribute assignments (see `new_game`, in `build_server`) that
    every subsequent `execute_code` call picks up automatically, because
    it reads `active.bot_ai` fresh at call time rather than a value fixed
    when `build_server` ran.

    `lock` serializes concurrent `new_game` calls against *each other* --
    so two overlapping `new_game` invocations can't both read the same
    `active.game_task`, both decide it needs cancelling, and race to
    overwrite `active.bot_ai`/`active.game_task` out from under one
    another. It is deliberately NOT acquired by `execute_code`, which
    never touches it -- see `new_game`'s docstring (in `build_server`) for
    the full reasoning, including what happens to an `execute_code` call
    already in flight against the old game when `new_game` runs
    concurrently.
    """

    bot_ai: ExecuteCodeBotAI
    game_task: "asyncio.Task[Result]"
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)


@dataclass
class ExecuteCodeSession:
    """What `serve_execute_code()` hands back: the live `FastMCP` server
    plus the `_ActiveGame` it's wired to, so a caller (a console-script
    entrypoint, or a test) can both serve the MCP tool and separately
    inspect/await the underlying game.

    `bot_ai`/`game_task` are properties, not plain fields, proxying
    through `active`: after a caller invokes the `new_game` tool (see
    `build_server`), `active.bot_ai`/`active.game_task` get reassigned to
    the new game -- these properties make `session.bot_ai`/
    `session.game_task` always reflect "whichever game is current", the
    same thing `execute_code` itself now talks to, rather than freezing at
    whatever game existed when this session was first constructed.
    """

    mcp: FastMCP
    active: _ActiveGame

    @property
    def bot_ai(self) -> ExecuteCodeBotAI:
        return self.active.bot_ai

    @property
    def game_task(self) -> "asyncio.Task[Result]":
        return self.active.game_task


def _install_sc2_pid_capture(bot_ai: ExecuteCodeBotAI) -> None:
    """Installs `bot_ai` as the target of `_pending_sc2_pid_capture` (see
    that global's docstring for why a single module-level slot is safe
    here) so that whenever the SC2 client this game is about to launch
    actually calls `SC2Process._launch()`, `_patched_sc2process_launch`
    records the real PID onto `bot_ai.sc2_pid` and into the single-instance
    guard's lockfile (`_track_sc2_pid`) -- see this module's "Single-instance
    guard + explicit SC2-client PID tracking" section for the full
    mechanism and why it exists.

    Shared by both `_launch_game` (solo-vs-built-in-AI, via `_host_game`)
    and `_launch_hosted_game` (a `host_game`-started match, via
    `_run_host_role` -- see the module docstring's "Hosting a two-player
    match" section): both ultimately construct a `SC2Process` somewhere
    inside the coroutine they schedule, and `_patched_sc2process_launch`'s
    interception point doesn't care which caller's `SC2Process` it is --
    only that exactly one `ExecuteCodeBotAI` is "pending capture" at the
    moment it fires, which is true either way for the same reason
    `_pending_sc2_pid_capture`'s docstring already gives."""
    def _capture_sc2_pid(pid: int) -> None:
        bot_ai.sc2_pid = pid
        _track_sc2_pid(pid, lockfile_path=DEFAULT_LOCKFILE_PATH)

    global _pending_sc2_pid_capture
    _pending_sc2_pid_capture = _capture_sc2_pid


def _launch_game(config: _GameConfig) -> tuple[ExecuteCodeBotAI, "asyncio.Task[Result]"]:
    """Construct one fresh `ExecuteCodeBotAI` and kick off its `game_task`
    via `_host_game` -- the shared plumbing both `_build_session` (the
    first game `sc2-sdk-mcp` starts) and the `new_game` MCP tool (every
    subsequent game) use, so there is exactly one place in this module
    that builds a `Sc2BotPlayer`/`Computer` pairing and calls
    `_host_game`/`asyncio.create_task` -- see the module docstring's "Why
    `sc2.main._host_game`..." section for why that call, specifically, is
    used here instead of the public `run_game()`.

    `bot_ai` is constructed with `config.snippet_timeout_seconds` as its
    `default_snippet_timeout_seconds` -- this is the one place that value
    actually reaches an `ExecuteCodeBotAI`, so a `new_game` call that
    overlays a new `_GameConfig` (see `build_server`'s `new_game`) and
    routes back through here is what makes the timeout survive across
    `new_game` calls the same way `realtime`/`map_name`/etc. already do.

    See `_install_sc2_pid_capture` for what installing the PID-capture
    hook before scheduling the task accomplishes."""
    bot_ai = ExecuteCodeBotAI(default_snippet_timeout_seconds=config.snippet_timeout_seconds)
    _install_sc2_pid_capture(bot_ai)

    game_task = asyncio.create_task(
        _host_game(
            maps.get(config.map_name),
            [Sc2BotPlayer(config.my_race, bot_ai), Computer(config.opponent_race, config.difficulty)],
            realtime=config.realtime,
            game_time_limit=config.game_time_limit,
        )
    )
    bot_ai.game_task = game_task
    return bot_ai, game_task


def _launch_hosted_game(
    *,
    map_name: str,
    my_race: Race,
    opponent_race_pin: "Race | None",
    host_ip: str,
    game_time_limit: "int | None",
    snippet_timeout_seconds: float,
    join_timeout: float,
) -> tuple[ExecuteCodeBotAI, "asyncio.Task[Result]", str]:
    """The `host_game` MCP tool's counterpart to `_launch_game`: construct
    one fresh `ExecuteCodeBotAI`, generate and encode a shareable match
    code, and kick off a `game_task` that creates the match, waits (up to
    `join_timeout`) for a peer to join it, then plays it out -- via
    `sdk.join._run_host_role`, not `_host_game` -- so once a peer connects,
    this game is driven by the exact same `ExecuteCodeBotAI.on_step`
    queue/`execute_code`/`start_task` machinery every other game in this
    module already uses. See the module docstring's "Hosting a two-player
    match" section for the full design this implements, including why
    `realtime` is unconditionally `True` here (never a parameter) and why
    `host_ip` defaults to loopback at the `host_game` tool layer, not here.

    Returns `(bot_ai, game_task, match_code)` -- unlike `_launch_game`,
    also handing back the match code, since there is no other channel for
    `host_game` (which does not wait for this task) to learn it.

    `bot_ai.host_state` is set here (not left for a caller to set) so it's
    never possible to observe a `bot_ai` with a live hosted `game_task` but
    no `host_state` -- `host_status`'s "no hosted game is active" check
    relies on that invariant.
    """
    bot_ai = ExecuteCodeBotAI(default_snippet_timeout_seconds=snippet_timeout_seconds)
    _install_sc2_pid_capture(bot_ai)

    portconfig = Portconfig()
    match_code = encode_match_code(
        host_ip=host_ip,
        portconfig=portconfig,
        map_name=map_name,
        race_pin=opponent_race_pin,
        token=secrets.token_urlsafe(16),
    )
    bot_ai.host_state = _HostGameState(match_code=match_code)

    async def _run_and_track() -> Result:
        # portconfig is this coroutine's to clean up -- mirrors
        # sdk.host_join.main_host's own try/finally around the same
        # Portconfig() it constructs, since that's the other (and, until
        # now, only) caller responsible for one of these.
        try:
            return await _run_host_role(
                map_name,
                my_race,
                bot_ai,
                portconfig,
                host_ip,
                True,  # realtime -- see the module docstring for why this is never a parameter
                game_time_limit,
                join_timeout=join_timeout,
            )
        except JoinTimeoutError:
            # See _HostGameState's docstring: recorded so host_status can
            # report a specific, clear reason rather than a generic
            # "the host task ended" failure. Re-raised (not swallowed) so
            # this task's own .exception()/.cancelled() reflect what
            # actually happened, the same as any other game_task failure
            # mode this module already lets propagate that way.
            bot_ai.host_state.timed_out = True
            raise
        finally:
            portconfig.clean()

    game_task = asyncio.create_task(_run_and_track())
    bot_ai.game_task = game_task
    return bot_ai, game_task, match_code


async def _teardown_active_game(active: _ActiveGame) -> bool:
    """Cancels `active`'s current `game_task` if it isn't already done, and
    untracks its SC2 client PID -- the "end whatever's running outright"
    step every way of starting a new game (`new_game`, `host_game`) shares.
    Factored out of `new_game`'s body (see the module docstring's "Hosting
    a two-player match" section for why) so there is exactly one place
    this is implemented; callers are expected to hold `active.lock` around
    this call, exactly as `new_game` already did before this was pulled
    out of it.

    Cancelling *immediately* rather than waiting for any in-flight
    `execute_code` snippet to finish first is deliberate -- see `new_game`'s
    own docstring below for the full reasoning (recovering a session
    wedged forever inside a runaway snippet is the other half of this
    behavior's job, and that case, by construction, never resolves on its
    own).

    Returns whether a previous game was actually torn down (`False` if
    `active.game_task` had already finished on its own before this call)."""
    old_bot_ai = active.bot_ai
    old_game_task = active.game_task
    previous_game_torn_down = False
    if not old_game_task.done():
        old_game_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await old_game_task
        previous_game_torn_down = True

    # Whether old_game_task above was just cancelled or had already
    # finished on its own before this call, the coroutine driving it
    # (_host_game or _run_host_role) has now fully exited either way --
    # its SC2 client is gone (via KillSwitch/__aexit__ cleanup; see this
    # module's "Single-instance guard..." section on SIGINT-based cleanup
    # normally working fine, it's only SIGTERM to a *stale* sc2-sdk-mcp
    # process later that can't rely on this). Drop it from this process's
    # own bookkeeping so the lockfile stops advertising a PID that's
    # already dead -- see _untrack_sc2_pid's docstring for why this is
    # hygiene, not a correctness requirement.
    _untrack_sc2_pid(old_bot_ai.sc2_pid, lockfile_path=DEFAULT_LOCKFILE_PATH)
    return previous_game_torn_down


def _host_status(bot_ai: ExecuteCodeBotAI, game_task: "asyncio.Task[Result]") -> dict[str, object]:
    """Snapshots a hosted game's join status into a plain dict -- backs the
    `host_status` MCP tool (see `build_server`). See the module docstring's
    "Hosting a two-player match" section for the full state derivation this
    implements and why no separate state machine is needed: every input
    here (`bot_ai.host_state`, `bot_ai.ready`, `game_task.done()`) already
    exists in this module for other reasons.

    `bot_ai.host_state is None` means `bot_ai` was not started via
    `host_game` at all (an ordinary solo game, or a hosted game that's
    since been replaced by `new_game`/another `host_game` call) -- reported
    as a clear `ok=False` error rather than a `status` value, since
    "waiting"/"joined"/"failed" all presuppose a hosted game actually
    exists to report on."""
    if bot_ai.host_state is None:
        return {
            "ok": False,
            "error": "No hosted game is active on the current game session. Call host_game first.",
        }

    state = bot_ai.host_state
    if bot_ai.ready.is_set():
        status = "joined"
        error = None
    elif game_task.done():
        status = "failed"
        if state.timed_out:
            error = "No peer joined within the configured join_timeout."
        elif game_task.cancelled():
            error = "The hosted game was cancelled (e.g. by a new_game call) before a peer joined."
        else:
            error = f"{type(game_task.exception()).__name__}: {game_task.exception()}"
    else:
        status = "waiting"
        error = None

    return {"ok": True, "match_code": state.match_code, "status": status, "error": error}


def build_server(active: _ActiveGame, defaults: _GameConfig, name: str = "sc2-sdk") -> FastMCP:
    """Build a `FastMCP` server exposing `execute_code`, `new_game`, and
    the standing-background-task trio `start_task`/`task_status`/
    `cancel_task` (see the module docstring's "Standing background tasks"
    section) against `active` -- all tools are defined once here and
    read/write `active.bot_ai`/`active.game_task` at call time rather than
    closing over a value fixed at construction time, so `new_game` can
    swap the game every other tool talks to without rebuilding this server
    (see `_ActiveGame`'s docstring for why that indirection is needed at
    all). Nothing about wiring any of these tools depends on `active.bot_ai`
    being a real, game-backed `ExecuteCodeBotAI` -- see `serve_execute_code()`
    below for how the real entrypoint constructs one."""
    mcp = FastMCP(name)

    @mcp.tool()
    async def execute_code(code: str, timeout_seconds: float | None = None) -> dict[str, object]:
        """Evaluate a Python snippet against live `bot`/`sdk` globals bound
        to the running game. The underlying game is paused (stepped, not
        real-time) while no snippet is being evaluated -- see this
        project's ticket #6 -- and advances by roughly one internal
        simulation step once this call returns and the next `on_step`
        begins waiting for the following call.

        Reads `active.bot_ai` once, at the top of this call, into a local
        -- "this call talks to whichever game was current when it
        started" -- rather than re-reading `active.bot_ai` again after the
        `ready.wait()` await below. That matters only if a `new_game` call
        races this one and swaps `active.bot_ai` out from under it between
        those two awaits; pinning the target once keeps this call's
        behavior a coherent "talk to one game start to finish" rather than
        possibly waiting on one game's `ready` event and then submitting
        to a different one. See `new_game`'s docstring for what happens to
        *this* call when that race happens and the old game gets torn
        down instead.

        This call is subject to a per-snippet timeout, with one automatic
        retry on timeout -- see the module docstring's "Per-call timeout
        and single automatic retry" section for the full mechanism, and
        `DEFAULT_SNIPPET_TIMEOUT_SECONDS` for the server's default and its
        reasoning. `timeout_seconds`, left as `None` by default, falls back
        to the server-wide default (`--snippet-timeout` on the CLI, or
        `new_game`'s own `snippet_timeout_seconds` argument since the
        server last started or was reconfigured); pass an explicit value
        here to raise (or lower) it for just this one call -- e.g. a
        snippet you know will legitimately wait a while for resources to
        accumulate, without raising the timeout for every other call too.
        A snippet that still hasn't finished after its timeout AND one
        automatic retry comes back as a structured `ok=False` result whose
        `error` names the timeout used and includes the snippet's own code,
        rather than this call hanging forever."""
        bot_ai = active.bot_ai
        await bot_ai.ready.wait()
        result = await bot_ai.submit(code, timeout_seconds=timeout_seconds)
        return dataclasses.asdict(result)

    @mcp.tool()
    async def start_task(
        code: str, description: str, max_iterations: int = DEFAULT_TASK_MAX_ITERATIONS
    ) -> dict[str, object]:
        """Registers a standing background task for an open-ended,
        goal-directed instruction that may legitimately need many turns to
        complete -- see the module docstring's "Standing background tasks"
        section for the full design this implements, including why this
        exists at all (a single `execute_code` call is the wrong shape for
        "have an SCV build Supply Depots until we have 30": it either has
        to loop internally, indistinguishable from a runaway snippet and
        subject to the same per-call timeout, or the calling agent has to
        poll via repeated `execute_code` calls, which blocks that agent's
        own turn for just as long either way).

        `code` is ONE turn's worth of bounded work, run repeatedly (once
        per `on_step`, interleaved fairly with ordinary `execute_code`
        calls and other tasks' turns through the same queue -- see the
        module docstring) until it signals the goal is met. Reuses
        `_eval_snippet`'s existing "trailing expression becomes the
        result" convention verbatim -- no new snippet-evaluation semantics
        -- plus the fact that `code`'s body literally becomes a real
        `async def` function body, so an explicit `return` deep inside an
        `if`/`else` works too, not just a bare trailing expression. The
        convention for what the returned value MEANS here: truthy ends the
        task successfully (`task_status` reports `"done"`), falsy means
        "keep going" (another turn is scheduled automatically). Concretely,
        the user's own supply-depot scenario as one task's `code`:

            from sc2.ids.unit_typeid import UnitTypeId

            depot_count = len(sdk.structures(UnitTypeId.SUPPLYDEPOT))
            if depot_count >= 30:
                return True
            if sdk.can_afford(UnitTypeId.SUPPLYDEPOT):
                depot_point = sdk.townhalls.first.position.towards(sdk.game_info.map_center, 6)
                depot_point = depot_point.offset((depot_count * 3, 0))
                await bot.build(UnitTypeId.SUPPLYDEPOT, near=depot_point)
            else:
                await bot._advance(22)
            return False

        registered via `start_task(code=<above>, description="SCVs build "
        "Supply Depots until we have 30")`. Each turn either builds one
        more depot (if affordable right now) or waits one tick for
        minerals to accumulate, then reports `False` to request another
        turn -- until a turn observes `depot_count >= 30` and returns
        `True`, ending the task.

        Each individual turn is subject to the exact same per-call timeout
        and single automatic retry ordinary `execute_code` calls get (see
        the module docstring's "Per-call timeout and single automatic
        retry" section) -- reused, not reimplemented -- so a turn that
        itself hangs is caught exactly like a hung ordinary snippet would
        be; after both attempts are exhausted, or on any exception, the
        *task* (not just that one turn) is marked `"failed"` and no
        further turns are scheduled. `max_iterations` (default
        `DEFAULT_TASK_MAX_ITERATIONS`; see its docstring) bounds how many
        turns a task will run before giving up as `"failed"` if its step
        code's return value never evaluates truthy -- raise it for a goal
        you know genuinely needs more turns than the default allows.

        Returns `{"task_id": ...}` IMMEDIATELY, once the task is
        registered and its first turn is enqueued -- this call does NOT
        wait for that first turn, let alone the whole task, to run. Poll
        `task_status(task_id)` to check progress or see the final result.

        Lifecycle: a task belongs to whichever `ExecuteCodeBotAI` was
        `active.bot_ai` when `start_task` was called, exactly like an
        ordinary `execute_code` call's snippet does. A task does NOT
        survive `new_game` -- once a new game replaces the one a task
        belonged to, that task's `task_id` becomes unresolvable
        (`task_status` reports "unknown task_id") and no further turns of
        it will ever run, with no special action needed to cancel it
        first. See the module docstring's "Standing background tasks"
        section, and `new_game`'s own docstring below, for why this
        requires no special-casing beyond what already happens today for
        an ordinary in-flight `execute_code` call caught by a `new_game`
        call."""
        bot_ai = active.bot_ai
        await bot_ai.ready.wait()
        task_id = bot_ai.start_task(code, description, max_iterations=max_iterations)
        return {"task_id": task_id}

    @mcp.tool()
    async def task_status(task_id: str | None = None) -> dict[str, object]:
        """Reports a background task's current progress -- see
        `start_task`'s docstring and the module docstring's "Standing
        background tasks" section for the overall design. This is the
        pull-based "check in on a long-running task" mechanism: MCP tools
        are request/response, so there is no channel for a task to push
        progress updates to a caller unprompted -- call this whenever you
        want a fresh snapshot instead.

        With `task_id` given: returns `{"ok": True, "task_id": ...,
        "description": ..., "status": "running"|"done"|"failed"|
        "cancelled", "iterations": <turns completed so far>,
        "max_iterations": ..., "log": [<recent per-turn summaries, oldest
        first, capped -- see _TASK_LOG_MAX_ENTRIES>], "result": <final
        truthy turn's result repr, once "done", else None>, "error": <why,
        once "failed", else None>}`, or `{"ok": False, "task_id": ...,
        "error": "Unknown task_id: ..."}` if `task_id` doesn't name a task
        registered against the current game (including a task that
        belonged to a game `new_game` has since replaced -- see
        `start_task`'s "Lifecycle" section).

        With `task_id` omitted (`None`, the default): lists every task
        currently known to the current game, each as the same per-task
        dict described above, under `{"tasks": [...]}` -- convenient for
        "what's running right now" without needing to already know a
        specific `task_id`."""
        bot_ai = active.bot_ai
        await bot_ai.ready.wait()
        if task_id is None:
            return {"tasks": [bot_ai.task_status(known_id) for known_id in bot_ai.list_task_ids()]}
        return bot_ai.task_status(task_id)

    @mcp.tool()
    async def cancel_task(task_id: str) -> dict[str, object]:
        """Stops a running background task from scheduling any further
        turns -- see `start_task`'s docstring and the module docstring's
        "Standing background tasks" section for the overall design.

        If `task_id` currently has a turn queued or actively running (at
        most one, ever -- see `_finish_task_turn`'s docstring), that turn
        is allowed to finish naturally; interrupting a turn already in
        flight is deliberately out of scope (a harder problem this task
        doesn't need to solve -- python-sc2's own internals aren't safe to
        interrupt mid-call any more than they're safe to call
        concurrently, see the module docstring's concurrency section).
        What this guarantees is that no turn AFTER that one is ever
        scheduled: once that in-flight-or-queued turn completes, the task
        is marked `"cancelled"` (see `task_status`) instead of continuing.

        Returns `{"ok": True, "task_id": ..., "status": "running"}`
        (still `"running"` at the moment this call returns -- the
        cancellation takes effect after the current/queued turn finishes,
        per above) on success, or `{"ok": False, "task_id": ..., "error":
        ...}` if `task_id` is unknown or the task has already finished
        (`"done"`/`"failed"`/`"cancelled"`) on its own -- there is nothing
        left to cancel either way."""
        bot_ai = active.bot_ai
        return bot_ai.cancel_task(task_id)

    @mcp.tool()
    async def new_game(
        map_name: str | None = None,
        my_race: str | None = None,
        opponent_race: str | None = None,
        difficulty: str | None = None,
        game_time_limit: int | None = None,
        realtime: bool | None = None,
        snippet_timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        """Forcibly end whatever game is currently active (if one is still
        running) and start a fresh one, without tearing down this stdio
        connection -- see this module's docstring and `_ActiveGame` for why
        the original ticket #6 design (one `execute_code` tool closed over
        one fixed `bot_ai`) couldn't do this, and made every "play another
        game" require a human to fully disconnect/reconnect via Claude
        Code's `/mcp` command.

        Parameters mirror `_build_session`'s (`map_name`/`my_race`/
        `opponent_race`/`difficulty`/`game_time_limit`/`realtime`), plus
        `snippet_timeout_seconds` (see `DEFAULT_SNIPPET_TIMEOUT_SECONDS`/
        the module docstring's "Per-call timeout and single automatic
        retry" section) -- but `my_race`/`opponent_race`/`difficulty` are
        accepted here as plain strings, not `Race`/`Difficulty` enum
        members -- MCP tool arguments cross the wire as JSON, so this
        reuses `_RACE_BY_NAME`/`_DIFFICULTY_BY_NAME`, the exact same
        name->enum lookup dicts `_parse_args` already uses for the
        equivalent `--race`/`--opponent-race`/`--difficulty` CLI flags,
        instead of inventing a second mapping. Any parameter left as
        `None` (the default for all seven) falls back to `defaults` --
        this server's *original* `sc2-sdk-mcp` launch configuration (see
        `main()`/`_main_async`), not a second, independently hardcoded set
        of "sensible defaults" that could silently drift from it -- so a
        caller who invokes `new_game` with no arguments gets "the same
        kind of game the server was launched with", matching today's
        implicit behavior when a human just reconnects via `/mcp` instead.
        This is also how `snippet_timeout_seconds` "survives a `new_game`
        call without needing to be re-specified": omitting it here falls
        back to `defaults.snippet_timeout_seconds` -- this server's
        original `--snippet-timeout` launch value -- exactly like omitting
        `realtime` falls back to the original `--realtime`, rather than
        resetting to some second, hardcoded default. (As with every other
        field here, an explicit override on one `new_game` call is
        one-shot -- it configures the game this call starts, not
        `defaults` itself -- so a *later* `new_game` call with no
        arguments still falls back to the original launch configuration,
        not to a previous call's override.)

        Concurrency design decision -- what happens to an `execute_code`
        call already in flight against the OLD game when `new_game` runs:
        this cancels `active.game_task` (the old game) *immediately*,
        rather than first waiting for any in-flight snippet to finish.
        That is deliberate, not an oversight, and is the other half of
        this ticket's brief: recovering a session permanently wedged
        inside a runaway `execute_code` snippet that never returns (an
        `on_step` stuck forever `await`-ing that snippet -- see this
        module's docstring). If `new_game` instead blocked until the
        in-flight call resolved, it could never recover that exact case,
        since by construction that call is never going to resolve on its
        own. Cancelling `game_task` propagates an `asyncio.CancelledError`
        into whatever the stuck `_host_game`/`on_step`/snippet `await`
        chain is currently suspended on -- ordinary Python
        cancellation-propagation semantics, not anything special-cased
        here -- which unwinds it, runs `_host_game`'s `async with
        SC2Process(...)` cleanup, and kills the underlying SC2 client
        process normally. The stale caller isn't left hanging either:
        `ExecuteCodeBotAI.submit()` already races its own result future
        against `self.game_task` (see `submit()`'s docstring -- built
        originally for "the match ended naturally mid-call"; a
        `new_game`-triggered cancellation is just another way `game_task`
        can finish first), so that in-flight call gets back a clean,
        structured `ok=False` result ("Match ended before this snippet
        could run...") as soon as the cancellation lands, instead of
        hanging or crashing the MCP session. The alternative -- block
        `new_game` until the in-flight call resolves -- was considered and
        rejected specifically because it cannot recover the wedged-forever
        case this ticket needs to handle.

        `active.lock` is held for this call's entire body to serialize
        concurrent `new_game` calls against each other (see `_ActiveGame`'s
        docstring) -- it is deliberately NOT held around `execute_code`,
        which would otherwise block every in-flight snippet call for
        however long a fresh game takes to load.

        Waits for the new game's `bot_ai.ready` event before returning --
        mirroring `serve_execute_code`'s own `await session.bot_ai.ready
        .wait()` -- so a caller that immediately follows up with
        `execute_code` doesn't race game startup.

        Returns a plain dict (same convention as `execute_code`'s
        `dataclasses.asdict(result)`) confirming what the new game
        actually started with and whether a previous game was torn down
        first.

        Also ends every standing background task (see `start_task`) that
        belonged to the OLD game, with no special-casing needed here to
        make that true: tasks live in `old_bot_ai._tasks` (see
        `ExecuteCodeBotAI.__init__`), and once this method reassigns
        `active.bot_ai` to `new_bot_ai` below, no tool call can reach
        `old_bot_ai` -- and therefore `old_bot_ai._tasks` -- ever again.
        Any task turn that was queued or in flight against the old game
        when `old_game_task.cancel()` ran above is unwound by the same
        plain `asyncio.CancelledError` propagation described above for an
        ordinary `execute_code` call -- it never reaches
        `_finish_task_turn`, so it leaves no trace (not marked
        `"cancelled"` or `"failed"`; it simply stops existing along with
        `old_bot_ai`). A caller that queries `task_status` for one of the
        old game's `task_id`s afterward gets the same "unknown task_id"
        response as a `task_id` that was never registered at all, since
        `active.bot_ai` (what every tool reads) is now `new_bot_ai`, whose
        `_tasks` starts empty.
        """
        async with active.lock:
            previous_game_torn_down = await _teardown_active_game(active)

            config = _GameConfig(
                map_name=map_name if map_name is not None else defaults.map_name,
                my_race=_RACE_BY_NAME[my_race] if my_race is not None else defaults.my_race,
                opponent_race=(
                    _RACE_BY_NAME[opponent_race] if opponent_race is not None else defaults.opponent_race
                ),
                difficulty=(
                    _DIFFICULTY_BY_NAME[difficulty] if difficulty is not None else defaults.difficulty
                ),
                game_time_limit=(
                    game_time_limit if game_time_limit is not None else defaults.game_time_limit
                ),
                realtime=realtime if realtime is not None else defaults.realtime,
                snippet_timeout_seconds=(
                    snippet_timeout_seconds
                    if snippet_timeout_seconds is not None
                    else defaults.snippet_timeout_seconds
                ),
            )
            new_bot_ai, new_game_task = _launch_game(config)
            active.bot_ai = new_bot_ai
            active.game_task = new_game_task
            await new_bot_ai.ready.wait()

            return {
                "map_name": config.map_name,
                "my_race": config.my_race.name,
                "opponent_race": config.opponent_race.name,
                "difficulty": config.difficulty.name,
                "game_time_limit": config.game_time_limit,
                "realtime": config.realtime,
                "snippet_timeout_seconds": config.snippet_timeout_seconds,
                "previous_game_torn_down": previous_game_torn_down,
            }

    @mcp.tool()
    async def host_game(
        map_name: str | None = None,
        my_race: str | None = None,
        opponent_race_pin: str | None = None,
        host_ip: str = "127.0.0.1",
        game_time_limit: int | None = None,
        snippet_timeout_seconds: float | None = None,
        join_timeout: float = DEFAULT_JOIN_TIMEOUT,
    ) -> dict[str, object]:
        """Host a real two-player match and return a shareable match code
        IMMEDIATELY, without waiting for a peer to connect -- see the
        module docstring's "Hosting a two-player match" section for the
        full design. Call `host_status` to poll whether a peer has joined
        yet; once it reports `"joined"`, this game is playable through the
        existing `execute_code`/`start_task` tools exactly like any other
        game this server serves.

        Ends whatever game is currently active first, the same "cancel
        outright, don't wait" teardown `new_game` already performs (see
        `_teardown_active_game`) -- so calling `host_game` is itself how you
        abandon a game you no longer want, same as calling `new_game` is.

        `map_name`/`my_race` fall back to this server's original launch
        configuration (`defaults`) if omitted, the same convention
        `new_game` uses -- but unlike `new_game`, there is no
        `opponent_race`/`difficulty` here: those describe the built-in AI
        `new_game` plays against, which doesn't apply once a real peer is
        joining. `opponent_race_pin`, if given, is embedded into the match
        code for the *joining* side to see (their own explicit race choice
        always wins over it) -- it does not affect this side's own race.

        `host_ip` defaults to loopback (`"127.0.0.1"`) -- NOT the Tailscale
        auto-detection the standalone `sc2-sdk-host` CLI defaults to -- see
        the module docstring for why: this defaults to a same-machine
        match, with no Tailscale involvement at all unless a real routable
        address is passed here explicitly.

        Realtime is not a parameter here: hosted games always run at
        wall-clock speed (see the module docstring for why "paused between
        calls" cannot work once a second, independently-paced client is
        synchronized into the same match).

        `join_timeout` (default: `sdk.host_join.DEFAULT_JOIN_TIMEOUT`, the
        same default the standalone CLI uses) bounds only how long this
        waits for a peer to connect, not the match itself once joined --
        see `host_status` for how a caller learns whether that timeout was
        hit.

        Returns `{"match_code": ..., "host_ip": ..., "map_name": ...,
        "my_race": ..., "opponent_race_pin": ..., "join_timeout": ...,
        "previous_game_torn_down": ...}`."""
        async with active.lock:
            previous_game_torn_down = await _teardown_active_game(active)

            resolved_map_name = map_name if map_name is not None else defaults.map_name
            resolved_my_race = _RACE_BY_NAME[my_race] if my_race is not None else defaults.my_race
            resolved_race_pin = _RACE_BY_NAME[opponent_race_pin] if opponent_race_pin is not None else None
            resolved_snippet_timeout = (
                snippet_timeout_seconds if snippet_timeout_seconds is not None else defaults.snippet_timeout_seconds
            )

            new_bot_ai, new_game_task, match_code = _launch_hosted_game(
                map_name=resolved_map_name,
                my_race=resolved_my_race,
                opponent_race_pin=resolved_race_pin,
                host_ip=host_ip,
                game_time_limit=game_time_limit,
                snippet_timeout_seconds=resolved_snippet_timeout,
                join_timeout=join_timeout,
            )
            active.bot_ai = new_bot_ai
            active.game_task = new_game_task

            return {
                "match_code": match_code,
                "host_ip": host_ip,
                "map_name": resolved_map_name,
                "my_race": resolved_my_race.name,
                "opponent_race_pin": resolved_race_pin.name if resolved_race_pin is not None else None,
                "join_timeout": join_timeout,
                "previous_game_torn_down": previous_game_torn_down,
            }

    @mcp.tool()
    async def host_status() -> dict[str, object]:
        """Reports a `host_game`-started match's current join status --
        see the module docstring's "Hosting a two-player match" section for
        the full design and `host_game`'s own docstring for how to start
        one.

        Returns `{"ok": True, "match_code": ..., "status":
        "waiting"|"joined"|"failed", "error": <why, only when "failed",
        else None>}`, or `{"ok": False, "error": ...}` if the current game
        wasn't started via `host_game` at all (an ordinary solo game, or a
        hosted game that's since been replaced by `new_game`/another
        `host_game` call).

        Does NOT wait for anything -- unlike `execute_code`/`new_game`,
        this never awaits `bot_ai.ready`, since the entire point is to
        check in on a wait that may still be ongoing without blocking on
        it."""
        return _host_status(active.bot_ai, active.game_task)

    return mcp


def _build_session(
    map_name: str = DEFAULT_MAP,
    my_race: Race = Race.Terran,
    opponent_race: Race = Race.Random,
    difficulty: Difficulty = Difficulty.Easy,
    game_time_limit: int | None = None,
    realtime: bool = False,
    snippet_timeout_seconds: float = DEFAULT_SNIPPET_TIMEOUT_SECONDS,
) -> ExecuteCodeSession:
    """Build the `FastMCP` server and kick off the game task, returning as
    soon as both exist -- deliberately *not* waiting for `bot_ai.ready`
    (contrast `serve_execute_code` below). `main()` uses this directly so
    stdio serving (and therefore a real client's `initialize()` handshake)
    starts immediately instead of stalling for however long the real SC2
    client takes to launch and load into a match -- `execute_code` itself
    already awaits `bot_ai.ready` per-call (see `build_server`), so nothing
    about correctness depends on waiting here too.

    `realtime` defaults to `False` (this ticket's original design: the game
    only advances one internal step per `execute_code` call -- see module
    docstring). Passing `True` instead lets the game run at wall-clock
    speed between calls -- `on_step`'s blocking `queue.get()` (see
    `ExecuteCodeBotAI`) still gates when *this bot's own* decisions get
    made, but the underlying SC2 engine no longer waits on that to keep
    simulating, so play looks and feels live instead of frozen between
    calls. Useful for a human spectating the rendered client; the
    still-paused default remains what the wiring test below verifies.

    These same seven parameters (including `snippet_timeout_seconds`, see
    `DEFAULT_SNIPPET_TIMEOUT_SECONDS`) are also remembered (as a
    `_GameConfig`) and handed to `build_server` as `defaults` -- the
    `new_game` MCP tool falls back to them for any argument a caller
    doesn't pass, so "call `new_game` with nothing" reproduces "the kind
    of game this session was originally built with" -- see
    `build_server`'s `new_game` docstring."""
    config = _GameConfig(
        map_name=map_name,
        my_race=my_race,
        opponent_race=opponent_race,
        difficulty=difficulty,
        game_time_limit=game_time_limit,
        realtime=realtime,
        snippet_timeout_seconds=snippet_timeout_seconds,
    )
    bot_ai, game_task = _launch_game(config)
    active = _ActiveGame(bot_ai=bot_ai, game_task=game_task)
    mcp = build_server(active, config)

    return ExecuteCodeSession(mcp=mcp, active=active)


async def serve_execute_code(
    map_name: str = DEFAULT_MAP,
    my_race: Race = Race.Terran,
    opponent_race: Race = Race.Random,
    difficulty: Difficulty = Difficulty.Easy,
    game_time_limit: int | None = None,
    realtime: bool = False,
    snippet_timeout_seconds: float = DEFAULT_SNIPPET_TIMEOUT_SECONDS,
) -> ExecuteCodeSession:
    """Launch a real game against the built-in AI -- stepped (paused
    between calls) by default, or wall-clock speed if `realtime=True`, see
    `_build_session` -- and return an `ExecuteCodeSession` wrapping a
    `FastMCP` server whose `execute_code` tool is live against it, once the
    game has actually loaded and is ready for calls -- callers that need a
    session back only once it's fully live (e.g. the wiring test below)
    should use this; `main()` does not, see `_build_session`.

    Does not itself serve any transport -- callers decide that. The
    console-script entrypoint (`main()` below) serves it over stdio, the
    same way any other MCP server does for a real client; the wiring test
    instead uses the official SDK's own in-memory
    `mcp.shared.memory.create_connected_server_and_client_session` transport
    to drive a real `mcp.client.session.ClientSession` against it on the
    same event loop -- a real client/server JSON-RPC exchange, just not
    piped through a subprocess's stdio, since the whole point under test is
    the same-event-loop pause/step wiring, not stdio framing.
    """
    session = _build_session(
        map_name=map_name,
        my_race=my_race,
        opponent_race=opponent_race,
        difficulty=difficulty,
        game_time_limit=game_time_limit,
        realtime=realtime,
        snippet_timeout_seconds=snippet_timeout_seconds,
    )
    await session.bot_ai.ready.wait()
    return session


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Same flag names/conventions as sdk.play._parse_args -- deliberately
    not shared code (that module is off-limits per this ticket's brief),
    just the same shape for a consistent CLI across the project's
    entrypoints."""
    parser = argparse.ArgumentParser(
        description=(
            "Launch a real SC2 game against the built-in AI and serve a single MCP tool, "
            "execute_code, over stdio -- see this module's docstring (src/sdk/mcp_server.py) "
            "for the full architecture writeup."
        )
    )
    parser.add_argument("--map", default=DEFAULT_MAP, help=f"Map name to play on (default: {DEFAULT_MAP}).")
    parser.add_argument("--race", choices=sorted(_RACE_BY_NAME), default="terran", help="Our race.")
    parser.add_argument(
        "--opponent-race", choices=sorted(_RACE_BY_NAME), default="random", help="Built-in AI's race."
    )
    parser.add_argument(
        "--difficulty", choices=sorted(_DIFFICULTY_BY_NAME), default="easy", help="Built-in AI's difficulty."
    )
    parser.add_argument(
        "--time-limit",
        type=int,
        default=None,
        help="In-game-second safety cap; the match is scored a Tie if unresolved by then.",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help=(
            "Run the game at wall-clock speed between execute_code calls instead of the "
            "default stepped/paused mode -- see _build_session's docstring. Useful when "
            "spectating the rendered client live."
        ),
    )
    parser.add_argument(
        "--snippet-timeout",
        type=float,
        default=DEFAULT_SNIPPET_TIMEOUT_SECONDS,
        help=(
            "Per-execute_code-call timeout in seconds, with one automatic retry on timeout "
            "before giving up and reporting a clear failure -- see the module docstring's "
            f"'Per-call timeout and single automatic retry' section (default: "
            f"{DEFAULT_SNIPPET_TIMEOUT_SECONDS:g}s). Persists across new_game calls that don't "
            "explicitly override it; a single execute_code call can also raise this for just "
            "itself via its own timeout_seconds argument, e.g. for a snippet known to "
            "legitimately wait a while on slow resource income."
        ),
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> None:
    # Uses _build_session, not serve_execute_code: a real client's stdio
    # handshake must not have to outlast the real SC2 client's launch time
    # -- see _build_session's docstring.
    session = _build_session(
        map_name=args.map,
        my_race=_RACE_BY_NAME[args.race],
        opponent_race=_RACE_BY_NAME[args.opponent_race],
        difficulty=_DIFFICULTY_BY_NAME[args.difficulty],
        game_time_limit=args.time_limit,
        realtime=args.realtime,
        snippet_timeout_seconds=args.snippet_timeout,
    )
    await session.mcp.run_stdio_async()


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint (`sc2-sdk-mcp`, see pyproject.toml):
    launch a real game against the built-in AI and serve `execute_code`
    over stdio for a real MCP client (e.g. an LLM coding agent) to drive.

    Runs `_run_single_instance_guard()` first, before parsing anything else
    into a running game or serving any MCP traffic -- see this module's
    "Single-instance guard + explicit SC2-client PID tracking" section for
    why this exists: a stale prior `sc2-sdk-mcp` process (and its own SC2
    client) left running from an earlier `/mcp` reconnect is found and
    terminated here, synchronously, before this process does anything else."""
    args = _parse_args(argv)
    _run_single_instance_guard()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
