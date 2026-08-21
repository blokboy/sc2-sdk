"""Ticket #8's `learnings/` example: a short, real Terran macro loop built
entirely on the verified `bot.*` API from `sdk.bot` (tickets #3-#5).

Where `bots/idle_example.py` (ticket #7) is deliberately a *one-shot*
action to document the `bots/` script convention, and
`tests/integration/test_verified_bot_actions.py` (ticket #3) is a *scripted
sequence* run once to exercise every action for a test, this is a third
shape worth having a copyable example of: a small **continuous decision
loop** -- the kind of thing a real bot script's `on_step` actually looks
like once it's doing more than one thing. Every decision below is a
`bot.*` call whose `Outcome` (see `sdk/outcomes.py`) is inspected before
moving on, per this project's central "verify, don't assume" design
decision (see `sdk/bot.py`'s module docstring).

What it does, each time `_maybe_act` runs (throttled to every `CHECK_EVERY`
`on_step` calls -- see below for why):

  1. If supply is getting low and no depot is already pending, build a
     Supply Depot (`bot.build`).
  2. If we're under our worker target and can afford it, train an SCV
     (`bot.train`).
  3. Once at least one Supply Depot has finished, build a Barracks
     (`bot.build`) -- gated on "finished", not just "pending", since a
     Barracks needs a *completed* Supply Depot as its tech prerequisite,
     not merely one under construction.
  4. Once the Barracks is up and ready, train Marines from it
     (`bot.train`).

Why throttle with `CHECK_EVERY` instead of acting on every `on_step`
-------------------------------------------------------------------
Every `bot.*` write action already internally advances several real
simulation steps while it polls to confirm its effect (see `sdk/bot.py`'s
module docstring) -- calling one on *every* `on_step` would mean this
loop's own decisions are constantly interleaved with, and made stale by,
those internal advances. Re-checking every `CHECK_EVERY` iterations instead
means each round of decisions is made against one coherent, freshly
re-observed game state.

Copy this file to `bots/<your_bot_name>.py` (or point `sc2-sdk-run-bot`'s
`--bots-dir` flag straight at this directory, as this repo's own README
and CI verification does) and run it with:

    sc2-sdk-run-bot macro_loop_bot --bots-dir learnings --race terran --opponent-race zerg

See `learnings/README.md` for the full walkthrough and what running this
for real actually produced.
"""

from __future__ import annotations

from sc2.ids.unit_typeid import UnitTypeId

from sdk.bot import VerifiedBotAI


class MacroLoopBot(VerifiedBotAI):
    """Trains workers up to a target, expands supply reactively, then builds
    one Barracks and starts training Marines -- a small, real, continuous
    macro loop built entirely on `bot.*`."""

    #: Stop training SCVs once we have this many -- a real bot would tie
    #: this to mineral-line saturation; kept as a flat constant here to
    #: keep the example's decision logic easy to follow.
    WORKER_TARGET = 16

    #: Re-run the decision loop every this-many on_step calls -- see module
    #: docstring for why acting on literally every step would be
    #: counterproductive given bot.* actions already advance steps
    #: internally while verifying.
    CHECK_EVERY = 16

    async def on_start(self) -> None:
        await super().on_start()
        #: Every outcome this bot recorded, in order -- inspectable after
        #: the game the same way tests/integration/test_verified_bot_actions.py
        #: inspects bot_ai.outcomes: run_bot_vs_builtin_ai/run_bot_script
        #: hand back control with this same live object still around.
        self.log: list[str] = []

    async def on_step(self, iteration: int) -> None:
        if iteration % self.CHECK_EVERY != 0:
            return
        await self._maybe_build_supply()
        await self._maybe_train_workers()
        await self._maybe_build_barracks()
        await self._maybe_train_marine()

    async def _maybe_build_supply(self) -> None:
        if self.supply_left >= 4 or self.already_pending(UnitTypeId.SUPPLYDEPOT) > 0:
            return
        if not self.can_afford(UnitTypeId.SUPPLYDEPOT) or not self.townhalls:
            return
        near = self.townhalls.first.position.towards(self.game_info.map_center, 6)
        outcome = await self.bot.build(UnitTypeId.SUPPLYDEPOT, near=near)
        self.log.append(f"[supply_depot] {outcome.detail}")

    async def _maybe_train_workers(self) -> None:
        if self.supply_workers >= self.WORKER_TARGET:
            return
        if not self.can_afford(UnitTypeId.SCV):
            return
        outcome = await self.bot.train(UnitTypeId.SCV)
        self.log.append(f"[train_scv] {outcome.detail}")

    async def _maybe_build_barracks(self) -> None:
        if self.structures(UnitTypeId.BARRACKS).amount > 0 or self.already_pending(UnitTypeId.BARRACKS) > 0:
            return
        # Gate on a *completed* depot (not merely already_pending), since
        # the Barracks' tech requirement needs a finished Supply Depot.
        if not self.structures(UnitTypeId.SUPPLYDEPOT).ready:
            return
        if not self.can_afford(UnitTypeId.BARRACKS) or not self.townhalls:
            return
        near = self.townhalls.first.position.towards(self.game_info.map_center, 8)
        outcome = await self.bot.build(UnitTypeId.BARRACKS, near=near)
        self.log.append(f"[barracks] {outcome.detail}")

    async def _maybe_train_marine(self) -> None:
        if not self.structures(UnitTypeId.BARRACKS).ready:
            return
        if not self.can_afford(UnitTypeId.MARINE):
            return
        outcome = await self.bot.train(UnitTypeId.MARINE)
        self.log.append(f"[train_marine] {outcome.detail}")
