"""Install Blizzard's official headless Linux SC2 package.

This is the client Blizzard distributes specifically for AI/bot development:
no Battle.net launcher, no GUI, no display required. It is documented (with
exact download URLs and the shared unzip password) in the "Downloads" section
of https://github.com/Blizzard/s2client-proto -- the packages themselves are
served from Blizzard's Akamai-fronted distribution host.

Reference implementation this mirrors: the official BurnySc2/python-sc2-docker
Dockerfile, which does the Linux-only equivalent of this in shell:
    wget http://blzdistsc2-a.akamaihd.net/Linux/SC2.$VERSION.zip
    unzip -P iagreetotheeula SC2.$VERSION.zip
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from install.paths import Sc2Installation, has_valid_install, platform_name

#: Latest headless package listed under "Linux Packages" in
#: https://github.com/Blizzard/s2client-proto#downloads as of this writing.
DEFAULT_SC2_VERSION = "4.10"

#: Verified reachable (HTTP 200, ~4.1 GB) at time of writing.
_DOWNLOAD_URL_TEMPLATE = "http://blzdistsc2-a.akamaihd.net/Linux/SC2.{version}.zip"

#: Every headless/map package Blizzard distributes this way shares this
#: password -- it exists only to gate the "AI and Machine Learning use"
#: license acknowledgement, not to keep the content secret.
UNZIP_PASSWORD = b"iagreetotheeula"

#: Matches python-sc2's own Linux default (sc2.paths.BASEDIR["Linux"]), so a
#: headless install lands wherever python-sc2 would look for it with no
#: SC2PATH override required.
DEFAULT_LINUX_INSTALL_DIR = Path("~/StarCraftII").expanduser()


class UnsupportedPlatformError(RuntimeError):
    """Raised when asked to install the headless Linux package on a
    non-Linux host. The package is a native Linux ELF binary; it cannot run
    under macOS or Windows, so there is nothing this function can usefully do
    there. On those platforms, `setup` should rely on Battle.net detection
    instead (see install.battlenet)."""


class HeadlessInstallError(RuntimeError):
    """Raised when the download or extraction did not produce a valid
    install (e.g. network failure, corrupted archive, unexpected layout)."""


@dataclass(frozen=True)
class DownloadResult:
    url: str
    bytes_written: int


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed Blizzard CDN URL
        return response.read()


def _extract_with_system_unzip(zip_path: Path, dest: Path, password: bytes) -> bool:
    """Extract via the system `unzip` binary, which decrypts PKWARE/ZipCrypto
    entries in C. Orders of magnitude faster than Python's stdlib `zipfile`
    for this ~4GB encrypted archive -- see the ticket #9 implementation notes
    for the measured extraction time (~1GB/hour) that motivated this.

    Returns False (does nothing) if `unzip` isn't on PATH, so the caller can
    fall back to the always-available pure-Python path -- this keeps the
    function correct on any host, just slow on ones without `unzip` installed.
    """
    unzip_bin = shutil.which("unzip")
    if unzip_bin is None:
        return False

    result = subprocess.run(
        [unzip_bin, "-q", "-o", "-P", password.decode(), str(zip_path), "-d", str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise HeadlessInstallError(
            f"System unzip failed extracting {zip_path} (exit {result.returncode}): {result.stderr.strip()}"
        )
    return True


def _extract_with_python_zipfile(zip_path: Path, dest: Path, password: bytes) -> None:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(path=dest, pwd=password)
    except zipfile.BadZipFile as exc:
        raise HeadlessInstallError(f"{zip_path} was not a valid zip archive.") from exc
    except RuntimeError as exc:
        # zipfile raises plain RuntimeError for a bad/missing password.
        raise HeadlessInstallError(
            f"Failed to extract {zip_path} -- the archive password may have changed. "
            f"See https://github.com/Blizzard/s2client-proto#downloads for the current password."
        ) from exc


def install_headless_linux(
    dest: Path | None = None,
    version: str = DEFAULT_SC2_VERSION,
    force: bool = False,
    downloader=_download,
) -> Sc2Installation:
    """Download and extract Blizzard's headless Linux SC2 package.

    Args:
        dest: install directory. Defaults to ``~/StarCraftII`` -- the same
            default python-sc2 uses on Linux, so no SC2PATH override is
            needed afterwards.
        version: package version suffix, e.g. "4.10" -> SC2.4.10.zip.
        force: re-download/extract even if a valid install already exists at
            ``dest``.
        downloader: injectable for testing; takes a URL, returns raw bytes.
            Defaults to a real HTTP GET.

    Raises:
        UnsupportedPlatformError: if not running on Linux.
        HeadlessInstallError: if the download/extraction didn't produce a
            valid install.
    """
    pf = platform_name()
    if pf != "Linux":
        raise UnsupportedPlatformError(
            f"install_headless_linux() requires Linux (got platform '{pf}'). "
            "The headless package is a native Linux binary and cannot run here. "
            "On macOS/Windows, install SC2 via Battle.net instead -- "
            "install.battlenet.detect_battlenet_install() will then find it."
        )

    dest = (dest or DEFAULT_LINUX_INSTALL_DIR).expanduser()

    if not force and has_valid_install(dest, pf):
        return Sc2Installation(path=dest, source="headless")

    dest.mkdir(parents=True, exist_ok=True)

    url = _DOWNLOAD_URL_TEMPLATE.format(version=version)
    raw = downloader(url)

    # Extraction needs a real file on disk either way (the system `unzip`
    # path requires one, and writing once up front avoids holding the ~4GB
    # archive in memory twice -- once as `raw`, once inside zipfile).
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        if not _extract_with_system_unzip(tmp_path, dest, UNZIP_PASSWORD):
            _extract_with_python_zipfile(tmp_path, dest, UNZIP_PASSWORD)
    finally:
        tmp_path.unlink(missing_ok=True)

    # The archive extracts to a single top-level "StarCraftII" directory;
    # flatten it into `dest` if that's not already what `dest` is named.
    nested = dest / "StarCraftII"
    if nested.is_dir() and not has_valid_install(dest, pf):
        for item in nested.iterdir():
            item.rename(dest / item.name)
        nested.rmdir()

    if not has_valid_install(dest, pf):
        raise HeadlessInstallError(
            f"Extraction to {dest} completed but no valid SC2 install (Versions/Base*/SC2_x64) was found there."
        )

    return Sc2Installation(path=dest, source="headless")
