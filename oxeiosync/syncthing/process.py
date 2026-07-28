"""Supervises the Syncthing child process.

This is the piece that makes Syncthing feel like a native application: it
launches the binary without flashing a console window, forces the GUI address
and API key to values we already know, streams the log output into the app, and
restarts Syncthing with a backoff if it dies unexpectedly.

``subprocess`` is used rather than ``QProcess`` because hiding the console
window on Windows needs ``CREATE_NO_WINDOW``, which Qt only exposes through an
API that PySide does not wrap.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections import deque
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .. import paths
from ..config import DEFAULT_GUI_ADDRESS, Config
from ..net import (
    describe_port_conflict,
    is_port_available,
    join_bind_address,
    split_bind_address,
)
from .api import SyncthingApi, SyncthingApiError
from .binary import find_syncthing, no_window_flags

log = logging.getLogger(__name__)

#: Delay before each successive restart attempt, in seconds.
RESTART_DELAYS = (1, 2, 5, 15, 30)
#: How many times to wait out a busy GUI port before calling it a conflict.
#: A restart briefly overlaps with the old listener, and that is not an error.
PORT_WAIT_ATTEMPTS = 3
#: Log fragments that mean the failure is permanent and retrying is pointless.
FATAL_LOG_MARKERS = (
    "Failed to start API",
    "Error opening database",
    "Cannot open database",
    "insufficient space",
)
#: Uptime after which a crash is treated as a fresh problem, not a retry loop.
STABLE_UPTIME = 60.0
#: How long to wait for a clean shutdown before killing the process.
SHUTDOWN_GRACE = 10.0
#: How long to wait after a terminate() before escalating to kill().
TERMINATE_GRACE = 5.0


class ProcessState(Enum):
    """Lifecycle of the supervised Syncthing process."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"

    def __str__(self) -> str:
        return self.value


class SyncthingProcess(QObject):
    """Starts, stops and babysits one Syncthing process."""

    #: Emitted with the new :class:`ProcessState` on every transition.
    state_changed = Signal(object)
    #: One line of Syncthing's merged stdout/stderr.
    log_line = Signal(str)
    #: A human-readable problem worth showing the user.
    error = Signal(str)

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._state = ProcessState.STOPPED
        self._popen: subprocess.Popen[str] | None = None
        self._reader: _OutputReader | None = None
        self._stopper: _Stopper | None = None
        self._log: deque[str] = deque(maxlen=max(100, config.log_lines_kept))
        self._binary: Path | None = None
        self._started_at = 0.0
        self._restart_attempt = 0
        #: True while a stop was asked for, so an exit is not treated as a crash.
        self._stop_requested = False
        #: True when a stop should be followed by a fresh start.
        self._restart_after_stop = False
        #: Consecutive launches deferred because the GUI port was still busy.
        self._port_wait_attempt = 0
        #: Set when the log shows a failure no restart can fix.
        self._fatal_reason: str | None = None
        # The address and key the *running* process was launched with. Settings
        # can change under us, and a shutdown request has to go to where
        # Syncthing is actually listening, not where it will listen next time.
        self._launched_gui_url = ""
        self._launched_api_key = ""

    # -------------------------------------------------------------- properties
    @property
    def state(self) -> ProcessState:
        return self._state

    @property
    def binary(self) -> Path | None:
        """The binary currently in use, once :meth:`start` has resolved one."""
        return self._binary

    def is_running(self) -> bool:
        return self._state in (ProcessState.STARTING, ProcessState.RUNNING)

    def log_tail(self) -> list[str]:
        """A snapshot of the retained log lines, oldest first."""
        return list(self._log)

    def mark_running(self) -> None:
        """Confirm from outside that Syncthing's API is answering.

        Detecting readiness from the log alone is fragile — the wording could
        change between releases — so whoever first reaches the REST API calls
        this as a second, authoritative source.
        """
        if self._state is ProcessState.STARTING:
            self._set_state(ProcessState.RUNNING)

    # ----------------------------------------------------------------- control
    def start(self) -> None:
        """Launch Syncthing. Does nothing if it is already up."""
        if self.is_running():
            log.debug("Ignoring start(): Syncthing is already %s", self._state)
            return
        if self._state is ProcessState.STOPPING:
            # Let the in-flight stop finish, then come back up.
            self._restart_after_stop = True
            return

        binary = find_syncthing(self._config.syncthing_path)
        if binary is None:
            self._fail(
                "No sync engine found. Use Settings to point oXeioSync at one, "
                "or let it download a copy."
            )
            return

        # Syncthing responds to a busy GUI port by logging a bind error and
        # exiting, so check it here where we can explain the problem.
        host, port = split_bind_address(self._config.gui_address)
        if not is_port_available(host, port):
            if self._port_wait_attempt < PORT_WAIT_ATTEMPTS:
                self._port_wait_attempt += 1
                self._append_log(
                    f"[oXeioSync] {join_bind_address(host, port)} is still busy; "
                    f"waiting (attempt {self._port_wait_attempt})"
                )
                QTimer.singleShot(1000, self.start)
                return
            self._port_wait_attempt = 0
            self._restart_attempt = 0
            self._fail(describe_port_conflict(host, port))
            return
        self._port_wait_attempt = 0

        self._binary = binary
        self._fatal_reason = None
        self._launched_gui_url = self._config.gui_url()
        self._launched_api_key = self._config.api_key
        command = self._build_command(binary)
        env = self._build_env(binary)

        log.info("Starting %s", " ".join(command))
        self._append_log(f"[oXeioSync] starting {binary}")

        try:
            self._popen = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                cwd=str(binary.parent),
                creationflags=no_window_flags(),
                # POSIX only: give Syncthing its own process group, so the whole
                # monitor-plus-worker tree can be signalled as a unit.
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            self._popen = None
            self._fail(f"Could not start the sync engine: {exc}")
            return

        self._stop_requested = False
        self._started_at = time.monotonic()
        self._set_state(ProcessState.STARTING)

        reader = _OutputReader(self._popen, self)
        reader.line.connect(self._on_log_line)
        reader.exited.connect(self._on_exited)
        # Without this, every restart would leave a finished QThread parented to
        # us for the lifetime of the application.
        reader.finished.connect(reader.deleteLater)
        self._reader = reader
        reader.start()

    def stop(self) -> None:
        """Ask Syncthing to shut down cleanly, killing it if it won't."""
        if self._state in (ProcessState.STOPPED, ProcessState.STOPPING):
            return
        if self._popen is None:
            self._set_state(ProcessState.STOPPED)
            return

        self._stop_requested = True
        self._restart_attempt = 0
        self._set_state(ProcessState.STOPPING)
        self._append_log("[oXeioSync] shutting down the sync engine")

        self._stopper = _Stopper(self._popen, self._running_api(), self)
        self._stopper.finished.connect(self._stopper.deleteLater)
        self._stopper.start()

    def _running_api(self) -> SyncthingApi:
        """An API client aimed at the instance that is currently running."""
        return SyncthingApi(
            self._launched_gui_url or self._config.gui_url(),
            self._launched_api_key or self._config.api_key,
        )

    def restart(self) -> None:
        """Stop Syncthing and bring it back up once it has exited."""
        if not self.is_running():
            self.start()
            return
        self._restart_after_stop = True
        self.stop()

    def shutdown_blocking(self, timeout: float = SHUTDOWN_GRACE + TERMINATE_GRACE) -> None:
        """Stop Syncthing and wait for it, for use while the app is quitting.

        Called from the GUI thread during teardown, when there is no event loop
        left to deliver the asynchronous signals :meth:`stop` relies on.
        """
        self._restart_after_stop = False
        popen = self._popen
        if popen is None or popen.poll() is not None:
            self._set_state(ProcessState.STOPPED)
            return

        self._stop_requested = True
        self._set_state(ProcessState.STOPPING)

        api = self._running_api()
        _terminate(popen, api, timeout)
        api.close()

        if self._reader is not None:
            self._reader.wait(2000)
        self._set_state(ProcessState.STOPPED)

    # ------------------------------------------------------------- invocation
    def _build_command(self, binary: Path) -> list[str]:
        """Assemble the Syncthing command line.

        Only flags that have been stable across Syncthing's whole 1.x series
        are used here; the GUI address and API key go through environment
        variables instead, which are not affected by the CLI's move to
        subcommands.
        """
        command = [
            str(binary),
            f"--home={self._config.resolved_syncthing_home()}",
            "--no-browser",
        ]
        command.extend(self._config.syncthing_extra_args)
        return command

    def _build_env(self, binary: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                # Forcing these means we never have to parse Syncthing's
                # config.xml to discover how to talk to it. Fall back to the
                # default rather than passing an empty override, which Syncthing
                # would read as "use whatever is in config.xml".
                "STGUIADDRESS": self._config.gui_address or DEFAULT_GUI_ADDRESS,
                "STGUIAPIKEY": self._config.api_key,
                # We are the supervisor: Syncthing must not respawn itself, or
                # we would lose track of the process we are meant to be
                # watching.
                "STNORESTART": "1",
            }
        )

        if binary in paths.bundled_syncthing_candidates():
            # The engine that ships with the application sits in the program
            # folder, which the installer replaces wholesale on every update.
            # An engine that upgraded itself there would be silently reverted by
            # the next install, and the digest published in the notice beside it
            # would stop describing the file it names. The copy oXeioSync
            # manages itself is in a folder no installer touches, and is left
            # free to upgrade exactly as before.
            env["STNOUPGRADE"] = "1"
        return env

    # ----------------------------------------------------------------- signals
    def _on_log_line(self, line: str) -> None:
        self._append_log(line)
        # Syncthing prints this once the GUI is listening, which is the first
        # moment the REST API is worth talking to.
        if self._state is ProcessState.STARTING and "GUI and API listening" in line:
            self._set_state(ProcessState.RUNNING)
        if self._fatal_reason is None and any(m in line for m in FATAL_LOG_MARKERS):
            self._fatal_reason = line.strip()
        self.log_line.emit(line)

    def _on_exited(self, exit_code: int) -> None:
        was_stable = (time.monotonic() - self._started_at) > STABLE_UPTIME
        self._popen = None
        # The reader has finished and is queued for deletion; do not keep a
        # reference that would outlive the C++ object.
        self._reader = None
        self._append_log(f"[oXeioSync] sync engine exited with code {exit_code}")
        self._set_state(ProcessState.STOPPED)

        if self._restart_after_stop:
            self._restart_after_stop = False
            self._stop_requested = False
            QTimer.singleShot(500, self.start)
            return

        if self._stop_requested:
            self._stop_requested = False
            return

        # Some failures are permanent — a port already bound, a locked database.
        # Retrying those just hides the real problem behind a restart loop.
        if self._fatal_reason is not None:
            reason, self._fatal_reason = self._fatal_reason, None
            self._restart_attempt = 0
            self._fail(f"The sync engine could not start: {reason}")
            return

        # Unexpected exit: reset the backoff if it had been up a while, then
        # retry with an increasing delay.
        if was_stable:
            self._restart_attempt = 0

        if self._restart_attempt >= len(RESTART_DELAYS):
            self._fail(
                f"The sync engine keeps exiting (last exit code {exit_code}). "
                "Giving up — check the log and restart it from the tray menu."
            )
            self._restart_attempt = 0
            return

        delay = RESTART_DELAYS[self._restart_attempt]
        self._restart_attempt += 1
        message = (
            f"Syncthing exited unexpectedly (code {exit_code}); "
            f"restarting in {delay}s (attempt {self._restart_attempt})"
        )
        log.warning(message)
        self._append_log(f"[oXeioSync] {message}")
        QTimer.singleShot(int(delay * 1000), self._restart_if_still_wanted)

    def _restart_if_still_wanted(self) -> None:
        # The user may have stopped things while the timer was pending.
        if self._state is ProcessState.STOPPED and not self._stop_requested:
            self.start()

    # ----------------------------------------------------------------- helpers
    def _set_state(self, state: ProcessState) -> None:
        if state is self._state:
            return
        log.debug("Syncthing process state: %s -> %s", self._state, state)
        self._state = state
        self.state_changed.emit(state)

    def _append_log(self, line: str) -> None:
        self._log.append(line)

    def _fail(self, message: str) -> None:
        log.error(message)
        self._append_log(f"[oXeioSync] {message}")
        self._set_state(ProcessState.STOPPED)
        self.error.emit(message)


class _OutputReader(QThread):
    """Reads the child's merged output until EOF, then reaps it."""

    line = Signal(str)
    exited = Signal(int)

    def __init__(self, popen: subprocess.Popen[str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._popen = popen

    def run(self) -> None:  # noqa: D102 - QThread entry point
        stream = self._popen.stdout
        if stream is not None:
            try:
                for raw in stream:
                    self.line.emit(raw.rstrip("\r\n"))
            except (OSError, ValueError) as exc:
                # The pipe can be torn down under us during a forced kill.
                log.debug("Stopped reading Syncthing output: %s", exc)
        self.exited.emit(self._popen.wait())


class _Stopper(QThread):
    """Performs the shutdown sequence off the GUI thread."""

    def __init__(
        self,
        popen: subprocess.Popen[str],
        api: SyncthingApi,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._popen = popen
        self._api = api

    def run(self) -> None:  # noqa: D102 - QThread entry point
        _terminate(self._popen, self._api, SHUTDOWN_GRACE)
        self._api.close()


def _terminate(popen: subprocess.Popen[str], api: SyncthingApi, grace: float) -> None:
    """Stop Syncthing as gently as the platform allows.

    The REST shutdown endpoint is the only truly graceful option — it lets
    Syncthing flush its database — because on Windows there is no graceful
    signal to send. So we ask nicely first and only escalate if it stops
    responding.
    """
    try:
        api.shutdown()
    except SyncthingApiError as exc:
        log.debug("Clean shutdown request failed (%s); falling back to signals", exc)

    try:
        popen.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        log.warning("Syncthing ignored the shutdown request; terminating")

    _signal_tree(popen, force=False)
    try:
        popen.wait(timeout=TERMINATE_GRACE)
        return
    except subprocess.TimeoutExpired:
        log.warning("Syncthing did not terminate; killing")

    _signal_tree(popen, force=True)
    try:
        popen.wait(timeout=TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        log.error("Syncthing process could not be killed")


def _signal_tree(popen: subprocess.Popen[str], force: bool) -> None:
    """Signal Syncthing *and everything it spawned*.

    Syncthing runs as a supervising process plus a worker child. Signalling only
    the process we launched would leave the worker alive, still holding the GUI
    port and the database — which then looks to the next launch like an
    unexplained port conflict.
    """
    if popen.poll() is not None:
        return

    if os.name == "nt":
        command = ["taskkill", "/PID", str(popen.pid), "/T"]
        if force:
            command.append("/F")
        try:
            subprocess.run(
                command,
                capture_output=True,
                timeout=TERMINATE_GRACE,
                creationflags=no_window_flags(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("taskkill failed (%s); signalling the parent only", exc)
            popen.kill() if force else popen.terminate()
        return

    import signal

    try:
        os.killpg(os.getpgid(popen.pid), signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError) as exc:
        log.debug("Could not signal the process group (%s); signalling directly", exc)
        popen.kill() if force else popen.terminate()


__all__ = ["ProcessState", "SyncthingProcess"]
