"""The main window: the embedded Syncthing UI, plus a log view.

The window is deliberately secondary to the tray icon. Closing it hides it by
default rather than quitting, which is what makes the application feel like a
background service with a face rather than a program you have to keep open.
"""

from __future__ import annotations

import base64
import logging
import time

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStatusBar,
    QTabWidget,
    QWidget,
)

from .. import APP_NAME, APP_VERSION
from ..config import Config
from ..syncthing.process import ProcessState
from ..syncthing.state import SyncStatus, SyncthingState
from ..syncthing.transfer import TransferSampler
from . import icons
from .dashboard import DashboardPage
from .tray import STATUS_LABELS
from .web_view import SyncthingWebView

log = logging.getLogger(__name__)

DEFAULT_SIZE = (1024, 720)

#: How often the heartbeat ticks, to notice the event loop being frozen by sleep.
HEARTBEAT_INTERVAL_MS = 1000
#: A heartbeat gap beyond this many seconds means the loop was frozen — the
#: machine slept — rather than a mere scheduling hiccup.
WAKE_GAP_SECONDS = 6.0


class MainWindow(QMainWindow):
    """Hosts the Syncthing web UI and the Syncthing log."""

    settings_requested = Signal()
    exit_requested = Signal()
    start_syncthing_requested = Signal()
    stop_syncthing_requested = Signal()
    restart_syncthing_requested = Signal()
    rescan_all_requested = Signal()

    #: Emitted when the window hides itself instead of closing, so the app can
    #: remind the user where it went.
    hidden_to_tray = Signal()

    def __init__(
        self,
        config: Config,
        state: SyncthingState,
        sampler: TransferSampler,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._state = state
        self._process_state = ProcessState.STOPPED
        #: Distinguishes "user closed the window" from "application is quitting".
        self._force_close = False
        #: Whether the window is meant to be on screen (vs hidden to the tray).
        #: Qt's own hidden/minimised state desyncs across a macOS lid sleep — it
        #: reports the still-on-screen window as hidden and minimised — so the
        #: wake recovery trusts this instead of isHidden()/isMinimized().
        self._intended_visible = False
        #: Set when a sleep happened while the window was hidden to the tray, so
        #: the surface is rebuilt when it is next brought back on screen.
        self._pending_surface_rebuild = False

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.app_icon())
        self.resize(*DEFAULT_SIZE)

        self._dashboard = DashboardPage(state, sampler, self)
        self._web_view = SyncthingWebView(config.gui_url(), config.api_key, self)
        self._log_view = _LogView(config.log_lines_kept, self)

        self._tabs = QTabWidget(self)
        # The dashboard leads: it is this application's own view of the sync.
        # The engine's configuration screen is a tab away for the things only it
        # can do — adding folders and pairing devices.
        self._tabs.addTab(self._dashboard, "Dashboard")
        self._tabs.addTab(self._web_view, "Configuration")
        self._tabs.addTab(self._log_view, "Log")
        # Stated explicitly rather than relying on the default: the web view
        # can claim focus as it loads, and the dashboard must be what opens.
        self._tabs.setCurrentWidget(self._dashboard)
        self.setCentralWidget(self._tabs)

        self._status_bar = QStatusBar(self)
        self.setStatusBar(self._status_bar)

        self._build_menus()
        self._restore_geometry()

        state.status_changed.connect(self._on_status_changed)
        state.connected.connect(self._web_view.reload_if_offline)

        # A lid-close sleep on macOS can leave the whole window painting blank
        # (Qt 6.11.1, QTBUG-147933: the native view's display lock is left held,
        # so the content — the tab bar included — stops painting and no repaint
        # recovers it; only rebuilding the view does). Such a sleep sends no
        # application-state or activation signal, so wake is spotted by a
        # heartbeat noticing a wall-clock jump: the event loop is frozen while the
        # machine sleeps, so on wake the first tick is seconds late.
        self._last_heartbeat = time.time()
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(HEARTBEAT_INTERVAL_MS)
        self._heartbeat.setTimerType(Qt.TimerType.VeryCoarseTimer)
        self._heartbeat.timeout.connect(self._on_heartbeat)
        self._heartbeat.start()

        self._on_status_changed(state.status)

    # ------------------------------------------------------------------- public
    def load_web_ui(self) -> None:
        self._web_view.load_syncthing()

    def append_log(self, line: str) -> None:
        self._log_view.append(line)

    def set_log(self, lines: list[str]) -> None:
        self._log_view.set_lines(lines)

    def set_process_state(self, process_state: object) -> None:
        if not isinstance(process_state, ProcessState):
            return
        self._process_state = process_state
        running = process_state in (ProcessState.STARTING, ProcessState.RUNNING)
        self._start_stop_action.setText(
            "Stop Sync Engine" if running else "Start Sync Engine"
        )
        self._start_stop_action.setEnabled(process_state is not ProcessState.STOPPING)
        self._restart_action.setEnabled(running)

        if process_state is ProcessState.STOPPED:
            self._web_view.show_placeholder("The sync engine is not running.")

    def reconfigure(self, config: Config) -> None:
        """Adopt settings the user just changed."""
        self._config = config
        self._web_view.reconfigure(config.gui_url(), config.api_key)
        self._web_view.load_syncthing()

    def show_and_raise(self) -> None:
        """Bring the window up from wherever it currently is."""
        self._intended_visible = True
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def prepare_for_quit(self) -> None:
        """Persist state and allow the next close to actually close."""
        self._force_close = True
        self._save_geometry()

    # -------------------------------------------------------------------- menus
    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        settings_action = file_menu.addAction("&Settings…")
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self.settings_requested)

        file_menu.addSeparator()

        close_action = file_menu.addAction("&Close Window")
        close_action.setShortcut(QKeySequence.StandardKey.Close)
        close_action.triggered.connect(self.close)

        exit_action = file_menu.addAction("E&xit")
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.exit_requested)

        engine_menu = self.menuBar().addMenu("&Engine")

        self._start_stop_action = engine_menu.addAction("Start Sync Engine")
        self._start_stop_action.triggered.connect(self._on_start_stop)

        self._restart_action = engine_menu.addAction("&Restart Sync Engine")
        self._restart_action.triggered.connect(self.restart_syncthing_requested)

        engine_menu.addSeparator()

        rescan_action = engine_menu.addAction("Rescan &All Folders")
        rescan_action.triggered.connect(self.rescan_all_requested)

        reload_action = engine_menu.addAction("Re&load Configuration")
        reload_action.setShortcut(QKeySequence.StandardKey.Refresh)
        reload_action.triggered.connect(self._web_view.load_syncthing)

        browser_action = engine_menu.addAction("Open Configuration in &Browser")
        browser_action.triggered.connect(self._open_in_browser)

        help_menu = self.menuBar().addMenu("&Help")
        open_data_action = help_menu.addAction("Open &Data Folder")
        open_data_action.triggered.connect(self._open_data_folder)
        about_action = help_menu.addAction("&About")
        about_action.triggered.connect(self._show_about)

    # ------------------------------------------------------------------ handlers
    def _on_start_stop(self) -> None:
        if self._process_state in (ProcessState.STARTING, ProcessState.RUNNING):
            self.stop_syncthing_requested.emit()
        else:
            self.start_syncthing_requested.emit()

    def _on_status_changed(self, status: object) -> None:
        if not isinstance(status, SyncStatus):
            return
        label = STATUS_LABELS.get(status, str(status))
        self._status_bar.showMessage(label)
        self.setWindowTitle(f"{APP_NAME} — {label}")

    def _open_in_browser(self) -> None:
        QDesktopServices.openUrl(QUrl(self._config.gui_url()))

    def _open_data_folder(self) -> None:
        from .. import paths

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.data_dir())))

    def _show_about(self) -> None:
        from .. import paths

        engine_version = self._state.snapshot.version or "not connected"

        # The engine ships with the application, which makes its licence terms
        # something this application distributes rather than merely points at.
        # Say where they are — but only when they are actually there, since a
        # build made without an engine downloads one instead. The install folder
        # is looked at first because the installer lifts a copy up to it; the
        # bundle's own directory is where they are otherwise.
        folders = (paths.install_dir(), *(p.parent for p in paths.bundled_syncthing_candidates()))
        notice = next(
            (d for d in folders if (d / paths.ENGINE_NOTICE_NAME).is_file()), None
        )
        provenance = (
            f"<br>Its licence and source notice are in {notice}."
            if notice is not None
            else ""
        )

        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME} {APP_VERSION}</h3>"
            "<p>Keeps your folders in sync, from the system tray.</p>"
            f"<p><b>Engine:</b> {engine_version}<br>"
            f"<b>Data folder:</b> {paths.data_dir()}<br>"
            f"<b>Portable mode:</b> {'yes' if paths.is_portable() else 'no'}</p>"
            # Attribution belongs somewhere, and About is where people look for
            # it. It is the only place the upstream project is named.
            "<p style='color:#898781'>Syncing is powered by Syncthing "
            f"(MPL-2.0), used unmodified.{provenance}</p>",
        )

    # ------------------------------------------------------------- window events
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._force_close:
            self._save_geometry()
            super().closeEvent(event)
            return

        self._save_geometry()
        event.ignore()

        if not self._config.close_to_tray:
            # With close-to-tray off, the close button means "quit". Merely
            # closing the window would leave the application running with
            # nothing on screen but a tray icon the user did not ask for.
            self.exit_requested.emit()
            return

        # Hide rather than quit: Syncthing keeps running in the background.
        self._intended_visible = False
        self.hide()
        self.hidden_to_tray.emit()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self._config.minimize_to_tray
        ):
            # Defer the hide: hiding a window from inside its own state-change
            # handler leaves the window manager mid-transition on Windows, which
            # can strand the taskbar button behind.
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        # If the machine slept while the window was hidden to the tray, its
        # native surface is wedged; rebuild it now that it is back on screen.
        if self._pending_surface_rebuild:
            self._pending_surface_rebuild = False
            QTimer.singleShot(0, self._recreate_surface)

    def _on_heartbeat(self) -> None:
        now = time.time()
        gap = now - self._last_heartbeat
        self._last_heartbeat = now
        # A gap far larger than the interval means the event loop was frozen,
        # which on macOS means the machine slept. It has to be the wall clock
        # (time.time): a monotonic clock also freezes across sleep and would hide
        # the gap entirely.
        if gap > WAKE_GAP_SECONDS:
            log.info("Woke after ~%.0fs asleep (intended_visible=%s)", gap, self._intended_visible)
            if self._intended_visible:
                QTimer.singleShot(0, self._recreate_surface)
            else:
                # Hidden to the tray during the sleep — the surface is rebuilt
                # when it is next brought back on screen (see showEvent), because
                # rebuilding a window that is not on screen does nothing.
                self._pending_surface_rebuild = True

    def _recreate_surface(self) -> None:
        """Rebuild the native window to clear a wedged post-sleep surface.

        Qt 6.11.1 leaves the macOS view's display lock held after a lid-close
        sleep (QTBUG-147933): the whole content — the tab bar included — paints
        blank, and repaint()/resize cannot help because they reuse the same
        locked view. Hiding and re-showing the window builds a fresh view whose
        lock is not held, so painting resumes.
        """
        # Only ever reached when the window is meant to be on screen — either it
        # was visible across the sleep, or it has just been brought back from the
        # tray (showEvent). Across a lid sleep Qt desyncs its own state (it
        # reports the on-screen window as hidden AND minimised, and a minimised
        # window is never painted — the blank), so isHidden()/isMinimized() are
        # only logged here, never trusted for control flow.
        log.info(
            "Rebuilding surface (hidden=%s minimized=%s)",
            self.isHidden(), self.isMinimized(),
        )
        geometry = self.geometry()
        # Destroy the wedged native view (and its held display lock), then show
        # it back to a proper, un-minimised state, which builds a fresh view.
        handle = self.windowHandle()
        if handle is not None:
            handle.destroy()
            log.info("Surface rebuild: native view destroyed")
        self.showNormal()
        self.setGeometry(geometry)
        self.raise_()
        self.activateWindow()
        # The embedded Chromium view loses its own surface across the same sleep.
        self._web_view.reload_after_wake()
        log.info("Window surface rebuilt after wake")

    def _hide_to_tray(self) -> None:
        self._intended_visible = False
        self.hide()
        self.hidden_to_tray.emit()

    # ---------------------------------------------------------------- geometry
    def _save_geometry(self) -> None:
        if self.isMinimized():
            return  # Don't persist a minimised window as the restore position.
        blob = bytes(self.saveGeometry().data())
        self._config.window_geometry = base64.b64encode(blob).decode("ascii")

    def _restore_geometry(self) -> None:
        if not self._config.window_geometry:
            return
        try:
            blob = base64.b64decode(self._config.window_geometry.encode("ascii"))
        except (ValueError, TypeError) as exc:
            log.warning("Discarding unreadable saved window geometry: %s", exc)
            return
        self.restoreGeometry(QByteArray(blob))


class _LogView(QPlainTextEdit):
    """Read-only, bounded view of Syncthing's console output."""

    def __init__(self, max_lines: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # A block limit means a long-running instance cannot grow without bound.
        self.setMaximumBlockCount(max(100, max_lines))

        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.setFont(font)

    def append(self, line: str) -> None:
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self.appendPlainText(line)
        # Only auto-scroll if the user was already at the bottom, so scrolling
        # back to read something is not constantly interrupted.
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def set_lines(self, lines: list[str]) -> None:
        self.setPlainText("\n".join(lines))
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
