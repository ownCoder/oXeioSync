"""Entry point: ``python -m oxeiosync``."""

from __future__ import annotations

import argparse
import hashlib
import logging
import logging.handlers
import os
import sys
import time

# The embedded configuration view (Chromium) can come back blank after the
# machine sleeps — a MacBook lid closed and reopened — because its GPU
# compositing surface is lost on sleep and not re-created on wake. Disabling GPU
# compositing for this one embedded view sidesteps that; the configuration page
# is static enough that hardware compositing buys it nothing worth a blank
# window. This must be set before QtWebEngine initialises, which the import just
# below triggers, so it lives above every Qt import. `setdefault` leaves an
# explicit override from the environment untouched.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu-compositing")

# QtWebEngine has to be imported before a QApplication exists, or the embedded
# browser fails to initialise. Keep this above every other Qt import.
import PySide6.QtWebEngineWidgets  # noqa: F401  isort:skip

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME, APP_VERSION, paths
from .app import Application
from .singleinstance import InstanceLock
from .ui.theme import apply_dark_theme

log = logging.getLogger(__name__)

LOG_FILE_BYTES = 1_000_000
LOG_FILE_COUNT = 3


def ipc_name() -> str:
    """Name of the socket used to reach an instance that is already running.

    Derived from the data folder, so exclusivity is scoped to the thing that
    actually cannot be shared — the Syncthing database. Two portable installs in
    separate folders are independent and may run at the same time.
    """
    digest = hashlib.sha256(str(paths.data_dir()).encode("utf-8")).hexdigest()
    return f"oxeiosync-{digest[:16]}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="oxeiosync",
        description=f"{APP_NAME} — keep your folders in sync from the system tray.",
    )
    parser.add_argument(
        "--minimized",
        "--minimised",
        dest="minimized",
        action="store_true",
        help="start in the tray without showing the window",
    )
    parser.add_argument(
        "--quit",
        action="store_true",
        help="ask a running instance to shut down, and wait until it has",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log debug output",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {APP_VERSION}",
    )
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    """Log to a rotating file, and to the console when there is one."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            paths.log_dir() / "oxeiosync.log",
            maxBytes=LOG_FILE_BYTES,
            backupCount=LOG_FILE_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        # A windowed build has no console: both stderr *and* stdout are None, so
        # a bare print() here would raise and take the whole launch down over a
        # log file we could not open.
        if sys.stderr is not None:
            print(f"Could not open the log file: {exc}", file=sys.stderr)
    else:
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Under pythonw.exe, and under a windowed bundle, there is no console.
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)


#: Commands understood over the single-instance socket.
IPC_SHOW = b"show"
IPC_QUIT = b"quit"

#: How long to wait for a running instance to finish shutting down. It stops the
#: sync engine over REST first, which is the slow part.
QUIT_TIMEOUT = 45.0


def _send_to_running_instance(command: bytes) -> bool:
    """Send one command to a running instance. True if one answered."""
    socket = QLocalSocket()
    socket.connectToServer(ipc_name())
    if not socket.waitForConnected(500):
        return False

    socket.write(command)
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def _hand_off_to_running_instance() -> bool:
    """Ask an already-running instance to show itself. True if one answered."""
    return _send_to_running_instance(IPC_SHOW)


def _quit_running_instance(lock: InstanceLock) -> int:
    """Ask a running instance to exit, and block until it really has.

    Written for installers and scripts. The window-close button only hides to
    the tray, so there is no way for an outside process to end this application
    politely — and killing it would take the sync engine down without letting it
    flush. Returning only once the lock is free means the caller can replace the
    files immediately afterwards.
    """
    if not _send_to_running_instance(IPC_QUIT):
        log.info("Nothing to quit: no running instance answered")
        return 0

    log.info("Asked the running instance to exit; waiting for it")
    deadline = time.monotonic() + QUIT_TIMEOUT
    while time.monotonic() < deadline:
        if lock.acquire():
            lock.release()
            log.info("The running instance has exited")
            return 0
        time.sleep(0.25)

    log.error("The running instance did not exit within %.0fs", QUIT_TIMEOUT)
    return 1


def _listen_for_other_instances(application: Application) -> QLocalServer:
    """Serve the single-instance socket so later launches can raise our window.

    Only called once the instance lock is held, which is what makes the
    ``removeServer`` below safe: no other live instance can own the name, so
    anything left behind is debris from a crash.
    """
    name = ipc_name()
    QLocalServer.removeServer(name)

    server = QLocalServer()
    if not server.listen(name):
        log.warning("Could not listen on %s: %s", name, server.errorString())
        return server

    def on_new_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return

        def dispatch() -> None:
            command = bytes(connection.readAll()).strip()
            if command == IPC_QUIT:
                log.info("Asked to exit by another instance")
                application.quit()
            else:
                application.handle_second_instance()

        connection.readyRead.connect(dispatch)
        connection.disconnected.connect(connection.deleteLater)

    server.newConnection.connect(on_new_connection)
    return server


def _install_excepthook() -> None:
    """Surface exceptions raised once the event loop is turning.

    A frozen build's bootloader shows a dialog for anything thrown before
    ``exec()``, but start-up is deliberately deferred into the event loop, and
    PySide routes an exception raised inside a slot to ``sys.excepthook`` and
    then carries on. In a windowed build, where there is no stderr, that leaves
    a half-started application with no window and no explanation anywhere.
    """

    def hook(exc_type, exc, traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc, traceback))
        if QApplication.instance() is not None:
            QMessageBox.critical(
                None,
                f"{APP_NAME} — unexpected error",
                f"{exc_type.__name__}: {exc}\n\n"
                f"Details were written to {paths.log_dir() / 'oxeiosync.log'}.",
            )

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    paths.ensure_dirs()
    setup_logging(args.verbose)
    log.info("Starting %s %s (data folder: %s)", APP_NAME, APP_VERSION, paths.data_dir())

    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationVersion(APP_VERSION)
    qapp.setOrganizationName(APP_NAME)
    # Before any widget exists, so the first paint is already dark — a window
    # that flashes white on the way up is the thing this most needs to avoid.
    apply_dark_theme(qapp)
    _install_excepthook()

    # The lock, not the socket, is what guarantees exclusivity: two launches a
    # few milliseconds apart would both find nobody listening, and on Windows
    # both could then successfully listen on the same name.
    lock = InstanceLock(paths.data_dir() / "oxeiosync.lock")

    if args.quit:
        # Deliberately before the lock is taken: this process is not becoming
        # the instance, it is ending one.
        return _quit_running_instance(lock)

    if not lock.acquire():
        if _hand_off_to_running_instance():
            log.info("Another instance is already running; asked it to show its window")
        else:
            # The lock is held but nothing answered. Rather than start a second
            # copy against the same database, say so and stop.
            log.error(
                "Another oXeioSync instance holds %s but is not responding. "
                "Close it (or end the process) before starting a new one.",
                lock.path,
            )
        return 0

    try:
        application = Application(qapp, start_minimized=args.minimized)
        server = _listen_for_other_instances(application)

        # Start once the event loop is turning, so anything modal has a painted
        # window to sit on top of.
        QTimer.singleShot(0, application.start)

        try:
            return qapp.exec()
        finally:
            server.close()
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
