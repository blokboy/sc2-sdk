"""Issue #13: fast, non-integration tests for `sdk.observation`'s
unrecognized-ability-id tolerance -- no SC2 client involved, unlike this
project's integration suite (see `tests/conftest.py`'s module docstring for
why that's the one primary integration seam).

python-sc2's own `Unit.orders` property (`sc2/unit.py`, `UnitOrder.
from_proto`) does a bare `game_data.abilities[proto.ability_id]` dict
subscript with no fallback, so a live game reporting an order with an
ability id that isn't a known `AbilityId` enum member (id 4135 was seen
live, on an SCV's order mid-harvest-transition -- exact ability
unconfirmed) raises an uncaught `KeyError` there. Because `unit.orders`
builds its whole order list eagerly, that `KeyError` used to propagate all
the way out of `sdk.observation.observe()` for *every* unit in the game
over one bad order on one unit, which in turn took down both
`bot.observe()` and `bot.build()`'s post-dispatch confirmation step (see
`bot.py`) for the rest of the session.

No real `sc2.unit.Unit`/`sc2.game_data.GameData` is constructed here --
both require a live game connection to build (`GameData` is populated from
the game client's static-data response, `Unit` from a game-state proto).
Instead, these stand in with minimal duck-typed stubs exposing just the
attributes `sdk.observation`'s private helpers actually read, which is
enough to reproduce python-sc2's real `KeyError`-on-lookup behavior
(`_StubUnit.orders` below mirrors `UnitOrder.from_proto`'s own unguarded
subscript) without booting SC2.
"""

from __future__ import annotations

from types import SimpleNamespace

from sdk.observation import _order_abilities, _order_ability_name, _snapshot

# A single "known" ability, standing in for one of the many entries
# sc2.game_data.GameData.abilities actually populates from the game
# client's static data at match start. The int id itself is arbitrary --
# only that it *is* a key in the stubbed `abilities` dict matters.
_KNOWN_ABILITY_ID = 3666
_KNOWN_ABILITY = SimpleNamespace(exact_id=SimpleNamespace(name="HARVEST_GATHER"))
_ABILITIES = {_KNOWN_ABILITY_ID: _KNOWN_ABILITY}

# The id from issue #13's live repro -- deliberately absent from
# `_ABILITIES`, the same way it was absent from the real
# `game_data.abilities` dict that day.
_UNRECOGNIZED_ABILITY_ID = 4135


class _StubUnit:
    """Just enough of python-sc2's `Unit` surface for
    `sdk.observation`'s `_snapshot`/`_order_abilities`.

    `.orders` mirrors `sc2.unit.Unit.orders`'s real behavior: it builds its
    whole list eagerly via a plain `abilities[ability_id]` subscript per
    order (mirroring `UnitOrder.from_proto`), so it raises `KeyError` if
    *any* order's ability id is missing from `abilities` -- before any
    order, including this unit's other, perfectly fine orders, comes back.
    `._proto.orders` mirrors the raw proto orders `_order_abilities`'s
    fallback path reads instead, each exposing only the `.ability_id` int
    the real proto order carries.
    """

    def __init__(self, tag: int, order_ability_ids: list[int], abilities: dict[int, object]) -> None:
        self.tag = tag
        self.type_id = SimpleNamespace(name="SCV")
        self.position = SimpleNamespace(x=1.0, y=2.0)
        self.health = 45.0
        self.health_max = 45.0
        self.shield = 0.0
        self.energy = 0.0
        self.build_progress = 1.0
        self.is_idle = False
        self._proto = SimpleNamespace(orders=[SimpleNamespace(ability_id=aid) for aid in order_ability_ids])
        self._order_ability_ids = order_ability_ids
        self._abilities = abilities

    @property
    def orders(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(ability=self._abilities[aid]) for aid in self._order_ability_ids]


def _game_data(abilities: dict[int, object]) -> SimpleNamespace:
    return SimpleNamespace(abilities=abilities)


def test_order_ability_name_resolves_known_id() -> None:
    assert _order_ability_name(_KNOWN_ABILITY_ID, _ABILITIES) == "HARVEST_GATHER"


def test_order_ability_name_falls_back_for_unrecognized_id() -> None:
    assert _order_ability_name(_UNRECOGNIZED_ABILITY_ID, _ABILITIES) == "UNKNOWN_ABILITY_4135"


def test_order_abilities_fast_path_all_orders_known() -> None:
    unit = _StubUnit(tag=1, order_ability_ids=[_KNOWN_ABILITY_ID], abilities=_ABILITIES)
    assert _order_abilities(unit, _game_data(_ABILITIES)) == ("HARVEST_GATHER",)


def test_order_abilities_isolates_one_unrecognized_order_from_a_good_one() -> None:
    # Reproduces issue #13: python-sc2's real `unit.orders` raises KeyError
    # for the WHOLE list when any one order's ability id is unrecognized --
    # including here, where the unit also has a perfectly good order
    # queued. `_order_abilities` must resolve the good order and fall back
    # only for the bad one, not lose the good order or propagate the
    # KeyError.
    unit = _StubUnit(tag=1, order_ability_ids=[_KNOWN_ABILITY_ID, _UNRECOGNIZED_ABILITY_ID], abilities=_ABILITIES)
    assert _order_abilities(unit, _game_data(_ABILITIES)) == ("HARVEST_GATHER", "UNKNOWN_ABILITY_4135")


def test_snapshot_does_not_raise_on_unit_with_unrecognized_ability_order() -> None:
    # The end-to-end regression: before this fix, this call raised
    # KeyError (see issue #13's traceback), which meant one such unit
    # anywhere in `ai.units` failed observe() for every unit in the game.
    unit = _StubUnit(tag=42, order_ability_ids=[_UNRECOGNIZED_ABILITY_ID], abilities=_ABILITIES)
    snapshot = _snapshot(unit, _game_data(_ABILITIES))
    assert snapshot.tag == 42
    assert snapshot.order_abilities == ("UNKNOWN_ABILITY_4135",)
