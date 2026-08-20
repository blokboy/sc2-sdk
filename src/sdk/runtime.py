"""Run an arbitrary `BotAI` instance (typically a `VerifiedBotAI` subclass,
see `bot.py`) against the game's built-in AI.

This is a deliberate sibling to `sdk.play.play_vs_builtin_ai`, not a
modification of it: ticket #3's brief explicitly says not to touch
`play.py`'s connect-and-play logic (the proven walking-skeleton path from
#2/#9) except for an actual bug fix, and `play_vs_builtin_ai` hardcodes a
trivial `_NullBot` -- there is no way to hand it a real bot instance without
changing its signature. `run_bot_vs_builtin_ai` below is the same
`sc2.maps`/`sc2.main.run_game`/`sc2.player.Bot`/`Computer` primitives,
generalized to accept any `BotAI` instance, so ticket #3 (and #4-#8 after
it) can drive a real verified-action bot through a real game without
touching the walking skeleton at all.
"""

from __future__ import annotations

from sc2 import maps
from sc2.bot_ai import BotAI
from sc2.data import Difficulty, Race, Result
from sc2.main import run_game
from sc2.player import Bot, Computer

#: Same fixed test map ticket #2's setup syncs and its harness defaults to.
DEFAULT_MAP = "AutomatonLE"


def run_bot_vs_builtin_ai(
    bot_ai: BotAI,
    map_name: str = DEFAULT_MAP,
    my_race: Race = Race.Terran,
    opponent_race: Race = Race.Random,
    difficulty: Difficulty = Difficulty.Easy,
    realtime: bool = False,
    game_time_limit: int | None = None,
) -> Result:
    """Launch a full game against the built-in AI, driving `bot_ai`
    (typically a `VerifiedBotAI` subclass instance) as our side, and run it
    to completion.

    Args:
        bot_ai: a constructed `BotAI` (usually `VerifiedBotAI`) instance.
            The same object is still live and inspectable after this
            function returns -- see `bot.py`'s `VerifiedBotAI` docstring
            for why that's the point.
        map_name: name of a synced map -- must already exist under the
            local install's Maps directory (run `setup` first).
        my_race: the race `bot_ai` plays. Defaults to Terran since that's
            the only race ticket #3's verified actions have been exercised
            against; pass explicitly for Protoss/Zerg bots.
        opponent_race: the built-in AI's race.
        difficulty: the built-in AI's difficulty.
        realtime: if False (default), the game steps only as fast as
            `bot_ai` responds -- deterministic and fast for tests.
        game_time_limit: optional wall-clock-independent safety cap, in
            in-game seconds; the match is scored a Tie if neither side has
            won by then.

    Returns:
        sc2.data.Result -- one of Victory, Defeat, Tie, or (in edge cases
        python-sc2 itself reports, e.g. a crashed client) Undecided.
    """
    result = run_game(
        maps.get(map_name),
        [Bot(my_race, bot_ai), Computer(opponent_race, difficulty)],
        realtime=realtime,
        game_time_limit=game_time_limit,
    )
    assert isinstance(result, Result), f"Expected a single Result for a vs-Computer game, got {result!r}"
    return result
