"""Start-on-login support.

On Windows this is a value under ``HKCU\\...\\CurrentVersion\\Run``, which needs
no elevation and applies to the current user only. Other platforms are reported
as unsupported rather than silently doing nothing, so the settings dialog can
disable the checkbox and say why.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import APP_NAME, paths

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: Passed to the autostarted instance so it comes up in the tray, not on screen.
MINIMIZED_FLAG = "--minimized"


def is_supported() -> bool:
    return os.name == "nt"


def launch_command() -> str:
    """The command line that should run at login.

    The registry's ``Run`` key gives no control over the working directory, and
    at login it will not be the project root — so a bare ``-m oxeiosync`` would
    simply fail to import. Three cases, in order of robustness:

    * frozen build — run the executable;
    * pip-installed — run the ``oxeiosync`` console script, which already knows
      where the package lives;
    * source checkout — invoke the interpreter with the checkout put on
      ``sys.path`` explicitly, so the import cannot depend on the cwd.
    """
    if paths.is_frozen():
        return f'"{sys.executable}" {MINIMIZED_FLAG}'

    script = installed_script()
    if script is not None:
        return f'"{script}" {MINIMIZED_FLAG}'

    root = str(paths.install_dir())
    bootstrap = (
        f"import sys; sys.path.insert(0, {root!r}); "
        "from oxeiosync.__main__ import main; sys.exit(main())"
    )
    return f'"{_windowless_interpreter()}" -c "{bootstrap}" {MINIMIZED_FLAG}'


def installed_script() -> Path | None:
    """The ``oxeiosync`` entry-point script pip creates, if the package is installed."""
    scripts_dir = Path(sys.executable).parent
    names = ("oxeiosync.exe",) if os.name == "nt" else ("oxeiosync",)
    for name in names:
        candidate = scripts_dir / name
        if candidate.is_file():
            return candidate
    return None


def _windowless_interpreter() -> Path:
    """``pythonw.exe`` where it exists, so login does not flash a console."""
    interpreter = Path(sys.executable)
    if os.name == "nt":
        windowless = interpreter.with_name("pythonw.exe")
        if windowless.is_file():
            return windowless
    return interpreter


def is_enabled() -> bool:
    if not is_supported():
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, APP_NAME)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not read the autostart registry key: %s", exc)
        return False
    return bool(value)


def set_enabled(enabled: bool) -> bool:
    """Add or remove the login entry. Returns True if the change stuck."""
    if not is_supported():
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
                log.info("Enabled start on login")
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    log.info("Disabled start on login")
                except FileNotFoundError:
                    pass  # Already absent; nothing to do.
    except OSError as exc:
        log.error("Could not update the autostart registry key: %s", exc)
        return False
    return True


def sync_with_config(desired: bool) -> bool:
    """Make the registry agree with the config, repairing a stale command.

    The stored command can go stale — the user moves the install, or switches
    from a source checkout to a packaged build — so a mismatch is rewritten
    rather than left pointing at something that no longer exists.
    """
    if not is_supported():
        return False
    if not desired:
        return set_enabled(False)

    if is_enabled() and _stored_command() == launch_command():
        return True
    return set_enabled(True)


def _stored_command() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, APP_NAME)
    except OSError:
        return ""
    return str(value)


def describe() -> str:
    """A short explanation for the settings dialog."""
    if not is_supported():
        return "Start on login is only implemented on Windows."
    if paths.is_frozen() or installed_script() is not None:
        return f"Runs: {launch_command()}"
    # The source-checkout form is a long bootstrap one-liner; showing it in full
    # tells the user nothing useful.
    return (
        f"Runs this source checkout from {paths.install_dir()}. "
        "Install the package (pip install -e .) for a tidier login entry."
    )
