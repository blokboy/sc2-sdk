"""Ticket #5: exercise the verified `bot.*`/`sdk.*` API against a real,
local, headless SC2 game (Zerg) -- no mocking python-sc2, per the spec's
one testing seam.

This is a sibling to `test_verified_bot_actions.py` (#3, Terran) and
`test_verified_bot_actions_protoss.py` (#4, Protoss), not a modification of
either: `Bot`/`VerifiedBotAI` (see `sdk.bot`) were built race-agnostic on
purpose specifically so #4/#5 wouldn't need to touch them -- see that
module's docstring. Every `bot.*` call below is the exact same code
Terran's and Protoss's tests exercise; only the concrete unit/structure/
upgrade choices and the Zerg-specific production model they depend on
(Larva, Drone-consuming structure morphs) are new.

Design question this ticket had to resolve, per its brief: is `Bot.train()`
/`Bot.build()` as already written in the generic layer (`sdk/bot.py`)
correct for Zerg's structurally different production model -- units morph
from Larva rather than being trained directly by a structure, and most
structures are themselves morphed from a Drone (which is consumed) rather
than built by a worker that survives? Answer, reached by actually reading
python-sc2's `bot_ai.py` (not assumed) and then confirmed empirically
against a real game below: both are already correct, and neither needed a
change to `bot.py`.

  - train(): python-sc2's own `BotAI.train()` already handles the larva
    case transparently --

        train_structures = self.structures if self.race != Race.Zerg else self.structures | self.larva

    -- i.e. Larva (which python-sc2 keeps in both `self.units` and its own
    `self.larva`, see `bot_ai_internal.py`'s state-update loop) are already
    unioned into the set of eligible idle "producers" `train()` searches,
    exactly analogous to what #4 found for Protoss warp-in (`train()`
    branching on `structure.type_id == UnitTypeId.WARPGATE`). `Bot.train()`
    (`sdk/bot.py`) calls straight through to `ai.train()` unmodified --
    nothing Zerg-specific needed to be added there. Verification
    (`already_pending()`, matching new unit tags) is unaffected too:
    `already_pending()` counts matching-ability orders across all own
    units, which already includes a Larva's in-progress morph order, with
    no larva-specific code required.

  - build(): a Zerg structure build genuinely *is* a morph -- the assigned
    Drone is consumed, and its own unit tag becomes the structure's tag
    (see python-sc2's `on_unit_type_changed` docstring, which explicitly
    lists "a hatchery morphed to lair" as one example of this general
    same-tag type-change mechanism; a Drone morphing into a structure it's
    building works the same way -- this is *not* the same as Terran's SCV,
    which remains a separate, alive unit throughout construction). But
    `Bot.build()`'s verification never inspects worker/drone bookkeeping at
    all -- it diffs `ai.structures(structure_type)` before vs. after (see
    `bot.py`'s `build()`: "confirmation requires the structure entity to
    actually appear"). The tag in question was never a member of
    `ai.structures(structure_type)` before the morph (while it was a Drone,
    the type-filter excludes it), so it shows up as a genuinely new element
    of that diff the instant the game reclassifies the same tag as a
    structure -- tag reuse or not. The existing diff-based confirmation is
    therefore already correct for Zerg with no change needed. See
    `build_spawning_pool`'s assertions below, which confirm this actually
    happens against a real game rather than just reasoning about it.

Zerg-specific production-model wrinkle worth calling out explicitly (the
kind of thing #3/#4 documented inline rather than papering over, per this
ticket's brief): Zerg starts a match with 3 Larva, unlike Terran/Protoss's
single townhall as their *only* production structure. This means, unlike
#3/#4's `train_only_idle_buildings=False` workaround (needed there because
their one townhall was still busy with the pre-cheat first unit's order), a
second `bot.train()` call here can just use the *default*
`train_only_idle_buildings=True` -- a different, still-idle Larva is
available without needing to queue behind the one already mid-morph. See
`train_drone_confirmed` below.

The scripted sequence exercises every acceptance-criterion action once,
against real Zerg mechanics rather than staged ones:
  - observe() before/after
  - train (== "select/morph larva into a unit"): a successful case, an
    insufficient-resources failure case, and a tech-requirement-not-met
    failure case (Zergling before a Spawning Pool exists -- a failure mode
    #3/#4 didn't exercise, since Terran's/Protoss's equivalent tier-1 units
    have no such prerequisite)
  - build (== "build/morph a structure"): an illegal-placement failure
    case, and a successful case (Spawning Pool)
  - research: a missing-prerequisite-structure failure case (Zergling
    Movement Speed before the Spawning Pool exists), and a successful case
    (once the Spawning Pool is up)
  - move / attack_move (successful cases, plus an unknown-tag failure case)
  - chat
  - match outcome, read back via bot.observe() after the game ends

`debug_all_resources`/`debug_fast_build` (via `sdk.*` raw passthrough) are
used for the same reason #3/#4 used them: fast, deterministic verification
of the successful-path cases rather than racing real production/build/
research timers. The insufficient-resources and unknown-unit-tag failure
cases are exercised *before* those cheats are granted, so they reflect
genuine constraints; the tech-requirement-not-met case doesn't depend on
resources at all (`Bot.train()` checks tech requirement before
affordability -- see `bot.py`), so it's exercised there too. The illegal-
placement and missing-prerequisite-structure cases run *after* the cheats
(same reasoning as #3/#4): every Zerg structure costs more than the 50
starting minerals, so testing either beforehand would be rejected for
insufficient resources before the constraint under test is ever checked.
"""

from __future__ import annotations

import pytest
from sc2.data import Difficulty, Race, Result
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from sdk.bot import VerifiedBotAI

_SAFETY_TIME_LIMIT = 150  # in-game seconds; see module docstring's timing note.


class _ZergAcceptanceBot(VerifiedBotAI):
    """Runs ticket #5's full scripted action sequence once, then idles."""

    async def on_start(self) -> None:
        await super().on_start()
        self.outcomes: dict[str, object] = {}
        self._script_started = False

    async def on_step(self, iteration: int) -> None:
        if self._script_started:
            return
        self._script_started = True
        await self._run_script()

    async def _wait_until(self, condition, max_polls: int, frames: int = 4) -> bool:
        """Advance the simulation in `frames`-sized steps, re-checking
        `condition()` after each, until it's true or `max_polls` is
        exhausted. Used for setup waits that aren't themselves one of the
        five verified action categories (e.g. "has this structure finished
        building" before starting the next scripted step) -- as opposed to
        `Bot.*`'s own internal verification polling, which this helper
        deliberately does not replace. Same helper #4 introduced."""
        for _ in range(max_polls):
            await self._advance_steps(frames)
            if condition():
                return True
        return False

    async def _run_script(self) -> None:
        bot = self.bot
        ai = self

        self.outcomes["observe_initial"] = bot.observe()

        # -- invalid actions, exercised against real (not staged) constraints --

        # Insufficient resources: our starting 50 minerals afford exactly
        # one Drone (morphed from a Larva); immediately trying a second
        # must fail cleanly. Exercised before any cheats are granted so the
        # scarcity is genuine.
        self.outcomes["train_drone_first"] = await bot.train(UnitTypeId.DRONE)
        self.outcomes["train_drone_insufficient_resources"] = await bot.train(UnitTypeId.DRONE)

        # Tech requirement not met: Zergling requires a Spawning Pool,
        # which we haven't built yet. Bot.train() checks tech requirement
        # before affordability (see bot.py), so this genuinely isolates
        # the tech-requirement constraint regardless of our (currently
        # zero) minerals -- a failure mode #3/#4 didn't need to exercise,
        # since SCV/Probe have no such prerequisite.
        self.outcomes["train_zergling_tech_requirement"] = await bot.train(UnitTypeId.ZERGLING)

        # Unknown unit tag: no unit under our control has ever had this tag.
        # No resource dependency, so it doesn't matter that we're broke here.
        self.outcomes["move_unknown_tag"] = await bot.move(units=[999_999_999], target=ai.game_info.map_center)

        # -- cheats for fast, deterministic verification of the remaining cases --
        # (Every Zerg structure costs more than our 50 starting minerals, so
        # the illegal-placement and missing-prerequisite cases below need these
        # granted first -- otherwise they'd be blocked by affordability before
        # the constraint under test is ever checked, per the module docstring.)
        await ai.client.debug_all_resources()
        await ai.client.debug_fast_build()
        await ai._advance_steps(4)  # let the cheats land before checking affordability

        # Illegal placement: max_distance=0 forces an exact-position check
        # against our own townhall's footprint, which can never be placeable.
        # Resources are plentiful now, so this genuinely isolates placement.
        townhall_pos = ai.townhalls.first.position
        self.outcomes["build_illegal_placement"] = await bot.build(
            UnitTypeId.SPAWNINGPOOL, near=townhall_pos, max_distance=0
        )

        # Missing prerequisite structure: Zergling Movement Speed research
        # requires a Spawning Pool, which we haven't built yet. Resources
        # are plentiful, so this genuinely isolates the missing-structure
        # constraint rather than affordability.
        self.outcomes["research_zerglingspeed_missing_prereq"] = await bot.research(UpgradeId.ZERGLINGMOVEMENTSPEED)

        # -- successful train --
        # Unlike #3/#4 (a single townhall as the only production
        # structure), we start with 3 Larva; the pre-cheat Drone morph
        # above only consumed one of them, so a second, still-idle Larva
        # is available and the *default* train_only_idle_buildings=True
        # is enough here -- see the module docstring's production-model
        # note. (Contrast with #3/#4's train_only_idle_buildings=False.)
        self.outcomes["train_drone_confirmed"] = await bot.train(UnitTypeId.DRONE)

        # -- successful build (Spawning Pool, a Drone-morph -- see module
        # docstring's design-question discussion) --
        spawningpool_point = townhall_pos.towards(ai.game_info.map_center, 6)
        self.outcomes["build_spawning_pool"] = await bot.build(UnitTypeId.SPAWNINGPOOL, near=spawningpool_point)

        # build() only confirms construction *started*; research below
        # needs the Spawning Pool actually complete.
        spawningpool_ready = await self._wait_until(
            lambda: bool(ai.structures(UnitTypeId.SPAWNINGPOOL).ready), max_polls=30
        )
        self.outcomes["spawningpool_ready_before_research"] = spawningpool_ready

        # -- successful research (Zergling Movement Speed, from the now-completed Spawning Pool) --
        self.outcomes["research_zerglingspeed"] = await bot.research(UpgradeId.ZERGLINGMOVEMENTSPEED)

        # -- move / attack-move --
        # Like Protoss's Probe (and unlike Terran's SCV), python-sc2's
        # `is_constructing_scv` check is Terran-specific, so it can't be
        # used to filter out a currently-building worker here. Not relied
        # on: the only build in this script has already completed by this
        # point, so all our Drones are free.
        movable = list(ai.workers)
        move_target = ai.game_info.map_center
        self.outcomes["move_worker"] = await bot.move(units=movable[0], target=move_target)

        attack_target = ai.enemy_start_locations[0] if ai.enemy_start_locations else ai.game_info.map_center
        self.outcomes["attack_move_worker"] = await bot.attack_move(units=[movable[1]], target=attack_target)

        # -- chat --
        self.outcomes["chat"] = await bot.chat("sc2-sdk ticket #5 verified-action integration test")

        self.outcomes["observe_after_script"] = bot.observe()


@pytest.mark.integration
def test_verified_bot_actions_against_real_game(sc2_verified_bot_harness):
    bot_ai = _ZergAcceptanceBot()
    result = sc2_verified_bot_harness(
        bot_ai,
        my_race=Race.Zerg,
        opponent_race=Race.Terran,
        difficulty=Difficulty.Easy,
        realtime=False,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )

    # The match itself resolved to a real, definite outcome -- this is the
    # sc2_game_harness-level guarantee this test also depends on.
    assert isinstance(result, Result)
    assert result in (Result.Victory, Result.Defeat, Result.Tie)

    outcomes = bot_ai.outcomes

    # -- observe(): own units/structures/resources/supply, at least. --
    initial = outcomes["observe_initial"]
    assert initial.minerals == 50
    assert any(u.type_name == "DRONE" for u in initial.units)
    assert any(u.type_name == "LARVA" for u in initial.units)
    assert any(s.type_name == "HATCHERY" for s in initial.structures)
    assert initial.supply_used > 0
    assert initial.match_result is None  # game was still in progress

    # -- train (== select/morph larva into a unit): successful case --
    train_ok = outcomes["train_drone_first"]
    assert train_ok.ok is True
    assert train_ok.dispatched_amount == 1

    # -- train: insufficient resources is a clear, actionable error --
    train_bad = outcomes["train_drone_insufficient_resources"]
    assert train_bad.ok is False
    assert train_bad.error is not None
    assert "insufficient" in train_bad.error.lower() or "resource" in train_bad.error.lower()

    # -- train: tech requirement not met (no Spawning Pool yet) is a clear, actionable error --
    train_tech_bad = outcomes["train_zergling_tech_requirement"]
    assert train_tech_bad.ok is False
    assert train_tech_bad.error is not None
    assert "tech requirement" in train_tech_bad.error.lower()

    # -- move: unknown unit tag is a clear, actionable error --
    move_bad = outcomes["move_unknown_tag"]
    assert move_bad.ok is False
    assert move_bad.error is not None
    assert "999999999" in move_bad.error or "999_999_999" in move_bad.error or "unknown" in move_bad.error.lower()

    # -- build: illegal placement is a clear, actionable error --
    build_bad = outcomes["build_illegal_placement"]
    assert build_bad.ok is False
    assert build_bad.error is not None
    assert "placement" in build_bad.error.lower() or "valid" in build_bad.error.lower()

    # -- research: missing prerequisite structure is a clear, actionable error --
    research_bad = outcomes["research_zerglingspeed_missing_prereq"]
    assert research_bad.ok is False
    assert research_bad.error is not None

    # -- train: successful, cheat-funded case, effect confirmed for real,
    # using the *default* train_only_idle_buildings (see module docstring) --
    train_confirmed = outcomes["train_drone_confirmed"]
    assert train_confirmed.ok is True
    assert train_confirmed.effect_confirmed is True

    # -- build (== build/morph a structure): successful case, effect
    # confirmed for real -- this is the empirical confirmation that
    # Bot.build()'s diff-based verification correctly handles Zerg's
    # Drone-consuming structure morph with no change needed (see module
    # docstring's design-question discussion). --
    spawningpool = outcomes["build_spawning_pool"]
    assert spawningpool.ok is True, spawningpool.error
    assert spawningpool.effect_confirmed is True
    assert spawningpool.structure_tag is not None
    assert outcomes["spawningpool_ready_before_research"] is True

    # -- research: successful case, effect confirmed for real --
    research = outcomes["research_zerglingspeed"]
    assert research.ok is True, research.error
    assert research.effect_confirmed is True

    # -- move / attack-move: dispatched and confirmed --
    move = outcomes["move_worker"]
    assert move.ok is True
    assert move.mode == "move"

    attack = outcomes["attack_move_worker"]
    assert attack.ok is True
    assert attack.mode == "attack_move"

    # -- chat: dispatched without error (see Bot.chat's documented
    # verification-strength limitation) --
    chat = outcomes["chat"]
    assert chat.ok is True

    # -- final observation reflects the built structures and trained units --
    final = outcomes["observe_after_script"]
    assert any(s.type_name == "SPAWNINGPOOL" for s in final.structures)
    assert any(u.type_name == "DRONE" for u in final.units)

    # -- match outcome is reported via bot.observe() once the game has ended --
    post_game_observation = bot_ai.bot.observe()
    assert post_game_observation.match_result == result
    assert bot_ai.bot.match_result == result
