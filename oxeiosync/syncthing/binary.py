"""Locating, downloading and verifying the Syncthing binary.

A release build ships an engine, so the usual first run finds one beside the
application and no network is involved at all. This module is what runs when
that is not the case: it looks for an existing binary — the copy oXeioSync
manages, the copy shipped with it, one on ``PATH`` — and, failing all of those,
fetches a release into its own data directory.

Downloads are checked against the SHA-256 digests published alongside the
release before anything is written into place; we are about to execute this
file, so a truncated or mismatched download must not survive. The release list
is read from GitHub, falling back to Syncthing's own metadata server, because
GitHub rate-limits unauthenticated callers by address and a shared connection
reaches that limit through no fault of the person waiting on the dialog.
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

#: Syncthing's own release metadata, which its built-in updater uses. It serves
#: the same release list in the same shape, but without GitHub's limit of 60
#: unauthenticated requests per hour per address — a limit shared offices, VPNs
#: and mobile networks reach through no fault of the user sitting in front of
#: this dialog. Tried whenever GitHub cannot be read.
UPGRADE_META_URL = "https://upgrades.syncthing.net/meta.json"

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
    itself, the copy shipped with the application, then ``PATH``. The managed
    copy comes first so that an engine updated after installation wins over the
    older one the installer laid down.
    """
    if configured_path:
        candidate = Path(configured_path).expanduser()
        return candidate if candidate.is_file() else None

    for candidate in (
        paths.managed_syncthing_path(),
        *paths.bundled_syncthing_candidates(),
    ):
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


def latest_release(
    include_prereleases: bool = False,
    *,
    tag: str | None = None,
    infix: str | None = None,
) -> SyncthingRelease:
    """Find the newest Syncthing release built for this platform.

    GitHub is asked first because it is authoritative; if it is unreachable or
    has rate-limited us, Syncthing's own metadata server is asked instead. Both
    hand back the same release list, and either way the download itself is
    still checked against the published SHA-256 digest.

    ``tag`` pins a particular release instead of taking the newest, and
    ``infix`` overrides the ``<os>-<arch>`` fragment. Neither is used by the
    application: they exist so the build tooling can vendor a known engine, for
    a machine other than the one doing the building, through this same code.
    """
    infix = infix or platform_asset_infix()
    problems: list[str] = []

    for label, url, params in (
        ("GitHub", GITHUB_RELEASES_API, {"per_page": 20}),
        ("the release mirror", UPGRADE_META_URL, {"arch": infix}),
    ):
        try:
            releases = _fetch_releases(label, url, params)
        except SyncthingBinaryError as exc:
            log.warning("Release lookup via %s failed: %s", label, exc)
            problems.append(str(exc))
            continue

        found = _select_asset(releases, infix, include_prereleases, tag)
        if found is not None:
            log.info("Found %s via %s", found.version, label)
            return found
        wanted = f"{tag} {infix}" if tag else infix
        problems.append(f"{label} listed no {wanted} build")

    # These messages are shown to the user in the download dialog, so they use
    # the application's own vocabulary rather than the upstream project's name.
    raise SyncthingBinaryError(
        "Could not work out which sync engine to download.\n\n"
        + "\n".join(f"• {problem}" for problem in problems)
        + "\n\nCheck the connection and try again, or download the engine "
        "yourself and point at it with the engine path in Settings."
    )


def _fetch_releases(label: str, url: str, params: dict[str, object]) -> list[dict]:
    """Read a list of releases from ``url``."""
    try:
        response = requests.get(
            url,
            params=params,
            timeout=NETWORK_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        releases = response.json()
    except requests.RequestException as exc:
        raise SyncthingBinaryError(f"Could not reach {label}: {exc}") from exc
    except ValueError as exc:
        raise SyncthingBinaryError(f"{label} returned an unreadable response: {exc}") from exc

    if not isinstance(releases, list):
        raise SyncthingBinaryError(f"{label} returned an unexpected release list")
    return releases


def _select_asset(
    releases: list[dict],
    infix: str,
    include_prereleases: bool,
    tag: str | None = None,
) -> SyncthingRelease | None:
    """Pick this platform's archive out of a release listing, newest first."""
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if tag is not None:
            # Asking for a tag by name is deliberate enough to override the
            # prerelease filter: someone pinning an -rc knows what they typed.
            if release.get("tag_name") != tag:
                continue
        elif release.get("prerelease") and not include_prereleases:
            continue

        assets = [a for a in (release.get("assets") or []) if isinstance(a, dict)]
        checksums_url = next(
            (
                _asset_url(a)
                for a in assets
                if a.get("name") == "sha256sum.txt.asc" and _asset_url(a)
            ),
            None,
        )
        for asset in assets:
            name = asset.get("name", "")
            url = _asset_url(asset)
            if url and infix in name and name.endswith((".zip", ".tar.gz")):
                return SyncthingRelease(
                    version=release.get("tag_name", "unknown"),
                    asset_name=name,
                    asset_url=url,
                    asset_size=int(asset.get("size") or 0),
                    checksums_url=checksums_url,
                )
    return None


def _asset_url(asset: dict) -> str | None:
    """The download URL of a release asset, whichever source described it.

    GitHub's API puts it in ``browser_download_url`` and uses ``url`` for the
    API handle; the mirror only has ``url``, and it is the download link.
    Anything that is not plain HTTPS is ignored — we execute what we fetch.
    """
    for key in ("browser_download_url", "url"):
        value = asset.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return None


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
        archive = fetch_verified_archive(release, tmp, progress, is_cancelled)
        extracted = extract_binary(archive, tmp / "unpacked")

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


def fetch_verified_archive(
    release: SyncthingRelease,
    into: Path,
    progress: ProgressCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Download ``release``'s archive into ``into`` and check its digest.

    Returns the path to the verified archive. Anything that fails verification
    raises instead of being returned — the caller is about to unpack a file it
    intends to execute. Split out from :func:`download_and_install` so the build
    tooling can vendor an engine through exactly the same checks the app uses.
    """
    into.mkdir(parents=True, exist_ok=True)
    # The asset name came off the network; take the bare filename so it cannot
    # steer the write out of the directory we chose.
    archive = into / Path(release.asset_name).name

    _download_file(release.asset_url, archive, release.asset_size, progress, is_cancelled)
    _verify_checksum(archive, release)
    return archive


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
    """Compare the download against the release's published SHA-256 digest.

    What this establishes is that the bytes on disk are the bytes the release
    published, fetched over TLS from the same origin as the digest list. The
    list is a clear-signed PGP message and its signature is *not* checked, so
    this defends against a corrupted or truncated download, not against a
    compromised release origin.
    """
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


def extract_binary(archive: Path, into: Path, name: str | None = None) -> Path:
    """Unpack the Syncthing executable out of a release archive.

    ``name`` overrides the filename to look for, which the build tooling needs
    when it vendors an engine for a platform other than the one it runs on.
    """
    into.mkdir(parents=True, exist_ok=True)
    wanted = name or paths.syncthing_binary_name()

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
    """Find the executable inside an archive whose layout we don't control.

    On platforms where the binary has no extension the name is ambiguous: a
    release archive carries the executable at its top level *and* service
    definitions also called ``syncthing`` deeper down (``etc/linux-systemd``,
    ``etc/freebsd-rc`` and the like). Picking the first match extracted an rc
    script instead of the engine. The real executable is the shallowest one, so
    among the matches the fewest-segment path wins — which on Windows, where the
    name ends in ``.exe`` and is unique, changes nothing.
    """
    matches = [
        name
        for name in names
        # Guard against path traversal in a downloaded archive, and ignore the
        # directory entries and extra files (LICENSE, README) alongside it.
        if ".." not in Path(name).parts and Path(name).name == wanted
    ]
    if not matches:
        raise SyncthingBinaryError(f"No {wanted} found inside the release archive")
    return min(matches, key=lambda name: len(Path(name).parts))
