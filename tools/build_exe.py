"""Build the application bundle, and optionally the installer image.

    python tools/build_exe.py [--clean] [--zip] [--installer]

Vendors the sync engine, regenerates the icon from the app's own drawing code,
runs PyInstaller against ``packaging/oxeiosync.spec``, and reports what came
out. The result is platform-shaped:

* **Windows** — a one-folder ``dist/oXeioSync/`` with ``oXeioSync.exe``.
  ``--zip`` packs the folder; ``--installer`` compiles ``oxeiosync.iss`` into a
  single setup executable with Inno Setup.
* **macOS** — a ``dist/oXeioSync.app`` bundle. ``--zip`` packs it with ``ditto``
  (which preserves the bundle's symlinks and executable bits); ``--installer``
  builds a drag-to-Applications ``dist/oXeioSync.dmg`` with ``hdiutil``.

The build is standalone by default: the engine is fetched once into
``build/vendor/`` and bundled, so an installed copy never needs the network to
start syncing. ``--no-engine`` opts out, and the result is then a build that
asks to download an engine on first run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "oxeiosync.spec"
ISS = ROOT / "packaging" / "oxeiosync.iss"

IS_MACOS = sys.platform == "darwin"

#: Inno Setup installs per-user by default these days, so look there first.
ISCC_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Inno Setup 6" / "ISCC.exe",
)
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "oXeioSync"
# The console build is written to its own folder, so a diagnostic build must not
# be judged against the windowed build's name.
BUILD_NAME = (
    "oXeioSync-console" if os.environ.get("OXEIOSYNC_BUILD_CONSOLE") == "1" else APP_NAME
)
ENGINE_NAME = "syncthing.exe" if os.name == "nt" else "syncthing"

# What PyInstaller produces, and where the launchable binary is inside it.
if IS_MACOS:
    APP_BUNDLE = DIST / f"{BUILD_NAME}.app"
    APP_DIR = APP_BUNDLE  # "the build", for size reporting and packing
    EXE = APP_BUNDLE / "Contents" / "MacOS" / BUILD_NAME
else:
    APP_DIR = DIST / BUILD_NAME
    EXE = APP_DIR / f"{BUILD_NAME}.exe"

# Relative places a bundled engine can sit, most likely first. The first two are
# the Windows / one-folder layout; the rest are where a macOS `.app` keeps its
# payload (Contents/Frameworks is what sys._MEIPASS points at inside a bundle).
_ENGINE_SUBDIRS = ("", "_internal", "Contents/Frameworks", "Contents/Resources", "Contents/MacOS")


def bundled_engine(app_dir: Path) -> Path | None:
    """The engine inside a built bundle, wherever the packager put it.

    Mirrors the lookup the application does at run time; a build that "succeeds"
    while leaving the engine somewhere the app will not look is a build that
    fails on the user's machine instead of on this one.
    """
    for relative in _ENGINE_SUBDIRS:
        candidate = (app_dir / relative / ENGINE_NAME) if relative else app_dir / ENGINE_NAME
        if candidate.is_file():
            return candidate
    return None


def run(command: list[str], **kwargs) -> int:
    print(f"$ {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=str(ROOT), **kwargs).returncode


def directory_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def pinned_engine_version() -> str | None:
    """The Syncthing release the build pins to, from pyproject.toml.

    Pinning keeps a given tag reproducible: without it the build vendors whatever
    engine is 'latest' at build time, so two builds of the same source can ship
    different engines.
    """
    import tomllib

    try:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    return data.get("tool", {}).get("oxeiosync", {}).get("engine_version")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    parser.add_argument("--zip", action="store_true", help="pack the build into an archive")
    parser.add_argument(
        "--installer",
        action="store_true",
        help="also build the installer image (Windows setup .exe, or macOS .dmg)",
    )
    parser.add_argument(
        "--no-engine",
        action="store_true",
        help="do not fetch an engine, and do not fail without one (one already "
        "vendored is still bundled; the report below says which happened)",
    )
    parser.add_argument(
        "--engine-version", help="pin the bundled engine to a release tag, e.g. v2.1.2"
    )
    args = parser.parse_args(argv[1:])

    if not SPEC.is_file():
        print(f"missing spec file: {SPEC}", file=sys.stderr)
        return 1

    if args.clean:
        for path in (BUILD, DIST):
            if path.exists():
                print(f"removing {path}")
                shutil.rmtree(path, ignore_errors=True)

    if args.no_engine:
        # Read by the spec, which otherwise refuses to build without an engine.
        os.environ["OXEIOSYNC_ALLOW_NO_ENGINE"] = "1"
        print("== skipping the sync engine (--no-engine)")
    else:
        version = args.engine_version or pinned_engine_version()
        print("== vendoring the sync engine" + (f" (pinned {version})" if version else ""))
        command = [sys.executable, str(ROOT / "tools" / "fetch_engine.py")]
        if version:
            command += ["--version", version]
        else:
            print(
                "WARNING: no engine version pinned (set tool.oxeiosync.engine_version "
                "in pyproject.toml) and none given; vendoring the latest release, "
                "which is not reproducible.",
                file=sys.stderr,
            )
        if run(command):
            print(
                "Could not vendor a sync engine. Fetch one by hand and pass it to\n"
                "  python tools/fetch_engine.py --from-archive <file> --sha256 <hex>\n"
                "or build without one using --no-engine.",
                file=sys.stderr,
            )
            return 1

    print("\n== regenerating the icon")
    if run([sys.executable, str(ROOT / "tools" / "make_icon.py")]):
        # The icon is cosmetic and the spec already tolerates its absence, so a
        # rendering failure (a headless build host with no working Qt platform
        # plugin, say) is a warning, not a dead build. A stale icon from an
        # earlier run is still used; otherwise the bundle takes the default one.
        print(
            "WARNING: could not regenerate the icon; continuing without a fresh "
            "one. Run tools/make_icon.py on a desktop session to refresh it.",
            file=sys.stderr,
        )

    print("\n== running PyInstaller")
    started = time.monotonic()
    code = run([sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC)])
    elapsed = time.monotonic() - started
    if code:
        print(f"PyInstaller failed with exit code {code}", file=sys.stderr)
        return code

    if not EXE.is_file():
        print(f"build reported success but {EXE} is missing", file=sys.stderr)
        return 1

    # On macOS, COLLECT leaves the intermediate one-folder tree in dist/ next to
    # the .app that BUNDLE wraps around it. Only the .app ships, so drop the
    # duplicate rather than leave 300 MB of it cluttering dist/.
    if IS_MACOS:
        onedir = DIST / BUILD_NAME
        if onedir.is_dir():
            shutil.rmtree(onedir, ignore_errors=True)

    total = directory_size(APP_DIR)
    files = sum(1 for f in APP_DIR.rglob("*") if f.is_file())
    print(f"\n== built in {elapsed:.0f}s")
    print(f"   {APP_DIR if IS_MACOS else EXE}")
    print(f"   {human(EXE.stat().st_size)} launcher, {human(total)} total, {files:,} files")

    engine = bundled_engine(APP_DIR)
    if engine is not None:
        print(f"   engine: {engine.relative_to(APP_DIR)} ({human(engine.stat().st_size)})")
    elif args.no_engine:
        print("   engine: none — this build downloads one on first run")
    else:
        print(
            f"build finished but no {ENGINE_NAME} is in {APP_DIR}.\n"
            "The spec was told to bundle one; check the PyInstaller output above.",
            file=sys.stderr,
        )
        return 1

    if IS_MACOS:
        return finalize_macos(APP_BUNDLE, do_zip=args.zip, do_installer=args.installer)

    if args.zip:
        code = pack_zip()
        if code:
            return code

    if args.installer:
        code = build_installer()
        if code:
            return code

    return 0


def _arch_tag() -> str:
    """The ``<os>-<arch>`` label used in archive names."""
    import platform

    machine = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    return f"{'macos' if IS_MACOS else 'windows'}-{machine}"


def pack_zip() -> int:
    """Zip the Windows one-folder build."""
    print("\n== packing")
    base = DIST / f"{BUILD_NAME}-{_arch_tag()}"
    made = shutil.make_archive(str(base), "zip", root_dir=DIST, base_dir=APP_DIR.name)
    print(f"   {Path(made)} ({human(os.path.getsize(made))})")
    return 0


def finalize_macos(app_in_dist: Path, do_zip: bool, do_installer: bool) -> int:
    """Sign the bundle and build its artifacts *outside* the source tree.

    The repo turned out to sit inside a file-provider-synced folder (iCloud
    Drive, OneDrive and the like), which re-stamps ``com.apple.FinderInfo`` onto
    the bundle the instant it is cleared — and codesign refuses a bundle carrying
    that ("resource fork, Finder information, or similar detritus not allowed").
    So the sign-and-package work happens in a temporary directory the file
    provider does not manage; only the finished, immutable artifacts (the signed
    ``.app`` and the ``.dmg``) are copied back into ``dist/``.

    Apple Silicon kills a bundle with no signature at all, so the ad-hoc
    signature here is not distribution signing (there is no Developer ID and no
    notarization) — it is simply what lets the ``.app`` launch locally.
    """
    tmp = Path(tempfile.mkdtemp(prefix="oxeiosync-macbuild-"))
    try:
        clean = tmp / app_in_dist.name
        # ditto --noextattr --noqtn drops most detritus on the way out; the
        # explicit clear then removes anything left (notably FinderInfo).
        if run(["ditto", "--noextattr", "--noqtn", str(app_in_dist), str(clean)]):
            print("could not copy the bundle out for signing", file=sys.stderr)
            return 1
        subprocess.run(["xattr", "-cr", str(clean)], check=False)

        print("\n== ad-hoc signing the bundle")
        if run(["codesign", "--force", "--deep", "--sign", "-", str(clean)]):
            print(
                "WARNING: ad-hoc signing failed; the .app may not launch on Apple "
                "Silicon.",
                file=sys.stderr,
            )
        elif run(["codesign", "--verify", "--deep", "--strict", str(clean)]):
            print("WARNING: the signed bundle did not verify.", file=sys.stderr)
        else:
            print("   ad-hoc signature verified")

        # Bring the signed bundle back. The sync provider may re-stamp the folder
        # it lands in, but the sealed _CodeSignature travels with it, and the
        # authoritative copy is the one going straight into the .dmg from tmp.
        shutil.rmtree(app_in_dist, ignore_errors=True)
        run(["ditto", str(clean), str(app_in_dist)])

        if do_zip and _pack_zip_macos(clean):
            return 1
        if do_installer and _build_dmg_macos(clean, tmp):
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _pack_zip_macos(app: Path) -> int:
    """Zip a signed .app with ditto, which preserves its symlinks and mode bits."""
    print("\n== packing")
    archive = DIST / f"{BUILD_NAME}-{_arch_tag()}.zip"
    archive.unlink(missing_ok=True)
    if run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)]):
        print("ditto failed to pack the bundle", file=sys.stderr)
        return 1
    print(f"   {archive} ({human(os.path.getsize(archive))})")
    return 0


def _build_dmg_macos(app: Path, tmp: Path) -> int:
    """Build a drag-to-Applications disk image with hdiutil, from a temp staging.

    hdiutil ships with macOS, so this needs nothing installed — the macOS
    counterpart to leaning on Inno Setup on Windows.
    """
    print("\n== building the disk image")
    staging = tmp / "dmg"
    staging.mkdir()

    # ditto, not copytree: it preserves the bundle's internal symlinks and the
    # executable bit, which a naive copy would flatten.
    if run(["ditto", str(app), str(staging / app.name)]):
        print("could not stage the .app for the image", file=sys.stderr)
        return 1
    # The /Applications symlink is the whole drag-install convention.
    os.symlink("/Applications", staging / "Applications")

    dmg = DIST / f"{APP_NAME}.dmg"
    dmg.unlink(missing_ok=True)
    started = time.monotonic()
    code = run(
        [
            "hdiutil", "create",
            "-volname", APP_NAME,
            "-srcfolder", str(staging),
            "-ov",
            "-format", "UDZO",  # zlib-compressed, read-only
            str(dmg),
        ],
        stdout=subprocess.DEVNULL,
    )
    if code:
        print(f"hdiutil failed with exit code {code}", file=sys.stderr)
        return code
    if not dmg.is_file():
        print(f"hdiutil reported success but {dmg} is missing", file=sys.stderr)
        return 1
    print(f"   {dmg}")
    print(f"   {human(dmg.stat().st_size)} in {time.monotonic() - started:.0f}s")
    return 0


def find_iscc() -> Path | None:
    """The Inno Setup command-line compiler, if it is installed."""
    on_path = shutil.which("ISCC")
    if on_path:
        return Path(on_path)
    return next((path for path in ISCC_CANDIDATES if path.is_file()), None)


def build_installer() -> int:
    print("\n== compiling the installer")
    iscc = find_iscc()
    if iscc is None:
        print(
            "Inno Setup 6 not found. Install it with:\n"
            "  winget install --id JRSoftware.InnoSetup",
            file=sys.stderr,
        )
        return 1

    started = time.monotonic()
    code = run([str(iscc), str(ISS)], stdout=subprocess.DEVNULL)
    if code:
        print(f"ISCC failed with exit code {code}", file=sys.stderr)
        return code

    setup = DIST / "oXeioSync-setup.exe"
    if not setup.is_file():
        print(f"ISCC reported success but {setup} is missing", file=sys.stderr)
        return 1
    print(f"   {setup}")
    print(f"   {human(setup.stat().st_size)} in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
