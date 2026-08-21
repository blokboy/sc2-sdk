"""Ticket #12 (https://github.com/blokboy/sc2-sdk/issues/12): the shareable
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
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Awaitable, TypeVar

from sc2.data import Race
from sc2.portconfig import Portconfig

_T = TypeVar("_T")


class JoinTimeoutError(RuntimeError):
    """No joiner connected within the host's configured wait window."""


@dataclass(frozen=True)
class MatchCode:
    host_ip: str
    portconfig: Portconfig
    map_name: str
    race_pin: Race | None
    token: str


def encode_match_code(
    host_ip: str,
    portconfig: Portconfig,
    map_name: str,
    race_pin: Race | None,
    token: str,
) -> str:
    """Pack a host's connection info into a single copy-pasteable string.

    `portconfig` is stored via its own `as_json` (see `sc2.portconfig`) --
    not reconstructed field-by-field -- so this stays correct if that
    format ever changes.
    """
    payload = {
        "host_ip": host_ip,
        "portconfig_json": portconfig.as_json,
        "map_name": map_name,
        "race_pin": race_pin.name if race_pin is not None else None,
        "token": token,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_match_code(code: str) -> MatchCode:
    raw = base64.urlsafe_b64decode(code.encode("ascii"))
    payload = json.loads(raw)
    race_pin_name = payload["race_pin"]
    return MatchCode(
        host_ip=payload["host_ip"],
        portconfig=Portconfig.from_json(payload["portconfig_json"]),
        map_name=payload["map_name"],
        race_pin=Race[race_pin_name] if race_pin_name is not None else None,
        token=payload["token"],
    )


def resolve_race(host_pin: Race | None, joiner_race: Race | None) -> Race:
    """The joiner's explicit choice always wins over a host-pinned race --
    see ticket #12's design discussion: the host is authoritative over the
    map and whether to pin a race at all, but a joiner who states a race
    has final say over their own side. Falls back to the host's pin if the
    joiner didn't choose one, and to `Race.Random` if neither did.
    """
    if joiner_race is not None:
        return joiner_race
    if host_pin is not None:
        return host_pin
    return Race.Random


async def wait_for_joiner(awaitable: Awaitable[_T], timeout: float) -> _T:
    """Bound how long the host waits for a joiner (ticket #12's timeout
    acceptance criterion) around whatever awaitable actually represents
    "a joiner showed up" in production -- the host's own blocking
    `join_game` call (see `sdk.join`'s docstring: that call doesn't return
    until the peer's engine handshake resolves, so wrapping it in a plain
    `asyncio.wait_for` is sufficient; there's no separate "waiting" signal
    to hook into). Kept generic and given a stub awaitable in tests so this
    seam doesn't require a real SC2 client.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise JoinTimeoutError(f"No joiner connected within {timeout}s.") from exc
