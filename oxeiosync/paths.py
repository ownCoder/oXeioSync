"""Filesystem locations used by oXeioSync.

Two layouts are supported:

*Installed*
    Configuration, logs and the Syncthing database live under a per-user
    application data directory (``%LOCALAPPDATA%\\oXeioSync`` on Windows).

*Portable*
    Everything lives in a ``data`` folder beside the application, so the whole
    installation can be relocated by copying a single directory.

Portable mode is selected by creating a ``data`` directory (or an empty
``portable`` marker file) next to the executable before first launch.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME

PORTABLE_MARKER = "portable"
PORTABLE_DATA_DIRNAME = "data"


def is_frozen() -> bool:
    """True when running from a PyInstaller-style bundle rather than source."""
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """The directory the application was started from."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # <install_dir>/oxeiosync/paths.py
    return Path(__file__).resolve().parent.parent


def is_portable() -> bool:
    root = install_dir()
    return (root / PORTABLE_MARKER).exists() or (root / PORTABLE_DATA_DIRNAME).is_dir()


def data_dir() -> Path:
    """Root directory for everything oXeioSync writes."""
    if is_portable():
        return install_dir() / PORTABLE_DATA_DIRNAME

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME

    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / APP_NAME

    return Path.home() / ".config" / APP_NAME


def config_file() -> Path:
    return data_dir() / "oxeiosync.json"


def syncthing_home() -> Path:
    """Where Syncthing keeps its own config.xml and database."""
    return data_dir() / "syncthing"


def log_dir() -> Path:
    return data_dir() / "logs"


def binary_dir() -> Path:
    """Where oXeioSync stores the Syncthing binary it downloads itself."""
    return data_dir() / "bin"


def syncthing_binary_name() -> str:
    return "syncthing.exe" if os.name == "nt" else "syncthing"


def managed_syncthing_path() -> Path:
    """Path to the Syncthing binary that oXeioSync downloads and updates."""
    return binary_dir() / syncthing_binary_name()


def bundled_syncthing_path() -> Path:
    """Path to a Syncthing binary shipped alongside the application, if any."""
    return install_dir() / syncthing_binary_name()


def ensure_dirs() -> None:
    """Create every directory the application expects to write into."""
    for path in (data_dir(), syncthing_home(), log_dir(), binary_dir()):
        path.mkdir(parents=True, exist_ok=True)
