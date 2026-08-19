"""`setup` entry point: get a working local SC2 client + map pool with one
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
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from install.battlenet import detect_battlenet_install
from install.headless import DEFAULT_SC2_VERSION, HeadlessInstallError, install_headless_linux
from install.maps import DEFAULT_MAPS, sync_maps
from install.paths import Sc2Installation, platform_name


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

    raise SystemExit(
        f"[setup] No Battle.net-managed SC2 install found on {pf}, and the headless "
        "package only runs on Linux. Install StarCraft II via Battle.net "
        "(https://starcraft2.com), then re-run setup -- or set SC2PATH to point at "
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
