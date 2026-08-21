"""Ticket #10: exercise `sdk.runtime.run_bot_vs_bot` against a real, local,
headless SC2 game -- no mocking python-sc2, per the spec's one testing seam.

Uses the same documented `bots/idle_example.py` script `test_bot_script_
runtime.py` exercises (see that file and `sdk.script_runner`'s module
docstring for the convention), loaded twice to get two independent
`VerifiedBotAI` instances -- proving self-play really is "one script's class,
instantiated twice, playing itself" rather than some special-cased path.
`run_game`'s Bot-vs-Bot shape (a two-element list of `Result`, not a single
`Result` like vs-Computer -- see `runtime.run_bot_vs_bot`'s docstring) is
asserted directly here, since that's the whole point of this ticket's
runtime primitive.
"""

from __future__ import annotations

import pytest
from sc2.data import Race, Result

from sdk.script_runner import load_bot_class, resolve_script_path

_SAFETY_TIME_LIMIT = 20 * 60  # in-game seconds; same generous cap other integration tests use.


@pytest.mark.integration
def test_documented_bot_script_plays_a_full_match_against_itself(sc2_selfplay_harness):
    bot_class = load_bot_class(resolve_script_path("idle_example"))
    bot_a = bot_class()
    bot_b = bot_class()

    results = sc2_selfplay_harness(
        bot_a,
        bot_b,
        map_name="AutomatonLE",
        race_a=Race.Terran,
        race_b=Race.Terran,
        realtime=False,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    assert isinstance(results, list)
    assert len(results) == 2
    for result in results:
        assert isinstance(result, Result)
        assert result in (Result.Victory, Result.Defeat, Result.Tie), (
            f"Expected each side to resolve to a definite outcome, got {result!r}"
        )
    # A two-player match's outcomes are opposite (or both Tie) -- never both
    # a Victory, which would indicate the harness mixed up which Result
    # belongs to which side.
    assert not (results[0] is Result.Victory and results[1] is Result.Victory)

    # Both instances really did play (not just get constructed) -- each
    # ran its own on_start via the same VerifiedBotAI machinery
    # test_bot_script_runtime.py's wiring test already exercises directly.
    assert bot_a._acted is True
    assert bot_b._acted is True
