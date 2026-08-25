<!--
GENERATED FILE -- do not hand-edit.

Produced by `scripts/gen_api_docs.py` from the actual source of the modules
listed there (via Python's `ast`, not hand-maintained prose) -- see that
script's module docstring for exactly what "public surface" means here and
why AST rather than `inspect`/import.

Regenerate after any change to a documented module's public surface:

    python scripts/gen_api_docs.py

A CI check (`.github/workflows/docs.yml`) runs `python scripts/gen_api_docs.py
--check` on every push/PR and fails if this file doesn't match what that
command would produce -- i.e. this file cannot silently drift from the real
`bot.*`/`sdk.*`/`install.*` surface.
-->

# sc2-sdk API reference

Generated reference for every public class, function, and constant `sc2-sdk`
itself defines across `sdk.*` (the verified `bot.*` action/observation layer,
raw `sdk.*` passthrough, and the three play-modality entry points: raw
connect-and-play, the MCP `execute_code` server, and the autonomous
bot-script runtime) and `install.*` (client detection/install + map pool
sync). It does **not** include python-sc2's own API (`sc2.*`) -- only this
project's surface.

For narrative usage (how these fit together, worked examples, how to run
things), see the root [`README.md`](../README.md) and
[`learnings/README.md`](../learnings/README.md).


## Contents

- [`sdk.bot`](#sdkbot)
- [`sdk.observation`](#sdkobservation)
- [`sdk.outcomes`](#sdkoutcomes)
- [`sdk.play`](#sdkplay)
- [`sdk.runtime`](#sdkruntime)
- [`sdk.script_runner`](#sdkscriptrunner)
- [`sdk.selfplay`](#sdkselfplay)
- [`sdk.mcp_server`](#sdkmcpserver)
- [`sdk.join`](#sdkjoin)
- [`sdk.matchcode`](#sdkmatchcode)
- [`sdk.host_join`](#sdkhostjoin)
- [`install.cli`](#installcli)
- [`install.battlenet`](#installbattlenet)
- [`install.headless`](#installheadless)
- [`install.maps`](#installmaps)
- [`install.paths`](#installpaths)

## `sdk.bot`

*Source: [`src/sdk/bot.py`](../src/sdk/bot.py)*

The verified `bot.*` action/observation layer, plus `sdk.*` raw passthrough.

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

`_advance_steps` drives its progress by sending a manual `RequestStep` to
the SC2 engine -- only a meaningful request when the engine is otherwise
paused, which non-realtime mode guarantees (nothing but `sc2.main`'s outer
loop ever steps it) but realtime mode does not: there, the engine already
free-runs on its own wall-clock timer and `sc2.main`'s own realtime branch
never calls `client.step()` itself. `Bot._advance` therefore branches on
`self._ai.realtime` and uses `Bot._advance_realtime` instead in that case --
same flush-then-reobserve shape, but it waits for the engine's own
free-running simulation to reach a target game loop rather than manually
stepping it. See `Bot._advance`'s docstring for the full reasoning.

Race-agnostic by design: train/build/research/move/chat are all generic
python-sc2 mechanisms (the same underlying `BotAI.train()`/`.build()`/
`.research()` work for any of the three races); nothing Terran-specific is
hardcoded here. Ticket #3 only *exercises* this against Terran integration
tests -- #4 (Protoss) and #5 (Zerg) are expected to reuse this class
directly rather than re-deriving the verification pattern.

### class `Bot`

Wraps a live `BotAI` instance with the verified `bot.*` API.

Constructed once per game (see `VerifiedBotAI` below, which every
integration test's bot subclasses). `bot.sdk` is the raw passthrough
tier -- literally the wrapped `BotAI` instance itself, since that
instance *is* "the underlying python-sc2 BotAI instance and its
unit/action API" the spec asks `sdk.*` to expose.

#### `Bot.__init__`

```python
def __init__(self, ai: BotAI) -> None
```

#### `Bot.sdk`

```python
@property
def sdk(self) -> BotAI
```

Raw passthrough to the underlying python-sc2 BotAI instance.

#### `Bot.observe`

```python
def observe(self) -> Observation
```

Report own units/structures/resources/supply, visible enemy
units/structures, minimap/vision, and match outcome (once known).

Pure and side-effect-free -- safe to call at any time, including
after the match has ended (it then reports the last-known state
plus the now-final `match_result`).

#### `Bot.train`

```python
async def train(self, unit_type: UnitTypeId, amount: int=1, closest_to: Point2 | None=None, train_only_idle_buildings: bool=True, max_wait_steps: int=_DEFAULT_MAX_WAIT_STEPS) -> TrainOutcome
```

Train `amount` of `unit_type` from any eligible, idle, completed
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

#### `Bot.build`

```python
async def build(self, structure_type: UnitTypeId, near: Unit | Point2, max_distance: int=20, build_worker: Unit | None=None, max_wait_steps: int=_BUILD_DEFAULT_MAX_WAIT_STEPS) -> BuildOutcome
```

Build `structure_type` near `near`, and confirm construction
actually started -- meaning a new structure entity of that type is
now observable (not merely that already_pending() ticked up, which
happens as soon as the assigned worker is *dispatched*, before it
has even arrived at the site -- see the module docstring on why a
real subsequent observation, not optimistic bookkeeping, is what
"confirmed" means here).

Checks `tech_requirement_progress` first, same as `train()`: unlike
`train()`'s underlying `BotAI.train`, `BotAI.build` doesn't validate
this itself -- it happily finds a placement, assigns a worker, and
issues the command even when the prerequisite (e.g. a Barracks
before any Supply Depot has *finished*, not just started) isn't
met. The SC2 server then silently drops the command server-side:
no error, no order ever appears on the worker, nothing to
re-observe -- so without this check here, `dispatched` above would
be `True` while nothing happened, and confirmation would fail for
an entirely different reason (never having been dispatched at all)
than what `effect_confirmed=False` normally means (dispatched, but
not yet observably true).

#### `Bot.research`

```python
async def research(self, upgrade_type: UpgradeId, max_wait_steps: int=_DEFAULT_MAX_WAIT_STEPS) -> ResearchOutcome
```

Research `upgrade_type` from any idle, completed structure that
can research it, and confirm research actually started.

#### `Bot.move`

```python
async def move(self, units: Unit | Units | int | list, target: Point2 | Unit, queue: bool=False, max_wait_steps: int=3) -> MoveOutcome
```

Move a unit or unit group to `target`, and confirm each unit
actually picked up a move order.

#### `Bot.attack_move`

```python
async def attack_move(self, units: Unit | Units | int | list, target: Point2 | Unit, queue: bool=False, max_wait_steps: int=3) -> MoveOutcome
```

Attack-move a unit or unit group toward `target`, and confirm
each unit actually picked up an attack order.

#### `Bot.chat`

```python
async def chat(self, message: str, team_only: bool=False) -> ChatOutcome
```

Send a chat message.

Known limitation, documented rather than papered over: python-sc2 /
the SC2 game API give no way to read a chat message back to confirm
the game client actually displayed it, so `effect_confirmed` here
can only ever reflect "the chat_send call completed without the
client rejecting it" -- not a true independent re-observation like
every other action above. This is called out explicitly rather than
silently claiming the same verification strength as train/build/
research/move.

### class `VerifiedBotAI`

Base class for a live-game `BotAI` that wires up `self.bot` (the
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

#### `VerifiedBotAI.on_start`

```python
async def on_start(self) -> None
```

#### `VerifiedBotAI.on_end`

```python
async def on_end(self, game_result: Result) -> None
```

## `sdk.observation`

*Source: [`src/sdk/observation.py`](../src/sdk/observation.py)*

Race-agnostic observation snapshot of a live python-sc2 game.

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

### class `UnitSnapshot`

A lightweight, JSON-friendly summary of one unit or structure.

Deliberately not the raw python-sc2 `Unit` object -- this is what
`bot.observe()` reports to keep the semantic tier's output plain data an
agent can reason about without learning python-sc2's own object model.
The raw `Unit` is still reachable via `sdk.*` (e.g. `sdk.units`,
`sdk.structures`) for anyone who needs it.

**Fields:**

- `tag: int`
- `type_name: str`
- `position: tuple[float, float]`
- `health: float`
- `health_max: float`
- `shield: float`
- `energy: float`
- `build_progress: float`
- `is_idle: bool`
- `order_abilities: tuple[str, ...]`

### class `MinimapSummary`

A compact summary of the visibility/creep minimap, not the raw grid.

python-sc2 exposes the full per-tile grid as `sdk.state.visibility` /
`sdk.state.creep` (numpy-backed `PixelMap`s) for anyone who needs
pixel-level data; this summary is what `bot.observe()` reports because
dumping a full WxH grid into every observation is neither
agent-friendly nor a meaningful "did my action work" signal on its own.

**Fields:**

- `width: int`
- `height: int`
- `visible_tile_count: int`
- `explored_tile_count: int`
- `creep_tile_count: int`

### class `Observation`

A full snapshot of what `bot.observe()` reports.

`match_result` is `None` while the game is ongoing and is populated
(Victory/Defeat/Tie/Undecided) once `on_end` has fired -- see
`bot.py`'s `VerifiedBotAI.on_end`. Calling `observe()` after the game
has ended still works (this function never touches the network); it
just reports whatever state was current as of the last real observation
plus the now-known match result.

**Fields:**

- `game_time: float`
- `units: tuple[UnitSnapshot, ...]`
- `structures: tuple[UnitSnapshot, ...]`
- `enemy_units: tuple[UnitSnapshot, ...]`
- `enemy_structures: tuple[UnitSnapshot, ...]`
- `minerals: int`
- `vespene: int`
- `supply_used: float`
- `supply_cap: float`
- `supply_left: float`
- `minimap: MinimapSummary`
- `match_result: Result | None`

### `observe`

```python
def observe(ai: BotAI, match_result: Result | None) -> Observation
```

Build an `Observation` from a `BotAI` instance's current state.

Race-agnostic: reads only attributes `BotAI` populates identically for
Terran, Protoss, and Zerg (`self.units`, `self.structures`,
`self.enemy_units`/`self.enemy_structures`, which python-sc2 already
restricts to currently-*visible* enemy units/structures -- not a
fog-of-war memory of everything ever seen).

## `sdk.outcomes`

*Source: [`src/sdk/outcomes.py`](../src/sdk/outcomes.py)*

Return types for `bot.py`'s verified `Bot.*` actions.

Every verified action returns one of these instead of the underlying
python-sc2 call's raw bool/int, so that a caller (an agent, or a test) can
distinguish three states instead of two:

  1. the command was rejected outright (`ok=False`) -- with `error` set to
     a clear, actionable message (insufficient resources, illegal
     placement, unknown unit tag, tech requirement not met, ...);
  2. the command was accepted and a *subsequent* observation confirms the
     intended effect actually happened (`ok=True`, `effect_confirmed=True`);
  3. the command was accepted but a subsequent observation could *not*
     confirm the effect within the verification window (`ok=True`,
     `effect_confirmed=False`) -- this is not necessarily a bug (e.g. a
     structure that legitimately takes longer to complete than the
     window this action polled for) but it is reported honestly rather
     than assumed.

`detail` is always a short human-readable sentence explaining what was
observed, useful for logging/debugging regardless of which state above
applies.

### class `TrainOutcome`

**Fields:**

- `ok: bool`
- `effect_confirmed: bool`
- `error: str | None`
- `detail: str`
- `unit_type: str`
- `requested_amount: int`
- `dispatched_amount: int`
- `new_unit_tags: tuple[int, ...]`

### class `BuildOutcome`

**Fields:**

- `ok: bool`
- `effect_confirmed: bool`
- `error: str | None`
- `detail: str`
- `structure_type: str`
- `position: tuple[float, float] | None`
- `structure_tag: int | None`

### class `ResearchOutcome`

**Fields:**

- `ok: bool`
- `effect_confirmed: bool`
- `error: str | None`
- `detail: str`
- `upgrade_type: str`

### class `MoveOutcome`

**Fields:**

- `ok: bool`
- `effect_confirmed: bool`
- `error: str | None`
- `detail: str`
- `mode: str`
- `requested_tags: tuple[int, ...]`
- `confirmed_tags: tuple[int, ...]`

### class `ChatOutcome`

**Fields:**

- `ok: bool`
- `effect_confirmed: bool`
- `error: str | None`
- `detail: str`
- `message: str`

## `sdk.play`

*Source: [`src/sdk/play.py`](../src/sdk/play.py)*

Raw python-sc2 connect-and-play: launch a full game against the game's
built-in AI and report the result.

This is deliberately *not* the sdk's `bot.*`/`sdk.*` verified-action API
(that's ticket #3, https://github.com/blokboy/sc2-sdk/issues/3) -- it's a
thin, direct use of python-sc2's own `run_game`/`BotAI`/`Computer` primitives,
just enough to prove the install + connect + play + report-result path works
end to end. Every later ticket's tests reuse this module's
`play_vs_builtin_ai` via the `sc2_game_harness` fixture in
`tests/conftest.py`.

### `DEFAULT_MAP`

```python
DEFAULT_MAP = 'AutomatonLE'
```

Fixed test map this ticket's setup syncs and its harness runs on by default (see install.maps.DEFAULT_MAPS).

### `play_vs_builtin_ai`

```python
def play_vs_builtin_ai(map_name: str=DEFAULT_MAP, my_race: Race=Race.Random, opponent_race: Race=Race.Random, difficulty: Difficulty=Difficulty.Easy, realtime: bool=False, game_time_limit: int | None=None) -> Result
```

Launch a full game against the built-in AI and run it to completion.

Args:
    map_name: name of a synced map (see install.maps.DEFAULT_MAPS),
        resolved via sc2.maps.get() -- must already exist under the
        local install's Maps directory (run `setup` first).
    my_race: the race our side plays.
    opponent_race: the built-in AI's race.
    difficulty: the built-in AI's difficulty.
    realtime: if False (default), the game steps only as fast as our
        (trivial) bot responds -- deterministic and fast for tests. If
        True, it runs at wall-clock speed.
    game_time_limit: optional wall-clock-independent safety cap, in
        in-game seconds; the match is scored a Tie if neither side has
        won by then. Recommended for automated/test runs so a stuck
        game can't hang forever.

Returns:
    sc2.data.Result -- one of Victory, Defeat, Tie, or (in edge cases
    python-sc2 itself reports, e.g. a crashed client) Undecided.

### `main`

```python
def main(argv: list[str] | None=None) -> int
```

## `sdk.runtime`

*Source: [`src/sdk/runtime.py`](../src/sdk/runtime.py)*

Run an arbitrary `BotAI` instance (typically a `VerifiedBotAI` subclass,
see `bot.py`) against the game's built-in AI.

This is a deliberate sibling to `sdk.play.play_vs_builtin_ai`, not a
modification of it: ticket #3's brief explicitly says not to touch
`play.py`'s connect-and-play logic (the proven walking-skeleton path from
#2/#9) except for an actual bug fix, and `play_vs_builtin_ai` hardcodes a
trivial `_NullBot` -- there is no way to hand it a real bot instance without
changing its signature. `run_bot_vs_builtin_ai` below is the same
`sc2.maps`/`sc2.main.run_game`/`sc2.player.Bot`/`Computer` primitives,
generalized to accept any `BotAI` instance, so ticket #3 (and #4-#8 after
it) can drive a real verified-action bot through a real game without
touching the walking skeleton at all.

### `DEFAULT_MAP`

```python
DEFAULT_MAP = 'AutomatonLE'
```

Same fixed test map ticket #2's setup syncs and its harness defaults to.

### `run_bot_vs_builtin_ai`

```python
def run_bot_vs_builtin_ai(bot_ai: BotAI, map_name: str=DEFAULT_MAP, my_race: Race=Race.Terran, opponent_race: Race=Race.Random, difficulty: Difficulty=Difficulty.Easy, realtime: bool=False, game_time_limit: int | None=None) -> Result
```

Launch a full game against the built-in AI, driving `bot_ai`
(typically a `VerifiedBotAI` subclass instance) as our side, and run it
to completion.

Args:
    bot_ai: a constructed `BotAI` (usually `VerifiedBotAI`) instance.
        The same object is still live and inspectable after this
        function returns -- see `bot.py`'s `VerifiedBotAI` docstring
        for why that's the point.
    map_name: name of a synced map -- must already exist under the
        local install's Maps directory (run `setup` first).
    my_race: the race `bot_ai` plays. Defaults to Terran since that's
        the only race ticket #3's verified actions have been exercised
        against; pass explicitly for Protoss/Zerg bots.
    opponent_race: the built-in AI's race.
    difficulty: the built-in AI's difficulty.
    realtime: if False (default), the game steps only as fast as
        `bot_ai` responds -- deterministic and fast for tests.
    game_time_limit: optional wall-clock-independent safety cap, in
        in-game seconds; the match is scored a Tie if neither side has
        won by then.

Returns:
    sc2.data.Result -- one of Victory, Defeat, Tie, or (in edge cases
    python-sc2 itself reports, e.g. a crashed client) Undecided.

### `run_bot_vs_bot`

```python
def run_bot_vs_bot(bot_a: BotAI, bot_b: BotAI, map_name: str=DEFAULT_MAP, race_a: Race=Race.Terran, race_b: Race=Race.Terran, realtime: bool=False, game_time_limit: int | None=None) -> list[Result]
```

Launch a full local match between two `BotAI` instances -- ticket
#10's self-play mode -- and run it to completion. Both objects are still
live and inspectable afterward, same as `run_bot_vs_builtin_ai`'s
`bot_ai` argument.

This is a sibling to `run_bot_vs_builtin_ai`, not a variant of it with an
`if opponent_is_a_bot` branch: `run_game` itself returns a *different
shape* depending on whether the second player is a `Computer` or another
`Bot` (see `sc2.main.run_game`'s own docstring -- a single `Result` for
vs-Computer, a `list` of two `Result`s for Bot-vs-Bot/Bot-vs-Human,
because that path actually spawns and hosts/joins two local SC2
processes rather than one client playing against the built-in AI
in-process). Folding both shapes behind one return type would either
lose information (always returning one `Result`) or force every existing
`run_bot_vs_builtin_ai` caller to start handling a list -- neither of
which this ticket's brief asks for.

Args:
    bot_a: a constructed `BotAI` (usually `VerifiedBotAI`) instance for
        the first side.
    bot_b: a constructed `BotAI` instance for the second side. Pass a
        second, separate instance of the same class as `bot_a` to play a
        script against itself (what `sc2-sdk-selfplay` does when given
        only one script), or a different class entirely for two
        different bots.
    map_name: see `run_bot_vs_builtin_ai`.
    race_a: the race `bot_a` plays. Defaults to Terran for the same
        reason `run_bot_vs_builtin_ai.my_race` does -- pass explicitly
        for Protoss/Zerg bots.
    race_b: the race `bot_b` plays.
    realtime: if False (default), the game steps only as fast as both
        bots respond -- deterministic and fast for tests, consistent
        with `run_bot_vs_builtin_ai`'s default.
    game_time_limit: see `run_bot_vs_builtin_ai`.

Returns:
    A two-element list of `sc2.data.Result`, `[result_for_bot_a,
    result_for_bot_b]` -- `run_game`'s own shape for a non-Computer
    match, passed through as-is rather than collapsed to one value.

## `sdk.script_runner`

*Source: [`src/sdk/script_runner.py`](../src/sdk/script_runner.py)*

Discover and run a standalone bot script -- ticket #7
(https://github.com/blokboy/sc2-sdk/issues/7): the "autonomous bot-script
runtime" play modality from the spec (#1), sitting alongside interactive
`execute_code` mode (#6). Where #6 pauses the game every step waiting for an
external caller's next snippet, this module's whole point is the opposite:
an agent authors a `BotAI` subclass *once*, as a normal Python file, and
this runner then plays a full game with it unattended -- no live per-tick
calls from any caller once the game starts.

Convention: the `bots/` directory
----------------------------------
Standalone bot scripts live as individual `.py` files directly under the
`bots/` directory at the repo root (`bots/<name>.py`, e.g.
`bots/rush_bot.py`) -- not a package, no `__init__.py` needed, since each
script is loaded directly from its file path (see `load_bot_class` below),
not imported as `bots.<name>`. This mirrors how `play.py`/`mcp_server.py`
are each one file defining what they need; a bot script is the same shape,
just relocated to a directory an agent can freely add new files to without
touching `src/sdk/`.

Each script must define **exactly one** class, at module level, that
subclasses `sc2.bot_ai.BotAI` (in practice, `sdk.bot.VerifiedBotAI`, which
is what makes the same `bot.*`/`sdk.*` API surface `execute_code` mode uses
available identically here -- see `bots/idle_example.py`). No fixed class
name is required (agents can call it `RushBot`, `MacroBot`, whatever fits);
the runner instead finds the one class *defined in that script's own
module* (as opposed to merely imported into it, e.g. `VerifiedBotAI` itself)
that is a `BotAI` subclass. Zero or more-than-one such classes is a clear
authoring error, reported as such rather than guessed at.

Discovery/loading mechanics
-----------------------------
`resolve_script_path` turns a bot name (or a literal `.py` path, for
ad-hoc/testing use) into a concrete file path under `bots/`.
`load_bot_class` then does a plain `importlib.util.spec_from_file_location`
+ `exec_module` load of that one file (no package/`sys.path` machinery
needed) and inspects the resulting module's own namespace for the single
`BotAI` subclass described above. `run_bot_script` ties both steps together
and hands a fresh instance of the discovered class to
`runtime.run_bot_vs_builtin_ai` -- the same synchronous `sc2.main.run_game`
entrypoint ticket #3 built, reused as-is (see that module's docstring for
why: this ticket doesn't need `mcp_server.py`'s `_host_game`/shared-event-
loop trick, since there's no concurrent MCP server here for the game to
coexist with -- just one script, run to completion).

### `BOTS_DIR`

```python
BOTS_DIR = Path(__file__).resolve().parent.parent.parent / 'bots'
```

Root-level directory (the documented convention -- see module docstring) standalone bot scripts live under. Resolved relative to this file's location (src/sdk/script_runner.py -> repo root), which holds for the editable install (`pip install -e .`) this project's README documents as the supported install path -- the same assumption `tests/conftest.py`'s `pythonpath = ["src"]` pytest config already makes.

### `resolve_script_path`

```python
def resolve_script_path(name_or_path: str, bots_dir: Path=BOTS_DIR) -> Path
```

Resolve `name_or_path` to a concrete script file.

Accepts either a bare bot name (resolved to `<bots_dir>/<name>.py`, the
documented convention) or a literal path to a `.py` file that already
exists (an escape hatch for ad-hoc scripts/tests outside `bots_dir`,
not itself part of the documented convention).

### `load_bot_class`

```python
def load_bot_class(script_path: Path) -> type[BotAI]
```

Dynamically import `script_path` and return the single `BotAI`
subclass it defines (see module docstring for the exact rule). Raises
`ValueError` with a clear message if the script defines zero or more
than one such class.

### `run_bot_script`

```python
def run_bot_script(name_or_path: str, map_name: str=DEFAULT_MAP, my_race: Race=Race.Terran, opponent_race: Race=Race.Random, difficulty: Difficulty=Difficulty.Easy, realtime: bool=True, game_time_limit: int | None=None, bots_dir: Path=BOTS_DIR) -> Result
```

Discover, load, and run a standalone bot script to completion.

`realtime` defaults to True here (unlike `runtime.run_bot_vs_builtin_ai`,
which defaults to False for fast/deterministic tests) because playing
unattended at real-time speed against a real opponent is this ticket's
entire point -- pass `realtime=False` to run stepped instead (useful for
CI/wiring tests, exactly like the sc2_verified_bot_harness fixture other
tickets' tests use).

Args:
    name_or_path: a bot name under `bots_dir` (resolved to
        `<bots_dir>/<name>.py`), or a literal path to a `.py` file.
    map_name, my_race, opponent_race, difficulty, game_time_limit: see
        `runtime.run_bot_vs_builtin_ai`.
    realtime: if True (default), the game runs at wall-clock speed with
        no per-tick calls from this function once `run_game` starts --
        the loaded script's own `on_step` drives everything. If False,
        the game steps only as fast as the bot script responds.
    bots_dir: override for where bot names resolve against; defaults to
        the documented `bots/` directory at the repo root. Exposed
        mainly so tests can point at a scratch directory shaped the
        same way without touching the real `bots/` contents.

Returns:
    sc2.data.Result -- see `runtime.run_bot_vs_builtin_ai`.

### `main`

```python
def main(argv: list[str] | None=None) -> int
```

## `sdk.selfplay`

*Source: [`src/sdk/selfplay.py`](../src/sdk/selfplay.py)*

Self-play mode -- ticket #10
(https://github.com/blokboy/sc2-sdk/issues/10): run a standalone bot script
(see `sdk.script_runner`'s module docstring for the `bots/<name>.py`
convention) against a second instance of itself, or against a *different*
bot script, in a single local match -- no human, no built-in AI, on either
side.

Deliberately its own module rather than an option bolted onto
`sdk.script_runner`: that module's `run_bot_script`/`main` are the proven
#7 vs-built-in-AI path, and this ticket's brief asks not to touch that
existing single-bot behavior. What *is* shared is the discovery mechanics
(`resolve_script_path`, `load_bot_class`) -- those were already plain,
dependency-free functions in `script_runner`, so this module imports and
reuses them directly rather than duplicating "find the one BotAI subclass a
script defines" a second time. The only genuinely new piece is
`runtime.run_bot_vs_bot` (two `Bot` players instead of one `Bot` + one
`Computer`) and the CLI wiring around loading *two* scripts instead of one.

### `run_bot_selfplay`

```python
def run_bot_selfplay(bot_a_name_or_path: str, bot_b_name_or_path: str | None=None, map_name: str=DEFAULT_MAP, race_a: Race=Race.Terran, race_b: Race=Race.Terran, realtime: bool=False, game_time_limit: int | None=None, bots_dir: Path=BOTS_DIR) -> list[Result]
```

Discover, load, and run one or two standalone bot scripts against
each other to completion via `runtime.run_bot_vs_bot`.

Args:
    bot_a_name_or_path: a bot name under `bots_dir`, or a literal path
        to a `.py` file -- same resolution rule as
        `script_runner.run_bot_script`.
    bot_b_name_or_path: same, for the second side. If omitted (the
        default), `bot_a_name_or_path` is loaded *twice* -- a fresh,
        separate instance of the same discovered class for each side --
        so a script plays against itself.
    map_name, race_a, race_b, game_time_limit: see
        `runtime.run_bot_vs_bot`.
    realtime: if False (default), the game steps only as fast as both
        bots respond -- fast/deterministic, matching
        `runtime.run_bot_vs_bot`'s own default (unlike
        `script_runner.run_bot_script`, which defaults to real-time
        since unattended single-script play is its whole point; here,
        fast local iteration is the more useful default for two bots
        testing against each other).
    bots_dir: override for where bot names resolve against; see
        `script_runner.run_bot_script`.

Returns:
    `[result_for_bot_a, result_for_bot_b]` -- see
    `runtime.run_bot_vs_bot`.

### `main`

```python
def main(argv: list[str] | None=None) -> int
```

## `sdk.mcp_server`

*Source: [`src/sdk/mcp_server.py`](../src/sdk/mcp_server.py)*

MCP `execute_code` interactive mode -- ticket #6
(https://github.com/blokboy/sc2-sdk/issues/6): an MCP server exposing an
`execute_code` tool that evaluates a Python snippet against live `bot`/
`sdk` globals bound to a running game, and a `new_game` tool that starts a
fresh game on the same stdio connection instead of requiring a full MCP
reconnect for every match (see `build_server`'s `new_game` docstring for
the design: a small mutable `_ActiveGame` holder both tools route through,
and why `new_game` cancels the previous `game_task` outright rather than
waiting for it to finish -- that same cancellation is also what recovers a
session wedged forever inside a runaway snippet that never returns). By
default the game is paused between calls rather than advancing on
wall-clock time; pass `realtime=True` (`--realtime` on the CLI, or
`new_game`'s `realtime` argument) to run it at wall-clock speed instead,
e.g. for a human spectating the rendered client live -- see
`_build_session`'s docstring for what that does and doesn't change.

Concurrency/communication design
---------------------------------
An `execute_code` call is a request arriving from outside the current call
stack (an MCP client, over stdio or an in-memory transport) asking to run
code against a game that's already in progress. The design chosen here is
"same process, same asyncio event loop, no IPC":

  - `ExecuteCodeBotAI` (below) is a `VerifiedBotAI` subclass (see `bot.py`)
    whose `on_step` does not run a scripted sequence like every prior
    ticket's bots -- instead it does exactly one thing: `await` a get from
    an `asyncio.Queue`, run whatever snippet arrives against `self.bot`/
    `self.sdk`, hand the result back via a per-call `asyncio.Future`, and
    return.
  - `sc2.main._play_game_ai`'s own outer loop (see `sc2/main.py` in the
    installed `python-sc2` package) is what actually drives the game
    forward in non-realtime mode: each iteration it awaits `ai.on_step(...)`
    to *return* before it calls `client.step()` to advance the simulation
    and fetch the next observation. So as long as `on_step` is blocked
    awaiting the next queued snippet, `client.step()` is never called and
    the game sits still -- for however long it takes an MCP client (an LLM
    composing a call) to make the next `execute_code` call. This is exactly
    what gives "pausing for each call rather than advancing on wall-clock
    time" for free, without needing to touch python-sc2's stepping logic at
    all: the mechanism already exists, we're just making `on_step`'s
    *content* be "wait for external input" instead of "run scripted code".
  - The MCP server (a `FastMCP` instance, from the official `mcp` SDK) and
    the running game share the *same* asyncio event loop as ordinary
    concurrent tasks: `serve_execute_code()` below creates the game as an
    `asyncio.Task` (see the note on `sc2.main._host_game` below) and hands
    back a `FastMCP` instance whose `execute_code` tool pushes onto that
    same bot instance's queue and awaits the per-call future. No IPC, no
    subprocess boundary, no serialization of `bot`/`sdk` themselves --
    `execute_code`'s snippet runs with the literal live `Bot`/`BotAI`
    Python objects as its globals, "exactly as a direct call would" (the
    acceptance criterion's own words), because it's *the same objects in
    the same process*, not a proxy across a wire.

Why `sc2.main._host_game` instead of the public `sc2.main.run_game`
---------------------------------------------------------------------
`run_game()` (used by `runtime.run_bot_vs_builtin_ai`, which this module
deliberately does NOT reuse) is a synchronous function that wraps its own
`asyncio.run(_host_game(...))` internally. `asyncio.run()` cannot be called
from inside an already-running event loop -- and this module needs the game
and the MCP server to run as two tasks on *one* already-running loop (see
above), not two separate loops requiring cross-loop synchronization. So
`serve_execute_code()` below calls `sc2.main._host_game` -- the exact same
coroutine `run_game()` itself awaits -- directly, and schedules it with
`asyncio.create_task()` instead. This mirrors the project's existing,
documented precedent for reaching into a private python-sc2 name for a
structural reason a public API doesn't support: see `bot.py`'s module
docstring on `BotAI._advance_steps`/`_after_step`. `_host_game` is not
`@final`/publicly documented the way `_advance_steps` is, so this is called
out explicitly here as a considered choice, not copied blindly: it is the
same call `run_game()`'s own single-non-computer-opponent branch makes,
unwrapped from its private `asyncio.run()`, doing nothing `run_game()`
doesn't already do.

Snippet evaluation semantics
-----------------------------
`_eval_snippet` treats the snippet like a single REPL cell: it's parsed,
wrapped in a synthetic `async def`, and if the snippet's last top-level
statement is a bare expression, that statement becomes `return <expr>` so
its value comes back as the call's `result` (auto-`await`-ed if the
expression itself evaluates to a coroutine, so `bot.train(...)` works
whether or not the snippet remembers to write `await`). Anything printed
via `print()` is captured and returned as `stdout`. Any exception raised
while compiling or running the snippet is caught and reported as a
structured `ok=False`/`error` result (mirroring this project's existing
outcomes.py convention) rather than propagating out of `on_step` and
crashing the running game.

Per-call timeout and single automatic retry
---------------------------------------------
The above except-`Exception` clause does NOT protect against a snippet
that simply never finishes -- and that's not hypothetical: a snippet with
a genuine bug (`while scvs_needed > 0: ... else: await bot._advance(22)`,
where the `else` was unreachable because of a missing supply-cap check)
looped forever, each iteration doing a real `await`, and wedged `on_step`
-- and therefore the whole game -- permanently, with no recovery short of
killing the whole `sc2-sdk-mcp` process. `new_game` (above) can recover
*after the fact* by cancelling the wedged game outright, but nothing
prevented getting wedged in the first place, and a merely-slow (not
actually buggy) call had no chance to just be retried.

`ExecuteCodeBotAI.on_step` now wraps the `_eval_snippet` call in
`asyncio.wait_for(..., timeout=request.timeout_seconds)` -- the same
cancellation mechanism `new_game` already relies on (see its docstring in
`build_server`): `wait_for` cancels its inner task on timeout, which
propagates an ordinary `asyncio.CancelledError` into whatever the
snippet's `await` chain is currently suspended on. `_eval_snippet`'s own
`except Exception` (not `BaseException`) does NOT swallow that
`CancelledError` -- `CancelledError` is a `BaseException` subclass, not an
`Exception` subclass, on the Python version this project targets -- so
the cancellation unwinds cleanly out of `wait_for` as a `TimeoutError`,
exactly as intended, rather than being silently caught and reported as an
ordinary snippet exception.

The retry policy, in `on_step`:

  1. **First timeout**: the caller's future is deliberately left unresolved
     (`execute_code`'s MCP call stays pending). The same `_PendingRequest`
     is mutated (`attempt` incremented from 1 to 2) and re-enqueued onto
     the *end* of `self._queue` via `await self._queue.put(request)` -- so
     any other calls already queued behind it get their turn first, and
     this one gets exactly one more shot after them.
  2. **Second timeout** (the retry also times out): the future IS resolved
     now, with `ExecuteCodeResult(ok=False, ...)` whose `error` spells out
     that the snippet timed out on both the original attempt and the
     automatic retry, the timeout duration used, and the snippet's own
     code -- so whoever reads the result (the MCP tool's return value,
     surfaced straight back to the calling agent) knows unambiguously what
     failed and why, without needing to go dig through server logs.
  3. A snippet that finishes within its timeout on either attempt resolves
     normally -- the caller never sees a timeout error at all in that case,
     only (if it happened) a brief delay while any calls queued behind the
     slow first attempt got processed first.

Timeout value: configurable, not one hardcoded constant, because this
project's `--realtime` mode has *legitimately* slow snippets that are not
bugs -- e.g. `while not sdk.can_afford(X): await bot._advance(22)` waiting
for minerals to accumulate can genuinely take a while in real time if
income is slow. See `DEFAULT_SNIPPET_TIMEOUT_SECONDS` for the default and
its reasoning, and `execute_code`'s `timeout_seconds` argument for the
per-call override a caller who knows a specific snippet will legitimately
run long can use instead of raising the server-wide default (which would
make every *other* call wait just as long before a real bug is ever
detected). The effective timeout for a given call -- server default or
per-call override -- is resolved once, in `submit()`, and carried on the
`_PendingRequest` itself, so a requeued retry reuses the exact same
timeout its first attempt used rather than silently re-resolving against
a `new_game`-updated server default mid-flight.

Interaction with `new_game`: a timed-out-and-requeued request lives in a
specific `ExecuteCodeBotAI` instance's `self._queue`, tied to that
instance's `on_step` loop. If `new_game` cancels that game entirely while
a retry is still queued (or in flight), the existing `submit()`/
`game_task`-racing logic (see `submit()`'s docstring) already produces a
clean "match ended" result for it -- exactly the same path any other
in-flight call takes during a `new_game`-triggered cancellation, since
`game_task.cancel()` only ever propagates a plain `CancelledError`
(distinct from the `TimeoutError` `wait_for` raises on an ordinary
timeout) out through `on_step`, past the retry logic entirely, and into
`_host_game`'s own cleanup. Nothing extra was needed for this case.

Standing background tasks: `start_task`/`task_status`/`cancel_task`
----------------------------------------------------------------------
The problem this solves: an open-ended, goal-directed instruction like
"have an SCV build Supply Depots until we have 30" can legitimately take
many real minutes (waiting on mineral income, one depot at a time) --
which is exactly the kind of "legitimately slow, not a bug" case the
per-call timeout above is deliberately generous about, but a *single*
`execute_code` call still isn't the right shape for it: either the
snippet loops internally until the goal is met (in which case it looks
identical to a runaway snippet from `on_step`'s perspective, and either
gets killed by the timeout or -- worse -- requires the caller to pass a
huge `timeout_seconds` override for this one goal, which the project
explicitly rejected: "arbitrary flagging of tasks with different cooldown
is very imprecise"), or the calling agent has to re-issue `execute_code`
itself in a polling loop, which blocks that agent's own turn for just as
long. Neither lets other `execute_code` calls (e.g. a human checking in
on a completely different part of the game) get serviced while the goal
is pending.

The fix implemented here is NOT a second, concurrently-scheduled
execution path -- see this module's "Concurrency/communication design"
section above for why that would be unsafe (two coroutines calling into
`self.bot`/`self.sdk` at genuinely overlapping times, racing on
python-sc2's unsynchronized internal state). Instead, a background task is
just a different *kind* of item on the exact same `self._queue` `on_step`
already drains one at a time:

  - `start_task(code, description, max_iterations)` registers a
    `_TaskState` (in `self._tasks`, keyed by a short incrementing
    `task_id`) and immediately enqueues ONE turn for it -- a
    `_PendingRequest` whose `task_id` is set (see `_PendingRequest`'s
    docstring) instead of carrying a caller-facing `future` -- via
    `self._queue.put_nowait(...)`. `put_nowait` never blocks (this
    module's queues are unbounded, same as `submit()`'s `await
    self._queue.put(...)`, which also never actually suspends), so
    `start_task` returns to its caller the instant the first turn is
    queued, without waiting for that turn -- let alone the whole goal --
    to run. This is what makes it "not just `execute_code` with a bigger
    timeout": the goal can take as many real-world turns as it needs,
    each one bounded by the ordinary per-call timeout, while the
    `start_task` call itself returns in microseconds.
  - Each turn is `code` -- ONE snippet, evaluated repeatedly, doing ONE
    bounded chunk of work per turn (check progress, do a small amount of
    work if the goal isn't met yet, or wait a tick if not yet
    affordable), then signal "keep going" or "goal met" via its return
    value -- reusing `_eval_snippet`'s *existing* "trailing expression (or
    an explicit `return`, since the snippet's body literally becomes a
    real `async def` function body -- nothing new needed for that to
    work) becomes the result" convention verbatim. No new
    snippet-evaluation semantics: the only new thing is a *convention* for
    what a task's return value means -- `True` (or any truthy value) ends
    the task successfully; `False`/falsy keeps it going. Concretely, the
    user's own supply-depot scenario as one task's `code`:

        from sc2.ids.unit_typeid import UnitTypeId

        depot_count = len(sdk.structures(UnitTypeId.SUPPLYDEPOT))
        if depot_count >= 30:
            return True
        if sdk.can_afford(UnitTypeId.SUPPLYDEPOT):
            depot_point = sdk.townhalls.first.position.towards(sdk.game_info.map_center, 6)
            depot_point = depot_point.offset((depot_count * 3, 0))
            await bot.build(UnitTypeId.SUPPLYDEPOT, near=depot_point)
        else:
            await bot._advance(22)
        return False

    registered via `start_task(code=..., description="SCVs build Supply
    Depots until we have 30")`. Each turn either builds one more depot (if
    affordable) or waits one tick for minerals, then reports `False` to
    request another turn -- until `depot_count >= 30`, when a turn
    finally returns `True` and the task is marked done. Every turn is
    independently subject to the same `asyncio.wait_for` timeout + single
    retry ordinary `execute_code` calls get (see the section above) --
    reused, not reimplemented (see `on_step` below) -- so a turn that
    itself hangs (e.g. a bug in the task's own code) is caught exactly
    like a hung ordinary snippet would be, it just marks the *task* failed
    afterward instead of resolving a caller's future.
  - `on_step` doesn't care whether the `_PendingRequest` it just dequeued
    came from `submit()` (ordinary `execute_code`) or `start_task`
    (a task turn) -- it runs `_eval_snippet` under the identical
    `asyncio.wait_for(..., timeout=request.timeout_seconds)` either way.
    Only what happens *after* that differs, and it's a single `if
    request.task_id is not None: ... else: ...` branch at each of the two
    existing resolution points (the timeout-exhausted branch and the
    normal-completion branch) -- see `_finish_task_turn`. This is what
    "reuses the existing timeout/retry machinery rather than forking a
    parallel implementation" means concretely: there is exactly one
    `asyncio.wait_for(_eval_snippet(...))` call site in this whole module,
    used by both kinds of request.
  - `_finish_task_turn` (called from `on_step` once a turn's outcome --
    success, ordinary failure, or both-attempts-timed-out -- is known)
    updates the task's bookkeeping and decides what happens next: a
    truthy result marks the task `"done"`; an `ok=False` result (an
    exception, or a timeout that exhausted its retry) marks it `"failed"`
    and stops -- a broken task does not spin forever; a falsy-but-`ok`
    result means "keep going", which either re-enqueues one more turn (if
    a `cancel_task` call hasn't set the task's `cancel_requested` flag,
    and `max_iterations` hasn't been reached) or ends the task as
    `"cancelled"`/`"failed"` (max-iterations-exhausted) instead. Because
    at most one turn per task is EVER sitting in `self._queue` or actively
    running at a time (a task's next turn is only enqueued from inside
    `_finish_task_turn`, i.e. strictly after the previous turn's result is
    already known), `cancel_task` never needs to interrupt an in-flight
    turn -- it just flips `cancel_requested`, and the in-flight (or
    already-queued) turn's own eventual `_finish_task_turn` call is
    guaranteed to be the next -- and only -- place that flag is ever
    consulted for that task.
  - Progress is pull-based: `task_status(task_id)` reports the task's
    current `status`/`iterations`/a capped recent-activity `log` (see
    `_TASK_LOG_MAX_ENTRIES`) /`result`/`error` on demand. Nothing pushes
    updates to a caller -- MCP tools are request/response, so there is no
    server-initiated notification channel to build here; a caller
    interested in a long-running task's progress polls `task_status`
    instead, the same way a human might glance at a build order in
    progress.
  - Lifecycle: tasks live in `self._tasks` on a specific `ExecuteCodeBotAI`
    instance, exactly like `self._queue` already does. `new_game` (see
    `build_server`) replaces `active.bot_ai` with a brand new
    `ExecuteCodeBotAI` (a fresh, empty `self._tasks`) and only swaps that
    reference in after the OLD game's `game_task` has been cancelled and
    fully awaited -- so by the time any tool call could observe the new
    `active.bot_ai`, the old one's `on_step` loop is provably not running
    anymore, and never will again (see `new_game`'s docstring in
    `build_server` for why cancellation propagates a plain
    `CancelledError` straight out of `on_step`, bypassing every branch
    discussed above -- `_finish_task_turn` is never reached for a turn
    caught mid-flight by a `new_game`-triggered cancellation, so that
    turn, and the task it belonged to, simply stop existing along with
    the old `ExecuteCodeBotAI` object itself, with nothing further to
    clean up). Concretely: a task does NOT survive `new_game` -- its
    `task_id` becomes unresolvable (`task_status` reports "unknown
    task_id", the same way it would for a `task_id` that was never
    registered at all) the moment a new game replaces the one the task
    belonged to, with no special-casing required anywhere in this
    mechanism beyond what `new_game`/`_ActiveGame` already do for an
    ordinary in-flight `execute_code` call.

### `DEFAULT_MAP`

```python
DEFAULT_MAP = 'AutomatonLE'
```

Same fixed test map the rest of the project's harnesses default to.

### `DEFAULT_SNIPPET_TIMEOUT_SECONDS`

```python
DEFAULT_SNIPPET_TIMEOUT_SECONDS = 45.0
```

Default per-`execute_code`-call timeout (in seconds), used by `on_step`'s `asyncio.wait_for` around `_eval_snippet` -- see the module docstring's "Per-call timeout and single automatic retry" section for the full mechanism this guards. 45 seconds: generous enough (tens of seconds, not single-digit seconds) that an ordinary realtime resource-wait loop -- e.g. `while not sdk.can_afford(X): await bot._advance(22)` waiting for minerals to accumulate under normal income -- comfortably finishes well within it, while still bounding a genuinely wedged snippet (like the real incident this feature was built for: a supply-cap bug that turned an `await bot._advance(22)` loop into an infinite one) to under a minute times two attempts, instead of hanging `on_step` -- and therefore the whole game -- forever. A snippet known in advance to legitimately need longer than this (e.g. a genuinely slow-income minerals wait that can take upwards of a minute) should pass its own `timeout_seconds` to `execute_code` rather than raising this server-wide default, which would make every *other* call wait just as long before a real bug is ever detected. See `_GameConfig.snippet_timeout_seconds` for how this default is overridden per-server (`--snippet-timeout`) and persists across `new_game` calls.

### `DEFAULT_TASK_MAX_ITERATIONS`

```python
DEFAULT_TASK_MAX_ITERATIONS = 1000
```

Default `start_task` `max_iterations` -- see the module docstring's "Standing background tasks" section for the overall mechanism. This bounds how many turns a background task will run before giving up as `"failed"` (exhausted) if its step code's trailing return value never evaluates truthy -- a backstop against a task whose goal is simply never reachable (e.g. a step-code bug that always reports "keep going", or a genuinely unreachable target like more depots than the map's supply cap allows) spinning forever, consuming one queued turn after another indefinitely. 1000 is deliberately generous: even a slow resource-bound goal like "Supply Depots until 30" (the module docstring's worked example) needs at most a few dozen turns, so 1000 leaves ample headroom for legitimately larger goals without meaningfully risking an actually-stuck task running "forever" in practice -- a caller with a goal it knows needs more turns than this can simply pass a larger `max_iterations` to `start_task` explicitly.

### `DEFAULT_LOCKFILE_PATH`

```python
DEFAULT_LOCKFILE_PATH = Path(tempfile.gettempdir()) / 'sc2-sdk-mcp.lock'
```

Where this process records "I am the current sc2-sdk-mcp instance, and here are the SC2 client PID(s) I explicitly spawned" -- read by the next sc2-sdk-mcp startup's single-instance guard to find and clean up a stale prior instance. A namespaced file under the system temp dir: this project has no existing convention for its own local/runtime state (`install.paths` is about locating the SC2 *client* install, a different concern -- see this section's docstring above), so this is a reasonable default rather than inventing a project-specific state directory for one small file.

### class `ExecuteCodeResult`

What one `execute_code` call reports back.

Mirrors the `ok`/`error` shape `outcomes.py` already established for
`bot.*` actions, extended with `stdout` (anything the snippet printed)
and `result` (`repr()` of the snippet's trailing-expression value, if
any -- see module docstring). `result`/`stdout`/`error` are kept as
plain strings rather than raw Python objects because this is what
crosses the MCP tool boundary as the JSON response body.

**Fields:**

- `ok: bool`
- `result: str | None`
- `stdout: str`
- `error: str | None`
- `traceback: str | None`

### class `ExecuteCodeBotAI`

A `VerifiedBotAI` (see `bot.py`) whose `on_step` blocks on an
external snippet queue instead of running a fixed scripted sequence --
see module docstring for why this is what makes the game "pause"
between `execute_code` calls.

`game_task` is set by `serve_execute_code()` once it has created the
task driving this bot through `_host_game` -- `submit()` races the
caller's own future against it so a snippet submitted after the match
has already ended reports a clear error instead of hanging forever
waiting for an `on_step` call that will never come again.

#### `ExecuteCodeBotAI.__init__`

```python
def __init__(self, default_snippet_timeout_seconds: float=DEFAULT_SNIPPET_TIMEOUT_SECONDS) -> None
```

#### `ExecuteCodeBotAI.on_start`

```python
async def on_start(self) -> None
```

#### `ExecuteCodeBotAI.on_step`

```python
async def on_step(self, iteration: int) -> None
```

#### `ExecuteCodeBotAI.start_task`

```python
def start_task(self, code: str, description: str, max_iterations: int=DEFAULT_TASK_MAX_ITERATIONS) -> str
```

Registers a new background task and enqueues its first turn --
see the `start_task` MCP tool (in `build_server`) for the
caller-facing contract, and the module docstring's "Standing
background tasks" section for the overall mechanism. Synchronous
and non-blocking (no `await` anywhere in this method): the queue
put is a `put_nowait` (see `_enqueue_task_turn`), so this returns
to its caller -- the `start_task` MCP tool, which does nothing
else after this call but package the `task_id` into its response
-- before the first turn has even had a chance to run, let alone
the whole goal complete.

#### `ExecuteCodeBotAI.cancel_task`

```python
def cancel_task(self, task_id: str) -> dict[str, object]
```

Requests that `task_id` stop scheduling further turns -- see the
`cancel_task` MCP tool (in `build_server`) for the caller-facing
contract. Only ever sets a flag (`_TaskState.cancel_requested`);
never touches `self._queue` or attempts to interrupt a turn
that's already running or already queued -- see `_finish_task_turn`'s
docstring for why checking the flag between turns is sufficient
and correct, and the module docstring's "Standing background
tasks" section for why interrupting an in-flight turn is
deliberately out of scope.

#### `ExecuteCodeBotAI.list_task_ids`

```python
def list_task_ids(self) -> 'list[str]'
```

Every `task_id` currently registered against this game instance,
in registration order -- backs `task_status()`'s no-argument
"list everything" form (see `build_server`). A thin, read-only view
over `self._tasks`'s keys (a `dict`, so insertion order is already
preserved) rather than exposing `self._tasks` itself, keeping this
class's internal bookkeeping structure private to it.

#### `ExecuteCodeBotAI.task_status`

```python
def task_status(self, task_id: str) -> dict[str, object]
```

Snapshots `task_id`'s current bookkeeping into a plain dict --
see the `task_status` MCP tool (in `build_server`) for the
caller-facing contract. Called fresh on every invocation (no
caching): `self._tasks[task_id]` is mutated in place by
`_finish_task_turn` as each turn completes, so this always reflects
genuinely current progress, including while the task is still
`"running"` -- not just a snapshot taken once at `start_task` time
or only once the task finishes.

#### `ExecuteCodeBotAI.submit`

```python
async def submit(self, code: str, timeout_seconds: float | None=None) -> ExecuteCodeResult
```

Called by the `execute_code` MCP tool handler: enqueue `code`
for the next `on_step` to run, and wait for its result -- or for a
clear error if the match ends before that happens.

`timeout_seconds`, if given, overrides `self.default_snippet_timeout_seconds`
for this call only -- resolved to a concrete float right here, once,
and carried on the `_PendingRequest` (see its docstring for why),
so a first-timeout retry (see `on_step`) reuses this same call's
chosen timeout rather than re-resolving against the server default.

### class `ExecuteCodeSession`

What `serve_execute_code()` hands back: the live `FastMCP` server
plus the `_ActiveGame` it's wired to, so a caller (a console-script
entrypoint, or a test) can both serve the MCP tool and separately
inspect/await the underlying game.

`bot_ai`/`game_task` are properties, not plain fields, proxying
through `active`: after a caller invokes the `new_game` tool (see
`build_server`), `active.bot_ai`/`active.game_task` get reassigned to
the new game -- these properties make `session.bot_ai`/
`session.game_task` always reflect "whichever game is current", the
same thing `execute_code` itself now talks to, rather than freezing at
whatever game existed when this session was first constructed.

**Fields:**

- `mcp: FastMCP`
- `active: _ActiveGame`

#### `ExecuteCodeSession.bot_ai`

```python
@property
def bot_ai(self) -> ExecuteCodeBotAI
```

#### `ExecuteCodeSession.game_task`

```python
@property
def game_task(self) -> 'asyncio.Task[Result]'
```

### `build_server`

```python
def build_server(active: _ActiveGame, defaults: _GameConfig, name: str='sc2-sdk') -> FastMCP
```

Build a `FastMCP` server exposing `execute_code`, `new_game`, and
the standing-background-task trio `start_task`/`task_status`/
`cancel_task` (see the module docstring's "Standing background tasks"
section) against `active` -- all tools are defined once here and
read/write `active.bot_ai`/`active.game_task` at call time rather than
closing over a value fixed at construction time, so `new_game` can
swap the game every other tool talks to without rebuilding this server
(see `_ActiveGame`'s docstring for why that indirection is needed at
all). Nothing about wiring any of these tools depends on `active.bot_ai`
being a real, game-backed `ExecuteCodeBotAI` -- see `serve_execute_code()`
below for how the real entrypoint constructs one.

### `serve_execute_code`

```python
async def serve_execute_code(map_name: str=DEFAULT_MAP, my_race: Race=Race.Terran, opponent_race: Race=Race.Random, difficulty: Difficulty=Difficulty.Easy, game_time_limit: int | None=None, realtime: bool=False, snippet_timeout_seconds: float=DEFAULT_SNIPPET_TIMEOUT_SECONDS) -> ExecuteCodeSession
```

Launch a real game against the built-in AI -- stepped (paused
between calls) by default, or wall-clock speed if `realtime=True`, see
`_build_session` -- and return an `ExecuteCodeSession` wrapping a
`FastMCP` server whose `execute_code` tool is live against it, once the
game has actually loaded and is ready for calls -- callers that need a
session back only once it's fully live (e.g. the wiring test below)
should use this; `main()` does not, see `_build_session`.

Does not itself serve any transport -- callers decide that. The
console-script entrypoint (`main()` below) serves it over stdio, the
same way any other MCP server does for a real client; the wiring test
instead uses the official SDK's own in-memory
`mcp.shared.memory.create_connected_server_and_client_session` transport
to drive a real `mcp.client.session.ClientSession` against it on the
same event loop -- a real client/server JSON-RPC exchange, just not
piped through a subprocess's stdio, since the whole point under test is
the same-event-loop pause/step wiring, not stdio framing.

### `main`

```python
def main(argv: list[str] | None=None) -> None
```

Console-script entrypoint (`sc2-sdk-mcp`, see pyproject.toml):
launch a real game against the built-in AI and serve `execute_code`
over stdio for a real MCP client (e.g. an LLM coding agent) to drive.

Runs `_run_single_instance_guard()` first, before parsing anything else
into a running game or serving any MCP traffic -- see this module's
"Single-instance guard + explicit SC2-client PID tracking" section for
why this exists: a stale prior `sc2-sdk-mcp` process (and its own SC2
client) left running from an earlier `/mcp` reconnect is found and
terminated here, synchronously, before this process does anything else.

The guard's lockfile path is `DEFAULT_LOCKFILE_PATH` unless `--multiplayer
INSTANCE_ID` was passed, in which case it's scoped to that id instead
(see `_lockfile_path_for`) -- letting two `--multiplayer`-launched
instances (e.g. one hosting, one joining a two-LLM match on the same
machine) coexist without either one's guard terminating the other.

## `sdk.join`

*Source: [`src/sdk/join.py`](../src/sdk/join.py)*

Ticket #11 (https://github.com/blokboy/sc2-sdk/issues/11): proof that the
SC2 engine's own join-game handshake works when a "join"-role client is
pointed at an explicit host address+port, instead of relying on
`sc2.main.run_game`'s single-call, single-process orchestration of both
sides. This is the foundational risk check underneath the planned host/join
1v1 feature (see the sibling "Host/join 1v1" ticket): if the primitive
proven here didn't work, that ticket's design would need to change before
being built.

What was actually confirmed
----------------------------
Reading python-sc2's `sc2/main.py` shows that `run_game()`'s Bot-vs-Bot path
(`sum(isinstance(p, (Human, Bot)) ...) > 1`) is *already* two independent
coroutines, `_host_game` and `_join_game`, that only happen to be scheduled
together via a single `asyncio.gather(...)` inside one `asyncio.run()` call.
Each one launches its own `SC2Process` (a genuinely separate OS child
process -- see `sc2/sc2process.py`) and only ever talks to *that* local
client over its own private loopback websocket (the "API port", e.g.
`ws://127.0.0.1:<picked-port>/sc2api`); the two SC2 engines themselves then
find each other over a *second*, independent set of ports (`Portconfig`,
below) using the game's own LAN networking, not the API websocket. That
second channel is where cross-machine join would have to happen, and
`s2clientprotocol.sc2api_pb2.RequestJoinGame` does carry the field for it:

    optional string host_ip = 8;  // Both game creator and joiner should
    // provide the ip address of the game creator in order to play
    // remotely. Defaults to localhost.

(comment copied verbatim from Blizzard/s2client-proto's `sc2api.proto`).
`sc2.client.Client.join_game()` builds a `RequestJoinGame` but never sets
`host_ip` -- there's no public python-sc2 API to reach it. `_join_game_at`
below fills that gap by constructing the same request `Client.join_game()`
would, plus `host_ip`, and executing it directly.

So: the primitive works as hoped, generalizes cleanly beyond `run_game`, and
required no protocol-level surgery -- just exposing one already-defined
proto field python-sc2 doesn't surface. Confirmed empirically, twice, in a
real (QEMU-emulated x86_64, per this repo's Docker path) headless Linux SC2
install: (1) `host_ip="127.0.0.1"` on both sides -- `run_join_game_match`
below, launched as two real `python -m sdk.join` OS processes with no
shared asyncio loop -- reaches a real match `Result` on both sides in
~3.75 minutes wall-clock for a 20-minute in-game safety cap; (2) as a
negative control, pointing the host side's `host_ip` at an unreachable
address (`10.255.255.1`) instead of `127.0.0.1` makes the host's own
`join_game` call hang indefinitely (it times out under
`run_join_game_match`'s `process_timeout`, never printing a `Result`) --
proving `host_ip` is actually load-bearing for the handshake, not a field
python-sc2/the engine silently ignores. `Portconfig.as_json`/`.from_json`
(see `sc2/portconfig.py`) already exist for exactly this cross-process
scenario -- they're what the AI-Arena/sc2ai ladder ecosystem's `BotProcess`
convention (`--LadderServer`/`--GamePort`/`--StartPort`, see `sc2/player.py`)
uses to hand identical port assignments to independently-launched bot
processes. That's strong independent evidence this exact split (one
process per side, ports agreed out of band) is a first-class, intended use
of the underlying API, not something being bent to a new purpose.

Surprises vs. the design assumption
------------------------------------
- `RequestCreateGame`'s player_setup for `Participant` slots (as opposed to
  `Computer`) carries no `race` field at all (see `sc2.controller.
  Controller.create_game`) -- race is only decided per side, at join time,
  in each side's own `RequestJoinGame.race`. So the host process does not
  need to know the joining side's race up front; the two sides are more
  independent than a first reading of `run_game`'s single `players` list
  suggests.
- `join_game`'s websocket round-trip (`Protocol._execute`) returns only
  once, well after the request is sent, with no separate "waiting for
  peer" signal in between -- i.e. the local engine's response to
  `RequestJoinGame` blocks until the actual LAN handshake with the peer
  engine resolves. That's *why* `run_game`'s existing Bot-vs-Bot path gets
  away with zero manual synchronization between `_host_game` and
  `_join_game` beyond starting both concurrently: the ordering/retry logic
  already lives inside the SC2 engines' own networking, invisible to the
  Python driving code. Splitting the two roles into genuinely separate OS
  processes below relies on that exact same engine-level behavior, which is
  indifferent to whether the two callers are asyncio tasks in one process
  or two unrelated `python -m sdk.join` invocations -- the engines only
  ever see socket traffic, never Python call stacks.
- Threads (each with their own asyncio loop, one option this ticket's brief
  allowed) turn out not to work cleanly here: `SC2Process.__aenter__`
  unconditionally calls `signal.signal(signal.SIGINT, ...)`
  (`sc2/sc2process.py`), and Python only allows `signal.signal()` from the
  interpreter's main thread. Launching a second `SC2Process` from a
  background thread raises `ValueError: signal only works in main thread of
  the main interpreter`. Real subprocesses (used below) sidestep this
  entirely -- each child's `SC2Process` runs on the main thread of its own
  process -- and match "two independent processes" more literally anyway.

What would differ for a genuinely remote (non-loopback) address
------------------------------------------------------------------
Not tested here (out of scope per the ticket), but reasoning from the
above:
  - `host_ip` would need to be the host machine's real routable address
    (LAN IP, or a tunnel/VPN endpoint), and the `Portconfig` ports (4 TCP
    ports: `server` pair + one `players` pair per guest) would need to be
    reachable from the joining machine -- i.e. open through any firewall
    and NAT'd/forwarded if the host isn't on a public or VPN-shared
    address. This is exactly the same requirement Blizzard's own ladder
    infrastructure (AI-Arena/sc2ai) documents for `BotProcess`-style
    external matches.
  - `RequestCreateGame.local_map` is a *local* filesystem path resolved
    independently by each engine (see `Controller.create_game`) -- there is
    no map-transfer step in this protocol. Both machines need the identical
    map file already present locally (this project's `install.maps` sync
    step handles that today, but only within one machine's install; a real
    host/join feature would need to guarantee both sides' map pools agree,
    e.g. by shipping/verifying the same fixed map pool on both sides rather
    than assuming it).
  - Nothing else in this module is loopback-specific: `host_ip` is a plain
    string passed straight into the proto request, so a real address would
    flow through unchanged -- the mechanism itself does not need to change,
    only what's on the other end of it.

Design of this module
----------------------
`_run_host_role`/`_run_join_role` are standalone coroutines, each just a
`SC2Process` + our own `host_ip`-aware join + python-sc2's own
`sc2.main._play_game_ai` step loop (reused as-is, unmodified, the same way
`sdk.mcp_server` already reaches into `sc2.main._host_game` for a
structural reason the public API doesn't cover -- see that module's
docstring for the precedent). `python -m sdk.join --role host|join ...`
(the `main()` below) is a thin CLI wrapper around each, so
`run_join_game_match()` can launch host and join as two real,
independently-scheduled OS processes (`subprocess.Popen`, not
`asyncio.gather` in one process) and still recover each side's `Result`.
This is deliberately a new, separate module rather than a change to
`sdk.play`/`sdk.runtime`: those launch python-sc2's `run_game()` directly
and this ticket's brief asks not to touch them.

### `DEFAULT_MAP`

```python
DEFAULT_MAP = 'AutomatonLE'
```

Same fixed test map this project's other harnesses default to.

### `DEFAULT_HOST_IP`

```python
DEFAULT_HOST_IP = '127.0.0.1'
```

Loopback proof only (see this module's docstring) -- the ticket's own acceptance criteria call out that real network/NAT traversal is out of scope, only that the *mechanism* (an explicit host_ip) generalizes.

### `run_join_game_match`

```python
def run_join_game_match(map_name: str=DEFAULT_MAP, host_race: Race=Race.Terran, join_race: Race=Race.Zerg, host_ip: str=DEFAULT_HOST_IP, realtime: bool=False, game_time_limit: int | None=None, process_timeout: float=600.0) -> tuple[Result, Result]
```

Launch a host-role and a join-role SC2 client as two independent,
concurrently-running OS processes (`python -m sdk.join`, see `main()`
below) and run one real 2-player match to completion between them.

This is the demoable code path the ticket asks for: unlike
`sdk.play.play_vs_builtin_ai`/`sdk.runtime.run_bot_vs_builtin_ai`
(both of which call python-sc2's `run_game()`, which spawns and wires
up both sides itself from one call), the two sides here are started as
two separate `subprocess.Popen` invocations. Neither process's asyncio
loop is aware the other exists as anything but "some peer reachable at
`host_ip` + the ports in `portconfig`" -- the same information a
genuinely different machine's join-role process would need.

Args:
    map_name: name of a synced map (see install.maps.DEFAULT_MAPS),
        resolved independently by each subprocess's own SC2 install.
    host_race: race the host-role side plays.
    join_race: race the join-role side plays.
    host_ip: address the join side is told to connect to, and the host
        side is told it's reachable at. `127.0.0.1` (default) proves
        the mechanism on one machine; see this module's docstring for
        what would need to hold for a genuinely remote address.
    realtime: if False (default), each side steps only as fast as its
        (trivial) bot responds.
    game_time_limit: optional wall-clock-independent safety cap, in
        in-game seconds, passed to both sides.
    process_timeout: wall-clock safety cap, in seconds, for each
        subprocess -- distinct from `game_time_limit` (which caps
        *in-game* time and is enforced by python-sc2 itself); this one
        guards against a subprocess never starting/connecting at all.

Returns:
    (host_result, join_result) -- each side's own `sc2.data.Result` for
    the match, exactly as each side's own `_play_game_ai` call
    observed it.

### `main`

```python
def main(argv: list[str] | None=None) -> int
```

## `sdk.matchcode`

*Source: [`src/sdk/matchcode.py`](../src/sdk/matchcode.py)*

Ticket #12 (https://github.com/blokboy/sc2-sdk/issues/12): the shareable
"match code" a host prints and a joiner pastes, plus the other pure,
SC2-client-free logic the host/join CLI needs (race-conflict resolution, a
generic join-wait timeout wrapper).

Kept separate from `sdk.join` (ticket #11's proven host_ip/portconfig
primitive) so that module's already-verified content stays untouched, per
its own docstring's ground rules.

Why the code carries a `token` that nothing ever checks
---------------------------------------------------------
The raw SC2 engine join handshake (see `sdk.join`'s docstring) has no
app-level authentication concept at all -- only `host_ip` and port numbers.
`Portconfig` also picks random ports per process, so the joiner has no way
to reach the host without already knowing those ports. That means the
match code itself -- specifically its `portconfig` payload -- is already an
unguessable shared secret: anyone who can decode a valid code already has
everything they'd need to join, `token` or not. Building a real
"reject-an-incorrect-token" check would require a separate rendezvous
server the joiner talks to *before* the code lets them do anything (a
distinct, larger feature this ticket deliberately doesn't build -- see the
ticket's discussion). `token` is carried through encode/decode so a future
rendezvous layer has a field to check, but today it is exactly as secret,
and exactly as unenforced, as the ports next to it.

### class `JoinTimeoutError`

No joiner connected within the host's configured wait window.

### class `MatchCode`

**Fields:**

- `host_ip: str`
- `portconfig: Portconfig`
- `map_name: str`
- `race_pin: Race | None`
- `token: str`

### `encode_match_code`

```python
def encode_match_code(host_ip: str, portconfig: Portconfig, map_name: str, race_pin: Race | None, token: str) -> str
```

Pack a host's connection info into a single copy-pasteable string.

`portconfig` is stored via its own `as_json` (see `sc2.portconfig`) --
not reconstructed field-by-field -- so this stays correct if that
format ever changes.

### `decode_match_code`

```python
def decode_match_code(code: str) -> MatchCode
```

### `resolve_race`

```python
def resolve_race(host_pin: Race | None, joiner_race: Race | None) -> Race
```

The joiner's explicit choice always wins over a host-pinned race --
see ticket #12's design discussion: the host is authoritative over the
map and whether to pin a race at all, but a joiner who states a race
has final say over their own side. Falls back to the host's pin if the
joiner didn't choose one, and to `Race.Random` if neither did.

### `wait_for_joiner`

```python
async def wait_for_joiner(awaitable: Awaitable[_T], timeout: float) -> _T
```

Bound how long the host waits for a joiner (ticket #12's timeout
acceptance criterion) around whatever awaitable actually represents
"a joiner showed up" in production -- the host's own blocking
`join_game` call (see `sdk.join`'s docstring: that call doesn't return
until the peer's engine handshake resolves, so wrapping it in a plain
`asyncio.wait_for` is sufficient; there's no separate "waiting" signal
to hook into). Kept generic and given a stub awaitable in tests so this
seam doesn't require a real SC2 client.

## `sdk.host_join`

*Source: [`src/sdk/host_join.py`](../src/sdk/host_join.py)*

Ticket #12 (https://github.com/blokboy/sc2-sdk/issues/12): `sc2-sdk-host`
and `sc2-sdk-join <code>`, the CLI pair for a private 1v1 between two
agent-controlled sides on separate machines, each running their own local
SC2 client.

Built entirely on already-proven pieces rather than new protocol work:
`sdk.join`'s `_run_host_role`/`_run_join_role` (ticket #11's proven
host_ip-aware join primitive, now with an optional `join_timeout`) and
`sdk.matchcode` (the shareable code format, race-conflict resolution, and
the timeout wrapper -- see that module's docstring for why the code's
`token` field isn't independently verified: the underlying SC2 protocol has
no authentication concept to check it against, so the code's `portconfig`
payload is already the actual shared secret).

Each side plays via a bot script, the same `bots/<name>.py` convention
`sc2-sdk-run-bot`/`sc2-sdk-selfplay` already use (reusing
`sdk.script_runner`'s discovery rather than duplicating it) -- this is what
"each side's human interacts via the existing local stdio execute_code/
run-bot mechanism, unchanged" (the ticket's own framing) means in practice:
nothing new is invented for *how* a side plays, only for how the two sides'
already-local clients find each other.

### `DEFAULT_MAP`

```python
DEFAULT_MAP = 'AutomatonLE'
```

### `DEFAULT_JOIN_TIMEOUT`

```python
DEFAULT_JOIN_TIMEOUT = 300.0
```

### `main_host`

```python
def main_host(argv: list[str] | None=None) -> int
```

### `main_join`

```python
def main_join(argv: list[str] | None=None) -> int
```

## `install.cli`

*Source: [`src/install/cli.py`](../src/install/cli.py)*

`setup` entry point: get a working local SC2 client + map pool with one
command, no Battle.net/GUI required.

Order of operations (mirrors the ticket's acceptance criteria):
  1. Detect an existing Battle.net install (Windows/Mac) -- use it if found.
  2. Otherwise, on Linux, install Blizzard's headless package.
  3. Otherwise (Mac/Windows with no Battle.net install), guide the user
     through installing it via Battle.net, then poll for it -- see
     `_prompt_for_battlenet_install`. Blizzard provides no supported
     silent/scriptable installer for Battle.net-managed SC2 (unlike the
     Linux headless package, which is an explicit, documented
     CI/automation artifact), so this waits for a human to finish an
     interactive install elsewhere rather than trying to automate it.
     Skipped entirely (falls straight to the actionable-error message
     below) when the `CI` env var is set.
  4. Sync the fixed map pool onto whichever install was selected.

Usage:
    python -m install.cli
    sc2-sdk-setup  (after `pip install -e .`)

### `BATTLENET_DOWNLOAD_URL`

```python
BATTLENET_DOWNLOAD_URL = 'https://starcraft2.com'
```

### `main`

```python
def main(argv: list[str] | None=None) -> int
```

## `install.battlenet`

*Source: [`src/install/battlenet.py`](../src/install/battlenet.py)*

Detection of an existing Battle.net-managed SC2 install (Windows/Mac).

`setup` prefers this over installing the headless Linux package: if a user
already has SC2 via Battle.net, re-installing would be wasteful and would
prevent them from watching games in the rendered client.

### `BATTLENET_PLATFORMS`

```python
BATTLENET_PLATFORMS = ('Windows', 'Darwin')
```

Battle.net only exists on these platforms; Linux has no Battle.net client.

### `detect_battlenet_install`

```python
def detect_battlenet_install(pf: str | None=None) -> Sc2Installation | None
```

Return the existing Battle.net install, if one is present and valid.

Checks (in order): Battle.net's own ExecuteInfo.txt record of the active
install, then the platform's conventional default install directory
(covers the case where ExecuteInfo.txt is missing/stale but the game is
still installed at the default location). Returns ``None`` on Linux,
where there is no Battle.net client, or if nothing valid is found.

## `install.headless`

*Source: [`src/install/headless.py`](../src/install/headless.py)*

Install Blizzard's official headless Linux SC2 package.

This is the client Blizzard distributes specifically for AI/bot development:
no Battle.net launcher, no GUI, no display required. It is documented (with
exact download URLs and the shared unzip password) in the "Downloads" section
of https://github.com/Blizzard/s2client-proto -- the packages themselves are
served from Blizzard's Akamai-fronted distribution host.

Reference implementation this mirrors: the official BurnySc2/python-sc2-docker
Dockerfile, which does the Linux-only equivalent of this in shell:
    wget http://blzdistsc2-a.akamaihd.net/Linux/SC2.$VERSION.zip
    unzip -P iagreetotheeula SC2.$VERSION.zip

### `DEFAULT_SC2_VERSION`

```python
DEFAULT_SC2_VERSION = '4.10'
```

Latest headless package listed under "Linux Packages" in https://github.com/Blizzard/s2client-proto#downloads as of this writing.

### `UNZIP_PASSWORD`

```python
UNZIP_PASSWORD = b'iagreetotheeula'
```

Every headless/map package Blizzard distributes this way shares this password -- it exists only to gate the "AI and Machine Learning use" license acknowledgement, not to keep the content secret.

### `DEFAULT_LINUX_INSTALL_DIR`

```python
DEFAULT_LINUX_INSTALL_DIR = Path('~/StarCraftII').expanduser()
```

Matches python-sc2's own Linux default (sc2.paths.BASEDIR["Linux"]), so a headless install lands wherever python-sc2 would look for it with no SC2PATH override required.

### class `UnsupportedPlatformError`

Raised when asked to install the headless Linux package on a
non-Linux host. The package is a native Linux ELF binary; it cannot run
under macOS or Windows, so there is nothing this function can usefully do
there. On those platforms, `setup` should rely on Battle.net detection
instead (see install.battlenet).

### class `HeadlessInstallError`

Raised when the download or extraction did not produce a valid
install (e.g. network failure, corrupted archive, unexpected layout).

### class `DownloadResult`

**Fields:**

- `url: str`
- `bytes_written: int`

### `install_headless_linux`

```python
def install_headless_linux(dest: Path | None=None, version: str=DEFAULT_SC2_VERSION, force: bool=False, downloader=_download) -> Sc2Installation
```

Download and extract Blizzard's headless Linux SC2 package.

Args:
    dest: install directory. Defaults to ``~/StarCraftII`` -- the same
        default python-sc2 uses on Linux, so no SC2PATH override is
        needed afterwards.
    version: package version suffix, e.g. "4.10" -> SC2.4.10.zip.
    force: re-download/extract even if a valid install already exists at
        ``dest``.
    downloader: injectable for testing; takes a URL, returns raw bytes.
        Defaults to a real HTTP GET.

Raises:
    UnsupportedPlatformError: if not running on Linux.
    HeadlessInstallError: if the download/extraction didn't produce a
        valid install.

## `install.maps`

*Source: [`src/install/maps.py`](../src/install/maps.py)*

Sync the fixed map pool this project's tests and scripts run on.

Source: Blizzard's official "Ladder2019Season1" map pack, listed under the
"Map Packs" section of https://github.com/Blizzard/s2client-proto#downloads.
Verified (by downloading and listing it while writing this module) to
contain exactly these seven maps, nothing more:

    AutomatonLE.SC2Map        (~0.9 MB)
    CyberForestLE.SC2Map      (~1.1 MB)
    KairosJunctionLE.SC2Map   (~1.4 MB)
    KingsCoveLE.SC2Map        (~1.4 MB)
    NewRepugnancyLE.SC2Map    (~1.4 MB)
    PortAleksanderLE.SC2Map   (~1.4 MB)
    YearZeroLE.SC2Map         (~1.6 MB)

All seven are standard, small, reliable ladder maps from the same season's
pool. AutomatonLE is this project's primary fixed test map; KairosJunctionLE
is kept as the documented fallback/second map used by the integration
harness; the rest sync alongside them so any map name in the pack is
playable without a one-off `--maps` override.

### `DEFAULT_MAPS`

```python
DEFAULT_MAPS = ('AutomatonLE', 'KairosJunctionLE', 'CyberForestLE', 'KingsCoveLE', 'NewRepugnancyLE', 'PortAleksanderLE', 'YearZeroLE')
```

Map names (matching sc2.maps.get()'s lookup, i.e. the .SC2Map stem) this project's setup syncs. AutomatonLE/KairosJunctionLE are what the integration harness actually runs on; the rest are the pack's remaining maps, synced too so they're playable out of the box.

### `sync_maps`

```python
def sync_maps(install_path: Path, maps: tuple[str, ...]=DEFAULT_MAPS, force: bool=False, downloader=_download) -> list[str]
```

Ensure each map in ``maps`` exists (as ``<name>.SC2Map``) directly
under the install's Maps directory, matching sc2.maps.get()'s lookup
convention (root-level file, not nested under a season subfolder).

Returns the list of map names now present. Only downloads the map pack
if at least one requested map is missing (or ``force=True``).

## `install.paths`

*Source: [`src/install/paths.py`](../src/install/paths.py)*

Platform-aware SC2 install-path resolution.

This mirrors the lookup order that ``python-sc2`` itself uses internally
(``sc2.paths.Paths``), so that what this module reports as "installed" agrees
with what ``python-sc2`` will actually find at runtime:

1. the ``SC2PATH`` environment variable, if set
2. a Battle.net ``ExecuteInfo.txt`` (written by the Battle.net launcher on
   Windows/Mac), parsed for the install directory it points at
3. the platform's conventional default install directory

Unlike ``sc2.paths.Paths`` (which calls ``sys.exit(1)`` when nothing is
found -- fine for a bot script, fatal for a test suite or a setup tool), every
function here is a pure, side-effect-free lookup that returns ``None`` on
failure. This is deliberate: ``tests/conftest.py`` needs to probe for a local
install *without* risking killing the pytest process, and ``install.cli``
needs to distinguish "nothing found" from "found, but broken" to decide
whether to install the headless package or report an actionable error.

### `DEFAULT_BASE_DIR`

```python
DEFAULT_BASE_DIR: dict[str, str] = {'Windows': 'C:/Program Files (x86)/StarCraft II', 'Darwin': '/Applications/StarCraft II', 'Linux': '~/StarCraftII'}
```

Directories checked, per platform, for a Battle.net-managed install.

### `EXECUTE_INFO_RELATIVE_PATH`

```python
EXECUTE_INFO_RELATIVE_PATH: dict[str, str] = {'Windows': 'Documents/StarCraft II/ExecuteInfo.txt', 'Darwin': 'Library/Application Support/Blizzard/StarCraft II/ExecuteInfo.txt'}
```

Where the Battle.net launcher records the active install path, per platform. Linux has no Battle.net launcher, so there is no entry for it.

### `BINARY_NAME`

```python
BINARY_NAME: dict[str, str] = {'Windows': 'SC2_x64.exe', 'Darwin': 'SC2.app/Contents/MacOS/SC2', 'Linux': 'SC2_x64'}
```

SC2 client executable, relative to an install's base directory + version dir.

### `platform_name`

```python
def platform_name() -> str
```

Return 'Windows', 'Darwin', or 'Linux'.

Honors the ``SC2PF`` override that ``python-sc2`` itself recognizes, so a
forced platform (e.g. for Wine setups) stays consistent between this
module and the library.

### `home_dir`

```python
def home_dir() -> Path
```

### `execute_info_path`

```python
def execute_info_path(pf: str | None=None) -> Path | None
```

### `parse_execute_info`

```python
def parse_execute_info(text: str) -> str | None
```

Extract the install base directory from ExecuteInfo.txt contents.

The file contains a line like::

    executable = C:\Program Files (x86)\StarCraft II\Versions\Base12345\SC2_x64.exe

We only need the portion before ``Versions``.

### `read_battlenet_base_dir`

```python
def read_battlenet_base_dir(pf: str | None=None) -> Path | None
```

Read and parse ExecuteInfo.txt, if present, for this platform.

### `default_base_dir`

```python
def default_base_dir(pf: str | None=None) -> Path | None
```

### `has_valid_install`

```python
def has_valid_install(base: Path, pf: str | None=None) -> bool
```

True if ``base`` looks like a real SC2 install: a Versions/Base* dir
containing the platform's executable.

### `maps_dir`

```python
def maps_dir(base: Path) -> Path
```

Match python-sc2's own convention: prefer a lowercase 'maps' dir if it
already exists, otherwise use 'Maps' (created if needed).

### `ensure_lowercase_maps_alias`

```python
def ensure_lowercase_maps_alias(base: Path) -> None
```

Work around a real, confirmed engine bug: the compiled SC2_x64 Linux
binary resolves the map file it's told to load through a hardcoded
lowercase 'maps' path internally, regardless of what path python-sc2's
own ``Map`` object reports on the Python side (which correctly finds the
capitalized 'Maps' directory). On a case-sensitive Linux filesystem this
means ``CreateGameError.InvalidMapPath`` at game-creation time even
though the map file genuinely exists -- both this project's own synced
maps and the headless package's own bundled ladder-map archive land
under 'Maps', not 'maps'.

Create a ``maps -> Maps`` symlink so both cases resolve to the same
content. A relative symlink (not absolute), so it stays correct if the
install directory is later moved. No-op if a ``maps`` entry already
exists or 'Maps' doesn't (nothing to alias).

### class `Sc2Installation`

**Fields:**

- `path: Path`
- `source: str`

#### `Sc2Installation.maps_path`

```python
@property
def maps_path(self) -> Path
```

### `find_installed_base`

```python
def find_installed_base(pf: str | None=None) -> Sc2Installation | None
```

Resolve a working SC2 install using the same priority order
``python-sc2`` uses at runtime: SC2PATH env var, then Battle.net's
ExecuteInfo.txt, then the platform default directory.

Returns ``None`` (never raises, never exits) if nothing valid is found.
