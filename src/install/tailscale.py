"""Detection and guided install of Tailscale -- the VPN mesh `sdk.host_join`
(ticket #12) relies on to reach a joiner's machine across networks, since
this project deliberately runs no relay/NAT-traversal of its own (see
`host_join.py`'s module docstring). Mirrors `install/battlenet.py`'s
detect-first shape and `install/cli.py`'s poll-based guided install (see
`_prompt_for_battlenet_install` there for why polling, not `input()`): no
supported silent installer exists for either, so both wait for a human to
finish an interactive install elsewhere.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
import webbrowser

TAILSCALE_DOWNLOAD_URL = "https://tailscale.com/download"

_POLL_INTERVAL_SECONDS = 5
_POLL_STATUS_EVERY_SECONDS = 30
_DEFAULT_MAX_WAIT_SECONDS = 20 * 60

#: Mac direct-download/App-Store builds don't always put `tailscale` on
#: PATH -- this is where the bundled CLI binary lives if so.
_MACOS_APP_BINARY = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _tailscale_binary() -> str | None:
    return shutil.which("tailscale") or (_MACOS_APP_BINARY if os.path.exists(_MACOS_APP_BINARY) else None)


def detect_tailscale_ip() -> str | None:
    """Return this machine's Tailscale IPv4 address if the `tailscale` CLI
    is installed and logged into a tailnet, else `None`."""
    binary = _tailscale_binary()
    if binary is None:
        return None
    try:
        result = subprocess.run([binary, "ip", "-4"], capture_output=True, text=True, timeout=10, check=False)
    except OSError:
        return None
    ip = result.stdout.strip()
    return ip or None


def prompt_for_tailscale_install(max_wait_seconds: float = _DEFAULT_MAX_WAIT_SECONDS) -> str | None:
    """Guide the user through installing Tailscale, then poll for a usable
    IP -- deliberately polling instead of blocking on `input()`, see
    `install.cli._prompt_for_battlenet_install`'s docstring for why the
    same shape applies here. Skips straight to `None` (no browser, no
    wait) when the `CI` env var is set."""
    if os.environ.get("CI"):
        return None

    print("[host] No Tailscale IP found -- you and your joiner need a way to reach each other,")
    print(f"[host] and this project runs no relay/NAT-traversal of its own. Opening {TAILSCALE_DOWNLOAD_URL}")
    print("[host] -- install it and log in, and this will pick up your Tailscale IP automatically.")
    with contextlib.suppress(Exception):
        webbrowser.open(TAILSCALE_DOWNLOAD_URL)

    print(
        f"[host] Waiting (checking every {_POLL_INTERVAL_SECONDS}s, up to "
        f"{int(max_wait_seconds // 60)} min) -- Ctrl-C to give up now."
    )
    deadline = time.monotonic() + max_wait_seconds
    last_status = time.monotonic()
    try:
        while time.monotonic() < deadline:
            ip = detect_tailscale_ip()
            if ip:
                print(f"[host] Found Tailscale IP {ip} -- using it.")
                return ip
            if time.monotonic() - last_status >= _POLL_STATUS_EVERY_SECONDS:
                print("[host] Still waiting for Tailscale to be installed and logged in...")
                last_status = time.monotonic()
            time.sleep(_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n[host] Gave up waiting.")
        return None

    print("[host] Gave up waiting -- pass --host-ip explicitly, or re-run once Tailscale is ready.")
    return None
