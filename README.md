# sc2-sdk

An SDK that lets an LLM coding agent install, connect to, and play StarCraft II,
built on [`python-sc2`](https://github.com/BurnySc2/python-sc2) (PyPI: `burnysc2`).

This is the **walking skeleton** stage (ticket #2): client install/detection,
map pool sync, and a raw connect-and-play script against the game's built-in
AI. The verified `bot.*`/`sdk.*` action/observation API described in the
[full spec](https://github.com/blokboy/sc2-sdk/issues/1) is built in
[ticket #3](https://github.com/blokboy/sc2-sdk/issues/3) onward.

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

## Tests

```bash
pytest
```

`tests/conftest.py` defines this project's **one integration-test harness**
(`sc2_game_harness`): it boots a real, local, headless SC2 game and asserts
on real subsequent game state -- no mocking of `python-sc2`. Every later
ticket's tests are expected to reuse this fixture.

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

Per the [spec](https://github.com/blokboy/sc2-sdk/issues/1): the `bot.*`
verified-action API, `sdk.*` raw passthrough layer, the MCP server, the
standalone bot-script runtime, self-play, and AI Arena ladder integration.
See issue #1 for the full phase breakdown.
