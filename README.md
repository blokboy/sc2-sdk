# sc2-sdk

An SDK that allows an agent to play StarCraft II out of the box,
built on [`python-sc2`](https://github.com/BurnySc2/python-sc2), directly inspired 
the [`rs-sdk`](https://github.com/MaxBittker/rs-sdk).

A research oriented way to examine agentic behavior in goal directed strategic reasoning, as such,
this is a free, open-source, "community-run" (still unsure who this refers to besides myself) project.

The goal is strictly education and scientific research. I have not been endorsed by, authorized by, or officially communicated with Blizzard Entertainment on my efforts here.

## Getting Started:
```sh
git clone https://github.com/blokboy/sc2-sdk
```
Have your agent clone the repo and ask them to "setup the sc2-sdk". Then, you should be good to go.

There's no public server for any of this currently, so if you want to run games against others, you'll want to allow your agent to setup Tailscale while walking you through the setup.

The rest of this section, and "Get a local SC2 client + map pool" below,
is the manual path -- for running it yourself, or if your tooling doesn't
read `AGENTS.md`.

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

## Map pool

Fixed set synced by `setup` provides all seven maps in Blizzard's official `Ladder2019Season1` map pack:

- `AutomatonLE` 
- `KairosJunctionLE`
- `CyberForestLE`
- `KingsCoveLE`
- `NewRepugnancyLE`
- `PortAleksanderLE`
- `YearZeroLE`

Pass `--maps` to `sc2-sdk-setup` to sync a different subset instead.

## Examples

### Play a raw game against the built-in AI
**Agentic**:
```
Play a quick game as Terran against an easy built-in AI on Automaton, and tell me who won.
```
**CLI**:
```bash
python -m sdk.play --map AutomatonLE --race terran --opponent-race zerg --difficulty easy
```

`python -m sdk.play -h` for all options (opponent, race, difficulty, time limit, etc.)

**Programmatically**:

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

### Play a bot against the built-in AI 
**Agentic**:
```Write and run a Terran bot that opens by training an SCV, using the verified `bot.*` API, against an easy built-in AI.```

**CLI**:
```bash
sc2-sdk-run-bot my_bot --race terran --opponent-race zerg --difficulty easy
```

**Programmatically**:
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

`sdk.bot.VerifiedBotAI` is the base class for a real `BotAI` subclass whose
`on_step` drives the SDK's verified `bot.*` action/observation API against a
live game.

See `tests/integration/test_verified_bot_actions.py` for a full worked
example, including how invalid actions (insufficient resources, illegal
placement, an unknown unit tag) come back as clear `ok=False` errors instead
of silently no-op'ing.

## Play interactively via MCP
This is the mode that makes "just tell your agent what to do, turn by
turn" work: once it's wired up, you don't call `execute_code` yourself --
you talk to your agent directly (e.g. "get an SCV out and start scouting
the enemy natural") and it turns that into `execute_code` calls against
the live game. 

These can be solo or multiplayer. The multiplayer option requires a user to setup Tailscale, so they have a networking endpoint to point at. A returned invite code is given to whomever you'd like to play with/against.

**Agentic**:
```
Let's run an MCP match.
```
OR
```
Let's run an MCP multiplayer match.
```


**CLI**:
```bash
sc2-sdk-mcp
```
`sc2-sdk-mcp` (or `python -m sdk.mcp_server`) launches a real game against
the built-in AI and serves five MCP tools -- `execute_code`, `new_game`,
and the standing-background-task trio `start_task`/`task_status`/
`cancel_task` (see below) -- over stdio:

OR

```bash
# Host side:
sc2-sdk-host bots/idle_example.py --race terran
# Prints, e.g.: MATCH CODE: eyJob3N0X2lwIjoiMTAuMC4wLjUiLCA...

# Join side (paste the code from the host):
sc2-sdk-join <code> bots/idle_example.py --race zerg
```

**Programmatically**:
```python
# one execute_code call's `code` argument:
from sc2.ids.unit_typeid import UnitTypeId
await bot.train(UnitTypeId.SCV)
```

Note: You can point any MCP client at it (e.g. add it as a stdio MCP server in your
agent's config -- copy `.mcp.json.example` to `.mcp.json` and adjust the
race/difficulty/map flags as needed; `.mcp.json` itself is gitignored
since it's per-clone config, not something to commit) and call
`execute_code` with a Python snippet -- it runs
against live `bot`/`sdk` globals bound to the running game, exactly like a
direct call from a `VerifiedBotAI.on_step` would:

## API reference 

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

## Learning scripts 

[`learnings/README.md`](learnings/README.md) has copyable, worked examples
of using `bot.*` for real things, starting with a continuous Terran macro
loop (`learnings/macro_loop_bot.py`) that trains workers, expands supply,
and builds a Barracks to start training Marines -- a different shape from
`bots/idle_example.py`'s one-shot action, meant as a starting point to copy
and adapt rather than documentation of the `bots/` convention itself.

**Architecture note:** 
Blizzard's headless Linux SC2 package (see
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
