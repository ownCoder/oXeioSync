"""The system tray icon: status at a glance, plus the main entry point to the app.

The tray is the primary UI — the window is optional. The icon's colour carries
the current sync status and it spins while data is moving, mirroring what
SyncTrayzor does.

Dynamic parts of the context menu (folders, devices, the start/stop item) are
rebuilt in ``aboutToShow`` rather than on every state change. Rebuilding a menu
that is already on screen is unsafe, and nobody can read a menu that isn't open.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import APP_NAME
from ..syncthing.process import ProcessState
from ..syncthing.state import FolderState, SyncStatus, SyncthingState
from . import icons

log = logging.getLogger(__name__)

#: Frame interval for the syncing animation, in milliseconds.
ANIMATION_INTERVAL_MS = 90

#: Statuses that should animate.
ANIMATED_STATUSES = {SyncStatus.SYNCING, SyncStatus.SCANNING, SyncStatus.CONNECTING}

STATUS_LABELS = {
    SyncStatus.STOPPED: "Sync engine is not running",
    SyncStatus.CONNECTING: "Connecting to the sync engine…",
    SyncStatus.IDLE: "Up to date",
    SyncStatus.SCANNING: "Scanning…",
    SyncStatus.SYNCING: "Syncing…",
    SyncStatus.WARNING: "Some folders are out of sync",
    SyncStatus.ERROR: "The sync engine reported an error",
}

#: How long balloon notifications stay up, in milliseconds.
NOTIFICATION_TIMEOUT_MS = 6000


class TrayIcon(QObject):
    """Owns the tray icon and its menu, and reports what the user picked."""

    show_window_requested = Signal()
    open_in_browser_requested = Signal()
    settings_requested = Signal()
    exit_requested = Signal()

    start_syncthing_requested = Signal()
    stop_syncthing_requested = Signal()
    restart_syncthing_requested = Signal()
    rescan_all_requested = Signal()

    def __init__(self, state: SyncthingState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._status = SyncStatus.STOPPED
        self._process_state = ProcessState.STOPPED
        self._frame = 0

        self._icon = QSystemTrayIcon(icons.status_icon(SyncStatus.STOPPED), self)
        self._icon.setToolTip(APP_NAME)
        self._icon.activated.connect(self._on_activated)
        self._icon.messageClicked.connect(self.show_window_requested)

        self._animation = QTimer(self)
        self._animation.setInterval(ANIMATION_INTERVAL_MS)
        self._animation.timeout.connect(self._advance_animation)

        self._build_menu()

        state.status_changed.connect(self.set_status)
        state.changed.connect(self._refresh_tooltip)

    # ----------------------------------------------------------------- lifecycle
    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._animation.stop()
        self._icon.hide()

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    # -------------------------------------------------------------------- state
    def set_status(self, status: object) -> None:
        """React to a new overall sync status."""
        if not isinstance(status, SyncStatus) or status is self._status:
            return
        self._status = status

        if status in ANIMATED_STATUSES:
            if not self._animation.isActive():
                self._animation.start()
        else:
            self._animation.stop()
            self._frame = 0

        self._apply_icon()
        self._refresh_tooltip()

    def set_process_state(self, process_state: object) -> None:
        if isinstance(process_state, ProcessState):
            self._process_state = process_state

    def notify(self, title: str, message: str) -> None:
        """Show a balloon notification, if the platform supports them."""
        if not QSystemTrayIcon.supportsMessages():
            log.debug("Notifications unsupported; dropping %r", title)
            return
        self._icon.showMessage(
            title,
            message,
            icons.status_icon(self._status),
            NOTIFICATION_TIMEOUT_MS,
        )

    # --------------------------------------------------------------------- menu
    def _build_menu(self) -> None:
        menu = QMenu()
        menu.aboutToShow.connect(self._refresh_menu)

        self._open_action = menu.addAction(f"Open {APP_NAME}")
        open_font = QFont(self._open_action.font())
        open_font.setBold(True)
        self._open_action.setFont(open_font)
        self._open_action.triggered.connect(self.show_window_requested)

        menu.addSeparator()

        # A disabled action is the conventional way to put a read-only status
        # line in a tray menu.
        self._status_action = menu.addAction(STATUS_LABELS[SyncStatus.STOPPED])
        self._status_action.setEnabled(False)

        self._folders_menu = menu.addMenu("Folders")
        self._folders_menu.aboutToShow.connect(self._refresh_folders_menu)
        self._devices_menu = menu.addMenu("Devices")
        self._devices_menu.aboutToShow.connect(self._refresh_devices_menu)

        menu.addSeparator()

        self._rescan_action = menu.addAction("Rescan All Folders")
        self._rescan_action.triggered.connect(self.rescan_all_requested)

        self._start_stop_action = menu.addAction("Start Sync Engine")
        self._start_stop_action.triggered.connect(self._on_start_stop)

        self._restart_action = menu.addAction("Restart Sync Engine")
        self._restart_action.triggered.connect(self.restart_syncthing_requested)

        menu.addSeparator()

        browser_action = menu.addAction("Open Configuration in Browser")
        browser_action.triggered.connect(self.open_in_browser_requested)

        settings_action = menu.addAction("Settings…")
        settings_action.triggered.connect(self.settings_requested)

        menu.addSeparator()

        exit_action = menu.addAction("Exit")
        exit_action.triggered.connect(self.exit_requested)

        # Keep a reference: QSystemTrayIcon does not take ownership, and a
        # garbage-collected menu means a tray icon that does nothing on click.
        self._menu = menu
        self._icon.setContextMenu(menu)

    def _refresh_menu(self) -> None:
        self._status_action.setText(STATUS_LABELS.get(self._status, str(self._status)))

        running = self._process_state in (ProcessState.STARTING, ProcessState.RUNNING)
        transitioning = self._process_state is ProcessState.STOPPING

        self._start_stop_action.setText(
            "Stop Sync Engine" if running else "Start Sync Engine"
        )
        self._start_stop_action.setEnabled(not transitioning)
        self._restart_action.setEnabled(running)
        self._rescan_action.setEnabled(self._state.is_connected)
        self._folders_menu.setEnabled(self._state.is_connected)
        self._devices_menu.setEnabled(self._state.is_connected)

    def _refresh_folders_menu(self) -> None:
        self._folders_menu.clear()
        folders = self._state.folders()
        if not folders:
            self._folders_menu.addAction("No folders configured").setEnabled(False)
            return

        for folder in folders:
            action = self._folders_menu.addAction(_folder_label(folder))
            if folder.path:
                action.setToolTip(folder.path)
                # Bind the path now; the folder object may be replaced by the
                # next snapshot before this action is ever triggered.
                path = folder.path
                action.triggered.connect(lambda _checked=False, p=path: _open_path(p))
            else:
                action.setEnabled(False)

    def _refresh_devices_menu(self) -> None:
        self._devices_menu.clear()
        devices = self._state.devices()
        if not devices:
            self._devices_menu.addAction("No devices configured").setEnabled(False)
            return

        for device in devices:
            if device.paused:
                suffix = "paused"
            elif device.connected:
                suffix = "connected"
            else:
                suffix = "disconnected"
            action = self._devices_menu.addAction(f"{device.display_name} — {suffix}")
            action.setEnabled(False)
            if device.address:
                action.setToolTip(device.address)

    # ------------------------------------------------------------------ handlers
    def _on_start_stop(self) -> None:
        if self._process_state in (ProcessState.STARTING, ProcessState.RUNNING):
            self.stop_syncthing_requested.emit()
        else:
            self.start_syncthing_requested.emit()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window_requested.emit()

    def _advance_animation(self) -> None:
        self._frame = (self._frame + 1) % icons.ANIMATION_FRAMES
        self._apply_icon()

    def _apply_icon(self) -> None:
        self._icon.setIcon(icons.status_icon(self._status, self._frame))

    def _refresh_tooltip(self) -> None:
        self._icon.setToolTip(self._state.tooltip())


def _folder_label(folder: FolderState) -> str:
    """Menu text for one folder, including progress when it is behind."""
    name = folder.display_name
    if folder.paused:
        return f"{name} — paused"
    if folder.error_count:
        return f"{name} — {folder.error_count} error(s)"
    if folder.completion < 100.0:
        return f"{name} — {folder.completion:.0f}%"
    return name


def _open_path(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))
