"""Start-on-login support.

On Windows this is a value under ``HKCU\\...\\CurrentVersion\\Run``; on macOS it
is a per-user LaunchAgent plist under ``~/Library/LaunchAgents``. Both need no
elevation and apply to the current user only, and both take effect at the next
login rather than immediately — writing the entry does not launch a second copy
of an application that is already running. Other platforms are reported as
unsupported rather than silently doing nothing, so the settings dialog can
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

#: Reverse-DNS label for the macOS LaunchAgent. launchd requires the plist's
#: ``Label`` and its filename to match, so this is used for both.
LAUNCH_AGENT_LABEL = "com.owncoder.oxeiosync"


def is_supported() -> bool:
    return os.name == "nt" or sys.platform == "darwin"


# ============================================================ command / argv
def launch_command() -> str:
    """The Windows ``Run``-key command line that should run at login.

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


def launch_argv() -> list[str]:
    """The argument vector for a macOS LaunchAgent's ``ProgramArguments``.

    Mirrors :func:`launch_command`'s three cases, but as a list rather than a
    shell string: a LaunchAgent takes an argument vector, so there is nothing to
    quote and a path with spaces cannot come apart. ``pythonw`` has no meaning
    here — launchd starts the process with no console to flash regardless.
    """
    if paths.is_frozen():
        return [sys.executable, MINIMIZED_FLAG]

    script = installed_script()
    if script is not None:
        return [str(script), MINIMIZED_FLAG]

    root = str(paths.install_dir())
    bootstrap = (
        f"import sys; sys.path.insert(0, {root!r}); "
        "from oxeiosync.__main__ import main; sys.exit(main())"
    )
    return [sys.executable, "-c", bootstrap, MINIMIZED_FLAG]


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


# =============================================================== dispatch
def is_enabled() -> bool:
    if os.name == "nt":
        return _windows_is_enabled()
    if sys.platform == "darwin":
        return _macos_is_enabled()
    return False


def set_enabled(enabled: bool) -> bool:
    """Add or remove the login entry. Returns True if the change stuck."""
    if os.name == "nt":
        return _windows_set_enabled(enabled)
    if sys.platform == "darwin":
        return _macos_set_enabled(enabled)
    return False


def sync_with_config(desired: bool) -> bool:
    """Make the login entry agree with the config, repairing a stale command.

    The stored command can go stale — the user moves the install, or switches
    from a source checkout to a packaged build — so a mismatch is rewritten
    rather than left pointing at something that no longer exists.
    """
    if not is_supported():
        return False
    if not desired:
        return set_enabled(False)

    if is_enabled() and _stored_launch() == _current_launch():
        return True
    return set_enabled(True)


def describe() -> str:
    """A short explanation for the settings dialog."""
    if not is_supported():
        return "Start on login is only supported on Windows and macOS."
    if paths.is_frozen() or installed_script() is not None:
        return f"Runs: {launch_command() if os.name == 'nt' else ' '.join(launch_argv())}"
    # The source-checkout form is a long bootstrap one-liner; showing it in full
    # tells the user nothing useful.
    return (
        f"Runs this source checkout from {paths.install_dir()}. "
        "Install the package (pip install -e .) for a tidier login entry."
    )


def _current_launch() -> object:
    """What the login entry should say, in whatever shape the platform stores."""
    return launch_command() if os.name == "nt" else launch_argv()


def _stored_launch() -> object:
    return _stored_command() if os.name == "nt" else _macos_stored_argv()


# ================================================================= Windows
def _windows_is_enabled() -> bool:
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


def _windows_set_enabled(enabled: bool) -> bool:
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


def _stored_command() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, APP_NAME)
    except OSError:
        return ""
    return str(value)


# =================================================================== macOS
def launch_agent_path() -> Path:
    """The per-user LaunchAgent plist that launchd reads at login."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _macos_plist_bytes() -> bytes:
    import plistlib

    document = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": launch_argv(),
        # Start at login; do not resurrect it if the user quits during a session
        # — that is what the Windows Run entry does, and start-on-login should
        # not become always-on.
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    return plistlib.dumps(document)


def _macos_is_enabled() -> bool:
    return launch_agent_path().is_file()


def _macos_set_enabled(enabled: bool) -> bool:
    path = launch_agent_path()
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_macos_plist_bytes())
            # Deliberately not `launchctl load`d here: loading a RunAtLoad agent
            # starts it at once, which would launch a second copy on top of the
            # one writing this file. It takes effect at the next login instead.
            log.info("Enabled start on login (%s)", path)
        elif path.is_file():
            path.unlink()
            log.info("Disabled start on login")
    except OSError as exc:
        log.error("Could not update the LaunchAgent %s: %s", path, exc)
        return False
    return True


def _macos_stored_argv() -> list[str]:
    import plistlib

    try:
        document = plistlib.loads(launch_agent_path().read_bytes())
    except (OSError, ValueError) as exc:
        log.debug("Could not read the LaunchAgent: %s", exc)
        return []
    argv = document.get("ProgramArguments")
    return [str(item) for item in argv] if isinstance(argv, list) else []
