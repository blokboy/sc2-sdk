# sc2-sdk

An SDK that lets an LLM coding agent install, connect to, and play StarCraft II,
built on [`python-sc2`](https://github.com/BurnySc2/python-sc2) (PyPI: `burnysc2`).

Client install/detection and map pool sync (ticket #2) plus a raw
connect-and-play script against the game's built-in AI are the walking
skeleton. The verified `bot.*`/`sdk.*` action/observation API (ticket #3) is
built on top of that -- see "Play a verified bot against the built-in AI"
below. Per-race macro helpers (Protoss/Zerg), the MCP server, and the
autonomous script runtime are later tickets; see the
[full spec](https://github.com/blokboy/sc2-sdk/issues/1).

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

If `setup` can't find or install a client (e.g. Mac/Windows with no
Battle.net install), it exits with an actionable message rather than
guessing.

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

## What's out of scope here

Per the [spec](https://github.com/blokboy/sc2-sdk/issues/1): per-race macro
helpers beyond the Terran acceptance tests (Protoss/Zerg -- #4/#5), the MCP
server (#6), the standalone bot-script runtime (#7), self-play, and AI Arena
ladder integration. See issue #1 for the full phase breakdown.
