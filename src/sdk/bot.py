"""The verified `bot.*` action/observation layer, plus `sdk.*` raw passthrough.

This is the core deliverable of ticket #3
(https://github.com/blokboy/sc2-sdk/issues/3): a `Bot` wrapper around a live
`python-sc2` `BotAI` instance that turns "issue a command" into "issue a
command and confirm what actually happened," per the project's central
design decision (see #1's spec, "Implementation Decisions"):

    `bot.*` -- the primary, high-level tier. Semantic, verified actions and
    observations... Each action re-observes game state after issuing the
    underlying python-sc2 call(s) and reports whether the intended effect
    occurred, not just that a command was dispatched.

    `sdk.*` -- the raw tier. Direct passthrough to the underlying
    python-sc2 BotAI instance and its unit/action API.

Design decision: why verification needs to advance real game steps
--------------------------------------------------------------------
python-sc2's `BotAI.do()` (which every write-side call -- train, build,
research, move, attack -- ultimately goes through) only *queues* a command
into `self.actions`; the command is not sent to the SC2 process until
`main.py`'s outer loop calls `_after_step()` after `on_step` returns, and
the server doesn't act on it until the *following* simulation step. So
"did the command actually take effect" cannot be answered synchronously,
purely from Python-side bookkeeping, within the same call that issued it --
only the server's own subsequent state does. python-sc2 itself ships a
`@final` method for exactly this situation: `BotAI._advance_steps(n)`,
documented as "meant to be used as a debugging and testing tool" -- it
flushes queued actions, steps the simulation, fetches a fresh observation,
and refreshes `self.units`/`self.structures`/`self.state` accordingly. Each
verified action below issues its underlying command, then calls
`_advance_steps(1)` in a small retry loop (bounded by `max_wait_steps`) and
re-observes until the intended effect is confirmed or the window elapses --
at which point it reports `effect_confirmed=False` rather than silently
assuming success. See `outcomes.py` for the exact three-way ok/confirmed/
error shape this produces, and `runtime.py` for how a `BotAI` subclass
built around this class is actually driven through a live game.

Race-agnostic by design: train/build/research/move/chat are all generic
python-sc2 mechanisms (the same underlying `BotAI.train()`/`.build()`/
`.research()` work for any of the three races); nothing Terran-specific is
hardcoded here. Ticket #3 only *exercises* this against Terran integration
tests -- #4 (Protoss) and #5 (Zerg) are expected to reuse this class
directly rather than re-deriving the verification pattern.
"""

from __future__ import annotations

from sc2.bot_ai import BotAI
from sc2.data import Result
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from sdk.observation import Observation, observe
from sdk.outcomes import BuildOutcome, ChatOutcome, MoveOutcome, ResearchOutcome, TrainOutcome

#: Number of raw simulation frames each verification poll advances by (see
#: Bot._advance). 4 matches python-sc2's own default per-iteration cadence
#: (Client.game_step), i.e. one poll below is worth roughly one normal bot
#: "tick" of game time (~0.18s).
_POLL_FRAMES = 4

#: Default number of verification polls (each _POLL_FRAMES long) a verified
#: action will retry, re-observing after each, before giving up on
#: confirming its effect. Kept small: these polls happen inside a single
#: on_step call (see the module docstring), so a large default would make
#: every action call expensive. Callers doing something that legitimately
#: takes longer (e.g. a structure whose builder has to walk further, or a
#: build with no fast-build cheat active) can pass a larger max_wait_steps
#: explicitly -- see e.g. build()'s own, larger default below.
_DEFAULT_MAX_WAIT_STEPS = 5


class Bot:
    """Wraps a live `BotAI` instance with the verified `bot.*` API.

    Constructed once per game (see `VerifiedBotAI` below, which every
    integration test's bot subclasses). `bot.sdk` is the raw passthrough
    tier -- literally the wrapped `BotAI` instance itself, since that
    instance *is* "the underlying python-sc2 BotAI instance and its
    unit/action API" the spec asks `sdk.*` to expose.
    """

    def __init__(self, ai: BotAI) -> None:
        self._ai = ai
        #: Set by VerifiedBotAI.on_end once the match concludes; None while
        #: the game is still in progress. Exposed via Observation.match_result.
        self.match_result: Result | None = None

    @property
    def sdk(self) -> BotAI:
        """Raw passthrough to the underlying python-sc2 BotAI instance."""
        return self._ai

    def observe(self) -> Observation:
        """Report own units/structures/resources/supply, visible enemy
        units/structures, minimap/vision, and match outcome (once known).

        Pure and side-effect-free -- safe to call at any time, including
        after the match has ended (it then reports the last-known state
        plus the now-final `match_result`)."""
        return observe(self._ai, self.match_result)

    # -- internal helpers ---------------------------------------------------

    def _require_live(self) -> None:
        if not self._ai.client.in_game:
            raise RuntimeError(
                "Cannot issue bot.* actions after the match has ended "
                f"(match_result={self.match_result!r}). Call bot.observe() to "
                "inspect the final state instead."
            )

    async def _advance(self, frames: int = _POLL_FRAMES) -> None:
        """Flush queued actions, advance the simulation by `frames` raw
        simulation frames, and refresh self._ai's state -- see the module
        docstring for why this is necessary for verification. Thin wrapper
        around python-sc2's own `BotAI._advance_steps`, which is `@final`
        and explicitly documented for this purpose."""
        await self._ai._advance_steps(frames)  # noqa: SLF001 -- see module docstring

    def _resolve_units(self, units: Unit | Units | int | list) -> tuple[list[Unit], list[int]]:
        """Normalize a caller-supplied unit selector (a Unit, a tag, or an
        iterable of either) into (found live Units, tags that don't
        currently resolve to any unit we control)."""
        if isinstance(units, Unit):
            requested = [units.tag]
        elif isinstance(units, int):
            requested = [units]
        else:
            requested = [u.tag if isinstance(u, Unit) else int(u) for u in units]
        found: list[Unit] = []
        missing: list[int] = []
        for tag in requested:
            unit = self._ai.all_own_units.find_by_tag(tag)
            if unit is None:
                missing.append(tag)
            else:
                found.append(unit)
        return found, missing

    # -- train ---------------------------------------------------------------

    async def train(
        self,
        unit_type: UnitTypeId,
        amount: int = 1,
        closest_to: Point2 | None = None,
        train_only_idle_buildings: bool = True,
        max_wait_steps: int = _DEFAULT_MAX_WAIT_STEPS,
    ) -> TrainOutcome:
        """Train `amount` of `unit_type` from any eligible, idle, completed
        production structure, and confirm production actually started.

        `train_only_idle_buildings` (passed straight through to python-sc2's
        `BotAI.train`) controls whether a structure that already has
        production queued is skipped (default) or queued behind -- pass
        False to add to an existing queue (e.g. a second unit at your only
        production structure while the first is still building).

        "Effect confirmed" here means the server-side pending-production
        count for `unit_type` increased (or, if it completes within the
        wait window, that the unit(s) themselves now exist) -- not merely
        that `python-sc2`'s local, optimistic bookkeeping thought the
        command would succeed.
        """
        ai = self._ai
        self._require_live()

        if ai.tech_requirement_progress(unit_type) < 1:
            return TrainOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Tech requirement not met for {unit_type.name}: the building(s) "
                    "required to train it aren't ready yet."
                ),
                detail="Command not dispatched: tech requirement check failed.",
                unit_type=unit_type.name,
                requested_amount=amount,
                dispatched_amount=0,
                new_unit_tags=(),
            )
        if not ai.can_afford(unit_type):
            cost = ai.calculate_cost(unit_type)
            return TrainOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Insufficient resources to train {unit_type.name}: need "
                    f"{cost.minerals} minerals / {cost.vespene} vespene, have "
                    f"{ai.minerals} / {ai.vespene}."
                ),
                detail="Command not dispatched: affordability check failed.",
                unit_type=unit_type.name,
                requested_amount=amount,
                dispatched_amount=0,
                new_unit_tags=(),
            )

        before_tags = {u.tag for u in ai.units(unit_type)}
        pending_before = ai.already_pending(unit_type)
        dispatched = ai.train(
            unit_type,
            amount=amount,
            closest_to=closest_to,
            train_only_idle_buildings=train_only_idle_buildings,
        )

        if dispatched == 0:
            idle_clause = "idle, " if train_only_idle_buildings else ""
            return TrainOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Could not train {unit_type.name}: no {idle_clause}completed structure "
                    f"capable of training it was available (checked {len(ai.structures)} "
                    "of our structures)."
                ),
                detail="Command not dispatched: no eligible production structure found.",
                unit_type=unit_type.name,
                requested_amount=amount,
                dispatched_amount=0,
                new_unit_tags=(),
            )

        new_tags: set[int] = set()
        confirmed = False
        for _ in range(max_wait_steps):
            await self._advance()
            pending_now = ai.already_pending(unit_type)
            new_tags = {u.tag for u in ai.units(unit_type)} - before_tags
            if pending_now >= pending_before + dispatched or len(new_tags) >= dispatched:
                confirmed = True
                break

        if confirmed:
            detail = (
                f"Dispatched {dispatched}/{amount} {unit_type.name}; confirmed via "
                f"{'newly observed unit(s)' if new_tags else 'increased pending-production count'}."
            )
        else:
            detail = (
                f"Dispatched {dispatched}/{amount} {unit_type.name}; not confirmed via "
                f"re-observation within {max_wait_steps} step(s)."
            )
        return TrainOutcome(
            ok=True,
            effect_confirmed=confirmed,
            error=None,
            detail=detail,
            unit_type=unit_type.name,
            requested_amount=amount,
            dispatched_amount=dispatched,
            new_unit_tags=tuple(sorted(new_tags)),
        )

    # -- build -----------------------------------------------------------------

    #: Building requires the assigned worker to walk to the placement
    #: point before construction can start, unlike train/research which
    #: dispatch from a structure that's already in place -- so build()'s
    #: default verification window is longer (see also: confirmation
    #: requires the structure entity to actually appear, not just
    #: already_pending() ticking up while the worker is still walking
    #: there -- see the loop below).
    _BUILD_DEFAULT_MAX_WAIT_STEPS = 40

    async def build(
        self,
        structure_type: UnitTypeId,
        near: Unit | Point2,
        max_distance: int = 20,
        build_worker: Unit | None = None,
        max_wait_steps: int = _BUILD_DEFAULT_MAX_WAIT_STEPS,
    ) -> BuildOutcome:
        """Build `structure_type` near `near`, and confirm construction
        actually started -- meaning a new structure entity of that type is
        now observable (not merely that already_pending() ticked up, which
        happens as soon as the assigned worker is *dispatched*, before it
        has even arrived at the site -- see the module docstring on why a
        real subsequent observation, not optimistic bookkeeping, is what
        "confirmed" means here).
        """
        ai = self._ai
        self._require_live()

        near_point = near.position if isinstance(near, Unit) else near
        position_report = (near_point.x, near_point.y) if isinstance(near_point, Point2) else None

        if not ai.can_afford(structure_type):
            cost = ai.calculate_cost(structure_type)
            return BuildOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Insufficient resources to build {structure_type.name}: need "
                    f"{cost.minerals} minerals / {cost.vespene} vespene, have "
                    f"{ai.minerals} / {ai.vespene}."
                ),
                detail="Command not dispatched: affordability check failed.",
                structure_type=structure_type.name,
                position=position_report,
                structure_tag=None,
            )

        before_tags = {u.tag for u in ai.structures(structure_type)}
        dispatched = await ai.build(
            structure_type, near=near, max_distance=max_distance, build_worker=build_worker
        )

        if not dispatched:
            return BuildOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Could not place {structure_type.name} near {position_report}: no "
                    f"valid buildable position was found within max_distance={max_distance}, "
                    "or no worker was available to build it."
                ),
                detail="Command not dispatched: placement or builder-availability check failed.",
                structure_type=structure_type.name,
                position=position_report,
                structure_tag=None,
            )

        # Confirmation requires the structure entity to actually appear --
        # NOT just already_pending(structure_type) ticking up, which
        # happens as soon as the worker is dispatched, well before it has
        # arrived and actually started construction (see docstring above).
        new_tag: int | None = None
        confirmed = False
        for _ in range(max_wait_steps):
            await self._advance()
            new_tags = {u.tag for u in ai.structures(structure_type)} - before_tags
            if new_tags:
                new_tag = next(iter(new_tags))
                confirmed = True
                break

        detail = (
            f"Dispatched build of {structure_type.name}; "
            + (
                f"confirmed (structure_tag={new_tag})."
                if confirmed
                else f"not confirmed via re-observation within {max_wait_steps} step(s)."
            )
        )
        return BuildOutcome(
            ok=True,
            effect_confirmed=confirmed,
            error=None,
            detail=detail,
            structure_type=structure_type.name,
            position=position_report,
            structure_tag=new_tag,
        )

    # -- research ----------------------------------------------------------------

    async def research(
        self,
        upgrade_type: UpgradeId,
        max_wait_steps: int = _DEFAULT_MAX_WAIT_STEPS,
    ) -> ResearchOutcome:
        """Research `upgrade_type` from any idle, completed structure that
        can research it, and confirm research actually started."""
        ai = self._ai
        self._require_live()

        if upgrade_type in ai.state.upgrades:
            return ResearchOutcome(
                ok=False,
                effect_confirmed=False,
                error=f"{upgrade_type.name} has already been researched.",
                detail="Command not dispatched: upgrade already completed.",
                upgrade_type=upgrade_type.name,
            )
        if not ai.can_afford(upgrade_type):
            cost = ai.calculate_cost(upgrade_type)
            return ResearchOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Insufficient resources to research {upgrade_type.name}: need "
                    f"{cost.minerals} minerals / {cost.vespene} vespene, have "
                    f"{ai.minerals} / {ai.vespene}."
                ),
                detail="Command not dispatched: affordability check failed.",
                upgrade_type=upgrade_type.name,
            )

        pending_before = ai.already_pending_upgrade(upgrade_type)
        try:
            dispatched = ai.research(upgrade_type)
        except AssertionError:
            return ResearchOutcome(
                ok=False,
                effect_confirmed=False,
                error=f"{upgrade_type.name} is not a known researchable upgrade.",
                detail="Command not dispatched: unrecognized upgrade type.",
                upgrade_type=upgrade_type.name,
            )

        if not dispatched:
            return ResearchOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Could not research {upgrade_type.name}: no idle, completed, "
                    "appropriately-tech'd structure was available to research it from "
                    "(or its prerequisite building/upgrade is missing)."
                ),
                detail="Command not dispatched: no eligible research structure found.",
                upgrade_type=upgrade_type.name,
            )

        confirmed = False
        for _ in range(max_wait_steps):
            await self._advance()
            if ai.already_pending_upgrade(upgrade_type) > pending_before or upgrade_type in ai.state.upgrades:
                confirmed = True
                break

        detail = (
            f"Dispatched research of {upgrade_type.name}; "
            + ("confirmed." if confirmed else f"not confirmed via re-observation within {max_wait_steps} step(s).")
        )
        return ResearchOutcome(
            ok=True,
            effect_confirmed=confirmed,
            error=None,
            detail=detail,
            upgrade_type=upgrade_type.name,
        )

    # -- move / attack-move --------------------------------------------------

    async def move(
        self,
        units: Unit | Units | int | list,
        target: Point2 | Unit,
        queue: bool = False,
        max_wait_steps: int = 3,
    ) -> MoveOutcome:
        """Move a unit or unit group to `target`, and confirm each unit
        actually picked up a move order."""
        return await self._move_or_attack("move", units, target, queue, max_wait_steps)

    async def attack_move(
        self,
        units: Unit | Units | int | list,
        target: Point2 | Unit,
        queue: bool = False,
        max_wait_steps: int = 3,
    ) -> MoveOutcome:
        """Attack-move a unit or unit group toward `target`, and confirm
        each unit actually picked up an attack order."""
        return await self._move_or_attack("attack_move", units, target, queue, max_wait_steps)

    async def _move_or_attack(
        self,
        mode: str,
        units: Unit | Units | int | list,
        target: Point2 | Unit,
        queue: bool,
        max_wait_steps: int,
    ) -> MoveOutcome:
        ai = self._ai
        self._require_live()

        found, missing = self._resolve_units(units)
        requested_tags = tuple(u.tag for u in found) + tuple(missing)

        if missing:
            return MoveOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Unknown unit tag(s): {missing}. These tags don't correspond to any "
                    "unit currently under our control."
                ),
                detail="Command not dispatched: one or more unit tags could not be resolved.",
                mode=mode,
                requested_tags=requested_tags,
                confirmed_tags=(),
            )
        if not found:
            return MoveOutcome(
                ok=False,
                effect_confirmed=False,
                error="No units given to move/attack-move.",
                detail="Command not dispatched: empty unit selection.",
                mode=mode,
                requested_tags=(),
                confirmed_tags=(),
            )

        for unit in found:
            command = unit.attack(target, queue=queue) if mode == "attack_move" else unit.move(target, queue=queue)
            ai.do(command, ignore_warning=True)

        confirmed_tags: set[int] = set()
        for _ in range(max_wait_steps):
            await self._advance()
            confirmed_tags = set()
            for tag in requested_tags:
                unit = ai.all_own_units.find_by_tag(tag)
                if unit is None:
                    continue  # died mid-verification; can't confirm, don't crash
                is_doing_it = unit.is_attacking if mode == "attack_move" else unit.is_moving
                if is_doing_it:
                    confirmed_tags.add(tag)
            if len(confirmed_tags) == len(found):
                break

        confirmed = len(confirmed_tags) == len(found)
        detail = (
            f"Dispatched {mode} for {len(found)} unit(s); confirmed for "
            f"{len(confirmed_tags)}/{len(found)} within {max_wait_steps} step(s)."
        )
        return MoveOutcome(
            ok=True,
            effect_confirmed=confirmed,
            error=None,
            detail=detail,
            mode=mode,
            requested_tags=requested_tags,
            confirmed_tags=tuple(sorted(confirmed_tags)),
        )

    # -- chat ------------------------------------------------------------------

    async def chat(self, message: str, team_only: bool = False) -> ChatOutcome:
        """Send a chat message.

        Known limitation, documented rather than papered over: python-sc2 /
        the SC2 game API give no way to read a chat message back to confirm
        the game client actually displayed it, so `effect_confirmed` here
        can only ever reflect "the chat_send call completed without the
        client rejecting it" -- not a true independent re-observation like
        every other action above. This is called out explicitly rather than
        silently claiming the same verification strength as train/build/
        research/move.
        """
        ai = self._ai
        self._require_live()
        if not isinstance(message, str) or not message:
            return ChatOutcome(
                ok=False,
                effect_confirmed=False,
                error="Chat message must be a non-empty string.",
                detail="Command not dispatched: invalid message.",
                message=message,
            )
        try:
            await ai.chat_send(message, team_only=team_only)
        except Exception as exc:  # noqa: BLE001 -- report any client-side rejection as a clear error
            return ChatOutcome(
                ok=False,
                effect_confirmed=False,
                error=f"chat_send failed: {exc}",
                detail="Command not dispatched: the client rejected the chat message.",
                message=message,
            )
        return ChatOutcome(
            ok=True,
            effect_confirmed=True,
            error=None,
            detail=(
                "chat_send completed without error. Note: this is not an independent "
                "re-observation (the SC2 API doesn't expose received chat back to the "
                "sender) -- see this method's docstring."
            ),
            message=message,
        )


class VerifiedBotAI(BotAI):
    """Base class for a live-game `BotAI` that wires up `self.bot` (the
    verified layer above) and `self.sdk` (raw passthrough), and records the
    final match result.

    This is the "concrete shape" for driving `bot.*`/`sdk.*` against a real
    game (see the module docstring and `runtime.py`): subclass this,
    override `on_step` to call `self.bot.*`/`self.sdk.*` and record
    whatever you want to assert on later onto `self` (a list, a dict, plain
    attributes -- whatever's convenient), and hand an *instance* of your
    subclass to `runtime.run_bot_vs_builtin_ai`. Because `run_game()`
    doesn't discard the bot instance, the same live Python object is still
    there to inspect afterward -- the test function then asserts on what
    got recorded, exactly per the pattern used by python-sc2's own example/
    test bots.

    #4-#8 (Protoss/Zerg macro, MCP execute_code, the autonomous script
    runtime) are all expected to subclass this rather than re-deriving the
    `self.bot`/`self.sdk` wiring.
    """

    async def on_start(self) -> None:
        self.bot = Bot(self)
        self.sdk = self

    async def on_end(self, game_result: Result) -> None:
        # self.bot only exists once on_start has run; guard for the (rare)
        # case where the game ends before that (e.g. an immediate forfeit).
        if hasattr(self, "bot"):
            self.bot.match_result = game_result
