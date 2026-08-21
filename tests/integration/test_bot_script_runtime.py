"""Ticket #7: wiring test for the standalone bot-script runtime -- confirms
a real `BotAI` subclass at the documented `bots/<name>.py` location is
discovered, loaded, and actually driven through a full real, non-mocked SC2
game via `sdk.script_runner.run_bot_script`, per the spec's one testing
seam (real local headless client, real subsequent game state -- no mocking
python-sc2).

This exercises the exact `bots/idle_example.py` file committed at the
documented location (see that file and `sdk.script_runner`'s module
docstring for the convention) -- not a synthetic stand-in written just for
this test. `tests/test_script_runner.py` covers the discovery/loading edge
cases (missing script, zero/multiple BotAI subclasses) without needing a
live game; this test is specifically "does the whole pipeline -- name ->
file -> import -> class -> instance -> run_game -> Result -- work for real."

Run stepped (`realtime=False`) with a safety time limit, same as every
other integration test's harness call, rather than at real-time speed:
proving the *wiring* is correct doesn't require timing out a full
wall-clock-speed match, and `run_bot_script`'s own `realtime=True` default
(documented in its docstring) is what a real unattended run uses.
"""

from __future__ import annotations

import pytest
from sc2.data import Difficulty, Race, Result

from sdk.bot import VerifiedBotAI

_SAFETY_TIME_LIMIT = 20 * 60  # in-game seconds; same generous cap other integration tests use.


@pytest.mark.integration
def test_documented_bot_script_is_discovered_loaded_and_run(sc2_bot_script_harness):
    result = sc2_bot_script_harness(
        "idle_example",
        map_name="AutomatonLE",
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        realtime=False,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    assert isinstance(result, Result)
    assert result in (Result.Victory, Result.Defeat, Result.Tie), (
        f"Expected the match to resolve to a definite outcome, got {result!r}"
    )


@pytest.mark.integration
def test_unknown_bot_name_raises_before_launching_a_game(sc2_bot_script_harness):
    with pytest.raises(FileNotFoundError, match="No bot script found"):
        sc2_bot_script_harness(
            "this_bot_does_not_exist",
            realtime=False,
            game_time_limit=_SAFETY_TIME_LIMIT,
        )


@pytest.mark.integration
def test_run_bot_script_hands_a_real_verified_bot_ai_instance_to_run_game(sc2_bot_script_harness, tmp_path):
    """Confirms the same `bot.*`/`sdk.*` surface `execute_code` mode uses is
    available identically inside a standalone script -- the loaded instance
    really is a `VerifiedBotAI` with `self.bot` wired up mid-game, not some
    reduced stand-in -- by pointing the harness at a scratch bot (via
    `bots_dir`) that records what `self.bot`/`self.sdk` looked like from
    inside its own `on_step`, the same pattern
    `test_verified_bot_actions.py` uses to assert on a live bot instance
    after the game runs.
    """
    script = tmp_path / "recording_bot.py"
    script.write_text(
        "from __future__ import annotations\n"
        "from sc2.ids.unit_typeid import UnitTypeId\n"
        "from sdk.bot import Bot, VerifiedBotAI\n"
        "\n"
        "class RecordingBot(VerifiedBotAI):\n"
        "    async def on_start(self) -> None:\n"
        "        await super().on_start()\n"
        "        self.saw_bot_wrapper = isinstance(self.bot, Bot)\n"
        "        self.saw_sdk_is_self = self.sdk is self\n"
        "        self.train_outcome = None\n"
        "        self._acted = False\n"
        "\n"
        "    async def on_step(self, iteration: int) -> None:\n"
        "        if self._acted:\n"
        "            return\n"
        "        self._acted = True\n"
        "        self.train_outcome = await self.bot.train(UnitTypeId.SCV)\n"
    )

    # run_bot_script itself only returns the final Result, not the bot
    # instance -- so drive discovery/loading/running directly here (the
    # same three calls run_bot_script makes internally) to get the live
    # instance back afterward, exactly like sdk.bot.VerifiedBotAI's own
    # docstring describes for asserting on recorded state post-game.
    from sdk.runtime import run_bot_vs_builtin_ai
    from sdk.script_runner import load_bot_class, resolve_script_path

    resolved = resolve_script_path("recording_bot", bots_dir=tmp_path)
    bot_class = load_bot_class(resolved)
    bot_ai = bot_class()
    assert isinstance(bot_ai, VerifiedBotAI)

    result = run_bot_vs_builtin_ai(
        bot_ai,
        map_name="AutomatonLE",
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        realtime=False,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    assert isinstance(result, Result)
    assert bot_ai.saw_bot_wrapper is True
    assert bot_ai.saw_sdk_is_self is True
    assert bot_ai.train_outcome is not None
    assert bot_ai.train_outcome.ok is True
