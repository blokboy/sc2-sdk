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
- [`sdk.mcp_server`](#sdkmcpserver)
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

## `sdk.mcp_server`

*Source: [`src/sdk/mcp_server.py`](../src/sdk/mcp_server.py)*

MCP `execute_code` interactive mode -- ticket #6
(https://github.com/blokboy/sc2-sdk/issues/6): an MCP server exposing a
single `execute_code` tool that evaluates a Python snippet against live
`bot`/`sdk` globals bound to a running game, with the game paused between
calls rather than advancing on wall-clock time.

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

### `DEFAULT_MAP`

```python
DEFAULT_MAP = 'AutomatonLE'
```

Same fixed test map the rest of the project's harnesses default to.

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
def __init__(self) -> None
```

#### `ExecuteCodeBotAI.on_start`

```python
async def on_start(self) -> None
```

#### `ExecuteCodeBotAI.on_step`

```python
async def on_step(self, iteration: int) -> None
```

#### `ExecuteCodeBotAI.submit`

```python
async def submit(self, code: str) -> ExecuteCodeResult
```

Called by the `execute_code` MCP tool handler: enqueue `code`
for the next `on_step` to run, and wait for its result -- or for a
clear error if the match ends before that happens.

### class `ExecuteCodeSession`

What `serve_execute_code()` hands back: the live `FastMCP` server
plus the live `ExecuteCodeBotAI`/game task it's wired to, so a caller
(a console-script entrypoint, or a test) can both serve the MCP tool
and separately inspect/await the underlying game.

**Fields:**

- `mcp: FastMCP`
- `bot_ai: ExecuteCodeBotAI`
- `game_task: 'asyncio.Task[Result]'`

### `build_server`

```python
def build_server(bot_ai: ExecuteCodeBotAI, name: str='sc2-sdk') -> FastMCP
```

Build a `FastMCP` server exposing the single `execute_code` tool
against `bot_ai`. Split out from `serve_execute_code()` purely as a
seam: it only touches `bot_ai.ready`/`bot_ai.submit()`, not anything
SC2-specific, so nothing about wiring the MCP tool itself depends on
`bot_ai` being a real, game-backed `ExecuteCodeBotAI` -- see
`serve_execute_code()` below for how the real entrypoint constructs
one.

### `serve_execute_code`

```python
async def serve_execute_code(map_name: str=DEFAULT_MAP, my_race: Race=Race.Terran, opponent_race: Race=Race.Random, difficulty: Difficulty=Difficulty.Easy, game_time_limit: int | None=None) -> ExecuteCodeSession
```

Launch a real game against the built-in AI (non-realtime, i.e.
stepped mode -- always; there is no realtime option here, since
stepped-between-calls is the entire point of this ticket) and return an
`ExecuteCodeSession` wrapping a `FastMCP` server whose `execute_code`
tool is live against it.

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

## `install.cli`

*Source: [`src/install/cli.py`](../src/install/cli.py)*

`setup` entry point: get a working local SC2 client + map pool with one
command, no Battle.net/GUI required.

Order of operations (mirrors the ticket's acceptance criteria):
  1. Detect an existing Battle.net install (Windows/Mac) -- use it if found.
  2. Otherwise, on Linux, install Blizzard's headless package.
  3. Otherwise (Mac/Windows with no Battle.net install), fail with an
     actionable message -- the headless package cannot run there.
  4. Sync the fixed map pool onto whichever install was selected.

Usage:
    python -m install.cli
    sc2-sdk-setup  (after `pip install -e .`)

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

Sync the small, fixed map pool this project's tests and scripts run on.

Source: Blizzard's official "Ladder2019Season1" map pack, listed under the
"Map Packs" section of https://github.com/Blizzard/s2client-proto#downloads.
Verified (by downloading and listing it while writing this module) to
contain, among others:

    AutomatonLE.SC2Map        (~0.9 MB)
    KairosJunctionLE.SC2Map   (~1.4 MB)

Both are standard, small, reliable ladder maps commonly used by python-sc2's
own test/example suite. AutomatonLE is this project's primary fixed test map;
KairosJunctionLE is kept as a documented fallback/second map.

### `DEFAULT_MAPS`

```python
DEFAULT_MAPS = ('AutomatonLE', 'KairosJunctionLE')
```

Map names (matching sc2.maps.get()'s lookup, i.e. the .SC2Map stem) this project's setup syncs and its integration harness runs on.

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
