"""The settings dialog.

Only host-application settings live here. Folders, devices, ignore patterns and
everything else Syncthing owns are edited in Syncthing's own UI, which is
embedded a tab away — duplicating them would mean two sources of truth.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import os
import secrets

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, autostart, paths
from ..config import DEFAULT_GUI_ADDRESS, Config
from ..net import is_port_available, join_bind_address, split_bind_address
from ..syncthing import binary
from .download import download_syncthing_interactive

log = logging.getLogger(__name__)

#: Changing any of these means the running Syncthing process is using stale
#: settings and has to be restarted for them to take effect.
RESTART_TRIGGERING_FIELDS = (
    "syncthing_path",
    "syncthing_home",
    "syncthing_extra_args",
    "gui_address",
    "api_key",
)


class SettingsDialog(QDialog):
    """Edits a copy of the configuration; the caller decides whether to keep it."""

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._original = config
        self._working = copy.deepcopy(config)

        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_syncthing_group())
        layout.addWidget(self._build_behaviour_group())
        layout.addWidget(self._build_notifications_group())
        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_from_config()

    # -------------------------------------------------------------------- groups
    def _build_syncthing_group(self) -> QGroupBox:
        group = QGroupBox("Sync engine", self)
        form = QFormLayout(group)

        self._path_edit = QLineEdit(group)
        self._path_edit.setPlaceholderText("Leave empty to detect automatically")
        browse = QPushButton("Browse…", group)
        browse.clicked.connect(self._on_browse_binary)
        download = QPushButton("Download…", group)
        download.clicked.connect(self._on_download)
        form.addRow("Executable:", _row(self._path_edit, browse, download))

        self._detected_label = QLabel(group)
        self._detected_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._detected_label.setWordWrap(True)
        form.addRow("", self._detected_label)

        self._home_edit = QLineEdit(group)
        self._home_edit.setPlaceholderText(str(paths.syncthing_home()))
        home_browse = QPushButton("Browse…", group)
        home_browse.clicked.connect(self._on_browse_home)
        form.addRow("Database folder:", _row(self._home_edit, home_browse))

        self._address_edit = QLineEdit(group)
        self._address_edit.setPlaceholderText(DEFAULT_GUI_ADDRESS)
        form.addRow("Engine address:", self._address_edit)

        self._api_key_edit = QLineEdit(group)
        regenerate = QPushButton("Regenerate", group)
        regenerate.clicked.connect(self._on_regenerate_key)
        form.addRow("API key:", _row(self._api_key_edit, regenerate))

        self._extra_args_edit = QLineEdit(group)
        self._extra_args_edit.setPlaceholderText("Extra command-line arguments, space separated")
        form.addRow("Extra arguments:", self._extra_args_edit)

        self._auto_start_check = QCheckBox(
            "Start the sync engine when oXeioSync starts", group
        )
        form.addRow("", self._auto_start_check)

        return group

    def _build_behaviour_group(self) -> QGroupBox:
        group = QGroupBox("Window and startup", self)
        layout = QVBoxLayout(group)

        self._login_check = QCheckBox("Start oXeioSync when I log in", group)
        self._login_check.setEnabled(autostart.is_supported())
        layout.addWidget(self._login_check)

        hint = QLabel(autostart.describe(), group)
        hint.setWordWrap(True)
        hint.setEnabled(False)
        layout.addWidget(hint)

        self._start_minimized_check = QCheckBox("Start minimised to the tray", group)
        layout.addWidget(self._start_minimized_check)

        self._close_to_tray_check = QCheckBox("Closing the window hides it to the tray", group)
        layout.addWidget(self._close_to_tray_check)

        self._minimize_to_tray_check = QCheckBox(
            "Minimising the window hides it to the tray", group
        )
        layout.addWidget(self._minimize_to_tray_check)

        return group

    def _build_notifications_group(self) -> QGroupBox:
        group = QGroupBox("Notifications", self)
        layout = QVBoxLayout(group)

        self._notify_synced_check = QCheckBox("A folder finished syncing", group)
        self._notify_devices_check = QCheckBox("A device connected or disconnected", group)
        self._notify_conflicts_check = QCheckBox("A file conflict was created", group)
        self._notify_errors_check = QCheckBox("A folder is out of sync", group)

        for check in (
            self._notify_synced_check,
            self._notify_devices_check,
            self._notify_conflicts_check,
            self._notify_errors_check,
        ):
            layout.addWidget(check)

        return group

    # ------------------------------------------------------------------- binding
    def _load_from_config(self) -> None:
        cfg = self._working
        self._path_edit.setText(cfg.syncthing_path)
        self._home_edit.setText(cfg.syncthing_home)
        self._address_edit.setText(cfg.gui_address)
        self._api_key_edit.setText(cfg.api_key)
        self._extra_args_edit.setText(" ".join(cfg.syncthing_extra_args))
        self._auto_start_check.setChecked(cfg.start_syncthing_automatically)

        # Read the real registry state rather than trusting the config: the user
        # may have removed the entry by hand since it was written.
        self._login_check.setChecked(
            autostart.is_enabled() if autostart.is_supported() else cfg.start_on_login
        )
        self._start_minimized_check.setChecked(cfg.start_minimized)
        self._close_to_tray_check.setChecked(cfg.close_to_tray)
        self._minimize_to_tray_check.setChecked(cfg.minimize_to_tray)

        self._notify_synced_check.setChecked(cfg.notify_folder_synced)
        self._notify_devices_check.setChecked(cfg.notify_device_connections)
        self._notify_conflicts_check.setChecked(cfg.notify_conflicts)
        self._notify_errors_check.setChecked(cfg.notify_folder_errors)

        self._refresh_detected_label()

    def _apply_to_working(self) -> None:
        cfg = self._working
        cfg.syncthing_path = self._path_edit.text().strip()
        cfg.syncthing_home = self._home_edit.text().strip()
        cfg.gui_address = self._address_edit.text().strip() or DEFAULT_GUI_ADDRESS
        cfg.api_key = self._api_key_edit.text().strip()
        cfg.syncthing_extra_args = self._extra_args_edit.text().split()
        cfg.start_syncthing_automatically = self._auto_start_check.isChecked()

        cfg.start_on_login = self._login_check.isChecked()
        cfg.start_minimized = self._start_minimized_check.isChecked()
        cfg.close_to_tray = self._close_to_tray_check.isChecked()
        cfg.minimize_to_tray = self._minimize_to_tray_check.isChecked()

        cfg.notify_folder_synced = self._notify_synced_check.isChecked()
        cfg.notify_device_connections = self._notify_devices_check.isChecked()
        cfg.notify_conflicts = self._notify_conflicts_check.isChecked()
        cfg.notify_folder_errors = self._notify_errors_check.isChecked()

    # ------------------------------------------------------------------ handlers
    def _on_browse_binary(self) -> None:
        pattern = (
            "Executables (*.exe);;All files (*)" if os.name == "nt" else "All files (*)"
        )
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Select the sync engine executable", self._path_edit.text(), pattern
        )
        if chosen:
            self._path_edit.setText(chosen)
            self._refresh_detected_label()

    def _on_browse_home(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select the sync database folder", self._home_edit.text()
        )
        if chosen:
            self._home_edit.setText(chosen)

    def _on_download(self) -> None:
        installed = download_syncthing_interactive(self)
        if installed is None:
            return
        # Clear any override so the freshly downloaded copy is what gets used.
        if self._path_edit.text().strip():
            self._path_edit.clear()
        self._refresh_detected_label()
        QMessageBox.information(
            self,
            f"{APP_NAME} — Sync engine installed",
            f"The sync engine was installed at:\n{installed}\n\n"
            "Restart it from the tray menu to use it.",
        )

    def _on_regenerate_key(self) -> None:
        confirmed = QMessageBox.question(
            self,
            f"{APP_NAME} — Regenerate API key",
            "Generate a new API key?\n\n"
            "Anything else using the old key — scripts, other sync "
            "clients — will stop working until it is updated.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed == QMessageBox.StandardButton.Yes:
            self._api_key_edit.setText(secrets.token_hex(16))

    def _refresh_detected_label(self) -> None:
        override = self._path_edit.text().strip()
        found = binary.find_syncthing(override)
        if found is None:
            self._detected_label.setText(
                "No sync engine found. Use Download… to fetch one."
            )
            return
        version = binary.probe_version(found)
        suffix = f" ({version})" if version else " (version unknown)"
        self._detected_label.setText(f"Using: {found}{suffix}")

    def _on_accept(self) -> None:
        self._apply_to_working()

        if not self._working.api_key:
            QMessageBox.warning(
                self,
                f"{APP_NAME} — Settings",
                "An API key is required so oXeioSync can talk to the sync engine.\n"
                "Use Regenerate to create one.",
            )
            return

        if not self._confirm_gui_address():
            return

        self.accept()

    def _confirm_gui_address(self) -> bool:
        """Warn if a newly chosen GUI address is already taken.

        Only a *changed* address is checked. The current one is very often
        occupied by the Syncthing instance oXeioSync is running right now, and
        warning about that would be nonsense.
        """
        if self._working.gui_address == self._original.gui_address:
            return True

        host, port = split_bind_address(self._working.gui_address)
        if is_port_available(host, port):
            return True

        answer = QMessageBox.question(
            self,
            f"{APP_NAME} — Address in use",
            f"Something is already listening on {join_bind_address(host, port)}, "
            "so the sync engine will not be able to start there.\n\n"
            "Save this address anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # -------------------------------------------------------------------- result
    def result_config(self) -> Config:
        """The edited configuration. Only meaningful after the dialog is accepted."""
        return self._working

    def requires_syncthing_restart(self) -> bool:
        """True if a setting changed that Syncthing only reads at launch."""
        return any(
            getattr(self._original, name) != getattr(self._working, name)
            for name in RESTART_TRIGGERING_FIELDS
        )

    def changed_fields(self) -> list[str]:
        """Names of every field the user actually changed, for logging."""
        return [
            field.name
            for field in dataclasses.fields(Config)
            if getattr(self._original, field.name) != getattr(self._working, field.name)
        ]


def _row(*widgets: QWidget) -> QWidget:
    """Lay widgets out side by side, with the first one taking the slack."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if index == 0 else 0)
    return container
