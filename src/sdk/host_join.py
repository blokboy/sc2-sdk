"""Ticket #12 (https://github.com/blokboy/sc2-sdk/issues/12): `sc2-sdk-host`
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
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from sc2.data import Race
from sc2.portconfig import Portconfig

from install.tailscale import detect_tailscale_ip, prompt_for_tailscale_install
from sdk.join import _run_host_role, _run_join_role
from sdk.matchcode import JoinTimeoutError, decode_match_code, encode_match_code, resolve_race
from sdk.script_runner import load_bot_class, resolve_script_path

DEFAULT_MAP = "AutomatonLE"
DEFAULT_JOIN_TIMEOUT = 300.0

_RACE_BY_NAME = {r.name.lower(): r for r in Race}


def _load_bot(script_path: str):
    bot_class = load_bot_class(resolve_script_path(script_path))
    return bot_class()


def _parse_host_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host a private 1v1 match and print a shareable join code.")
    parser.add_argument("bot_script", help="Path under bots/ defining the BotAI subclass this side plays.")
    parser.add_argument("--map", default=DEFAULT_MAP, help=f"Map to play on (default: {DEFAULT_MAP}).")
    parser.add_argument("--race", choices=sorted(_RACE_BY_NAME), required=True, help="This (host) side's race.")
    parser.add_argument(
        "--opponent-race-pin",
        choices=sorted(_RACE_BY_NAME),
        default=None,
        help="Pin the joiner's race. Omit to let the joiner choose (their choice always wins on conflict).",
    )
    parser.add_argument(
        "--host-ip",
        default=None,
        help="Address the joiner's machine can reach this host at (e.g. a Tailscale/VPN address). "
        "Omit to auto-detect (or get guided through installing) a Tailscale IP -- see "
        "install.tailscale. This project doesn't operate any relay/NAT-traversal itself, see README.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_JOIN_TIMEOUT,
        help=f"Seconds to wait for a joiner before giving up (default: {DEFAULT_JOIN_TIMEOUT}).",
    )
    parser.add_argument("--realtime", action="store_true", help="Run at wall-clock speed instead of stepped.")
    parser.add_argument("--time-limit", type=int, default=None, help="In-game-second safety cap.")
    return parser.parse_args(argv)


def _parse_join_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Join a private 1v1 match using a host's shareable code.")
    parser.add_argument("code", help="The shareable code sc2-sdk-host printed.")
    parser.add_argument("bot_script", help="Path under bots/ defining the BotAI subclass this side plays.")
    parser.add_argument(
        "--race",
        choices=sorted(_RACE_BY_NAME),
        default=None,
        help="This (joining) side's race. Wins over a host-pinned race if both are set; "
        "falls back to the host's pin, then Random, if omitted.",
    )
    parser.add_argument("--realtime", action="store_true", help="Run at wall-clock speed instead of stepped.")
    parser.add_argument("--time-limit", type=int, default=None, help="In-game-second safety cap.")
    return parser.parse_args(argv)


def main_host(argv: list[str] | None = None) -> int:
    args = _parse_host_args(argv)
    my_race = _RACE_BY_NAME[args.race]
    race_pin = _RACE_BY_NAME[args.opponent_race_pin] if args.opponent_race_pin else None
    bot_ai = _load_bot(args.bot_script)

    host_ip = args.host_ip or detect_tailscale_ip() or prompt_for_tailscale_install()
    if host_ip is None:
        print(
            "ERROR: no --host-ip given and no Tailscale IP available. Pass --host-ip explicitly "
            "(e.g. a LAN address, if you and the joiner are on the same network), or install and "
            "log into Tailscale first.",
            file=sys.stderr,
        )
        return 1

    portconfig = Portconfig()
    try:
        token = secrets.token_urlsafe(16)
        code = encode_match_code(
            host_ip=host_ip,
            portconfig=portconfig,
            map_name=args.map,
            race_pin=race_pin,
            token=token,
        )
        print(f"MATCH CODE: {code}")
        print("Share this with whoever is joining. Waiting for them to connect...")

        try:
            result = asyncio.run(
                _run_host_role(
                    args.map,
                    my_race,
                    bot_ai,
                    portconfig,
                    host_ip,
                    args.realtime,
                    args.time_limit,
                    join_timeout=args.timeout,
                )
            )
        except JoinTimeoutError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    finally:
        portconfig.clean()

    print(f"RESULT: {result.name}")
    return 0


def main_join(argv: list[str] | None = None) -> int:
    args = _parse_join_args(argv)
    decoded = decode_match_code(args.code)
    requested_race = _RACE_BY_NAME[args.race] if args.race else None
    my_race = resolve_race(host_pin=decoded.race_pin, joiner_race=requested_race)
    bot_ai = _load_bot(args.bot_script)

    try:
        result = asyncio.run(
            _run_join_role(
                my_race,
                bot_ai,
                decoded.portconfig,
                decoded.host_ip,
                args.realtime,
                args.time_limit,
            )
        )
    finally:
        decoded.portconfig.clean()

    print(f"RESULT: {result.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main_host())
