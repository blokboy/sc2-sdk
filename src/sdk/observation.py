"""Race-agnostic observation snapshot of a live python-sc2 game.

`observe()` is a pure, side-effect-free read of whatever state the wrapped
`BotAI` instance currently holds (`self.units`, `self.structures`,
`self.state`, ...) -- it never advances the game or issues any command.
That split (read vs. act) is deliberate: `bot.observe()` is always safe to
call, including *after* the match has ended, in which case it reports the
final match result via `Observation.match_result` instead of live
unit/resource data (which will simply be whatever was last known).

See `bot.py`'s `Bot` class for the verified-action layer that calls this
function after each action to determine whether the intended effect
actually occurred.
"""

from __future__ import annotations

from dataclasses import dataclass

from sc2.bot_ai import BotAI
from sc2.data import Result
from sc2.unit import Unit


@dataclass(frozen=True)
class UnitSnapshot:
    """A lightweight, JSON-friendly summary of one unit or structure.

    Deliberately not the raw python-sc2 `Unit` object -- this is what
    `bot.observe()` reports to keep the semantic tier's output plain data an
    agent can reason about without learning python-sc2's own object model.
    The raw `Unit` is still reachable via `sdk.*` (e.g. `sdk.units`,
    `sdk.structures`) for anyone who needs it.
    """

    tag: int
    type_name: str
    position: tuple[float, float]
    health: float
    health_max: float
    shield: float
    energy: float
    build_progress: float
    is_idle: bool
    order_abilities: tuple[str, ...]


def _snapshot(unit: Unit) -> UnitSnapshot:
    return UnitSnapshot(
        tag=unit.tag,
        type_name=unit.type_id.name,
        position=(unit.position.x, unit.position.y),
        health=unit.health,
        health_max=unit.health_max,
        shield=unit.shield,
        energy=unit.energy,
        build_progress=unit.build_progress,
        is_idle=unit.is_idle,
        order_abilities=tuple(order.ability.exact_id.name for order in unit.orders),
    )


@dataclass(frozen=True)
class MinimapSummary:
    """A compact summary of the visibility/creep minimap, not the raw grid.

    python-sc2 exposes the full per-tile grid as `sdk.state.visibility` /
    `sdk.state.creep` (numpy-backed `PixelMap`s) for anyone who needs
    pixel-level data; this summary is what `bot.observe()` reports because
    dumping a full WxH grid into every observation is neither
    agent-friendly nor a meaningful "did my action work" signal on its own.
    """

    width: int
    height: int
    visible_tile_count: int
    explored_tile_count: int  # currently-visible or previously-seen ("fogged")
    creep_tile_count: int


def _minimap_summary(ai: BotAI) -> MinimapSummary:
    visibility = ai.state.visibility
    creep = ai.state.creep
    # visibility values: 0=Hidden, 1=Fogged (previously seen), 2=Visible now.
    visible = int((visibility.data_numpy == 2).sum())
    explored = int((visibility.data_numpy >= 1).sum())
    creep_count = int((creep.data_numpy >= 1).sum())
    return MinimapSummary(
        width=visibility.width,
        height=visibility.height,
        visible_tile_count=visible,
        explored_tile_count=explored,
        creep_tile_count=creep_count,
    )


@dataclass(frozen=True)
class Observation:
    """A full snapshot of what `bot.observe()` reports.

    `match_result` is `None` while the game is ongoing and is populated
    (Victory/Defeat/Tie/Undecided) once `on_end` has fired -- see
    `bot.py`'s `VerifiedBotAI.on_end`. Calling `observe()` after the game
    has ended still works (this function never touches the network); it
    just reports whatever state was current as of the last real observation
    plus the now-known match result.
    """

    game_time: float
    units: tuple[UnitSnapshot, ...]
    structures: tuple[UnitSnapshot, ...]
    enemy_units: tuple[UnitSnapshot, ...]
    enemy_structures: tuple[UnitSnapshot, ...]
    minerals: int
    vespene: int
    supply_used: float
    supply_cap: float
    supply_left: float
    minimap: MinimapSummary
    match_result: Result | None


def observe(ai: BotAI, match_result: Result | None) -> Observation:
    """Build an `Observation` from a `BotAI` instance's current state.

    Race-agnostic: reads only attributes `BotAI` populates identically for
    Terran, Protoss, and Zerg (`self.units`, `self.structures`,
    `self.enemy_units`/`self.enemy_structures`, which python-sc2 already
    restricts to currently-*visible* enemy units/structures -- not a
    fog-of-war memory of everything ever seen).
    """
    return Observation(
        game_time=ai.time,
        units=tuple(_snapshot(u) for u in ai.units),
        structures=tuple(_snapshot(u) for u in ai.structures),
        enemy_units=tuple(_snapshot(u) for u in ai.enemy_units),
        enemy_structures=tuple(_snapshot(u) for u in ai.enemy_structures),
        minerals=ai.minerals,
        vespene=ai.vespene,
        supply_used=ai.supply_used,
        supply_cap=ai.supply_cap,
        supply_left=ai.supply_left,
        minimap=_minimap_summary(ai),
        match_result=match_result,
    )
