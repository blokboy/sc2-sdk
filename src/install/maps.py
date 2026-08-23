"""Sync the fixed map pool this project's tests and scripts run on.

Source: Blizzard's official "Ladder2019Season1" map pack, listed under the
"Map Packs" section of https://github.com/Blizzard/s2client-proto#downloads.
Verified (by downloading and listing it while writing this module) to
contain exactly these seven maps, nothing more:

    AutomatonLE.SC2Map        (~0.9 MB)
    CyberForestLE.SC2Map      (~1.1 MB)
    KairosJunctionLE.SC2Map   (~1.4 MB)
    KingsCoveLE.SC2Map        (~1.4 MB)
    NewRepugnancyLE.SC2Map    (~1.4 MB)
    PortAleksanderLE.SC2Map   (~1.4 MB)
    YearZeroLE.SC2Map         (~1.6 MB)

All seven are standard, small, reliable ladder maps from the same season's
pool. AutomatonLE is this project's primary fixed test map; KairosJunctionLE
is kept as the documented fallback/second map used by the integration
harness; the rest sync alongside them so any map name in the pack is
playable without a one-off `--maps` override.
"""

from __future__ import annotations

import io
import urllib.request
import zipfile
from pathlib import Path

from install.paths import maps_dir

_MAP_PACK_URL = "http://blzdistsc2-a.akamaihd.net/MapPacks/Ladder2019Season1.zip"
_MAP_PACK_PASSWORD = b"iagreetotheeula"

#: Map names (matching sc2.maps.get()'s lookup, i.e. the .SC2Map stem) this
#: project's setup syncs. AutomatonLE/KairosJunctionLE are what the
#: integration harness actually runs on; the rest are the pack's remaining
#: maps, synced too so they're playable out of the box.
DEFAULT_MAPS = (
    "AutomatonLE",
    "KairosJunctionLE",
    "CyberForestLE",
    "KingsCoveLE",
    "NewRepugnancyLE",
    "PortAleksanderLE",
    "YearZeroLE",
)


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed Blizzard CDN URL
        return response.read()


def sync_maps(
    install_path: Path,
    maps: tuple[str, ...] = DEFAULT_MAPS,
    force: bool = False,
    downloader=_download,
) -> list[str]:
    """Ensure each map in ``maps`` exists (as ``<name>.SC2Map``) directly
    under the install's Maps directory, matching sc2.maps.get()'s lookup
    convention (root-level file, not nested under a season subfolder).

    Returns the list of map names now present. Only downloads the map pack
    if at least one requested map is missing (or ``force=True``).
    """
    target_dir = maps_dir(install_path)
    target_dir.mkdir(parents=True, exist_ok=True)

    missing = [name for name in maps if force or not (target_dir / f"{name}.SC2Map").exists()]
    if missing:
        raw = downloader(_MAP_PACK_URL)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                filename = Path(info.filename).name
                if not filename.endswith(".SC2Map"):
                    continue
                stem = filename[: -len(".SC2Map")]
                if stem not in maps:
                    continue
                with archive.open(info, pwd=_MAP_PACK_PASSWORD) as src:
                    (target_dir / filename).write_bytes(src.read())

    present = [name for name in maps if (target_dir / f"{name}.SC2Map").exists()]
    still_missing = set(maps) - set(present)
    if still_missing:
        raise RuntimeError(
            f"Map pack sync did not produce: {sorted(still_missing)}. "
            f"Expected them in {_MAP_PACK_URL}; the pack contents may have changed."
        )
    return present
