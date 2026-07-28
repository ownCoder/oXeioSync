"""Build the Windows executable, and optionally the installer.

    python tools/build_exe.py [--clean] [--zip] [--installer]

Vendors the sync engine, regenerates the icon from the app's own drawing code,
runs PyInstaller against ``packaging/oxeiosync.spec``, and reports what came
out. ``--zip`` packs the folder into an archive; ``--installer`` compiles
``packaging/oxeiosync.iss`` into a single setup executable.

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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "oxeiosync.spec"
ISS = ROOT / "packaging" / "oxeiosync.iss"

#: Inno Setup installs per-user by default these days, so look there first.
ISCC_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Inno Setup 6" / "ISCC.exe",
)
DIST = ROOT / "dist"
BUILD = ROOT / "build"
# The console build is written to its own folder, so a diagnostic build must not
# be judged against the windowed build's name.
BUILD_NAME = (
    "oXeioSync-console" if os.environ.get("OXEIOSYNC_BUILD_CONSOLE") == "1" else "oXeioSync"
)
APP_DIR = DIST / BUILD_NAME
EXE = APP_DIR / f"{BUILD_NAME}.exe"
ENGINE_NAME = "syncthing.exe" if os.name == "nt" else "syncthing"


def bundled_engine(app_dir: Path) -> Path | None:
    """The engine inside a built bundle, wherever the packager put it.

    Mirrors the lookup the application does at run time; a build that "succeeds"
    while leaving the engine somewhere the app will not look is a build that
    fails on the user's machine instead of on this one.
    """
    for candidate in (app_dir / ENGINE_NAME, app_dir / "_internal" / ENGINE_NAME):
        if candidate.is_file():
            return candidate
    return None


def run(command: list[str], **kwargs) -> int:
    print(f"$ {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=str(ROOT), **kwargs).returncode


def directory_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    parser.add_argument("--zip", action="store_true", help="pack dist/ into a zip")
    parser.add_argument(
        "--installer", action="store_true", help="also compile the setup executable"
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
        print("== vendoring the sync engine")
        command = [sys.executable, str(ROOT / "tools" / "fetch_engine.py")]
        if args.engine_version:
            command += ["--version", args.engine_version]
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
        return 1

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

    total = directory_size(APP_DIR)
    files = sum(1 for f in APP_DIR.rglob("*") if f.is_file())
    print(f"\n== built in {elapsed:.0f}s")
    print(f"   {EXE}")
    print(f"   {human(EXE.stat().st_size)} exe, {human(total)} total, {files:,} files")

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

    if args.zip:
        archive = DIST / f"{BUILD_NAME}-windows-x64"
        print("\n== packing")
        made = shutil.make_archive(str(archive), "zip", root_dir=DIST, base_dir=APP_DIR.name)
        print(f"   {made} ({human(os.path.getsize(made))})")

    if args.installer:
        code = build_installer()
        if code:
            return code

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
