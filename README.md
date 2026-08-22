# sc2-sdk

An SDK that lets an LLM coding agent install, connect to, and play StarCraft II,
built on [`python-sc2`](https://github.com/BurnySc2/python-sc2) (PyPI: `burnysc2`).

Client install/detection and map pool sync (ticket #2) plus a raw
connect-and-play script against the game's built-in AI are the walking
skeleton. The verified `bot.*`/`sdk.*` action/observation API (ticket #3) is
built on top of that -- see "Play a verified bot against the built-in AI"
below. Per-race macro helpers (Protoss/Zerg) extend the same API (#4/#5). The
MCP `execute_code` interactive server (ticket #6) is built on top of all of
that -- see "Play interactively via MCP" below. The autonomous bot-script
runtime (ticket #7) is the third play modality, built on the same `bot.*`/
`sdk.*` API -- see "Play an autonomous bot script" below. See the
[full spec](https://github.com/blokboy/sc2-sdk/issues/1) for how all three
fit together.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Get a local SC2 client + map pool

```bash
python -m install.cli
# or, after the editable install above:
sc2-sdk-setup
```

This will:

1. **On Windows/Mac**, look for an existing Battle.net-managed install (via
   Battle.net's own `ExecuteInfo.txt` record, or the platform's default
   install directory) and use it if found -- no re-install, and you can watch
   games in the rendered client.
2. **On Linux**, if no Battle.net install is found, download and extract
   Blizzard's official headless package (no Battle.net/GUI required) to
   `~/StarCraftII` by default. Source: the "Linux Packages" section of
   [Blizzard/s2client-proto](https://github.com/Blizzard/s2client-proto#downloads).
   Packages are password-protected zips; the password (`iagreetotheeula`) is
   the license-acknowledgement gate documented there, not a secret.
3. Sync the fixed map pool this project's scripts and tests run on (see
   "Map pool" below) into the install's `Maps` directory.

On Mac/Windows with no Battle.net install found, `setup` opens the
Battle.net download page and polls for the install to finish (Ctrl-C to
give up early) -- there's no supported silent/scriptable installer for
Battle.net-managed SC2 (unlike the Linux headless package above), so this
guides a human through an interactive install rather than automating it.
Skipped when the `CI` env var is set, or if you Ctrl-C out, or it times out
after 20 minutes -- any of those fall through to an actionable message
rather than guessing.

`python-sc2` resolves the install location itself in this order: the
`SC2PATH` env var, then Battle.net's `ExecuteInfo.txt`, then the platform
default directory. A headless install at the Linux default (`~/StarCraftII`)
needs no extra configuration; a custom `--dest` does -- `setup` will print
the `export SC2PATH=...` line to run in that case.

## Map pool

Fixed set synced by `setup` (`install/maps.py:DEFAULT_MAPS`):

- `AutomatonLE` -- primary test map
- `KairosJunctionLE` -- secondary map

Both are standard, small ladder maps, sourced from Blizzard's official
`Ladder2019Season1` map pack (same `s2client-proto#downloads` page, "Map
Packs" section).

## Play a raw game against the built-in AI

```bash
python -m sdk.play --map AutomatonLE --race terran --opponent-race zerg --difficulty easy
```

Prints `RESULT: Victory|Defeat|Tie` and exits 0 once the game resolves. See
`python -m sdk.play -h` for all options (race, opponent race, difficulty,
`--realtime`, `--time-limit`).

Programmatically:

```python
from sc2.data import Race, Difficulty
from sdk.play import play_vs_builtin_ai

result = play_vs_builtin_ai(
    map_name="AutomatonLE",
    my_race=Race.Terran,
    opponent_race=Race.Zerg,
    difficulty=Difficulty.Easy,
)
```

## Play a verified bot against the built-in AI (ticket #3)

`sdk.bot.VerifiedBotAI` is the base class for a real `BotAI` subclass whose
`on_step` drives the SDK's verified `bot.*` action/observation API against a
live game:

```python
from sc2.data import Race, Difficulty
from sc2.ids.unit_typeid import UnitTypeId
from sdk.bot import VerifiedBotAI
from sdk.runtime import run_bot_vs_builtin_ai


class MyBot(VerifiedBotAI):
    async def on_step(self, iteration: int) -> None:
        if iteration == 0:
            outcome = await self.bot.train(UnitTypeId.SCV)
            print(outcome)  # TrainOutcome(ok=True, effect_confirmed=True, ...)


result = run_bot_vs_builtin_ai(MyBot(), my_race=Race.Terran, difficulty=Difficulty.Easy)
```

- **`bot.*`** (`sdk.bot.Bot`, reached via `self.bot` inside your `BotAI`
  subclass): `observe()`, `train()`, `build()`, `research()`, `move()`,
  `attack_move()`, `chat()`. Each write action re-observes real subsequent
  game state (not just python-sc2's own optimistic local bookkeeping) before
  reporting whether the intended effect actually occurred -- see the outcome
  dataclasses in `sdk/outcomes.py` (`ok` / `effect_confirmed` / `error` /
  `detail`) and `sdk/bot.py`'s module docstring for why that requires
  advancing real simulation steps, not just checking return values.
- **`sdk.*`** (reached via `self.sdk`, or directly as `self` inside your
  `BotAI` subclass): raw passthrough to the underlying python-sc2 `BotAI`
  instance -- `self.sdk.units`, `self.sdk.client.debug_all_resources()`,
  etc. -- for anything the verified tier doesn't cover.
- Race-agnostic: `Bot`/`VerifiedBotAI` work for any of the three races.
  Ticket #3 only *exercises* this against Terran; #4/#5 build Protoss/Zerg
  macro helpers on top of the same classes.
- Match outcome (`Result.Victory`/`Defeat`/`Tie`) is captured in `on_end` and
  surfaced via `bot.observe().match_result` (or `bot.match_result` directly)
  once the game ends -- safe to call after `run_game()` returns, since the
  bot instance you constructed is still the same live object.

See `tests/integration/test_verified_bot_actions.py` for a full worked
example, including how invalid actions (insufficient resources, illegal
placement, an unknown unit tag) come back as clear `ok=False` errors instead
of silently no-op'ing.

## Play interactively via MCP (ticket #6)

`sc2-sdk-mcp` (or `python -m sdk.mcp_server`) launches a real game against
the built-in AI and serves a single MCP tool, `execute_code`, over stdio:

```bash
sc2-sdk-mcp
```

Point any MCP client at it (e.g. add it as a stdio MCP server in your
agent's config) and call `execute_code` with a Python snippet -- it runs
against live `bot`/`sdk` globals bound to the running game, exactly like a
direct call from a `VerifiedBotAI.on_step` would:

```python
# one execute_code call's `code` argument:
from sc2.ids.unit_typeid import UnitTypeId
await bot.train(UnitTypeId.SCV)
```

The response is JSON: `{"ok": ..., "result": ..., "stdout": ..., "error":
..., "traceback": ...}` -- `result` is `repr()` of the snippet's trailing
expression value (if any, auto-`await`-ed if it's a coroutine), `stdout` is
anything the snippet printed, and a failing snippet (a syntax error, a
raised exception) comes back as a structured `ok=False`/`error` result
instead of crashing the session.

**The game only advances when you call `execute_code`.** It runs in
python-sc2's non-realtime/stepped mode: between calls, the bot's `on_step`
is simply blocked waiting for the next snippet, so nothing about python-sc2's
own stepping logic needed to change to get "the game pauses while you think"
-- see `src/sdk/mcp_server.py`'s module docstring for the full
concurrency/architecture writeup (same process, same asyncio event loop, no
IPC: the MCP server and the running game are two tasks on one loop).

Race-agnostic, same as `bot.*`/`sdk.*` themselves -- `sc2-sdk-mcp`'s CLI flags
(`--race`, `--opponent-race`, `--difficulty`, `--map`) select which race you
play. Run `sc2-sdk-mcp --help` for the full list. See
`tests/integration/test_execute_code_mcp.py` for a full worked example,
including the wiring test that confirms the game is genuinely paused/stepped
(not free-running) between calls.

## Play an autonomous bot script (ticket #7)

Where `execute_code` mode (#6) pauses the game every step waiting for the
next externally-supplied snippet, this mode is the opposite: an agent
authors a `BotAI` subclass **once**, as a normal Python file, and the SDK
then plays a full game with it unattended -- no live per-tick calls from any
caller once the game starts, optionally at full real-time speed against a
real opponent.

**Convention:** a standalone bot script is a `.py` file directly under the
`bots/` directory at the repo root (`bots/<name>.py`), defining exactly one
class that subclasses `sc2.bot_ai.BotAI` -- in practice `sdk.bot.VerifiedBotAI`,
to get the same `bot.*`/`sdk.*` API surface `execute_code` mode uses. No
fixed class name is required; the runner (`sdk.script_runner`) finds the one
`BotAI` subclass a script *defines* (as opposed to merely imports, e.g.
`VerifiedBotAI` itself) -- see `bots/idle_example.py` for a minimal worked
example.

```bash
sc2-sdk-run-bot idle_example --race terran --opponent-race zerg
```

Runs `bots/idle_example.py` to completion at real-time speed by default and
prints `RESULT: Victory|Defeat|Tie`. Pass `--no-realtime` to step as fast as
the script responds instead (useful for CI). See `sc2-sdk-run-bot -h` for
all options (map, race, opponent race, difficulty, `--time-limit`,
`--bots-dir` to point at a different directory of scripts).

Programmatically:

```python
from sc2.data import Race, Difficulty
from sdk.script_runner import run_bot_script

result = run_bot_script(
    "idle_example",
    my_race=Race.Terran,
    opponent_race=Race.Zerg,
    difficulty=Difficulty.Easy,
)
```

`run_bot_script` discovers and loads the named script (`resolve_script_path`
+ `load_bot_class`), constructs an instance of the class it finds, and hands
it straight to `runtime.run_bot_vs_builtin_ai` (the same synchronous
`sc2.main.run_game` entrypoint ticket #3's tests use) -- so a script written
against `self.bot`/`self.sdk` inside `on_step` behaves identically here and
in `execute_code` mode; the only difference is who's calling `on_step` and
how often. See `tests/integration/test_bot_script_runtime.py` for the
wiring test confirming a real script at the documented location is
discovered, loaded, and run, and `tests/test_script_runner.py` for the
discovery/loading edge cases (missing script, zero/multiple `BotAI`
subclasses in one file).

## Play self-play (ticket #10)

Two `BotAI` instances play each other directly -- no human, no built-in AI,
on either side. This reuses the same local two-client mechanism
`python-sc2`'s `run_game` already uses for Bot-vs-Bot, exposed through the
same layering as every other play modality here: a library primitive
(`sdk.runtime.run_bot_vs_bot`) plus a thin CLI wrapper
(`sc2-sdk-selfplay`).

```bash
# A script against a second instance of itself:
sc2-sdk-selfplay bots/idle_example.py

# Two different scripts against each other:
sc2-sdk-selfplay bots/idle_example.py bots/another_bot.py
```

Both forms reuse `sc2-sdk-run-bot`'s exact discovery convention
(`resolve_script_path` + `load_bot_class` from `sdk.script_runner`): a bare
name resolves to `bots/<name>.py`, or pass a literal `.py` path directly.
Runs stepped (`--no-realtime`'s *opposite* is the default here, unlike
`sc2-sdk-run-bot`) -- fast/deterministic iteration is the more useful
default when both sides are scripts under test rather than one side playing
unattended in real time; pass `--realtime` to run at wall-clock speed
instead. See `sc2-sdk-selfplay -h` for all options (map, `--race-a`/
`--race-b`, `--time-limit`, `--bots-dir`).

Programmatically, drive two already-constructed `BotAI` instances directly
with the runtime primitive (no script-loading involved -- pair it with
`sdk.script_runner.load_bot_class`/`resolve_script_path` if you're starting
from a `bots/<name>.py` file, exactly like `sc2-sdk-selfplay` does
internally):

```python
from sc2.data import Race
from sdk.runtime import run_bot_vs_bot
from sdk.script_runner import load_bot_class, resolve_script_path

BotClass = load_bot_class(resolve_script_path("idle_example"))
result_a, result_b = run_bot_vs_bot(
    BotClass(),
    BotClass(),
    race_a=Race.Terran,
    race_b=Race.Terran,
)
```

`run_bot_vs_bot` returns a **two-element list** of `sc2.data.Result` (one
per side), not a single `Result` like `run_bot_vs_builtin_ai` -- that's
`run_game`'s own return shape for a non-Computer match (see
`sdk.runtime.run_bot_vs_bot`'s docstring), passed through as-is. See
`tests/integration/test_selfplay.py` for the wiring test confirming a real
script, loaded twice, plays a full match against itself.

## Play a 1v1 with another agent (ticket #12)

Two participants, each on their own machine with their own local SC2
client, play a real 1v1 -- each watching their own side render live on
their own screen, exactly like a normal local game. One side **hosts**
(picks the map, optionally pins the opponent's race, prints a shareable
code) and the other **joins** using that code.

```bash
# Host side:
sc2-sdk-host bots/idle_example.py --race terran
# Prints, e.g.: MATCH CODE: eyJob3N0X2lwIjoiMTAuMC4wLjUiLCA...

# Join side (paste the code from the host):
sc2-sdk-join <code> bots/idle_example.py --race zerg
```

- **Networking is not built or operated by this project.** `--host-ip` must
  already be reachable from the joiner's machine -- same network, or a VPN/
  tunnel you set up yourselves. There is no relay or NAT-traversal service;
  see the design discussion on
  [issue #12](https://github.com/blokboy/sc2-sdk/issues/12) for why that's
  a deliberate scope line, not an oversight.
- **`--host-ip` is optional -- Tailscale-aware by default.** Omit it and
  `sc2-sdk-host` looks for a local Tailscale IP (`install.tailscale`); if
  Tailscale isn't installed, it opens the download page and waits for you
  to install and log in (polling, not a blocking prompt -- same guided
  shape as the Battle.net install flow above), then picks up the IP
  automatically. Pass `--host-ip` explicitly to skip this (e.g. same-LAN
  play, or your own VPN/tunnel).
- **Both machines need the same map already synced** -- run `sc2-sdk-setup`
  (see "Get a local SC2 client + map pool" above) on both sides first. The
  underlying protocol resolves the map as a local file path independently
  on each machine; there's no map-transfer step.
- **Race conflicts:** if the host pins `--opponent-race-pin` and the joiner
  also passes `--race`, the joiner's choice wins. Omit either to fall back
  to the other's choice, or `Random` if neither is set.
- **The host's `--timeout`** (default 300s) bounds how long it waits for a
  joiner before giving up with a clear error, rather than hanging forever.
- **The match code carries a token, but nothing checks it.** The raw SC2
  join protocol has no authentication concept -- only network address and
  port numbers, which the code already carries. The code's connection
  details are themselves the actual shared secret; `token` is there for a
  future rendezvous layer to check against, not enforced today. See
  `sdk.matchcode`'s module docstring for the full reasoning.
- Each side plays via the same `bots/<name>.py` script convention
  `sc2-sdk-run-bot`/`sc2-sdk-selfplay` already use -- nothing new for *how*
  a side plays, only for how the two already-local clients find each other.
  Built directly on ticket #11's proven `host_ip`-aware join primitive
  (`sdk.join`); see that module's docstring for what was empirically
  confirmed about the underlying mechanism, and `sdk.matchcode`'s for the
  shareable-code format. See `tests/integration/test_host_join_cli.py` for
  the end-to-end wiring test (loopback, both roles as real subprocesses).

## Tests

```bash
pytest
```

`tests/conftest.py` defines this project's integration-test harnesses:
`sc2_game_harness` (the walking-skeleton `play_vs_builtin_ai`, ticket #2) and
`sc2_verified_bot_harness` (`run_bot_vs_builtin_ai`, for driving a
`VerifiedBotAI` subclass, ticket #3). Both boot a real, local, headless SC2
game and assert on real subsequent game state -- no mocking of `python-sc2`.

If no local SC2 install is found, integration tests **skip** (not fail) with
a message pointing at `setup`. To actually run them, you need a real machine
with:

- A working local SC2 client (see "Get a local SC2 client" above) --
  Linux headless is the easiest unattended path; Battle.net on Mac/Windows
  also works.
- The synced map pool (`setup` handles this).
- Enough disk (the full client + maps is several GB) and, for the headless
  Linux path, an actual Linux host or VM -- the headless package is a native
  Linux binary and won't run under macOS/Windows emulation layers other than
  Wine (which `python-sc2` also supports; see `sc2.paths` if you need that
  path).

Then: `pytest -v tests/integration/`.

## Real-game integration tests in Docker (ticket #9)

You don't need a Linux host, Battle.net, or any manual setup to get a real,
passing run of the integration suite. One command builds a self-contained
image (headless SC2 client + map pool installed *inside the container* via
`sc2-sdk-setup`, same code path as above) and runs `pytest -m integration`
against it:

```bash
./scripts/run-integration-docker.sh
```

That's `docker build` + `docker run` under the hood (see the script and
`Dockerfile`); run those two commands directly if you'd rather not use the
wrapper.

**Architecture note:** Blizzard's headless Linux SC2 package (see
`src/install/headless.py`) is an x86_64-only binary -- there is no arm64
build. The `Dockerfile` pins `--platform linux/amd64`, so:

- On an x86_64 Docker host (including GitHub Actions' standard Linux
  runners, see `.github/workflows/integration.yml`), this runs natively.
- On an arm64 host (e.g. Apple Silicon via Colima/Docker Desktop), it runs
  under QEMU user-mode emulation. Whether the actual SC2 client boots and
  completes a game correctly under emulation is host-dependent -- see the
  ticket's implementation notes for exactly what was and wasn't verified in
  the environment this was built in.

A GitHub Actions workflow (`.github/workflows/integration.yml`) builds this
same image and runs the integration suite on every push/PR, on x86_64
runners.

## API reference (ticket #8)

[`docs/API.md`](docs/API.md) is a generated reference of every public
`bot.*`/`sdk.*`/`install.*` class, function, and constant this project
defines -- produced from the actual source (via `scripts/gen_api_docs.py`,
which walks it with Python's `ast`), not hand-maintained prose, so it can't
silently drift out of sync with the real API surface. Regenerate it after
changing a documented module's public surface:

```bash
python scripts/gen_api_docs.py
```

`python scripts/gen_api_docs.py --check` (what
`.github/workflows/docs.yml` runs on every push/PR) fails loudly instead of
writing the file if `docs/API.md` doesn't match what the current source
would produce. This check is pure static analysis over this repo's own
Python source -- no SC2 client, no Docker, not even this project's runtime
dependencies installed -- so it's a separate, fast workflow from
`.github/workflows/integration.yml`'s real-game Docker build.

## Learnings / example scripts (ticket #8)

[`learnings/README.md`](learnings/README.md) has copyable, worked examples
of using `bot.*` for real things, starting with a continuous Terran macro
loop (`learnings/macro_loop_bot.py`) that trains workers, expands supply,
and builds a Barracks to start training Marines -- a different shape from
`bots/idle_example.py`'s one-shot action, meant as a starting point to copy
and adapt rather than documentation of the `bots/` convention itself.

## What's out of scope here

Per the [spec](https://github.com/blokboy/sc2-sdk/issues/1): self-play and
AI Arena ladder integration. See issue #1 for the full phase breakdown.
