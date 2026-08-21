"""Example standalone bot script -- documents ticket #7's `bots/` convention
(see `sdk.script_runner`'s module docstring for the full rules) and backs
its wiring test (`tests/integration/test_bot_script_runtime.py`).

A standalone bot script is a plain `.py` file under this directory that
defines exactly one class subclassing `sc2.bot_ai.BotAI` -- in practice
`sdk.bot.VerifiedBotAI`, as here, to get the same `bot.*`/`sdk.*` API
surface `execute_code` mode (ticket #6) uses. No fixed class name is
required; call it whatever fits the strategy (`RushBot`, `MacroBot`, ...).

Run it with:

    sc2-sdk-run-bot idle_example --race terran --opponent-race zerg

This particular script is intentionally trivial -- it trains one SCV via
the verified `bot.*` API on its first step, then idles for the rest of the
game -- it exists to document and exercise the convention, not as a real
strategy. A real strategy script would drive the whole game from `on_step`
(build order, army composition, attack timings, ...) exactly like this one
drives its one action, just with a lot more of it.
"""

from __future__ import annotations

from sc2.ids.unit_typeid import UnitTypeId

from sdk.bot import VerifiedBotAI


class IdleExampleBot(VerifiedBotAI):
    """Trains one SCV via `bot.train()` on its first step, then idles."""

    async def on_start(self) -> None:
        await super().on_start()
        self._acted = False

    async def on_step(self, iteration: int) -> None:
        if self._acted:
            return
        self._acted = True
        await self.bot.train(UnitTypeId.SCV)
