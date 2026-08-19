"""Detection of an existing Battle.net-managed SC2 install (Windows/Mac).

`setup` prefers this over installing the headless Linux package: if a user
already has SC2 via Battle.net, re-installing would be wasteful and would
prevent them from watching games in the rendered client.
"""

from __future__ import annotations

from install.paths import (
    Sc2Installation,
    default_base_dir,
    has_valid_install,
    platform_name,
    read_battlenet_base_dir,
)

#: Battle.net only exists on these platforms; Linux has no Battle.net client.
BATTLENET_PLATFORMS = ("Windows", "Darwin")


def detect_battlenet_install(pf: str | None = None) -> Sc2Installation | None:
    """Return the existing Battle.net install, if one is present and valid.

    Checks (in order): Battle.net's own ExecuteInfo.txt record of the active
    install, then the platform's conventional default install directory
    (covers the case where ExecuteInfo.txt is missing/stale but the game is
    still installed at the default location). Returns ``None`` on Linux,
    where there is no Battle.net client, or if nothing valid is found.
    """
    pf = pf or platform_name()
    if pf not in BATTLENET_PLATFORMS:
        return None

    base = read_battlenet_base_dir(pf)
    if base is not None and has_valid_install(base, pf):
        return Sc2Installation(path=base, source="battlenet")

    default_base = default_base_dir(pf)
    if default_base is not None and has_valid_install(default_base, pf):
        return Sc2Installation(path=default_base, source="battlenet")

    return None
