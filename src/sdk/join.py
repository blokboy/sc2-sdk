"""Ticket #11 (https://github.com/blokboy/sc2-sdk/issues/11): proof that the
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
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from s2clientprotocol import sc2api_pb2 as sc_pb
from sc2 import maps
from sc2.bot_ai import BotAI
from sc2.client import Client
from sc2.data import Race, Result
from sc2.main import _play_game_ai
from sc2.player import Bot
from sc2.portconfig import Portconfig
from sc2.protocol import ConnectionAlreadyClosedError
from sc2.sc2process import SC2Process

#: Same fixed test map this project's other harnesses default to.
DEFAULT_MAP = "AutomatonLE"

#: Loopback proof only (see this module's docstring) -- the ticket's own
#: acceptance criteria call out that real network/NAT traversal is out of
#: scope, only that the *mechanism* (an explicit host_ip) generalizes.
DEFAULT_HOST_IP = "127.0.0.1"

_RACE_BY_NAME = {r.name.lower(): r for r in Race}

#: This repo's `src` layout dir, prepended to the subprocess's PYTHONPATH
#: (see `_subprocess_env`) so `python -m sdk.join` resolves even when this
#: package isn't pip-installed and the only reason the *parent* process can
#: import `sdk` is pytest's own `pythonpath = ["src"]` ini setting (which
#: only patches the parent's `sys.path`, not a freshly spawned child's).
_SRC_DIR = str(Path(__file__).resolve().parents[1])


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = _SRC_DIR if not existing else f"{_SRC_DIR}{os.pathsep}{existing}"
    return env


class _NullBot(BotAI):
    """Smallest legal `BotAI` for a connectivity proof -- see
    `sdk.play._NullBot`, the same idea, duplicated rather than imported
    since that module is explicitly off-limits to touch for this ticket."""

    async def on_step(self, iteration: int) -> None:
        return None


async def _join_game_at(
    client: Client,
    race: Race,
    portconfig: Portconfig,
    host_ip: str,
    name: str | None = None,
) -> int:
    """Same request `sc2.client.Client.join_game()` builds, plus
    `RequestJoinGame.host_ip` -- the one field that method never sets (see
    this module's docstring for why that field is exactly the primitive
    this ticket needs to prove). Duplicated rather than monkeypatched:
    `Client.join_game()` builds and sends its request in one method body,
    so there's no seam to inject one extra field into an existing call
    without either reimplementing it (this) or patching library internals
    at the bytecode level -- reimplementing is more legible.
    """
    ifopts = sc_pb.InterfaceOptions(
        raw=True,
        score=True,
        show_cloaked=True,
        show_burrowed_shadows=True,
        raw_affects_selection=client.raw_affects_selection,
        raw_crop_to_playable_area=False,
        show_placeholders=True,
    )
    request = sc_pb.RequestJoinGame(race=race.value, options=ifopts)
    request.server_ports.game_port = portconfig.server[0]
    request.server_ports.base_port = portconfig.server[1]
    for player_ports in portconfig.players:
        port_set = request.client_ports.add()
        port_set.game_port = player_ports[0]
        port_set.base_port = player_ports[1]
    if name is not None:
        request.player_name = name
    request.host_ip = host_ip

    result = await client._execute(join_game=request)
    client._game_result = None
    client._player_id = result.join_game.player_id
    return result.join_game.player_id


async def _run_host_role(
    map_name: str,
    my_race: Race,
    bot_ai: BotAI,
    portconfig: Portconfig,
    host_ip: str,
    realtime: bool,
    game_time_limit: int | None,
    name: str | None = None,
) -> Result:
    """The "host" role: this process's local SC2 client calls
    `RequestCreateGame` (defining the match/map/two participant slots),
    then joins it as one of those slots -- mirrors `sc2.main._host_game`,
    generalized to take an explicit `host_ip` for the join step, and
    designed to be runnable independently of any "join"-role process (i.e.
    not paired via `asyncio.gather` in the same Python process)."""
    async with SC2Process() as server:
        await server.ping()
        # The second participant slot's race is a placeholder: per this
        # module's docstring, RequestCreateGame's Participant slots don't
        # carry a race at all -- each side's own join_game call decides its
        # own race independently.
        create_response = await server.create_game(
            maps.get(map_name), [Bot(my_race, None), Bot(Race.Random, None)], realtime
        )
        if create_response.create_game.HasField("error"):
            raise RuntimeError(f"Could not create game: {create_response.create_game}")

        client = Client(server._ws)
        player_id = await _join_game_at(client, my_race, portconfig, host_ip, name=name)
        result = await _play_game_ai(client, player_id, bot_ai, realtime, game_time_limit)
        try:
            await client.leave()
        except ConnectionAlreadyClosedError:
            pass
        await client.quit()
        return result


async def _run_join_role(
    my_race: Race,
    bot_ai: BotAI,
    portconfig: Portconfig,
    host_ip: str,
    realtime: bool,
    game_time_limit: int | None,
    name: str | None = None,
) -> Result:
    """The "join" role: this process's own local SC2 client (a second,
    independent `SC2Process` from the host's) joins the match the host
    already created, pointed at `host_ip` -- mirrors `sc2.main._join_game`,
    generalized the same way as `_run_host_role` above."""
    async with SC2Process() as server:
        await server.ping()
        client = Client(server._ws)
        player_id = await _join_game_at(client, my_race, portconfig, host_ip, name=name)
        result = await _play_game_ai(client, player_id, bot_ai, realtime, game_time_limit)
        try:
            await client.leave()
        except ConnectionAlreadyClosedError:
            pass
        await client.quit()
        return result


def run_join_game_match(
    map_name: str = DEFAULT_MAP,
    host_race: Race = Race.Terran,
    join_race: Race = Race.Zerg,
    host_ip: str = DEFAULT_HOST_IP,
    realtime: bool = False,
    game_time_limit: int | None = None,
    process_timeout: float = 600.0,
) -> tuple[Result, Result]:
    """Launch a host-role and a join-role SC2 client as two independent,
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
    """
    portconfig = Portconfig()
    try:
        with tempfile.TemporaryDirectory(prefix="sc2_join_proof_") as tmp_dir:
            host_log = Path(tmp_dir) / "host.log"
            join_log = Path(tmp_dir) / "join.log"

            host_argv = [
                sys.executable,
                "-m",
                "sdk.join",
                "--role",
                "host",
                "--map",
                map_name,
                "--race",
                host_race.name.lower(),
                "--host-ip",
                host_ip,
                "--portconfig-json",
                portconfig.as_json,
            ]
            join_argv = [
                sys.executable,
                "-m",
                "sdk.join",
                "--role",
                "join",
                "--race",
                join_race.name.lower(),
                "--host-ip",
                host_ip,
                "--portconfig-json",
                portconfig.as_json,
            ]
            if realtime:
                host_argv.append("--realtime")
                join_argv.append("--realtime")
            if game_time_limit is not None:
                host_argv.extend(["--time-limit", str(game_time_limit)])
                join_argv.extend(["--time-limit", str(game_time_limit)])

            # Writing each child's stdout/stderr to its own file (rather
            # than PIPE) deliberately avoids the classic subprocess
            # deadlock: two children each producing enough output to fill
            # an undrained pipe while we .wait() on them one at a time.
            subprocess_env = _subprocess_env()
            with open(host_log, "w") as host_out, open(join_log, "w") as join_out:
                host_proc = subprocess.Popen(host_argv, stdout=host_out, stderr=subprocess.STDOUT, env=subprocess_env)
                join_proc = subprocess.Popen(join_argv, stdout=join_out, stderr=subprocess.STDOUT, env=subprocess_env)

                try:
                    host_proc.wait(timeout=process_timeout)
                    join_proc.wait(timeout=process_timeout)
                finally:
                    for proc in (host_proc, join_proc):
                        if proc.poll() is None:
                            proc.kill()
                            proc.wait()

            host_result = _read_result(host_log, host_proc.returncode, role="host")
            join_result = _read_result(join_log, join_proc.returncode, role="join")
            return host_result, join_result
    finally:
        portconfig.clean()


def _read_result(log_path: Path, returncode: int, role: str) -> Result:
    output = log_path.read_text(errors="replace")
    for line in reversed(output.splitlines()):
        if line.startswith("RESULT: "):
            return Result[line.removeprefix("RESULT: ").strip()]
    raise RuntimeError(
        f"{role}-role subprocess (exit code {returncode}) never printed a RESULT line. "
        f"Last output:\n{output[-4000:]}"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Single-role side of a host/join match; see run_join_game_match().")
    parser.add_argument("--role", choices=("host", "join"), required=True)
    parser.add_argument("--map", default=DEFAULT_MAP, help="Only meaningful for --role host.")
    parser.add_argument("--race", choices=sorted(_RACE_BY_NAME), required=True)
    parser.add_argument("--host-ip", default=DEFAULT_HOST_IP)
    parser.add_argument("--portconfig-json", required=True)
    parser.add_argument("--realtime", action="store_true")
    parser.add_argument("--time-limit", type=int, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    portconfig = Portconfig.from_json(args.portconfig_json)
    race = _RACE_BY_NAME[args.race]

    if args.role == "host":
        result = asyncio.run(
            _run_host_role(
                args.map, race, _NullBot(), portconfig, args.host_ip, args.realtime, args.time_limit
            )
        )
    else:
        result = asyncio.run(
            _run_join_role(race, _NullBot(), portconfig, args.host_ip, args.realtime, args.time_limit)
        )

    print(f"RESULT: {result.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
