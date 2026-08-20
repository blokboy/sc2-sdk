"""Ticket #4: exercise the verified `bot.*`/`sdk.*` API against a real,
local, headless SC2 game (Protoss) -- no mocking python-sc2, per the spec's
one testing seam.

This is a sibling to `test_verified_bot_actions.py` (#3, Terran), not a
modification of it: `Bot`/`VerifiedBotAI` (see `sdk.bot`) were built
race-agnostic on purpose specifically so #4/#5 wouldn't need to touch them
-- see that module's docstring. Every `bot.*` call below is the exact same
code Terran's test exercises; only the concrete unit/structure/upgrade
choices and the game mechanics they depend on (Pylon power fields, Warp
Gate) are Protoss-specific.

Live-game test-driving pattern is identical to #3's: a `VerifiedBotAI`
subclass runs a scripted sequence of `bot.*` calls once, on its first
`on_step`, and records every `Outcome` onto `self.outcomes` for the test
function to assert on afterward (see `sdk.bot.VerifiedBotAI`'s docstring).

The scripted sequence exercises every acceptance-criterion action once,
against real Protoss mechanics rather than staged ones:
  - observe() before/after
  - train (a successful case, and an insufficient-resources failure case)
  - research (a missing-prerequisite-structure failure case: no
    Cybernetics Core yet)
  - build (a successful case requiring a Pylon power field, and an
    illegal-placement failure case)
  - research (a successful case: Warp Gate research from a Cybernetics
    Core)
  - train/warp-in: a normal Gateway train, and -- after researching Warp
    Gate and morphing the Gateway into a Warp Gate -- a warp-in train.
    `Bot.train()` itself needs no Protoss-specific code for this: it calls
    straight through to python-sc2's own `BotAI.train()`, which already
    branches on `structure.type_id == UnitTypeId.WARPGATE` and calls
    `structure.warp_in(...)` instead of `structure.train(...)` -- see
    `bot_ai.py`'s `train()`. This is exactly what "race-agnostic by
    design" (sdk.bot's module docstring) means in practice.
  - move / attack_move (successful cases, plus an unknown-tag failure case)
  - chat
  - match outcome, read back via bot.observe() after the game ends

Morphing the Gateway into a Warp Gate (`AbilityId.MORPH_WARPGATE`) is not
itself one of the five verified action categories (train/build/research/
move/chat) `bot.*` covers -- it's a structure self-cast ability, closer in
kind to the `debug_*` cheats below. It's dispatched via `sdk.*` raw
passthrough as scripted setup, exactly like those cheats, so that the
subsequent `bot.train(ZEALOT)` call has a real Warp Gate available to
exercise python-sc2's warp-in branch.

`debug_all_resources`/`debug_fast_build` (via `sdk.*` raw passthrough) are
used for the same reason #3 used them: fast, deterministic verification of
the successful-path cases rather than racing real production/build/research
timers. The insufficient-resources and unknown-unit-tag failure cases are
exercised *before* those cheats are granted, so they reflect genuine
constraints. The illegal-placement and missing-prerequisite-structure
failure cases run *after* the cheats (same reasoning as #3's illegal-
placement case): every Protoss structure costs more than the 50 starting
minerals, so testing either beforehand would be rejected for insufficient
resources before the constraint under test is ever checked.
"""

from __future__ import annotations

import pytest
from sc2.data import Difficulty, Race, Result
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from sdk.bot import VerifiedBotAI

#: in-game seconds. Larger than #3's Terran equivalent (120) because the
#: warp-in retry loop (see "train/warp-in, part 2" below) can burn a real,
#: non-trivial number of simulation frames across up to 3 attempts.
_SAFETY_TIME_LIMIT = 240


class _ProtossAcceptanceBot(VerifiedBotAI):
    """Runs ticket #4's full scripted action sequence once, then idles."""

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
        deliberately does not replace."""
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
        # one Probe; immediately trying a second must fail cleanly.
        # Exercised before any cheats are granted so the scarcity is genuine.
        self.outcomes["train_probe_first"] = await bot.train(UnitTypeId.PROBE)
        self.outcomes["train_probe_insufficient_resources"] = await bot.train(UnitTypeId.PROBE)

        # Unknown unit tag: no unit under our control has ever had this tag.
        # No resource dependency, so it doesn't matter that we're broke here.
        self.outcomes["move_unknown_tag"] = await bot.move(units=[999_999_999], target=ai.game_info.map_center)

        # -- cheats for fast, deterministic verification of the remaining cases --
        # (Every Protoss structure costs more than our 50 starting minerals, so
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
            UnitTypeId.PYLON, near=townhall_pos, max_distance=0
        )

        # Missing prerequisite structure: Warp Gate research requires a
        # Cybernetics Core, which we haven't built yet. Resources are
        # plentiful, so this genuinely isolates the missing-structure
        # constraint rather than affordability.
        self.outcomes["research_warpgate_missing_prereq"] = await bot.research(UpgradeId.WARPGATERESEARCH)

        # -- successful train --
        # We only have one Nexus, and it's still legitimately busy with the
        # pre-cheat Probe order above (debug_fast_build doesn't retroactively
        # speed up an order already issued before it was granted) --
        # train_only_idle_buildings=False queues behind it, exactly like
        # requesting a second unit at your only production structure while
        # the first is still building.
        self.outcomes["train_probe_confirmed"] = await bot.train(UnitTypeId.PROBE, train_only_idle_buildings=False)

        # -- successful build (Pylon near home, to power everything below) --
        pylon_point = ai.townhalls.first.position.towards(ai.game_info.map_center, 6)
        self.outcomes["build_pylon"] = await bot.build(UnitTypeId.PYLON, near=pylon_point)

        # build() only confirms construction *started*; the rest of this
        # script needs the Pylon actually up and providing a power field
        # before it can place powered structures near it.
        pylon_ready = await self._wait_until(
            lambda: bool(ai.structures(UnitTypeId.PYLON).ready), max_polls=15
        )
        self.outcomes["pylon_ready_before_next_build"] = pylon_ready

        # -- second build: Gateway, within the Pylon's power field --
        # Offset magnitude deliberately > 4 (see the warp-in comment below):
        # python-sc2's own warp-in target picker samples a point at exactly
        # distance 4 from a random owned Pylon, in a random direction, with
        # no placement-legality check of its own. Keeping every other
        # structure's footprint outside that radius-4 circle (footprint
        # half-width ~1.5, so placed at offset magnitude ~5.4-5.8 keeps the
        # near edge at ~4-4.3, clear of it) keeps that circle fully open so
        # the picker can't land on top of them.
        gateway_point = pylon_point.offset((5, 3))
        self.outcomes["build_gateway"] = await bot.build(UnitTypeId.GATEWAY, near=gateway_point)
        gateway_ready = await self._wait_until(
            lambda: bool(ai.structures(UnitTypeId.GATEWAY).ready), max_polls=30
        )
        self.outcomes["gateway_ready_before_next_build"] = gateway_ready

        # -- third build: Cybernetics Core, prerequisite for Warp Gate research --
        cybercore_point = pylon_point.offset((-5, 3))
        self.outcomes["build_cybernetics_core"] = await bot.build(UnitTypeId.CYBERNETICSCORE, near=cybercore_point)
        cybercore_ready = await self._wait_until(
            lambda: bool(ai.structures(UnitTypeId.CYBERNETICSCORE).ready), max_polls=30
        )
        self.outcomes["cybercore_ready_before_research"] = cybercore_ready

        # -- train/warp-in, part 1: a normal Gateway train --
        # This is the same Gateway we'll morph into a Warp Gate below; we
        # train from it *before* morphing so this exercises the ordinary
        # `structure.train()` path (structure.type_id == GATEWAY) rather
        # than the warp-in one, isolating the two.
        self.outcomes["train_zealot_gateway"] = await bot.train(UnitTypeId.ZEALOT)

        # Let the Gateway finish (and go idle again) before morphing it --
        # a busy production structure can still be morphed in principle, but
        # we want a clean "idle Warp Gate" for the warp-in case below.
        gateway_idle_again = await self._wait_until(
            lambda: bool(ai.structures(UnitTypeId.GATEWAY).ready) and not ai.structures(UnitTypeId.GATEWAY).ready.first.orders,
            max_polls=30,
        )
        self.outcomes["gateway_idle_before_research"] = gateway_idle_again

        # -- successful research (Warp Gate, from the now-completed Cybernetics Core) --
        self.outcomes["research_warpgate"] = await bot.research(UpgradeId.WARPGATERESEARCH)

        # research() only confirms research *started*; morphing to Warp Gate
        # requires the upgrade to have actually *completed*.
        warpgate_researched = await self._wait_until(
            lambda: UpgradeId.WARPGATERESEARCH in ai.state.upgrades, max_polls=60
        )
        self.outcomes["warpgate_research_completed"] = warpgate_researched

        # -- morph Gateway -> Warp Gate --
        # Not one of bot.*'s five verified action categories (see module
        # docstring) -- dispatched via sdk.* raw passthrough as scripted
        # setup, same as the debug_* cheats above.
        gateway_unit = ai.structures(UnitTypeId.GATEWAY).ready.first
        ai.do(gateway_unit(AbilityId.MORPH_WARPGATE), ignore_warning=True)
        warpgate_morphed = await self._wait_until(
            lambda: bool(ai.structures(UnitTypeId.WARPGATE).ready), max_polls=30
        )
        self.outcomes["warpgate_morph_confirmed"] = warpgate_morphed

        # -- train/warp-in, part 2: warp-in via the now-morphed Warp Gate --
        # Only a Warp Gate remains capable of training a Zealot at this
        # point (the Gateway that used to be is now that Warp Gate), so
        # python-sc2's own BotAI.train() takes its
        # `structure.type_id == UnitTypeId.WARPGATE` branch and calls
        # `structure.warp_in(...)` -- see this file's module docstring.
        #
        # Two real Protoss-specific wrinkles surfaced empirically here,
        # neither of which is a bug in sdk.bot.Bot.train() (it delegates
        # straight through to python-sc2's own BotAI.train(), unchanged
        # -- see the module docstring's "race-agnostic by design" note):
        #
        #   1. Unlike a normal train/build, debug_fast_build does NOT
        #      reduce warp-in's cast+channel time to (near) zero -- it
        #      still takes a real, non-trivial number of simulation frames
        #      even with the cheat active, hence the larger poll budget
        #      below than any other verified action in this suite.
        #   2. python-sc2's BotAI.train() picks the warp-in location itself
        #      (`pylons.random.position.random_on_distance(4)`, hardcoded,
        #      not something bot.train() can influence) with no placement-
        #      legality check of its own -- this is a documented upstream
        #      limitation (BotAI.train()'s own docstring: "Warning:
        #      currently has issues with warp gate warp ins"). Even with
        #      every other structure kept outside that radius-4 circle
        #      (see the Gateway/Cybernetics Core placement comment above),
        #      our own Nexus sits close enough that part of the circle can
        #      still clip its footprint, occasionally producing a picked
        #      point the server silently rejects -- dispatched_amount=1
        #      (python-sc2 got a truthy UnitCommand back) but no unit ever
        #      appears, so effect_confirmed stays False no matter how long
        #      we wait. Retrying re-rolls a fresh random location each
        #      time, so a small retry loop resolves it -- exactly the
        #      documented meaning of effect_confirmed=False in outcomes.py
        #      ("not necessarily a bug ... reported honestly" -- i.e. a
        #      legitimate case for the caller to just try again).
        for attempt in range(1, 4):
            outcome = await bot.train(UnitTypeId.ZEALOT, max_wait_steps=80)
            self.outcomes["train_zealot_warpgate"] = outcome
            self.outcomes["train_zealot_warpgate_attempts"] = attempt
            if outcome.effect_confirmed:
                break

        # -- move / attack-move --
        # Unlike Terran's SCV, python-sc2's `is_constructing_scv` check is
        # Terran-specific (it only matches TERRANBUILD_* abilities -- see
        # `IS_CONSTRUCTING_SCV` in python-sc2's constants.py), so it's
        # always False for Probes and can't be used to filter out a
        # currently-building worker here. Not relied on: every build above
        # has already completed by this point in the script, so all our
        # Probes are free.
        movable = list(ai.workers)
        move_target = ai.game_info.map_center
        self.outcomes["move_worker"] = await bot.move(units=movable[0], target=move_target)

        attack_target = ai.enemy_start_locations[0] if ai.enemy_start_locations else ai.game_info.map_center
        self.outcomes["attack_move_worker"] = await bot.attack_move(units=[movable[1]], target=attack_target)

        # -- chat --
        self.outcomes["chat"] = await bot.chat("sc2-sdk ticket #4 verified-action integration test")

        self.outcomes["observe_after_script"] = bot.observe()


@pytest.mark.integration
def test_verified_bot_actions_against_real_game(sc2_verified_bot_harness):
    bot_ai = _ProtossAcceptanceBot()
    result = sc2_verified_bot_harness(
        bot_ai,
        my_race=Race.Protoss,
        opponent_race=Race.Zerg,
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
    assert any(u.type_name == "PROBE" for u in initial.units)
    assert any(s.type_name == "NEXUS" for s in initial.structures)
    assert initial.supply_used > 0
    assert initial.match_result is None  # game was still in progress

    # -- train: successful case --
    train_ok = outcomes["train_probe_first"]
    assert train_ok.ok is True
    assert train_ok.dispatched_amount == 1

    # -- train: insufficient resources is a clear, actionable error --
    train_bad = outcomes["train_probe_insufficient_resources"]
    assert train_bad.ok is False
    assert train_bad.error is not None
    assert "insufficient" in train_bad.error.lower() or "resource" in train_bad.error.lower()

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
    research_bad = outcomes["research_warpgate_missing_prereq"]
    assert research_bad.ok is False
    assert research_bad.error is not None

    # -- train: successful, cheat-funded case, effect confirmed for real --
    train_confirmed = outcomes["train_probe_confirmed"]
    assert train_confirmed.ok is True
    assert train_confirmed.effect_confirmed is True

    # -- build: successful case, effect confirmed for real --
    pylon = outcomes["build_pylon"]
    assert pylon.ok is True
    assert pylon.effect_confirmed is True
    assert pylon.structure_tag is not None
    assert outcomes["pylon_ready_before_next_build"] is True

    gateway = outcomes["build_gateway"]
    assert gateway.ok is True
    assert gateway.effect_confirmed is True
    assert outcomes["gateway_ready_before_next_build"] is True

    cybercore = outcomes["build_cybernetics_core"]
    assert cybercore.ok is True
    assert cybercore.effect_confirmed is True
    assert outcomes["cybercore_ready_before_research"] is True

    # -- train/warp-in, part 1: normal Gateway train, effect confirmed for real --
    zealot_gateway = outcomes["train_zealot_gateway"]
    assert zealot_gateway.ok is True, zealot_gateway.error
    assert zealot_gateway.effect_confirmed is True

    # -- research: successful case, effect confirmed for real --
    research = outcomes["research_warpgate"]
    assert research.ok is True, research.error
    assert research.effect_confirmed is True
    assert outcomes["warpgate_research_completed"] is True

    # -- Warp Gate morph (setup, not a verified bot.* action) actually landed --
    assert outcomes["warpgate_morph_confirmed"] is True

    # -- train/warp-in, part 2: warp-in via Warp Gate, effect confirmed for real --
    zealot_warpgate = outcomes["train_zealot_warpgate"]
    assert zealot_warpgate.ok is True, zealot_warpgate.error
    assert zealot_warpgate.effect_confirmed is True

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
    assert any(s.type_name == "PYLON" for s in final.structures)
    assert any(s.type_name == "GATEWAY" for s in final.structures) or any(
        s.type_name == "WARPGATE" for s in final.structures
    )
    assert any(s.type_name == "CYBERNETICSCORE" for s in final.structures)
    assert any(u.type_name == "ZEALOT" for u in final.units)

    # -- match outcome is reported via bot.observe() once the game has ended --
    post_game_observation = bot_ai.bot.observe()
    assert post_game_observation.match_result == result
    assert bot_ai.bot.match_result == result
