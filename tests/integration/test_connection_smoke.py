"""Minimal post-setup proof that the local SC2 client can run a game.

Unlike the full integration suite, this test is intentionally suitable for
onboarding: it launches one client on the primary synced map and stops after
one in-game second.
"""

from __future__ import annotations

import pytest
from sc2.data import Difficulty, Race, Result


@pytest.mark.integration
def test_local_client_connects_to_primary_map(sc2_game_harness):
    result = sc2_game_harness(
        map_name="AutomatonLE",
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.VeryEasy,
        realtime=False,
        game_time_limit=1,
    )

    assert result in (Result.Victory, Result.Defeat, Result.Tie)
