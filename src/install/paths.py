"""Platform-aware SC2 install-path resolution.

This mirrors the lookup order that ``python-sc2`` itself uses internally
(``sc2.paths.Paths``), so that what this module reports as "installed" agrees
with what ``python-sc2`` will actually find at runtime:

1. the ``SC2PATH`` environment variable, if set
2. a Battle.net ``ExecuteInfo.txt`` (written by the Battle.net launcher on
   Windows/Mac), parsed for the install directory it points at
3. the platform's conventional default install directory

Unlike ``sc2.paths.Paths`` (which calls ``sys.exit(1)`` when nothing is
found -- fine for a bot script, fatal for a test suite or a setup tool), every
function here is a pure, side-effect-free lookup that returns ``None`` on
failure. This is deliberate: ``tests/conftest.py`` needs to probe for a local
install *without* risking killing the pytest process, and ``install.cli``
needs to distinguish "nothing found" from "found, but broken" to decide
whether to install the headless package or report an actionable error.
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path

#: Directories checked, per platform, for a Battle.net-managed install.
DEFAULT_BASE_DIR: dict[str, str] = {
    "Windows": "C:/Program Files (x86)/StarCraft II",
    "Darwin": "/Applications/StarCraft II",
    "Linux": "~/StarCraftII",
}

#: Where the Battle.net launcher records the active install path, per platform.
#: Linux has no Battle.net launcher, so there is no entry for it.
EXECUTE_INFO_RELATIVE_PATH: dict[str, str] = {
    "Windows": "Documents/StarCraft II/ExecuteInfo.txt",
    "Darwin": "Library/Application Support/Blizzard/StarCraft II/ExecuteInfo.txt",
}

#: SC2 client executable, relative to an install's base directory + version dir.
BINARY_NAME: dict[str, str] = {
    "Windows": "SC2_x64.exe",
    "Darwin": "SC2.app/Contents/MacOS/SC2",
    "Linux": "SC2_x64",
}


def platform_name() -> str:
    """Return 'Windows', 'Darwin', or 'Linux'.

    Honors the ``SC2PF`` override that ``python-sc2`` itself recognizes, so a
    forced platform (e.g. for Wine setups) stays consistent between this
    module and the library.
    """
    return os.environ.get("SC2PF", platform.system())


def home_dir() -> Path:
    return Path.home().expanduser()


def execute_info_path(pf: str | None = None) -> Path | None:
    pf = pf or platform_name()
    rel = EXECUTE_INFO_RELATIVE_PATH.get(pf)
    if rel is None:
        return None
    return home_dir() / Path(rel)


def parse_execute_info(text: str) -> str | None:
    """Extract the install base directory from ExecuteInfo.txt contents.

    The file contains a line like::

        executable = C:\\Program Files (x86)\\StarCraft II\\Versions\\Base12345\\SC2_x64.exe

    We only need the portion before ``Versions``.
    """
    match = re.search(r" = (.*)Versions", text)
    if not match:
        return None
    return match.group(1)


def read_battlenet_base_dir(pf: str | None = None) -> Path | None:
    """Read and parse ExecuteInfo.txt, if present, for this platform."""
    info_path = execute_info_path(pf)
    if info_path is None or not info_path.is_file():
        return None
    text = info_path.read_text(errors="ignore")
    if not text:
        return None
    base = parse_execute_info(text)
    if base is None:
        return None
    return Path(base)


def default_base_dir(pf: str | None = None) -> Path | None:
    pf = pf or platform_name()
    raw = DEFAULT_BASE_DIR.get(pf)
    if raw is None:
        return None
    return Path(raw).expanduser()


def has_valid_install(base: Path, pf: str | None = None) -> bool:
    """True if ``base`` looks like a real SC2 install: a Versions/Base* dir
    containing the platform's executable."""
    pf = pf or platform_name()
    versions_dir = base / "Versions"
    if not versions_dir.is_dir():
        return False
    binary_name = BINARY_NAME.get(pf)
    if binary_name is None:
        return False
    for entry in versions_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("Base") and (entry / binary_name).exists():
            return True
    return False


def maps_dir(base: Path) -> Path:
    """Match python-sc2's own convention: prefer a lowercase 'maps' dir if it
    already exists, otherwise use 'Maps' (created if needed)."""
    lower = base / "maps"
    if lower.is_dir():
        return lower
    return base / "Maps"


@dataclass(frozen=True)
class Sc2Installation:
    path: Path
    source: str  # "battlenet" | "headless" | "env"

    @property
    def maps_path(self) -> Path:
        return maps_dir(self.path)


def find_installed_base(pf: str | None = None) -> Sc2Installation | None:
    """Resolve a working SC2 install using the same priority order
    ``python-sc2`` uses at runtime: SC2PATH env var, then Battle.net's
    ExecuteInfo.txt, then the platform default directory.

    Returns ``None`` (never raises, never exits) if nothing valid is found.
    """
    pf = pf or platform_name()

    env_path = os.environ.get("SC2PATH")
    if env_path:
        base = Path(env_path).expanduser()
        if has_valid_install(base, pf):
            return Sc2Installation(path=base, source="env")

    bn_base = read_battlenet_base_dir(pf)
    if bn_base is not None and has_valid_install(bn_base, pf):
        return Sc2Installation(path=bn_base, source="battlenet")

    default_base = default_base_dir(pf)
    if default_base is not None and has_valid_install(default_base, pf):
        return Sc2Installation(path=default_base, source="battlenet" if pf != "Linux" else "headless")

    return None
