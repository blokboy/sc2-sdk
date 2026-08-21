"""Discover and run a standalone bot script -- ticket #7
(https://github.com/blokboy/sc2-sdk/issues/7): the "autonomous bot-script
runtime" play modality from the spec (#1), sitting alongside interactive
`execute_code` mode (#6). Where #6 pauses the game every step waiting for an
external caller's next snippet, this module's whole point is the opposite:
an agent authors a `BotAI` subclass *once*, as a normal Python file, and
this runner then plays a full game with it unattended -- no live per-tick
calls from any caller once the game starts.

Convention: the `bots/` directory
----------------------------------
Standalone bot scripts live as individual `.py` files directly under the
`bots/` directory at the repo root (`bots/<name>.py`, e.g.
`bots/rush_bot.py`) -- not a package, no `__init__.py` needed, since each
script is loaded directly from its file path (see `load_bot_class` below),
not imported as `bots.<name>`. This mirrors how `play.py`/`mcp_server.py`
are each one file defining what they need; a bot script is the same shape,
just relocated to a directory an agent can freely add new files to without
touching `src/sdk/`.

Each script must define **exactly one** class, at module level, that
subclasses `sc2.bot_ai.BotAI` (in practice, `sdk.bot.VerifiedBotAI`, which
is what makes the same `bot.*`/`sdk.*` API surface `execute_code` mode uses
available identically here -- see `bots/idle_example.py`). No fixed class
name is required (agents can call it `RushBot`, `MacroBot`, whatever fits);
the runner instead finds the one class *defined in that script's own
module* (as opposed to merely imported into it, e.g. `VerifiedBotAI` itself)
that is a `BotAI` subclass. Zero or more-than-one such classes is a clear
authoring error, reported as such rather than guessed at.

Discovery/loading mechanics
-----------------------------
`resolve_script_path` turns a bot name (or a literal `.py` path, for
ad-hoc/testing use) into a concrete file path under `bots/`.
`load_bot_class` then does a plain `importlib.util.spec_from_file_location`
+ `exec_module` load of that one file (no package/`sys.path` machinery
needed) and inspects the resulting module's own namespace for the single
`BotAI` subclass described above. `run_bot_script` ties both steps together
and hands a fresh instance of the discovered class to
`runtime.run_bot_vs_builtin_ai` -- the same synchronous `sc2.main.run_game`
entrypoint ticket #3 built, reused as-is (see that module's docstring for
why: this ticket doesn't need `mcp_server.py`'s `_host_game`/shared-event-
loop trick, since there's no concurrent MCP server here for the game to
coexist with -- just one script, run to completion).
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
from pathlib import Path

from sc2.bot_ai import BotAI
from sc2.data import Difficulty, Race, Result

from sdk.runtime import DEFAULT_MAP, run_bot_vs_builtin_ai

#: Root-level directory (the documented convention -- see module docstring)
#: standalone bot scripts live under. Resolved relative to this file's
#: location (src/sdk/script_runner.py -> repo root), which holds for the
#: editable install (`pip install -e .`) this project's README documents as
#: the supported install path -- the same assumption `tests/conftest.py`'s
#: `pythonpath = ["src"]` pytest config already makes.
BOTS_DIR = Path(__file__).resolve().parent.parent.parent / "bots"

_RACE_BY_NAME = {r.name.lower(): r for r in Race}
_DIFFICULTY_BY_NAME = {d.name.lower(): d for d in Difficulty}


def resolve_script_path(name_or_path: str, bots_dir: Path = BOTS_DIR) -> Path:
    """Resolve `name_or_path` to a concrete script file.

    Accepts either a bare bot name (resolved to `<bots_dir>/<name>.py`, the
    documented convention) or a literal path to a `.py` file that already
    exists (an escape hatch for ad-hoc scripts/tests outside `bots_dir`,
    not itself part of the documented convention).
    """
    literal = Path(name_or_path)
    if literal.suffix == ".py" and literal.is_file():
        return literal.resolve()

    script_path = (bots_dir / f"{name_or_path}.py").resolve()
    if not script_path.is_file():
        raise FileNotFoundError(
            f"No bot script found for {name_or_path!r}: tried it as a literal "
            f".py file path, then as {script_path} (the 'bots/<name>.py' "
            "convention -- see README.md's 'Play an autonomous bot script' "
            "section)."
        )
    return script_path


def load_bot_class(script_path: Path) -> type[BotAI]:
    """Dynamically import `script_path` and return the single `BotAI`
    subclass it defines (see module docstring for the exact rule). Raises
    `ValueError` with a clear message if the script defines zero or more
    than one such class.
    """
    module_name = f"sdk._bot_script__{script_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {script_path} as a Python module.")
    module = importlib.util.module_from_spec(spec)
    # Registered in sys.modules before exec so dataclasses/typing machinery
    # that expects a module to already be importable by name (e.g. for
    # forward-reference resolution) works the same as a normal import.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    candidates = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        # obj.__module__ == module.__name__ is what excludes classes merely
        # *imported* into the script (e.g. `from sdk.bot import
        # VerifiedBotAI`) from counting as "defined here" -- only a class
        # this script itself declares should be a discovery candidate.
        if issubclass(obj, BotAI) and obj.__module__ == module.__name__
    ]
    if not candidates:
        raise ValueError(
            f"{script_path} does not define a BotAI subclass. Convention: a "
            "standalone bot script must define exactly one class (typically "
            "subclassing sdk.bot.VerifiedBotAI) at module level -- see "
            "bots/idle_example.py."
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(c.__name__ for c in candidates))
        raise ValueError(
            f"{script_path} defines multiple BotAI subclasses ({names}); "
            "convention requires exactly one per script."
        )
    return candidates[0]


def run_bot_script(
    name_or_path: str,
    map_name: str = DEFAULT_MAP,
    my_race: Race = Race.Terran,
    opponent_race: Race = Race.Random,
    difficulty: Difficulty = Difficulty.Easy,
    realtime: bool = True,
    game_time_limit: int | None = None,
    bots_dir: Path = BOTS_DIR,
) -> Result:
    """Discover, load, and run a standalone bot script to completion.

    `realtime` defaults to True here (unlike `runtime.run_bot_vs_builtin_ai`,
    which defaults to False for fast/deterministic tests) because playing
    unattended at real-time speed against a real opponent is this ticket's
    entire point -- pass `realtime=False` to run stepped instead (useful for
    CI/wiring tests, exactly like the sc2_verified_bot_harness fixture other
    tickets' tests use).

    Args:
        name_or_path: a bot name under `bots_dir` (resolved to
            `<bots_dir>/<name>.py`), or a literal path to a `.py` file.
        map_name, my_race, opponent_race, difficulty, game_time_limit: see
            `runtime.run_bot_vs_builtin_ai`.
        realtime: if True (default), the game runs at wall-clock speed with
            no per-tick calls from this function once `run_game` starts --
            the loaded script's own `on_step` drives everything. If False,
            the game steps only as fast as the bot script responds.
        bots_dir: override for where bot names resolve against; defaults to
            the documented `bots/` directory at the repo root. Exposed
            mainly so tests can point at a scratch directory shaped the
            same way without touching the real `bots/` contents.

    Returns:
        sc2.data.Result -- see `runtime.run_bot_vs_builtin_ai`.
    """
    script_path = resolve_script_path(name_or_path, bots_dir=bots_dir)
    bot_class = load_bot_class(script_path)
    bot_ai = bot_class()
    return run_bot_vs_builtin_ai(
        bot_ai,
        map_name=map_name,
        my_race=my_race,
        opponent_race=opponent_race,
        difficulty=difficulty,
        realtime=realtime,
        game_time_limit=game_time_limit,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bot",
        help="Bot name, resolved to bots/<name>.py (or a literal .py file path).",
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
        "--no-realtime",
        action="store_true",
        help="Run stepped (as fast as the script responds) instead of at real-time speed.",
    )
    parser.add_argument(
        "--time-limit",
        type=int,
        default=None,
        help="In-game-second safety cap; the match is scored a Tie if unresolved by then.",
    )
    parser.add_argument(
        "--bots-dir",
        type=Path,
        default=BOTS_DIR,
        help=f"Directory bot names resolve against (default: {BOTS_DIR}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = run_bot_script(
        args.bot,
        map_name=args.map,
        my_race=_RACE_BY_NAME[args.race],
        opponent_race=_RACE_BY_NAME[args.opponent_race],
        difficulty=_DIFFICULTY_BY_NAME[args.difficulty],
        realtime=not args.no_realtime,
        game_time_limit=args.time_limit,
        bots_dir=args.bots_dir,
    )
    print(f"RESULT: {result.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
