"""Raw python-sc2 connect-and-play: launch a full game against the game's
built-in AI and report the result.

This is deliberately *not* the sdk's `bot.*`/`sdk.*` verified-action API
(that's ticket #3, https://github.com/blokboy/sc2-sdk/issues/3) -- it's a
thin, direct use of python-sc2's own `run_game`/`BotAI`/`Computer` primitives,
just enough to prove the install + connect + play + report-result path works
end to end. Every later ticket's tests reuse this module's
`play_vs_builtin_ai` via the `sc2_game_harness` fixture in
`tests/conftest.py`.
"""

from __future__ import annotations

import argparse
import sys

from sc2 import maps
from sc2.bot_ai import BotAI
from sc2.data import Difficulty, Race, Result
from sc2.main import run_game
from sc2.player import Bot, Computer

#: Fixed test map this ticket's setup syncs and its harness runs on by
#: default (see install.maps.DEFAULT_MAPS).
DEFAULT_MAP = "AutomatonLE"

_RACE_BY_NAME = {r.name.lower(): r for r in Race}
_DIFFICULTY_BY_NAME = {d.name.lower(): d for d in Difficulty}


class _NullBot(BotAI):
    """Minimal do-nothing BotAI.

    python-sc2's `run_game` needs a controllable `Bot(race, ai)` participant
    to pit against `Computer(...)`; this is the smallest possible one. It is
    intentionally not a "real" bot -- it exists only so this raw script has
    a legal player to hand to run_game, without reaching for the sdk's
    higher-level API that ticket #3 hasn't built yet.
    """

    async def on_step(self, iteration: int) -> None:
        return None


def play_vs_builtin_ai(
    map_name: str = DEFAULT_MAP,
    my_race: Race = Race.Random,
    opponent_race: Race = Race.Random,
    difficulty: Difficulty = Difficulty.Easy,
    realtime: bool = False,
    game_time_limit: int | None = None,
) -> Result:
    """Launch a full game against the built-in AI and run it to completion.

    Args:
        map_name: name of a synced map (see install.maps.DEFAULT_MAPS),
            resolved via sc2.maps.get() -- must already exist under the
            local install's Maps directory (run `setup` first).
        my_race: the race our side plays.
        opponent_race: the built-in AI's race.
        difficulty: the built-in AI's difficulty.
        realtime: if False (default), the game steps only as fast as our
            (trivial) bot responds -- deterministic and fast for tests. If
            True, it runs at wall-clock speed.
        game_time_limit: optional wall-clock-independent safety cap, in
            in-game seconds; the match is scored a Tie if neither side has
            won by then. Recommended for automated/test runs so a stuck
            game can't hang forever.

    Returns:
        sc2.data.Result -- one of Victory, Defeat, Tie, or (in edge cases
        python-sc2 itself reports, e.g. a crashed client) Undecided.
    """
    result = run_game(
        maps.get(map_name),
        [Bot(my_race, _NullBot()), Computer(opponent_race, difficulty)],
        realtime=realtime,
        game_time_limit=game_time_limit,
    )
    # run_game returns a single Result for Bot-vs-Computer (a list only for
    # Human/Bot-vs-Human/Bot); assert to catch a python-sc2 API change loudly
    # rather than silently mis-typing the return value.
    assert isinstance(result, Result), f"Expected a single Result for a vs-Computer game, got {result!r}"
    return result


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", default=DEFAULT_MAP, help=f"Map name to play on (default: {DEFAULT_MAP}).")
    parser.add_argument(
        "--race",
        choices=sorted(_RACE_BY_NAME),
        default="random",
        help="Our race.",
    )
    parser.add_argument(
        "--opponent-race",
        choices=sorted(_RACE_BY_NAME),
        default="random",
        help="Built-in AI's race.",
    )
    parser.add_argument(
        "--difficulty",
        choices=sorted(_DIFFICULTY_BY_NAME),
        default="easy",
        help="Built-in AI's difficulty.",
    )
    parser.add_argument("--realtime", action="store_true", help="Run at wall-clock speed instead of stepped.")
    parser.add_argument(
        "--time-limit",
        type=int,
        default=None,
        help="In-game-second safety cap; the match is scored a Tie if unresolved by then.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = play_vs_builtin_ai(
        map_name=args.map,
        my_race=_RACE_BY_NAME[args.race],
        opponent_race=_RACE_BY_NAME[args.opponent_race],
        difficulty=_DIFFICULTY_BY_NAME[args.difficulty],
        realtime=args.realtime,
        game_time_limit=args.time_limit,
    )
    print(f"RESULT: {result.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
