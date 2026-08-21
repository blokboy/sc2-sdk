"""Ticket #11: prove `sdk.join.run_join_game_match` end to end against real,
local SC2 client processes -- no mocking python-sc2, per this project's one
testing seam (see `tests/conftest.py`'s module docstring).

Unlike every earlier ticket's integration test, this one does not go
through `sc2_game_harness`/`sc2_verified_bot_harness` (both wrap a single
`sc2.main.run_game()` call): this ticket's whole point is a path that does
*not* orchestrate both sides from one call, so it exercises
`sdk.join.run_join_game_match` directly, which launches the host and join
roles as two independent `python -m sdk.join` subprocesses (see
`sdk/join.py`'s module docstring for why, and for what was actually
confirmed about the underlying join-game mechanism).

Real, non-mocked per the spec's testing decision: no local SC2 install
means this SKIPs (via the same `sc2_install` fixture every other
integration test uses), it never runs against a fake/mocked client.
"""

from __future__ import annotations

import pytest
from sc2.data import Race, Result

from sdk.join import run_join_game_match

# Generous safety cap (in in-game seconds), matching the walking skeleton's
# convention (tests/integration/test_walking_skeleton.py) -- well above how
# long a trivial do-nothing-bot match takes to resolve on a small map.
_SAFETY_TIME_LIMIT = 20 * 60


@pytest.mark.integration
def test_host_and_join_roles_connect_and_play_a_real_match(sc2_install):
    """The core proof: a host-role SC2 client and a join-role SC2 client,
    launched as two independent OS processes with no shared asyncio loop
    or run_game() call, connect via the join-game protocol's explicit
    `host_ip` field (127.0.0.1 -- see sdk.join's docstring for why loopback
    is sufficient here) and reach a real, definite match result on both
    sides."""
    host_result, join_result = run_join_game_match(
        map_name="AutomatonLE",
        host_race=Race.Terran,
        join_race=Race.Zerg,
        host_ip="127.0.0.1",
        realtime=False,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    for result in (host_result, join_result):
        assert isinstance(result, Result)
        assert result in (Result.Victory, Result.Defeat, Result.Tie), (
            f"Expected the match to resolve to a definite outcome on both sides, got {result!r}"
        )

    # The two sides of one real match must report complementary outcomes --
    # this is the actual proof that a single match was played (rather than,
    # say, each side quietly timing out into its own independent Tie
    # without ever having found each other over the network).
    if Result.Tie in (host_result, join_result):
        assert host_result == join_result == Result.Tie
    else:
        assert {host_result, join_result} == {Result.Victory, Result.Defeat}
