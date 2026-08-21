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


def run_bot_vs_bot(
    bot_a: BotAI,
    bot_b: BotAI,
    map_name: str = DEFAULT_MAP,
    race_a: Race = Race.Terran,
    race_b: Race = Race.Terran,
    realtime: bool = False,
    game_time_limit: int | None = None,
) -> list[Result]:
    """Launch a full local match between two `BotAI` instances -- ticket
    #10's self-play mode -- and run it to completion. Both objects are still
    live and inspectable afterward, same as `run_bot_vs_builtin_ai`'s
    `bot_ai` argument.

    This is a sibling to `run_bot_vs_builtin_ai`, not a variant of it with an
    `if opponent_is_a_bot` branch: `run_game` itself returns a *different
    shape* depending on whether the second player is a `Computer` or another
    `Bot` (see `sc2.main.run_game`'s own docstring -- a single `Result` for
    vs-Computer, a `list` of two `Result`s for Bot-vs-Bot/Bot-vs-Human,
    because that path actually spawns and hosts/joins two local SC2
    processes rather than one client playing against the built-in AI
    in-process). Folding both shapes behind one return type would either
    lose information (always returning one `Result`) or force every existing
    `run_bot_vs_builtin_ai` caller to start handling a list -- neither of
    which this ticket's brief asks for.

    Args:
        bot_a: a constructed `BotAI` (usually `VerifiedBotAI`) instance for
            the first side.
        bot_b: a constructed `BotAI` instance for the second side. Pass a
            second, separate instance of the same class as `bot_a` to play a
            script against itself (what `sc2-sdk-selfplay` does when given
            only one script), or a different class entirely for two
            different bots.
        map_name: see `run_bot_vs_builtin_ai`.
        race_a: the race `bot_a` plays. Defaults to Terran for the same
            reason `run_bot_vs_builtin_ai.my_race` does -- pass explicitly
            for Protoss/Zerg bots.
        race_b: the race `bot_b` plays.
        realtime: if False (default), the game steps only as fast as both
            bots respond -- deterministic and fast for tests, consistent
            with `run_bot_vs_builtin_ai`'s default.
        game_time_limit: see `run_bot_vs_builtin_ai`.

    Returns:
        A two-element list of `sc2.data.Result`, `[result_for_bot_a,
        result_for_bot_b]` -- `run_game`'s own shape for a non-Computer
        match, passed through as-is rather than collapsed to one value.
    """
    result = run_game(
        maps.get(map_name),
        [Bot(race_a, bot_a), Bot(race_b, bot_b)],
        realtime=realtime,
        game_time_limit=game_time_limit,
    )
    assert isinstance(result, list) and all(isinstance(r, Result) for r in result), (
        f"Expected a two-element list of Result for a Bot-vs-Bot game, got {result!r}"
    )
    return result
