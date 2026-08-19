"""Ticket #2 walking skeleton: boot a real local SC2 game against the
built-in AI and confirm the result is surfaced back to the caller.

These are the tests that exercise `sc2_game_harness` (tests/conftest.py) for
the first time; every later ticket's integration tests are expected to reuse
that same fixture rather than re-deriving how to boot a game.

Real, non-mocked per the spec's testing decision: no local SC2 install means
these SKIP (see tests/conftest.py:sc2_install), they never run against a
fake/mocked client.
"""

from __future__ import annotations

from sc2.data import Difficulty, Race, Result

# Generous safety cap (in in-game seconds) so a stuck client can't hang the
# suite forever; well above how long an Easy built-in AI game typically
# takes to resolve on a small ladder map.
_SAFETY_TIME_LIMIT = 20 * 60


def test_full_game_vs_builtin_ai_runs_to_completion_and_reports_result(sc2_game_harness):
    result = sc2_game_harness(
        map_name="AutomatonLE",
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        realtime=False,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    # This is the real, subsequent game state this walking skeleton exists to
    # prove we can observe: the match actually resolved (not just "a command
    # was sent" -- python-sc2 reports Undecided if e.g. the client crashed).
    assert isinstance(result, Result)
    assert result in (Result.Victory, Result.Defeat, Result.Tie), (
        f"Expected the match to resolve to a definite outcome, got {result!r}"
    )


def test_race_and_difficulty_are_respected_on_the_secondary_map(sc2_game_harness):
    """Exercises the harness with a different map/race/difficulty
    combination than the primary test, to prove those parameters actually
    reach the launched game rather than being ignored -- and that the
    second map `setup` synced (see install.maps.DEFAULT_MAPS) is playable
    too, not just the primary one."""
    result = sc2_game_harness(
        map_name="KairosJunctionLE",
        my_race=Race.Protoss,
        opponent_race=Race.Terran,
        difficulty=Difficulty.VeryEasy,
        realtime=False,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    assert isinstance(result, Result)
    assert result in (Result.Victory, Result.Defeat, Result.Tie)
