# learnings/

Copyable, worked examples of using the `bot.*`/`sdk.*` API for real
things -- as opposed to the root README's narrative "how does each play
modality work" sections, or `bots/idle_example.py`'s minimal one-action
script that exists purely to document the `bots/<name>.py` convention
(ticket #7). Start here if you want something to copy, adapt, and run.

See [`../docs/API.md`](../docs/API.md) for the generated reference of every
public `bot.*`/`sdk.*`/`install.*` class, function, and constant this
project defines, and the root [`README.md`](../README.md) for how the three
play modalities (verified-action scripts, MCP `execute_code`, autonomous
bot scripts) fit together.

## `macro_loop_bot.py` -- a real, continuous Terran macro loop

[`macro_loop_bot.py`](macro_loop_bot.py) is a small `VerifiedBotAI`
subclass that runs an actual macro decision loop, not a one-shot scripted
sequence:

1. Build a Supply Depot when supply is getting low and none is already
   pending (`bot.build`).
2. Train SCVs up to a worker target, while affordable (`bot.train`).
3. Once a Supply Depot has *finished* (not just started), build a Barracks
   (`bot.build`).
4. Once the Barracks is ready, train Marines from it (`bot.train`).

Every decision is one of `bot.*`'s verified actions -- each call's
`Outcome` (`ok` / `effect_confirmed` / `error` / `detail`, see
[`../src/sdk/outcomes.py`](../src/sdk/outcomes.py)) is recorded onto
`self.log` rather than assumed to have worked, the same "verify, don't
assume" pattern the SDK's own action layer uses internally (see
[`../src/sdk/bot.py`](../src/sdk/bot.py)'s module docstring).

This is deliberately a different shape from this project's other two
examples, to round out the set of copyable starting points:

| Example | Shape | Purpose |
|---|---|---|
| `bots/idle_example.py` (ticket #7) | One action, once | Documents the minimal `bots/<name>.py` convention |
| `tests/integration/test_verified_bot_actions.py` (ticket #3) | Full scripted sequence, once | Exercises every `bot.*` action for a test |
| `learnings/macro_loop_bot.py` (this ticket) | Continuous decision loop, gated on real game state | Shows what an actual bot's `on_step` looks like once it's doing more than one thing |

### Copy it

```bash
cp learnings/macro_loop_bot.py bots/my_bot.py
sc2-sdk-run-bot my_bot --race terran --opponent-race zerg
```

Or run it directly from `learnings/` without copying, by pointing
`sc2-sdk-run-bot`'s `--bots-dir` flag at this directory (the same override
`sdk.script_runner.run_bot_script`'s `bots_dir` parameter documents as an
escape hatch for scripts living outside the default `bots/` location):

```bash
sc2-sdk-run-bot macro_loop_bot --bots-dir learnings --race terran --opponent-race zerg
```

Pass `--no-realtime --time-limit 120` for a fast, deterministic, CI-style
run instead of real-time speed against a real opponent.

### Verified against a real game

This isn't just plausible-looking code -- it was actually run against a
real, local, headless SC2 game (inside this project's own
`sc2-sdk-integration` Docker image; see the root README's "Real-game
integration tests in Docker" section) as part of writing this ticket,
stepped (non-realtime) with a 180-in-game-second safety cap:

```
RESULT: Tie
LOG_ENTRY_COUNT: 49
[train_scv] Dispatched 1/1 SCV; confirmed via increased pending-production count.
...
[supply_depot] Dispatched build of SUPPLYDEPOT; confirmed (structure_tag=4353163266).
...
[barracks] Dispatched build of BARRACKS; confirmed (structure_tag=4358406145).
...
[train_marine] Dispatched 1/1 MARINE; confirmed via increased pending-production count.
...
[supply_depot] Dispatched build of SUPPLYDEPOT; confirmed (structure_tag=4361027587).
...
```

The `Tie` result is expected and not a bug: this example only demonstrates
the macro loop (workers/supply/production), never issues an attack order,
and the 180-second safety cap elapsed before either side otherwise won --
`sdk.runtime.run_bot_vs_builtin_ai`'s `game_time_limit` scores an unresolved
match a `Tie` by design (see its docstring). What matters for this example
is what the 49 recorded `bot.*` outcomes show: real, `effect_confirmed=True`
Supply Depot and Barracks builds, and repeated confirmed SCV/Marine
training -- i.e. every stage of the macro loop above was exercised for
real, not merely reviewed by reading the source. The "no eligible
production structure found" entries interleaved above are also real and
expected: they're `bot.train`'s honest report that every production
structure happened to be busy the moment that particular loop iteration
checked -- not a failure of the bot, just what a genuine, unforced macro
loop's `Outcome` log actually looks like turn to turn.
