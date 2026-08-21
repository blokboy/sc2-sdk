"""Unit tests for sdk.matchcode: the pure encode/decode, race-conflict, and
timeout-wrapper logic ticket #12 (host/join 1v1) builds its CLI on top of.
No SC2 client involved -- see tests/conftest.py's module docstring for why
that's this project's one integration-test seam, and why these stay outside
it.
"""

from __future__ import annotations

import asyncio

import pytest
from sc2.data import Race
from sc2.portconfig import Portconfig

from sdk.matchcode import JoinTimeoutError, decode_match_code, encode_match_code, resolve_race, wait_for_joiner


def test_match_code_round_trips_all_fields():
    portconfig = Portconfig()
    try:
        code = encode_match_code(
            host_ip="203.0.113.5",
            portconfig=portconfig,
            map_name="AutomatonLE",
            race_pin=Race.Terran,
            token="secret-token-123",
        )
        decoded = decode_match_code(code)

        assert decoded.host_ip == "203.0.113.5"
        assert decoded.portconfig.as_json == portconfig.as_json
        assert decoded.map_name == "AutomatonLE"
        assert decoded.race_pin == Race.Terran
        assert decoded.token == "secret-token-123"
    finally:
        portconfig.clean()


def test_match_code_round_trips_no_race_pin():
    portconfig = Portconfig()
    try:
        code = encode_match_code(
            host_ip="203.0.113.5",
            portconfig=portconfig,
            map_name="AutomatonLE",
            race_pin=None,
            token="secret-token-123",
        )
        decoded = decode_match_code(code)

        assert decoded.race_pin is None
    finally:
        portconfig.clean()


def test_resolve_race_joiner_choice_wins_over_conflicting_host_pin():
    assert resolve_race(host_pin=Race.Terran, joiner_race=Race.Zerg) == Race.Zerg


def test_resolve_race_joiner_choice_used_when_host_did_not_pin():
    assert resolve_race(host_pin=None, joiner_race=Race.Protoss) == Race.Protoss


def test_resolve_race_falls_back_to_host_pin_when_joiner_did_not_choose():
    assert resolve_race(host_pin=Race.Terran, joiner_race=None) == Race.Terran


def test_resolve_race_defaults_to_random_when_neither_specified():
    assert resolve_race(host_pin=None, joiner_race=None) == Race.Random


def test_wait_for_joiner_returns_result_when_awaitable_finishes_in_time():
    async def fast():
        return "joined"

    result = asyncio.run(wait_for_joiner(fast(), timeout=1.0))
    assert result == "joined"


def test_wait_for_joiner_raises_clear_error_when_awaitable_never_finishes():
    async def never():
        await asyncio.sleep(10)
        return "too late"

    with pytest.raises(JoinTimeoutError, match="0.05"):
        asyncio.run(wait_for_joiner(never(), timeout=0.05))
