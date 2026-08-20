"""SDK core context.

- `sdk.play` -- ticket #2's raw python-sc2 connect-and-play script
  (`play_vs_builtin_ai`), a trivial do-nothing bot just to prove the
  install + connect + play + report-result path works end to end.
- `sdk.observation` / `sdk.outcomes` / `sdk.bot` -- ticket #3's verified
  `bot.*` action/observation layer (`Bot`, `VerifiedBotAI`) and `sdk.*` raw
  passthrough, per the project spec
  (https://github.com/blokboy/sc2-sdk/issues/1). Race-agnostic; #4/#5
  (Protoss/Zerg) are expected to build on `sdk.bot.VerifiedBotAI` directly.
- `sdk.runtime` -- ticket #3's generalization of `sdk.play`'s run_game
  wiring to drive an arbitrary `BotAI` instance (not just the trivial
  walking-skeleton bot) against the built-in AI.
"""
