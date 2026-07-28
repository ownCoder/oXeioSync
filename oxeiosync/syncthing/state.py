"""The application's view of what Syncthing is currently doing.

Two sources are combined:

*Snapshots*
    Periodic REST calls that rebuild the full picture — folders, devices,
    completion, errors. Authoritative but coarse, and taken on a worker thread
    because each one is a handful of blocking HTTP requests.

*Events*
    Syncthing's long-polled event feed, which says what just changed. Fine
    grained and immediate, and the only place transient facts live: "this
    folder finished syncing", "this file conflicted".

Events drive the notifications and trigger a debounced snapshot; a slow
heartbeat refresh runs regardless, so a missed event cannot leave the UI stale
indefinitely.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..config import Config
from .api import SyncthingApi, SyncthingApiError

log = logging.getLogger(__name__)

#: Marker Syncthing puts in the name of every conflicting copy it creates.
CONFLICT_MARKER = ".sync-conflict-"
#: Quiet period after an event before a snapshot is taken, in milliseconds.
SNAPSHOT_DEBOUNCE_MS = 400
#: Heartbeat refresh interval, in milliseconds.
SNAPSHOT_INTERVAL_MS = 30_000
#: How often to retry the snapshot while Syncthing is still starting up.
RECONNECT_INTERVAL_MS = 2_000

#: Folder states Syncthing reports while it is actively moving data.
BUSY_FOLDER_STATES = {"syncing", "sync-preparing", "sync-waiting"}
SCANNING_FOLDER_STATES = {"scanning", "scan-waiting", "cleaning", "cleaning-waiting"}


class SyncStatus(Enum):
    """The single overall status shown on the tray icon."""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    IDLE = "idle"
    SCANNING = "scanning"
    SYNCING = "syncing"
    WARNING = "warning"
    ERROR = "error"

    def __str__(self) -> str:
        return self.value


@dataclass
class FolderState:
    """One shared folder, as far as the UI needs to care."""

    id: str
    label: str = ""
    path: str = ""
    state: str = "unknown"
    paused: bool = False
    completion: float = 100.0
    need_bytes: int = 0
    global_bytes: int = 0
    error_count: int = 0

    @property
    def display_name(self) -> str:
        return self.label or self.id

    @property
    def is_busy(self) -> bool:
        return self.state in BUSY_FOLDER_STATES

    @property
    def is_scanning(self) -> bool:
        return self.state in SCANNING_FOLDER_STATES


@dataclass
class DeviceState:
    """One remote device."""

    id: str
    name: str = ""
    connected: bool = False
    paused: bool = False
    address: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.id[:7]


@dataclass
class Snapshot:
    """A complete, consistent-enough reading of Syncthing's state."""

    version: str = ""
    my_id: str = ""
    folders: dict[str, FolderState] = field(default_factory=dict)
    devices: dict[str, DeviceState] = field(default_factory=dict)
    system_errors: list[str] = field(default_factory=list)


class SyncthingState(QObject):
    """Aggregated Syncthing state, with signals the UI can bind to."""

    #: The overall status changed; carries a :class:`SyncStatus`.
    status_changed = Signal(object)
    #: Any part of the folder/device detail changed. Cue to redraw.
    changed = Signal()
    #: The API became reachable / stopped being reachable.
    connected = Signal()
    disconnected = Signal(str)

    #: Notification-worthy things that just happened.
    folder_sync_finished = Signal(str)  # folder display name
    device_connected = Signal(str)  # device display name
    device_disconnected = Signal(str)  # device display name
    conflict_detected = Signal(str, str)  # folder display name, item path
    folder_out_of_sync = Signal(str, str)  # folder display name, first error

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._snapshot = Snapshot()
        self._status = SyncStatus.STOPPED
        self._is_connected = False
        self._process_running = False
        self._worker: _SnapshotWorker | None = None
        #: Set when an event arrives while a snapshot is already in flight.
        self._snapshot_again = False

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SNAPSHOT_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._take_snapshot)

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(SNAPSHOT_INTERVAL_MS)
        self._heartbeat.timeout.connect(self._take_snapshot)

    # -------------------------------------------------------------- properties
    @property
    def status(self) -> SyncStatus:
        return self._status

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def folders(self) -> list[FolderState]:
        return sorted(self._snapshot.folders.values(), key=lambda f: f.display_name.lower())

    def devices(self) -> list[DeviceState]:
        return sorted(self._snapshot.devices.values(), key=lambda d: d.display_name.lower())

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        """Begin refreshing. Safe to call more than once."""
        self._heartbeat.start()
        self._take_snapshot()

    def stop(self) -> None:
        """Stop refreshing and wait for any in-flight snapshot."""
        self._heartbeat.stop()
        self._debounce.stop()
        if self._worker is not None:
            self._worker.wait(5000)
            self._worker = None

    def set_process_running(self, running: bool) -> None:
        """Tell the model whether the Syncthing process is meant to be up.

        Without this the model cannot distinguish "Syncthing is off" from
        "Syncthing is on but not answering yet", which are different icons.
        """
        if running == self._process_running:
            return
        self._process_running = running
        if running:
            self.start()
        else:
            self._is_connected = False
            self._snapshot = Snapshot()
            self.changed.emit()
        self._recompute_status()

    def refresh(self) -> None:
        """Request a snapshot soon, coalescing repeated calls."""
        self._debounce.start()

    # ------------------------------------------------------------------ events
    def handle_event(self, event: dict[str, Any]) -> None:
        """Fold one Syncthing event into the model.

        Unknown event types are ignored on purpose: Syncthing adds new ones
        between releases, and none of them should be able to break the UI.
        """
        event_type = event.get("type", "")
        data = event.get("data") or {}

        handler = {
            "StateChanged": self._on_state_changed,
            "FolderSummary": self._on_folder_summary,
            "FolderCompletion": self._on_folder_completion,
            "FolderErrors": self._on_folder_errors,
            "DeviceConnected": self._on_device_connected,
            "DeviceDisconnected": self._on_device_disconnected,
            "ItemFinished": self._on_item_finished,
            "ConfigSaved": self._on_config_saved,
        }.get(event_type)

        if handler is None:
            return
        try:
            handler(data)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # An event whose payload is not the shape we expect must never be
            # able to take the UI down with it.
            log.warning("Malformed %s event ignored: %s", event_type, exc)

    def _on_state_changed(self, data: dict[str, Any]) -> None:
        folder = self._folder(data.get("folder", ""))
        if folder is None:
            return
        previous, folder.state = folder.state, str(data.get("to", "unknown"))

        # "was syncing, now idle" is the only reliable "folder finished" signal.
        if previous in BUSY_FOLDER_STATES and folder.state == "idle":
            self.folder_sync_finished.emit(folder.display_name)

        self._recompute_status()
        self.changed.emit()

    def _on_folder_summary(self, data: dict[str, Any]) -> None:
        folder = self._folder(data.get("folder", ""))
        summary = data.get("summary") or {}
        if folder is None or not summary:
            return
        _apply_folder_summary(folder, summary)
        self._recompute_status()
        self.changed.emit()

    def _on_folder_completion(self, data: dict[str, Any]) -> None:
        folder = self._folder(data.get("folder", ""))
        if folder is None:
            return
        # This event reports how complete a *remote* device is. Our own
        # progress comes from FolderSummary, so only refresh the display.
        self.changed.emit()

    def _on_folder_errors(self, data: dict[str, Any]) -> None:
        folder = self._folder(data.get("folder", ""))
        if folder is None:
            return

        raw = data.get("errors")
        errors = (
            [entry for entry in raw if isinstance(entry, dict)]
            if isinstance(raw, list)
            else []
        )
        had_errors = folder.error_count > 0
        folder.error_count = len(errors)

        if errors and not had_errors:
            first = errors[0]
            detail = f"{first.get('path', '?')}: {first.get('error', 'unknown error')}"
            self.folder_out_of_sync.emit(folder.display_name, detail)

        self._recompute_status()
        self.changed.emit()

    def _on_device_connected(self, data: dict[str, Any]) -> None:
        device_id = str(data.get("id", ""))
        device = self._snapshot.devices.get(device_id)
        if device is None:
            device = DeviceState(id=device_id, name=str(data.get("deviceName", "")))
            self._snapshot.devices[device_id] = device
        device.connected = True
        device.address = str(data.get("addr", ""))
        if data.get("deviceName"):
            device.name = str(data["deviceName"])

        self.device_connected.emit(device.display_name)
        self.changed.emit()

    def _on_device_disconnected(self, data: dict[str, Any]) -> None:
        device_id = str(data.get("id", ""))
        device = self._snapshot.devices.get(device_id)
        if device is None:
            return
        device.connected = False
        device.address = ""
        self.device_disconnected.emit(device.display_name)
        self.changed.emit()

    def _on_item_finished(self, data: dict[str, Any]) -> None:
        item = str(data.get("item", ""))
        if CONFLICT_MARKER not in item:
            return
        folder = self._folder(str(data.get("folder", "")))
        name = folder.display_name if folder else str(data.get("folder", ""))
        self.conflict_detected.emit(name, item)

    def _on_config_saved(self, _data: dict[str, Any]) -> None:
        # Folders or devices may have been added or removed; only a full
        # snapshot can tell us what the new shape is.
        self.refresh()

    # --------------------------------------------------------------- snapshots
    def _take_snapshot(self) -> None:
        if not self._process_running:
            return
        if self._worker is not None and self._worker.isRunning():
            # Don't pile up concurrent readings; remember to redo it after.
            self._snapshot_again = True
            return

        worker = _SnapshotWorker(self._config.gui_url(), self._config.api_key, self)
        worker.ready.connect(self._on_snapshot_ready)
        worker.failed.connect(self._on_snapshot_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_worker_finished(self) -> None:
        self._worker = None
        if self._snapshot_again:
            self._snapshot_again = False
            self.refresh()

    def _on_snapshot_ready(self, snapshot: object) -> None:
        assert isinstance(snapshot, Snapshot)
        self._snapshot = snapshot
        if not self._is_connected:
            self._is_connected = True
            self.connected.emit()
        self._recompute_status()
        self.changed.emit()

    def _on_snapshot_failed(self, reason: str) -> None:
        log.debug("Snapshot failed: %s", reason)
        if self._is_connected:
            self._is_connected = False
            self.disconnected.emit(reason)
            self.changed.emit()
        self._recompute_status()
        # While Syncthing is still coming up, retry faster than the heartbeat.
        if self._process_running:
            QTimer.singleShot(RECONNECT_INTERVAL_MS, self._take_snapshot)

    # ----------------------------------------------------------------- helpers
    def _folder(self, folder_id: str) -> FolderState | None:
        if not folder_id:
            return None
        folder = self._snapshot.folders.get(folder_id)
        if folder is None:
            # An event about a folder we have not snapshotted yet: create a
            # placeholder now and let the next snapshot fill in the details.
            folder = FolderState(id=folder_id)
            self._snapshot.folders[folder_id] = folder
            self.refresh()
        return folder

    def _recompute_status(self) -> None:
        status = self._derive_status()
        if status is self._status:
            return
        self._status = status
        self.status_changed.emit(status)

    def _derive_status(self) -> SyncStatus:
        return derive_status(
            process_running=self._process_running,
            is_connected=self._is_connected,
            folders=self._snapshot.folders.values(),
            system_errors=self._snapshot.system_errors,
        )

    def tooltip(self) -> str:
        """A short multi-line summary for the tray icon's tooltip."""
        from .. import APP_NAME

        lines = [APP_NAME]
        if self._status is SyncStatus.STOPPED:
            lines.append("Sync engine is not running")
            return "\n".join(lines)
        if self._status is SyncStatus.CONNECTING:
            lines.append("Connecting to the sync engine…")
            return "\n".join(lines)

        folders = [f for f in self._snapshot.folders.values() if not f.paused]
        busy = [f for f in folders if f.is_busy or f.completion < 100.0]
        online = sum(1 for d in self._snapshot.devices.values() if d.connected)
        total_devices = len(self._snapshot.devices)

        if busy:
            worst = min(busy, key=lambda f: f.completion)
            lines.append(f"Syncing — {worst.display_name} at {worst.completion:.0f}%")
        elif self._status is SyncStatus.ERROR:
            lines.append("The sync engine reported an error")
        elif self._status is SyncStatus.WARNING:
            failing = [f for f in folders if f.error_count]
            lines.append(f"{len(failing)} folder(s) out of sync")
        else:
            lines.append("Up to date")

        lines.append(f"{len(folders)} folder(s), {online}/{total_devices} device(s) connected")
        if self._snapshot.version:
            lines.append(f"Engine {self._snapshot.version}")
        return "\n".join(lines)


def derive_status(
    *,
    process_running: bool,
    is_connected: bool,
    folders: Iterable[FolderState],
    system_errors: Sequence[str],
) -> SyncStatus:
    """Reduce the whole picture down to the one status the tray icon shows.

    Ordered by how much the user needs to know: an error outranks a warning,
    which outranks progress. Paused folders are ignored entirely — a folder the
    user deliberately stopped is not a problem to report.
    """
    if not process_running:
        return SyncStatus.STOPPED
    if not is_connected:
        return SyncStatus.CONNECTING

    active = [f for f in folders if not f.paused]

    if system_errors or any(f.state == "error" for f in active):
        return SyncStatus.ERROR
    if any(f.error_count for f in active):
        return SyncStatus.WARNING
    if any(f.is_busy or f.completion < 100.0 for f in active):
        return SyncStatus.SYNCING
    if any(f.is_scanning for f in active):
        return SyncStatus.SCANNING
    return SyncStatus.IDLE


def _apply_folder_summary(folder: FolderState, summary: dict[str, Any]) -> None:
    """Copy the fields we use out of a ``/rest/db/status`` payload."""
    folder.state = str(summary.get("state", folder.state))
    folder.need_bytes = int(summary.get("needBytes") or 0)
    folder.global_bytes = int(summary.get("globalBytes") or 0)
    folder.error_count = int(summary.get("errors") or 0)

    if folder.global_bytes <= 0:
        folder.completion = 100.0
    else:
        synced = max(0, folder.global_bytes - folder.need_bytes)
        folder.completion = min(100.0, 100.0 * synced / folder.global_bytes)


class _SnapshotWorker(QThread):
    """Reads the whole of Syncthing's state with blocking REST calls."""

    ready = Signal(object)  # Snapshot
    failed = Signal(str)

    def __init__(self, base_url: str, api_key: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api = SyncthingApi(base_url, api_key)

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            snapshot = self._read()
        except SyncthingApiError as exc:
            self.failed.emit(str(exc))
        else:
            self.ready.emit(snapshot)
        finally:
            self._api.close()

    def _read(self) -> Snapshot:
        config = self._api.system_config()
        status = self._api.system_status()
        version = self._api.system_version()
        connections = (self._api.connections() or {}).get("connections") or {}

        snapshot = Snapshot(
            version=str(version.get("version", "")),
            my_id=str(status.get("myID", "")),
            system_errors=[str(e.get("message", e)) for e in self._api.system_errors()],
        )

        for entry in config.get("devices") or []:
            device_id = str(entry.get("deviceID", ""))
            if not device_id or device_id == snapshot.my_id:
                continue  # Skip ourselves: we are not a remote peer.
            connection = connections.get(device_id) or {}
            snapshot.devices[device_id] = DeviceState(
                id=device_id,
                name=str(entry.get("name", "")),
                connected=bool(connection.get("connected")),
                paused=bool(entry.get("paused")),
                address=str(connection.get("address", "")),
            )

        for entry in config.get("folders") or []:
            folder_id = str(entry.get("id", ""))
            if not folder_id:
                continue
            folder = FolderState(
                id=folder_id,
                label=str(entry.get("label", "")),
                path=str(entry.get("path", "")),
                paused=bool(entry.get("paused")),
            )
            if not folder.paused:
                # A folder can disappear between the config read and this call.
                try:
                    _apply_folder_summary(folder, self._api.folder_status(folder_id))
                except SyncthingApiError as exc:
                    log.debug("No status for folder %s: %s", folder_id, exc)
            snapshot.folders[folder_id] = folder

        return snapshot
