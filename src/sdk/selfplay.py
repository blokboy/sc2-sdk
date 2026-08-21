"""Self-play mode -- ticket #10
(https://github.com/blokboy/sc2-sdk/issues/10): run a standalone bot script
(see `sdk.script_runner`'s module docstring for the `bots/<name>.py`
convention) against a second instance of itself, or against a *different*
bot script, in a single local match -- no human, no built-in AI, on either
side.

Deliberately its own module rather than an option bolted onto
`sdk.script_runner`: that module's `run_bot_script`/`main` are the proven
#7 vs-built-in-AI path, and this ticket's brief asks not to touch that
existing single-bot behavior. What *is* shared is the discovery mechanics
(`resolve_script_path`, `load_bot_class`) -- those were already plain,
dependency-free functions in `script_runner`, so this module imports and
reuses them directly rather than duplicating "find the one BotAI subclass a
script defines" a second time. The only genuinely new piece is
`runtime.run_bot_vs_bot` (two `Bot` players instead of one `Bot` + one
`Computer`) and the CLI wiring around loading *two* scripts instead of one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sc2.data import Race, Result

from sdk.runtime import DEFAULT_MAP, run_bot_vs_bot
from sdk.script_runner import BOTS_DIR, load_bot_class, resolve_script_path

_RACE_BY_NAME = {r.name.lower(): r for r in Race}


def run_bot_selfplay(
    bot_a_name_or_path: str,
    bot_b_name_or_path: str | None = None,
    map_name: str = DEFAULT_MAP,
    race_a: Race = Race.Terran,
    race_b: Race = Race.Terran,
    realtime: bool = False,
    game_time_limit: int | None = None,
    bots_dir: Path = BOTS_DIR,
) -> list[Result]:
    """Discover, load, and run one or two standalone bot scripts against
    each other to completion via `runtime.run_bot_vs_bot`.

    Args:
        bot_a_name_or_path: a bot name under `bots_dir`, or a literal path
            to a `.py` file -- same resolution rule as
            `script_runner.run_bot_script`.
        bot_b_name_or_path: same, for the second side. If omitted (the
            default), `bot_a_name_or_path` is loaded *twice* -- a fresh,
            separate instance of the same discovered class for each side --
            so a script plays against itself.
        map_name, race_a, race_b, game_time_limit: see
            `runtime.run_bot_vs_bot`.
        realtime: if False (default), the game steps only as fast as both
            bots respond -- fast/deterministic, matching
            `runtime.run_bot_vs_bot`'s own default (unlike
            `script_runner.run_bot_script`, which defaults to real-time
            since unattended single-script play is its whole point; here,
            fast local iteration is the more useful default for two bots
            testing against each other).
        bots_dir: override for where bot names resolve against; see
            `script_runner.run_bot_script`.

    Returns:
        `[result_for_bot_a, result_for_bot_b]` -- see
        `runtime.run_bot_vs_bot`.
    """
    script_a = resolve_script_path(bot_a_name_or_path, bots_dir=bots_dir)
    class_a = load_bot_class(script_a)

    if bot_b_name_or_path is None:
        class_b = class_a
    else:
        script_b = resolve_script_path(bot_b_name_or_path, bots_dir=bots_dir)
        class_b = load_bot_class(script_b)

    # Two separate instances even when class_a is class_b -- a script
    # playing "against itself" still needs its own independent BotAI object
    # per side (shared mutable instance state would otherwise leak between
    # the two players of the same match).
    return run_bot_vs_bot(
        class_a(),
        class_b(),
        map_name=map_name,
        race_a=race_a,
        race_b=race_b,
        realtime=realtime,
        game_time_limit=game_time_limit,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bot_a",
        help="Bot name, resolved to bots/<name>.py (or a literal .py file path).",
    )
    parser.add_argument(
        "bot_b",
        nargs="?",
        default=None,
        help="Second bot, same resolution rule as bot_a. If omitted, bot_a plays a second instance of itself.",
    )
    parser.add_argument("--map", default=DEFAULT_MAP, help=f"Map name to play on (default: {DEFAULT_MAP}).")
    parser.add_argument("--race-a", choices=sorted(_RACE_BY_NAME), default="terran", help="bot_a's race.")
    parser.add_argument("--race-b", choices=sorted(_RACE_BY_NAME), default="terran", help="bot_b's race.")
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="Run at real-time speed instead of stepped (default: stepped, for fast/deterministic iteration).",
    )
    parser.add_argument(
        "--time-limit",
        type=int,
        default=None,
        help="In-game-second safety cap; the match is scored a Tie for both sides if unresolved by then.",
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
    results = run_bot_selfplay(
        args.bot_a,
        args.bot_b,
        map_name=args.map,
        race_a=_RACE_BY_NAME[args.race_a],
        race_b=_RACE_BY_NAME[args.race_b],
        realtime=args.realtime,
        game_time_limit=args.time_limit,
        bots_dir=args.bots_dir,
    )
    print(f"RESULT A ({args.bot_a}): {results[0].name}")
    print(f"RESULT B ({args.bot_b or args.bot_a}): {results[1].name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
