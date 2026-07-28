"""Locating, downloading and verifying the Syncthing binary.

oXeioSync does not bundle Syncthing. On first run it looks for an existing
binary and, failing that, fetches the latest release from GitHub into its own
data directory. Downloads are checked against the SHA-256 digests published
alongside the release before anything is written into place — we are about to
execute this file, so a truncated or mismatched download must not survive.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from .. import paths

log = logging.getLogger(__name__)

GITHUB_RELEASES_API = "https://api.github.com/repos/syncthing/syncthing/releases"
DOWNLOAD_CHUNK = 64 * 1024
NETWORK_TIMEOUT = 30.0

#: Called with (bytes_so_far, total_bytes); total is 0 when unknown.
ProgressCallback = Callable[[int, int], None]


class SyncthingBinaryError(Exception):
    """Raised when a binary cannot be found, downloaded or verified."""


@dataclass(frozen=True)
class SyncthingRelease:
    """A downloadable Syncthing build for this platform."""

    version: str
    asset_name: str
    asset_url: str
    asset_size: int
    checksums_url: str | None


# --------------------------------------------------------------------- locating
def find_syncthing(configured_path: str = "") -> Path | None:
    """Return the Syncthing binary to use, or None if there isn't one.

    Search order: the user's explicit override, the copy oXeioSync manages
    itself, a copy shipped beside the application, then ``PATH``.
    """
    if configured_path:
        candidate = Path(configured_path).expanduser()
        return candidate if candidate.is_file() else None

    for candidate in (paths.managed_syncthing_path(), paths.bundled_syncthing_path()):
        if candidate.is_file():
            return candidate

    on_path = shutil.which(paths.syncthing_binary_name())
    return Path(on_path) if on_path else None


def probe_version(binary: Path) -> str | None:
    """Return the version string of a Syncthing binary, or None if it won't run.

    Also serves as a sanity check that the file is actually Syncthing and not,
    say, an HTML error page that got saved with the right name.
    """
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not run %s: %s", binary, exc)
        return None

    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"syncthing\s+(v[\d][^\s]*)", output, re.IGNORECASE)
    if match:
        return match.group(1)
    log.warning("Unexpected --version output from %s: %r", binary, output[:200])
    return None


def no_window_flags() -> int:
    """CREATE_NO_WINDOW on Windows, 0 elsewhere.

    Syncthing is a console application; without this every launch flashes a
    console window, which is unacceptable for a tray app.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ------------------------------------------------------------------- downloading
def platform_asset_infix() -> str:
    """The ``<os>-<arch>`` fragment GitHub release assets are named with."""
    system = platform.system().lower()
    os_part = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system)
    if os_part is None:
        raise SyncthingBinaryError(f"Unsupported operating system: {platform.system()}")

    machine = platform.machine().lower()
    arch_part = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "i386": "386",
        "i686": "386",
        "x86": "386",
    }.get(machine)
    if arch_part is None:
        raise SyncthingBinaryError(f"Unsupported CPU architecture: {platform.machine()}")

    return f"{os_part}-{arch_part}"


def latest_release(include_prereleases: bool = False) -> SyncthingRelease:
    """Ask GitHub for the newest Syncthing release built for this platform."""
    try:
        response = requests.get(
            GITHUB_RELEASES_API,
            params={"per_page": 20},
            timeout=NETWORK_TIMEOUT,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        releases = response.json()
    except requests.RequestException as exc:
        raise SyncthingBinaryError(f"Could not reach GitHub: {exc}") from exc
    except ValueError as exc:
        raise SyncthingBinaryError(f"GitHub returned an unreadable response: {exc}") from exc

    infix = platform_asset_infix()
    for release in releases:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not include_prereleases:
            continue

        assets = release.get("assets") or []
        checksums_url = next(
            (a["browser_download_url"] for a in assets if a.get("name") == "sha256sum.txt.asc"),
            None,
        )
        for asset in assets:
            name = asset.get("name", "")
            if infix in name and name.endswith((".zip", ".tar.gz")):
                return SyncthingRelease(
                    version=release.get("tag_name", "unknown"),
                    asset_name=name,
                    asset_url=asset["browser_download_url"],
                    asset_size=int(asset.get("size") or 0),
                    checksums_url=checksums_url,
                )

    # These messages are shown to the user in the download dialog, so they use
    # the application's own vocabulary rather than the upstream project's name.
    raise SyncthingBinaryError(
        f"No release found with a {infix} build. "
        "Set the engine path manually in Settings."
    )


def download_and_install(
    release: SyncthingRelease,
    destination: Path | None = None,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Download ``release``, verify it, and put the binary at ``destination``.

    The archive is downloaded and unpacked in a temporary directory; the final
    binary is only moved into place once its digest checks out. Returns the
    path to the installed binary.
    """
    destination = destination or paths.managed_syncthing_path()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="oxeiosync-syncthing-") as tmp_name:
        tmp = Path(tmp_name)
        archive = tmp / release.asset_name

        _download_file(release.asset_url, archive, release.asset_size, progress, is_cancelled)
        _verify_checksum(archive, release)
        extracted = _extract_binary(archive, tmp / "unpacked")

        if probe_version(extracted) is None:
            raise SyncthingBinaryError(
                "The downloaded file does not behave like the sync engine; "
                "refusing to install it."
            )

        # Replace atomically where the platform allows it. On Windows a running
        # binary cannot be overwritten, so move the old one aside first.
        if destination.exists():
            backup = destination.with_suffix(destination.suffix + ".old")
            backup.unlink(missing_ok=True)
            try:
                destination.replace(backup)
            except OSError as exc:
                raise SyncthingBinaryError(
                    f"Could not replace {destination} — is the engine still running? ({exc})"
                ) from exc

        shutil.move(str(extracted), str(destination))

    destination.chmod(destination.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log.info("Installed Syncthing %s at %s", release.version, destination)
    return destination


def _download_file(
    url: str,
    target: Path,
    expected_size: int,
    progress: ProgressCallback | None,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    try:
        with requests.get(url, stream=True, timeout=NETWORK_TIMEOUT) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or expected_size or 0)
            done = 0
            with target.open("wb") as handle:
                for chunk in response.iter_content(DOWNLOAD_CHUNK):
                    if is_cancelled is not None and is_cancelled():
                        raise SyncthingBinaryError("Download cancelled")
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
    except requests.RequestException as exc:
        raise SyncthingBinaryError(f"Download failed: {exc}") from exc


def _verify_checksum(archive: Path, release: SyncthingRelease) -> None:
    """Compare the download against the release's published SHA-256 digest."""
    if not release.checksums_url:
        raise SyncthingBinaryError(
            f"Release {release.version} publishes no checksums; refusing to install "
            "an unverified binary."
        )

    try:
        response = requests.get(release.checksums_url, timeout=NETWORK_TIMEOUT)
        response.raise_for_status()
        listing = response.text
    except requests.RequestException as exc:
        raise SyncthingBinaryError(f"Could not fetch checksums: {exc}") from exc

    expected = _find_digest(listing, release.asset_name)
    if expected is None:
        raise SyncthingBinaryError(
            f"No SHA-256 digest published for {release.asset_name}; "
            "refusing to install an unverified binary."
        )

    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    actual = digest.hexdigest()

    if actual != expected:
        raise SyncthingBinaryError(
            f"Checksum mismatch for {release.asset_name}: "
            f"expected {expected}, got {actual}. Download discarded."
        )
    log.debug("Verified SHA-256 of %s", release.asset_name)


def _find_digest(listing: str, asset_name: str) -> str | None:
    """Pull one file's digest out of a ``sha256sum``-style listing.

    The published file is a clear-signed PGP message; the digest lines inside
    it are plain ``<hex>  <filename>`` pairs, which is all we need.
    """
    for line in listing.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip())
        if match and Path(match.group(2).strip()).name == asset_name:
            return match.group(1).lower()
    return None


def _extract_binary(archive: Path, into: Path) -> Path:
    """Unpack the Syncthing executable out of a release archive."""
    into.mkdir(parents=True, exist_ok=True)
    wanted = paths.syncthing_binary_name()

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            member = _match_member(zf.namelist(), wanted)
            target = into / wanted
            with zf.open(member) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tf:
            member = _match_member(tf.getnames(), wanted)
            extracted = tf.extractfile(member)
            if extracted is None:
                raise SyncthingBinaryError(f"{member} in {archive.name} is not a regular file")
            target = into / wanted
            with extracted, target.open("wb") as sink:
                shutil.copyfileobj(extracted, sink)
    else:
        raise SyncthingBinaryError(f"Don't know how to unpack {archive.name}")

    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    return target


def _match_member(names: list[str], wanted: str) -> str:
    """Find the executable inside an archive whose layout we don't control."""
    for name in names:
        # Guard against path traversal in a downloaded archive, and ignore the
        # directory entries and extra files (LICENSE, README) alongside it.
        if ".." in Path(name).parts:
            continue
        if Path(name).name == wanted:
            return name
    raise SyncthingBinaryError(f"No {wanted} found inside the release archive")
