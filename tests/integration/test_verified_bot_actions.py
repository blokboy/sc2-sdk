"""Ticket #3: exercise the verified `bot.*`/`sdk.*` API against a real,
local, headless SC2 game (Terran) -- no mocking python-sc2, per the spec's
one testing seam.

Live-game test-driving pattern (see `sdk.bot.VerifiedBotAI`'s docstring for
the full rationale): a `VerifiedBotAI` subclass below runs a scripted
sequence of `bot.*` calls once, on its very first `on_step`, and records
every `Outcome` it gets back onto `self.outcomes`. Because `python-sc2`'s
`run_game()` doesn't discard the bot instance it was given, the *same live
object* is still there after `sc2_verified_bot_harness` returns -- these
test functions assert on `bot_ai.outcomes[...]` directly, exactly the
pattern described in the ticket brief ("the test function then asserts on
what got recorded").

The scripted sequence exercises every acceptance-criterion action once:
  - observe() before/after
  - train (a successful case, and an insufficient-resources failure case)
  - build (a successful case, and an illegal-placement failure case)
  - research (a successful case)
  - move / attack_move (successful cases, plus an unknown-tag failure case)
  - chat
  - match outcome, read back via bot.observe() after the game ends

`debug_all_resources`/`debug_fast_build` (via `sdk.*` raw passthrough) are
used to make the successful-path cases fast and deterministic rather than
racing real production/build timers. The insufficient-resources and
unknown-unit-tag failure cases are exercised *before* those cheats are
granted, so they reflect genuine constraints, not staged ones. The
illegal-placement case runs *after* the cheats: every Terran structure costs
more than the 50 starting minerals, so testing it beforehand would (validly,
but unhelpfully for this test) be rejected for insufficient resources before
placement is ever checked -- see `_run_script`'s comment at that call site.
"""

from __future__ import annotations

import pytest
from sc2.data import Difficulty, Race, Result
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId

from sdk.bot import VerifiedBotAI

_SAFETY_TIME_LIMIT = 120  # in-game seconds; see module docstring's timing note.


class _TerranAcceptanceBot(VerifiedBotAI):
    """Runs ticket #3's full scripted action sequence once, then idles."""

    async def on_start(self) -> None:
        await super().on_start()
        self.outcomes: dict[str, object] = {}
        self._script_started = False

    async def on_step(self, iteration: int) -> None:
        if self._script_started:
            return
        self._script_started = True
        await self._run_script()

    async def _run_script(self) -> None:
        bot = self.bot
        ai = self

        self.outcomes["observe_initial"] = bot.observe()

        # -- invalid actions, exercised against real (not staged) constraints --

        # Insufficient resources: our starting 50 minerals afford exactly
        # one SCV; immediately trying a second must fail cleanly. Exercised
        # before any cheats are granted so the scarcity is genuine.
        self.outcomes["train_scv_first"] = await bot.train(UnitTypeId.SCV)
        self.outcomes["train_scv_insufficient_resources"] = await bot.train(UnitTypeId.SCV)

        # Unknown unit tag: no unit under our control has ever had this tag.
        # No resource dependency, so it doesn't matter that we're broke here.
        self.outcomes["move_unknown_tag"] = await bot.move(units=[999_999_999], target=ai.game_info.map_center)

        # -- cheats for fast, deterministic verification of the remaining cases --
        # (Every Terran structure costs more than our 50 starting minerals, so
        # the illegal-placement case below needs these granted first -- otherwise
        # it's blocked by affordability before placement is even checked, which
        # is a real, correct precondition order in Bot.build() but would test
        # the wrong constraint here.)
        await ai.client.debug_all_resources()
        await ai.client.debug_fast_build()
        await ai._advance_steps(4)  # let the cheats land before checking affordability

        # Illegal placement: max_distance=0 forces an exact-position check
        # against our own townhall's footprint, which can never be placeable.
        # Resources are plentiful now, so this genuinely isolates placement.
        townhall_pos = ai.townhalls.first.position
        self.outcomes["build_illegal_placement"] = await bot.build(
            UnitTypeId.SUPPLYDEPOT, near=townhall_pos, max_distance=0
        )

        # -- successful train --
        # We only have one Command Center, and it's still legitimately busy
        # with the pre-cheat SCV order above (debug_fast_build doesn't
        # retroactively speed up an order already issued before it was
        # granted) -- train_only_idle_buildings=False queues behind it,
        # exactly like requesting a second unit at your only production
        # structure while the first is still building.
        self.outcomes["train_scv_confirmed"] = await bot.train(UnitTypeId.SCV, train_only_idle_buildings=False)

        # -- successful build (SupplyDepot near home) --
        depot_point = ai.townhalls.first.position.towards(ai.game_info.map_center, 6)
        self.outcomes["build_supply_depot"] = await bot.build(UnitTypeId.SUPPLYDEPOT, near=depot_point)

        # -- second build: EngineeringBay, prerequisite for the research test --
        ebay_point = ai.townhalls.first.position.towards(ai.game_info.map_center, 6).offset((3, 3))
        self.outcomes["build_engineering_bay"] = await bot.build(UnitTypeId.ENGINEERINGBAY, near=ebay_point)

        # build() only confirms construction *started*; give it a bit longer
        # to finish (fast_build makes this quick, but not same-frame).
        for _ in range(15):
            await ai._advance_steps(4)
            if ai.structures(UnitTypeId.ENGINEERINGBAY).ready:
                break

        # -- research (no prerequisite upgrade, so this is a clean single-step case) --
        self.outcomes["research_hisec_auto_tracking"] = await bot.research(UpgradeId.HISECAUTOTRACKING)

        # -- move / attack-move: use two workers not mid-construction --
        movable = [w for w in ai.workers if not w.is_constructing_scv]
        if len(movable) < 2:
            movable = list(ai.workers)
        move_target = ai.game_info.map_center
        self.outcomes["move_worker"] = await bot.move(units=movable[0], target=move_target)

        attack_target = ai.enemy_start_locations[0] if ai.enemy_start_locations else ai.game_info.map_center
        self.outcomes["attack_move_worker"] = await bot.attack_move(units=[movable[1]], target=attack_target)

        # -- chat --
        self.outcomes["chat"] = await bot.chat("sc2-sdk ticket #3 verified-action integration test")

        self.outcomes["observe_after_script"] = bot.observe()


@pytest.mark.integration
def test_verified_bot_actions_against_real_game(sc2_verified_bot_harness):
    bot_ai = _TerranAcceptanceBot()
    result = sc2_verified_bot_harness(
        bot_ai,
        my_race=Race.Terran,
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
    assert any(u.type_name == "SCV" for u in initial.units)
    assert any(s.type_name in ("COMMANDCENTER",) for s in initial.structures)
    assert initial.supply_used > 0
    assert initial.match_result is None  # game was still in progress

    # -- train: successful case --
    train_ok = outcomes["train_scv_first"]
    assert train_ok.ok is True
    assert train_ok.dispatched_amount == 1

    # -- train: insufficient resources is a clear, actionable error --
    train_bad = outcomes["train_scv_insufficient_resources"]
    assert train_bad.ok is False
    assert train_bad.error is not None
    assert "insufficient" in train_bad.error.lower() or "resource" in train_bad.error.lower()

    # -- build: illegal placement is a clear, actionable error --
    build_bad = outcomes["build_illegal_placement"]
    assert build_bad.ok is False
    assert build_bad.error is not None
    assert "placement" in build_bad.error.lower() or "valid" in build_bad.error.lower()

    # -- move: unknown unit tag is a clear, actionable error --
    move_bad = outcomes["move_unknown_tag"]
    assert move_bad.ok is False
    assert move_bad.error is not None
    assert "999999999" in move_bad.error or "999_999_999" in move_bad.error or "unknown" in move_bad.error.lower()

    # -- train: successful, cheat-funded case, effect confirmed for real --
    train_confirmed = outcomes["train_scv_confirmed"]
    assert train_confirmed.ok is True
    assert train_confirmed.effect_confirmed is True

    # -- build: successful case, effect confirmed for real --
    depot = outcomes["build_supply_depot"]
    assert depot.ok is True
    assert depot.effect_confirmed is True
    assert depot.structure_tag is not None

    ebay = outcomes["build_engineering_bay"]
    assert ebay.ok is True
    assert ebay.effect_confirmed is True

    # -- research: successful case, effect confirmed for real --
    research = outcomes["research_hisec_auto_tracking"]
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

    # -- final observation reflects the built structures --
    final = outcomes["observe_after_script"]
    assert any(s.type_name == "SUPPLYDEPOT" for s in final.structures)
    assert any(s.type_name == "ENGINEERINGBAY" for s in final.structures)

    # -- match outcome is reported via bot.observe() once the game has ended --
    post_game_observation = bot_ai.bot.observe()
    assert post_game_observation.match_result == result
    assert bot_ai.bot.match_result == result
