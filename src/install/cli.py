"""`setup` entry point: get a working local SC2 client + map pool with one
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
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
import webbrowser
from pathlib import Path

from install.battlenet import detect_battlenet_install
from install.headless import DEFAULT_SC2_VERSION, HeadlessInstallError, install_headless_linux
from install.maps import DEFAULT_MAPS, sync_maps
from install.paths import Sc2Installation, platform_name

BATTLENET_DOWNLOAD_URL = "https://starcraft2.com"

_POLL_INTERVAL_SECONDS = 5
_POLL_STATUS_EVERY_SECONDS = 30
_DEFAULT_MAX_WAIT_SECONDS = 20 * 60


def _prompt_for_battlenet_install(
    pf: str, max_wait_seconds: float = _DEFAULT_MAX_WAIT_SECONDS
) -> Sc2Installation | None:
    """Guide the user through installing SC2 via Battle.net, then poll for
    it -- deliberately polling instead of blocking on `input()`. Nothing
    that runs this command and only surfaces output once it exits (a
    real terminal included, since a human's keystrokes there aren't
    piped back to this process's stdin as it runs -- an agent coding
    tool's non-interactive shell command execution, which is what this
    was actually built against, doubly so) can deliver a keystroke this
    process could read mid-run, so a "read a line, then re-check" design
    would just hang until EOF. Polling means "check back periodically"
    works the same either way. Skips straight to `None` (no browser, no
    wait) when the `CI` env var is set, so an automated run fails fast via
    the caller's actionable-error fallback instead of polling for up to
    `max_wait_seconds` with nobody there to finish the install."""
    if os.environ.get("CI"):
        return None

    print(f"[setup] No Battle.net-managed SC2 install found on {pf}.")
    print(f"[setup] Opening {BATTLENET_DOWNLOAD_URL} -- install Battle.net + StarCraft II there (free to play).")
    with contextlib.suppress(Exception):
        webbrowser.open(BATTLENET_DOWNLOAD_URL)

    print(
        f"[setup] Waiting for the install to finish (checking every {_POLL_INTERVAL_SECONDS}s, "
        f"up to {int(max_wait_seconds // 60)} min) -- Ctrl-C to give up now."
    )
    deadline = time.monotonic() + max_wait_seconds
    last_status = time.monotonic()
    try:
        while time.monotonic() < deadline:
            existing = detect_battlenet_install(pf)
            if existing is not None:
                print(f"[setup] Found Battle.net install at {existing.path} -- using it.")
                return existing
            if time.monotonic() - last_status >= _POLL_STATUS_EVERY_SECONDS:
                print("[setup] Still waiting for the install to finish...")
                last_status = time.monotonic()
            time.sleep(_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n[setup] Gave up waiting.")
        return None

    print("[setup] Gave up waiting -- re-run setup once the install finishes.")
    return None


def _select_installation(dest: Path | None, sc2_version: str, force_headless: bool) -> Sc2Installation:
    pf = platform_name()

    if not force_headless:
        existing = detect_battlenet_install(pf)
        if existing is not None:
            print(f"[setup] Found existing Battle.net install at {existing.path} -- using it.")
            return existing

    if pf == "Linux":
        print(f"[setup] No existing install found; installing headless Linux client (SC2.{sc2_version})...")
        return install_headless_linux(dest=dest, version=sc2_version)

    prompted = _prompt_for_battlenet_install(pf)
    if prompted is not None:
        return prompted

    raise SystemExit(
        f"[setup] No Battle.net-managed SC2 install found on {pf}, and the headless "
        "package only runs on Linux. Install StarCraft II via Battle.net "
        f"({BATTLENET_DOWNLOAD_URL}), then re-run setup -- or set SC2PATH to point at "
        "an existing install."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Headless install directory (Linux only). Defaults to ~/StarCraftII.",
    )
    parser.add_argument(
        "--sc2-version",
        default=DEFAULT_SC2_VERSION,
        help=f"Headless package version to install (default: {DEFAULT_SC2_VERSION}).",
    )
    parser.add_argument(
        "--force-headless",
        action="store_true",
        help="Install the headless package even if a Battle.net install is detected.",
    )
    parser.add_argument(
        "--maps",
        nargs="*",
        default=list(DEFAULT_MAPS),
        help=f"Map names to sync (default: {' '.join(DEFAULT_MAPS)}).",
    )
    args = parser.parse_args(argv)

    try:
        installation = _select_installation(args.dest, args.sc2_version, args.force_headless)
    except HeadlessInstallError as exc:
        print(f"[setup] Headless install failed: {exc}", file=sys.stderr)
        return 1

    print(f"[setup] Syncing map pool: {', '.join(args.maps)}")
    synced = sync_maps(installation.path, maps=tuple(args.maps))
    print(f"[setup] Maps ready in {installation.maps_path}: {', '.join(synced)}")

    print(f"[setup] Done. SC2 install ({installation.source}) ready at {installation.path}")
    if installation.source == "headless":
        print(f'[setup] If python-sc2 does not auto-detect this install, run: export SC2PATH="{installation.path}"')

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
