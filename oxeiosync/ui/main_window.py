"""The main window: the embedded Syncthing UI, plus a log view.

The window is deliberately secondary to the tray icon. Closing it hides it by
default rather than quitting, which is what makes the application feel like a
background service with a face rather than a program you have to keep open.
"""

from __future__ import annotations

import base64
import logging

from PySide6.QtCore import QByteArray, QEvent, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont, QKeySequence
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
            "(MPL-2.0), downloaded separately and not modified.</p>",
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

    def _hide_to_tray(self) -> None:
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
