# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for oXeioSync.

Built as a **one-folder** bundle, not one-file. QtWebEngine ships a helper
executable plus resource and locale files that it locates relative to the Qt
installation; one-file unpacks all of that to a fresh temporary directory on
every launch, which makes start-up slow and the helper's path fragile. A folder
is also what an installer wants to lay down anyway.

Nothing here is generated from the app's own data files, because it has none:
the icons are painted with QPainter and the injected CSS/JS are string literals.
The only asset is the .ico, and `tools/make_icon.py` renders that from the same
drawing code the tray uses.

One thing that is *not* the app's own is bundled all the same: the sync engine,
vendored into `build/vendor/` by `tools/fetch_engine.py`. Shipping it is what
makes the build installable on a machine that has no usable route to GitHub, and
the build fails without one rather than quietly producing a bundle that needs
the network on first run.
"""

import os
import sys
from pathlib import Path

# __file__ is not defined while a spec runs; PyInstaller provides SPECPATH.
SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821
WORK_DIR = Path(workpath).resolve()  # noqa: F821
ROOT = SPEC_DIR.parent

APP_NAME = "oXeioSync"
ICON = SPEC_DIR / "oxeiosync.ico"

# A windowed build has nowhere to print a start-up failure. Setting
# OXEIOSYNC_BUILD_CONSOLE=1 produces an otherwise identical build that keeps its
# console, which is the only practical way to read such a failure.
WITH_CONSOLE = os.environ.get("OXEIOSYNC_BUILD_CONSOLE") == "1"
BUILD_NAME = f"{APP_NAME}-console" if WITH_CONSOLE else APP_NAME

# --- version resource, so Explorer's Properties tab is not blank -------------
version_line = next(
    line for line in (ROOT / "oxeiosync" / "__init__.py").read_text(encoding="utf-8").splitlines()
    if line.startswith("APP_VERSION")
)
APP_VERSION = version_line.split("=", 1)[1].strip().strip('"').strip("'")
_parts = (APP_VERSION.split(".") + ["0", "0", "0", "0"])[:4]
VERSION_TUPLE = tuple(int(p) if p.isdigit() else 0 for p in _parts)

# Written into the build directory, not the source tree: a spec should not leave
# generated files behind next to the code.
VERSION_FILE = WORK_DIR / "version_info.txt"
WORK_DIR.mkdir(parents=True, exist_ok=True)
VERSION_FILE.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION_TUPLE}, prodvers={VERSION_TUPLE},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '{APP_NAME}'),
        StringStruct('FileDescription', 'Keeps your folders in sync, from the system tray'),
        StringStruct('FileVersion', '{APP_VERSION}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{APP_NAME}'),
        StringStruct('ProductVersion', '{APP_VERSION}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
    encoding="utf-8",
)

# --- the sync engine ---------------------------------------------------------
# Imported rather than reimplemented: the mapping from this machine to a release
# asset name lives in the application, and a build that guessed it differently
# would vendor one engine and ship another.
sys.path.insert(0, str(ROOT))
from oxeiosync.syncthing.binary import platform_asset_infix  # noqa: E402

ENGINE_ARCH = os.environ.get("OXEIOSYNC_ENGINE_ARCH") or platform_asset_infix()
ENGINE_DIR = ROOT / "build" / "vendor" / ENGINE_ARCH
ENGINE_NAME = "syncthing.exe" if ENGINE_ARCH.startswith("windows") else "syncthing"
ENGINE = ENGINE_DIR / ENGINE_NAME

# Shipping the engine means shipping its licence terms with it.
ENGINE_LEGAL = (
    "ENGINE-NOTICE.txt",
    "LICENSE-engine.txt",
    "NOTICE-engine.txt",
    "AUTHORS-engine.txt",
)

# An escape hatch for working on the packaging itself, where waiting on a 30 MB
# download to test a spec change is pure friction. Not for release builds: the
# resulting bundle asks for the network on first run, which is the whole problem.
ALLOW_NO_ENGINE = os.environ.get("OXEIOSYNC_ALLOW_NO_ENGINE") == "1"

# Entries are (destination, source, typecode) — the shape `a.datas` holds after
# Analysis has run, which is not the (source, destination) pair that
# `Analysis(datas=...)` accepts. These are appended below rather than passed in,
# so this is the shape that matters; the filter above reading `entry[0]` as a
# destination is the same fact seen from the other side.
engine_datas = []
if ENGINE.is_file():
    engine_datas.append((ENGINE_NAME, str(ENGINE), "DATA"))
    engine_datas += [
        (name, str(ENGINE_DIR / name), "DATA")
        for name in ENGINE_LEGAL
        if (ENGINE_DIR / name).is_file()
    ]
    print(f"spec: bundling the sync engine from {ENGINE}")
elif ALLOW_NO_ENGINE:
    print("spec: WARNING — building with no sync engine (OXEIOSYNC_ALLOW_NO_ENGINE=1)")
else:
    raise SystemExit(
        f"No sync engine vendored for {ENGINE_ARCH}: expected {ENGINE}\n"
        f"Run:  python tools/fetch_engine.py --arch {ENGINE_ARCH}\n"
        "Or set OXEIOSYNC_ALLOW_NO_ENGINE=1 to build a bundle that downloads one "
        "on first run."
    )

# --- what to leave out -------------------------------------------------------
# Excluding a PySide6 *binding* module does not remove the Qt DLL behind it:
# QtWebEngine links Qt6Qml/Qt6Quick/Qt6Positioning/Qt6WebChannel and those DLLs
# still arrive through binary dependency analysis. What the exclusion removes is
# the unused .pyd and, for QtQml, the whole qml/ import tree the hook would
# otherwise collect. QtPrintSupport and QtWebChannel are NOT excluded: shiboken
# imports them when PySide6.QtWebEngineCore loads.
EXCLUDES = [
    # Bindings pulled in only by QtWebEngine's hiddenimport chain, never by this
    # application. Verified against the module list the running app actually
    # loads with a live QWebEngineView.
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtQuick3D",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPositioning",
    # Qt modules with no path into this application at all
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
    "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtNfc", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtSerialBus", "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio", "PySide6.QtSql", "PySide6.QtStateMachine",
    "PySide6.QtSvgWidgets", "PySide6.QtTest", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "PySide6.QtXml",
    # Other bindings: not installed, but their absence stops a future build
    # aborting on "multiple Qt bindings found".
    "PyQt5", "PyQt6", "PySide2",
    # Development-only Python packages
    "tkinter", "_tkinter", "pytest", "_pytest", "pluggy", "iniconfig",
    "ruff", "PyInstaller", "_pyinstaller_hooks_contrib", "altgraph",
    "setuptools", "pkg_resources", "_distutils_hack", "pip", "wheel",
    "numpy", "matplotlib", "IPython",
]

a = Analysis(  # noqa: F821
    # Not oxeiosync/__main__.py directly: PyInstaller runs the entry script
    # without package context, which breaks its relative imports.
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    # QtWebEngineWidgets must be importable before QApplication exists; naming
    # it here guarantees the hook runs even though __main__ imports it oddly.
    hiddenimports=[
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
        # Reached only through the launcher, so the analyser needs telling.
        "oxeiosync",
        "oxeiosync.__main__",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# --- trim collected data -----------------------------------------------------
# The QtWebEngine hook collects its resources and translations directory
# wholesale, so these cannot be dropped with `excludes` — they have to be
# filtered off `a.datas` afterwards.
#
# Deliberately NOT dropped: opengl32sw.dll. It is 20 MB of software OpenGL, and
# it is the fallback that keeps the embedded page rendering on a virtual machine
# or a box with broken GPU drivers — the one failure mode that is miserable to
# diagnose remotely.
KEEP_LOCALE = "en-US.pak"


def _is_droppable(dest: str) -> bool:
    name = Path(dest).name
    lowered = dest.replace("\\", "/").lower()

    # Debug variants of resources Qt only opens in a debug build (~77 MB).
    if ".debug." in name:
        return True
    # The DevTools front-end (~11 MB). Nothing here ever opens an inspector.
    if name == "qtwebengine_devtools_resources.pak":
        return True
    # Qt's own UI translations (~9 MB). The app never builds a QTranslator, so
    # Qt falls back to the English source strings regardless.
    if name.endswith(".qm"):
        return True
    # Chromium's locale packs (~43 MB). The interface is English-only; Chromium
    # falls back to en-US.
    if "qtwebengine_locales/" in lowered and name != KEEP_LOCALE:
        return True
    # The PDF image plugin, sole consumer of the 4 MB Qt6Pdf.dll.
    if lowered.endswith("imageformats/qpdf.dll"):
        return True
    return False


# Qt's TLS backend and the OpenSSL pair it wants. Nothing here asks Qt to do
# TLS: QtNetwork is used only for the local single-instance socket, the embedded
# browser uses Chromium's own BoringSSL, and the HTTPS downloads go through
# `requests`, which uses Python's _ssl and its own libssl-3/libcrypto-3 (no
# `-x64` suffix) — those stay. Dropping these also avoids shipping a copy of
# OpenSSL that was scraped off this machine's PATH rather than chosen.
DROPPABLE_BINARIES = (
    "imageformats/qpdf.dll",
    "tls/qopensslbackend.dll",
    "libssl-3-x64.dll",
    "libcrypto-3-x64.dll",
)


def _is_droppable_binary(dest: str) -> bool:
    lowered = dest.replace("\\", "/").lower()
    return any(lowered.endswith(name) for name in DROPPABLE_BINARIES)


_before_datas, _before_binaries = len(a.datas), len(a.binaries)
a.datas = [entry for entry in a.datas if not _is_droppable(entry[0])]
a.binaries = [entry for entry in a.binaries if not _is_droppable_binary(entry[0])]
print(
    f"spec: dropped {_before_datas - len(a.datas)} data files and "
    f"{_before_binaries - len(a.binaries)} binaries"
)

# Appended here, after Analysis has run and after the drop filters, for two
# reasons. A drop rule written for Qt's collected resources can never take the
# engine out with it; and an entry added at this point is past the step that
# reclassifies an MZ file handed to `Analysis(datas=...)` as a binary, so the
# engine is copied verbatim rather than walked for imports and rewritten. It is
# somebody else's executable and should arrive byte for byte as published —
# which is also what its licence asks of anyone who redistributes it.
a.datas += engine_datas

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=BUILD_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console: this is a tray application, and a console window flashing on
    # every launch (including at login) is exactly what it exists to avoid.
    console=WITH_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
    version=str(VERSION_FILE),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=BUILD_NAME,
)
