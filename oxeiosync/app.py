"""Application assembly: owns the pieces and connects them to each other.

Every component here is deliberately ignorant of the others. The process
supervisor does not know a tray icon exists; the state model does not know it is
driving notifications. This class is the only place that knows the whole shape
of the application, which keeps the interesting logic testable in isolation.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME, autostart, paths
from . import config as config_module
from .net import find_free_port, is_port_available, join_bind_address, split_bind_address
from .syncthing import binary
from .syncthing.api import EVENT_POLL_TIMEOUT, SyncthingApi, SyncthingApiError
from .syncthing.events import EventPoller
from .syncthing.process import ProcessState, SyncthingProcess
from .syncthing.recent import BACKLOG as RECENT_BACKLOG
from .syncthing.recent import RecentFiles
from .syncthing.state import SyncthingState
from .syncthing.transfer import TransferSampler
from .ui import icons
from .ui.download import download_syncthing_interactive
from .ui.main_window import MainWindow
from .ui.settings_dialog import SettingsDialog
from .ui.tray import TrayIcon

log = logging.getLogger(__name__)

#: Seconds allowed on top of one poll's hold for the pollers to finish on quit:
#: the request itself, and the thread waking up to notice it was told to stop.
POLLER_STOP_MARGIN = 2.0
#: How long quit() waits for the pollers, all told: a whole hold, so a poll that
#: has only just begun can run its course, plus the margin.
POLLER_STOP_DEADLINE = EVENT_POLL_TIMEOUT + POLLER_STOP_MARGIN


def running_threads(*roots: QObject | None) -> list[str]:
    """The kinds of QThread under *roots* that are still running.

    Found by walking the object tree rather than from a list of known workers:
    a poller, a snapshot, a download started from a dialog on the window — a
    worker added later cannot be missed by forgetting to register it.
    """
    names = []
    for root in roots:
        if root is None:
            continue
        names.extend(
            type(thread).__name__ for thread in root.findChildren(QThread) if thread.isRunning()
        )
    return names


class Application(QObject):
    """The running application."""

    def __init__(self, qapp: QApplication, start_minimized: bool = False) -> None:
        super().__init__()
        self._qapp = qapp
        self._quitting = False
        #: Background threads quit() could not see finish, by name. Read by
        #: main() after the event loop ends: a live QThread must not be torn down.
        self.unfinished_threads: list[str] = []
        #: The "still running in the tray" hint is shown at most once per run.
        self._explained_tray = False

        paths.ensure_dirs()
        first_run = not paths.config_file().exists()
        self._config = config_module.load()

        # Generate the API key before anything else: the process supervisor
        # passes it to Syncthing at launch and the API client needs it to
        # connect, so it has to exist before either is built.
        dirty = False
        if not self._config.api_key:
            self._config.ensure_api_key()
            dirty = True
        if first_run:
            dirty |= self._choose_initial_gui_port()
        if dirty:
            config_module.save(self._config)

        self._state = SyncthingState(self._config, self)
        self._process = SyncthingProcess(self._config, self)
        self._poller = EventPoller(self._config.gui_url(), self._config.api_key, self)
        # A second feed, for file changes, which the general one leaves out. It
        # replays what the engine remembers, because the Recent tab shows history.
        self._disk_poller = EventPoller(
            self._config.gui_url(), self._config.api_key, self,
            disk=True, backlog=RECENT_BACKLOG,
        )
        self._sampler = TransferSampler(self._config, self)
        self._recent = RecentFiles(self)

        self._tray = TrayIcon(self._state, self)
        self._window = MainWindow(self._config, self._state, self._sampler, self._recent, None)
        self._window.set_log(self._process.log_tail())

        self._wire()

        qapp.setWindowIcon(icons.app_icon())
        qapp.setQuitOnLastWindowClosed(False)  # The tray keeps us alive.

        self._start_minimized = start_minimized or self._config.start_minimized

    def _choose_initial_gui_port(self) -> bool:
        """Move off the default GUI port on first run if something else has it.

        Syncthing's default port is commonly already taken — by an existing
        Syncthing install, by SyncTrayzor, or by another user's session. Picking
        a free one now means a first launch just works instead of failing with a
        bind error the user has to go and diagnose.
        """
        host, port = split_bind_address(self._config.gui_address)
        if is_port_available(host, port):
            return False

        free = find_free_port(host, port + 1)
        if free is None:
            log.warning("Port %s is busy and no nearby port is free", port)
            return False

        self._config.gui_address = join_bind_address(host, free)
        log.info(
            "Port %s is in use; Syncthing's GUI will use %s instead", port, free
        )
        return True

    # ------------------------------------------------------------------ start-up
    def start(self) -> None:
        """Show the UI and bring Syncthing up."""
        if not TrayIcon.is_available():
            log.warning("No system tray available; the window will be shown instead")
            self._start_minimized = False
        self._tray.show()

        if not self._start_minimized:
            self._window.show_and_raise()

        autostart.sync_with_config(self._config.start_on_login)
        self._poller.start()
        self._disk_poller.start()

        if self._config.start_syncthing_automatically:
            # Defer past the first event-loop turn so the window is painted
            # before any modal first-run download dialog appears on top of it.
            QTimer.singleShot(0, self._start_syncthing)

    def _start_syncthing(self) -> None:
        missing = binary.find_syncthing(self._config.syncthing_path) is None
        if missing and not self._offer_download():
            return
        self._process.start()

    def _offer_download(self) -> bool:
        """First-run path: ask to fetch Syncthing. True if one is now available."""
        answer = QMessageBox.question(
            self._window,
            f"{APP_NAME} — Sync engine not found",
            "oXeioSync could not find its sync engine.\n\n"
            "Download the latest release now? It will be verified and stored in "
            f"{paths.binary_dir()}.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._window.append_log(
                "[oXeioSync] no sync engine; set one in Settings to continue"
            )
            return False

        installed: Path | None = download_syncthing_interactive(self._window)
        return installed is not None

    # -------------------------------------------------------------------- wiring
    def _wire(self) -> None:
        # --- process -> everything else
        self._process.state_changed.connect(self._on_process_state_changed)
        self._process.log_line.connect(self._window.append_log)
        self._process.error.connect(self._on_process_error)

        # --- events -> state model
        self._poller.event_received.connect(self._state.handle_event)
        self._poller.connected.connect(self._state.refresh)

        # --- file changes -> recent images
        self._disk_poller.backlog_received.connect(self._recent.reset)
        self._disk_poller.event_received.connect(self._recent.add)

        # --- state -> notifications
        self._state.connected.connect(self._on_api_connected)
        self._state.folder_sync_finished.connect(self._on_folder_synced)
        self._state.device_connected.connect(self._on_device_connected)
        self._state.device_disconnected.connect(self._on_device_disconnected)
        self._state.conflict_detected.connect(self._on_conflict)
        self._state.folder_out_of_sync.connect(self._on_folder_out_of_sync)
        self._state.status_changed.connect(self._tray.set_status)

        # --- tray -> actions
        self._tray.show_window_requested.connect(self._window.show_and_raise)
        self._tray.open_in_browser_requested.connect(self._open_in_browser)
        self._tray.settings_requested.connect(self._show_settings)
        self._tray.exit_requested.connect(self.quit)
        self._tray.start_syncthing_requested.connect(self._start_syncthing)
        self._tray.stop_syncthing_requested.connect(self._process.stop)
        self._tray.restart_syncthing_requested.connect(self._process.restart)
        self._tray.rescan_all_requested.connect(self._rescan_all)

        # --- window -> actions
        self._window.settings_requested.connect(self._show_settings)
        self._window.exit_requested.connect(self.quit)
        self._window.start_syncthing_requested.connect(self._start_syncthing)
        self._window.stop_syncthing_requested.connect(self._process.stop)
        self._window.restart_syncthing_requested.connect(self._process.restart)
        self._window.rescan_all_requested.connect(self._rescan_all)
        self._window.hidden_to_tray.connect(self._on_hidden_to_tray)

    # ------------------------------------------------------------------ handlers
    def _on_process_state_changed(self, state: object) -> None:
        if not isinstance(state, ProcessState):
            return
        running = state in (ProcessState.STARTING, ProcessState.RUNNING)
        self._tray.set_process_state(state)
        self._window.set_process_state(state)
        self._state.set_process_running(running)
        if not running:
            # The engine's memory of changes goes with it; a stopped engine's
            # list would otherwise linger as if it were current.
            self._recent.clear()

        # Sampling only makes sense while there is an engine to sample; its
        # rate baseline is discarded on stop so the first reading after a
        # restart is not one huge spike.
        if running:
            self._sampler.start()
        else:
            self._sampler.stop()

    def _on_process_error(self, message: str) -> None:
        self._tray.notify(f"{APP_NAME} — Sync engine problem", message)

    def _on_api_connected(self) -> None:
        # Reaching the REST API is stronger evidence that Syncthing is up than
        # anything we can infer from its log output.
        self._process.mark_running()
        self._window.load_web_ui()

    def _on_folder_synced(self, folder_name: str) -> None:
        if self._config.notify_folder_synced:
            self._tray.notify("Folder synced", f"{folder_name} finished syncing.")

    def _on_device_connected(self, device_name: str) -> None:
        if self._config.notify_device_connections:
            self._tray.notify("Device connected", f"{device_name} is now connected.")

    def _on_device_disconnected(self, device_name: str) -> None:
        if self._config.notify_device_connections:
            self._tray.notify("Device disconnected", f"{device_name} went offline.")

    def _on_conflict(self, folder_name: str, item: str) -> None:
        if self._config.notify_conflicts:
            self._tray.notify(
                "File conflict",
                f"A conflicting copy was created in {folder_name}:\n{item}",
            )

    def _on_folder_out_of_sync(self, folder_name: str, detail: str) -> None:
        if self._config.notify_folder_errors:
            self._tray.notify("Folder out of sync", f"{folder_name}\n{detail}")

    def _on_hidden_to_tray(self) -> None:
        # Say this once per session at most; repeating it every time the window
        # is closed would be nagging.
        if self._explained_tray:
            return
        self._explained_tray = True
        self._tray.notify(
            f"{APP_NAME} is still running",
            "Your folders keep syncing in the background. "
            "Click the tray icon to bring the window back.",
        )

    def _open_in_browser(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(self._config.gui_url()))

    def _rescan_all(self) -> None:
        api = SyncthingApi(self._config.gui_url(), self._config.api_key)
        try:
            api.rescan()
        except SyncthingApiError as exc:
            log.warning("Rescan failed: %s", exc)
            self._tray.notify(f"{APP_NAME} — Rescan failed", str(exc))
        else:
            self._state.refresh()
        finally:
            api.close()

    # ------------------------------------------------------------------ settings
    def _show_settings(self) -> None:
        dialog = SettingsDialog(self._config, self._window)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return

        new_config = dialog.result_config()
        changed = dialog.changed_fields()
        needs_restart = dialog.requires_syncthing_restart()
        log.info("Settings changed: %s", ", ".join(changed) or "nothing")

        # Mutate in place: the process supervisor, state model and poller all
        # hold a reference to this object and read it lazily.
        for field_name in changed:
            setattr(self._config, field_name, getattr(new_config, field_name))
        config_module.save(self._config)

        autostart.sync_with_config(self._config.start_on_login)

        if needs_restart:
            self._poller.reconfigure(self._config.gui_url(), self._config.api_key)
            self._disk_poller.reconfigure(self._config.gui_url(), self._config.api_key)
            self._sampler.reconfigure(self._config.gui_url(), self._config.api_key)
            self._sampler.clear()
            self._window.reconfigure(self._config)
            if self._process.is_running():
                self._process.restart()

    # ---------------------------------------------------------------------- exit
    def quit(self) -> None:
        """Shut everything down in dependency order, then leave."""
        if self._quitting:
            return
        self._quitting = True
        log.info("Shutting down")

        # Told first, because telling them is all that can be done: a poll in
        # flight only ends when the engine answers it, and it may as well run
        # out while the rest of the shutdown goes on.
        self._poller.stop()
        self._disk_poller.stop()

        self._window.prepare_for_quit()
        self._window.close()
        self._tray.hide()
        # Off screen now, so a thumbnail still decoding holds up nothing visible.
        if not self._window.wait_for_background_work(2000):
            log.warning("Thumbnail decoding did not finish in time")

        self._sampler.stop()
        if not self._state.stop():
            log.warning("The snapshot worker did not stop in time")
            self.unfinished_threads.append("snapshot worker")

        self._process.shutdown_blocking()

        # One deadline for both pollers, long enough for a poll that has just
        # started to run its course. A poller still running past it is named,
        # so main() can leave without destroying a live thread.
        deadline = time.monotonic() + POLLER_STOP_DEADLINE
        for name, poller in (("events", self._poller), ("file changes", self._disk_poller)):
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if not poller.wait(remaining):
                log.warning("Event poller (%s) did not stop in time", name)
                self.unfinished_threads.append(f"event poller ({name})")

        config_module.save(self._config)
        self._qapp.quit()

    def handle_second_instance(self) -> None:
        """Another copy was launched: surface this one instead of starting again."""
        self._window.show_and_raise()

    def threads_still_running(self) -> list[str]:
        """What quit() saw outlive its waits, and any thread still running now.

        Read by main() once the event loop has ended. The window is searched as
        well as this object: it has no parent, and a first-run download runs
        under a dialog that belongs to it.
        """
        names = list(self.unfinished_threads)
        for name in running_threads(self, self._window):
            if name not in names:
                names.append(name)
        return names
