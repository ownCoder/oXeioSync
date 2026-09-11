"""oXeioSync — a desktop tray host for Syncthing."""

from . import version as _version

APP_NAME = "oXeioSync"
#: Counted from git on every build, never typed in — see :mod:`oxeiosync.version`.
APP_VERSION = _version.resolve()

__all__ = ["APP_NAME", "APP_VERSION"]
