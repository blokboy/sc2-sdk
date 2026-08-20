"""MCP `execute_code` interactive mode -- ticket #6
(https://github.com/blokboy/sc2-sdk/issues/6): an MCP server exposing a
single `execute_code` tool that evaluates a Python snippet against live
`bot`/`sdk` globals bound to a running game, with the game paused between
calls rather than advancing on wall-clock time.

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
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import dataclasses
import io
import traceback
from dataclasses import dataclass

from sc2 import maps
from sc2.data import Difficulty, Race, Result
from sc2.main import _host_game  # noqa: SLF001 -- see module docstring
from sc2.player import Bot as Sc2BotPlayer
from sc2.player import Computer

from mcp.server.fastmcp import FastMCP

from sdk.bot import VerifiedBotAI

#: Same fixed test map the rest of the project's harnesses default to.
DEFAULT_MAP = "AutomatonLE"

#: Same name->enum lookup convention sdk.play._parse_args uses, for the same
#: --race/--opponent-race/--difficulty CLI flags below.
_RACE_BY_NAME = {r.name.lower(): r for r in Race}
_DIFFICULTY_BY_NAME = {d.name.lower(): d for d in Difficulty}


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


@dataclass
class _PendingRequest:
    code: str
    future: "asyncio.Future[ExecuteCodeResult]"


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

    def __init__(self) -> None:
        super().__init__()
        #: Set once on_start has wired up self.bot/self.sdk -- execute_code
        #: callers should wait on this before submitting a snippet.
        self.ready: asyncio.Event = asyncio.Event()
        self._queue: "asyncio.Queue[_PendingRequest]" = asyncio.Queue()
        #: Set from outside, after this instance has been handed to
        #: asyncio.create_task(_host_game(...)) -- see serve_execute_code.
        self.game_task: "asyncio.Task[Result] | None" = None

    async def on_start(self) -> None:
        await super().on_start()
        self.ready.set()

    async def on_step(self, iteration: int) -> None:
        # This blocking get() -- not any change to python-sc2's own
        # stepping code -- is the entire mechanism behind "the game runs in
        # non-realtime/stepped mode... pausing for each call": see module
        # docstring.
        request = await self._queue.get()
        result = await _eval_snippet(request.code, {"bot": self.bot, "sdk": self.sdk})
        if not request.future.done():
            request.future.set_result(result)

    async def submit(self, code: str) -> ExecuteCodeResult:
        """Called by the `execute_code` MCP tool handler: enqueue `code`
        for the next `on_step` to run, and wait for its result -- or for a
        clear error if the match ends before that happens."""
        future: "asyncio.Future[ExecuteCodeResult]" = asyncio.get_running_loop().create_future()
        await self._queue.put(_PendingRequest(code=code, future=future))

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
class ExecuteCodeSession:
    """What `serve_execute_code()` hands back: the live `FastMCP` server
    plus the live `ExecuteCodeBotAI`/game task it's wired to, so a caller
    (a console-script entrypoint, or a test) can both serve the MCP tool
    and separately inspect/await the underlying game."""

    mcp: FastMCP
    bot_ai: ExecuteCodeBotAI
    game_task: "asyncio.Task[Result]"


def build_server(bot_ai: ExecuteCodeBotAI, name: str = "sc2-sdk") -> FastMCP:
    """Build a `FastMCP` server exposing the single `execute_code` tool
    against `bot_ai`. Split out from `serve_execute_code()` purely as a
    seam: it only touches `bot_ai.ready`/`bot_ai.submit()`, not anything
    SC2-specific, so nothing about wiring the MCP tool itself depends on
    `bot_ai` being a real, game-backed `ExecuteCodeBotAI` -- see
    `serve_execute_code()` below for how the real entrypoint constructs
    one."""
    mcp = FastMCP(name)

    @mcp.tool()
    async def execute_code(code: str) -> dict[str, object]:
        """Evaluate a Python snippet against live `bot`/`sdk` globals bound
        to the running game. The underlying game is paused (stepped, not
        real-time) while no snippet is being evaluated -- see this
        project's ticket #6 -- and advances by roughly one internal
        simulation step once this call returns and the next `on_step`
        begins waiting for the following call."""
        await bot_ai.ready.wait()
        result = await bot_ai.submit(code)
        return dataclasses.asdict(result)

    return mcp


async def serve_execute_code(
    map_name: str = DEFAULT_MAP,
    my_race: Race = Race.Terran,
    opponent_race: Race = Race.Random,
    difficulty: Difficulty = Difficulty.Easy,
    game_time_limit: int | None = None,
) -> ExecuteCodeSession:
    """Launch a real game against the built-in AI (non-realtime, i.e.
    stepped mode -- always; there is no realtime option here, since
    stepped-between-calls is the entire point of this ticket) and return an
    `ExecuteCodeSession` wrapping a `FastMCP` server whose `execute_code`
    tool is live against it.

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
    bot_ai = ExecuteCodeBotAI()
    mcp = build_server(bot_ai)

    game_task = asyncio.create_task(
        _host_game(
            maps.get(map_name),
            [Sc2BotPlayer(my_race, bot_ai), Computer(opponent_race, difficulty)],
            realtime=False,
            game_time_limit=game_time_limit,
        )
    )
    bot_ai.game_task = game_task

    await bot_ai.ready.wait()
    return ExecuteCodeSession(mcp=mcp, bot_ai=bot_ai, game_task=game_task)


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
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> None:
    session = await serve_execute_code(
        map_name=args.map,
        my_race=_RACE_BY_NAME[args.race],
        opponent_race=_RACE_BY_NAME[args.opponent_race],
        difficulty=_DIFFICULTY_BY_NAME[args.difficulty],
        game_time_limit=args.time_limit,
    )
    await session.mcp.run_stdio_async()


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint (`sc2-sdk-mcp`, see pyproject.toml):
    launch a real game against the built-in AI and serve `execute_code`
    over stdio for a real MCP client (e.g. an LLM coding agent) to drive."""
    args = _parse_args(argv)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
