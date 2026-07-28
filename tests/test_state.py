"""Tests for the aggregated state model: status derivation and event handling."""

from __future__ import annotations

import pytest

from oxeiosync.config import Config
from oxeiosync.syncthing.state import (
    CONFLICT_MARKER,
    DeviceState,
    FolderState,
    SyncStatus,
    SyncthingState,
    _apply_folder_summary,
    derive_status,
)


def _status(folders=(), *, connected=True, running=True, errors=()):
    return derive_status(
        process_running=running,
        is_connected=connected,
        folders=folders,
        system_errors=list(errors),
    )


# --------------------------------------------------------------- status ordering
def test_stopped_process_outranks_everything():
    busy = FolderState(id="a", state="syncing")
    assert _status([busy], running=False) == SyncStatus.STOPPED


def test_running_but_unreachable_is_connecting():
    assert _status([], connected=False) == SyncStatus.CONNECTING


def test_no_folders_is_idle():
    assert _status([]) == SyncStatus.IDLE


def test_folder_in_error_state_wins_over_progress():
    folders = [FolderState(id="a", state="error"), FolderState(id="b", state="syncing")]
    assert _status(folders) == SyncStatus.ERROR


def test_system_error_is_reported_even_when_folders_are_fine():
    assert _status([FolderState(id="a", state="idle")], errors=["disk full"]) == SyncStatus.ERROR


def test_pull_errors_are_a_warning_not_an_error():
    folders = [FolderState(id="a", state="idle", error_count=3)]
    assert _status(folders) == SyncStatus.WARNING


def test_warning_outranks_syncing():
    folders = [
        FolderState(id="a", state="idle", error_count=1),
        FolderState(id="b", state="syncing"),
    ]
    assert _status(folders) == SyncStatus.WARNING


def test_busy_folder_is_syncing():
    assert _status([FolderState(id="a", state="syncing")]) == SyncStatus.SYNCING


def test_incomplete_folder_is_syncing_even_when_idle():
    """Idle-but-behind means peers are offline; the user still isn't up to date."""
    folders = [FolderState(id="a", state="idle", completion=42.0)]
    assert _status(folders) == SyncStatus.SYNCING


def test_scanning_is_reported_when_nothing_is_transferring():
    assert _status([FolderState(id="a", state="scanning")]) == SyncStatus.SCANNING


def test_syncing_outranks_scanning():
    folders = [FolderState(id="a", state="scanning"), FolderState(id="b", state="syncing")]
    assert _status(folders) == SyncStatus.SYNCING


def test_paused_folders_are_ignored_entirely():
    """A folder the user paused on purpose is not a problem to report."""
    folders = [FolderState(id="a", state="error", error_count=5, paused=True)]
    assert _status(folders) == SyncStatus.IDLE


# ------------------------------------------------------------- completion maths
@pytest.mark.parametrize(
    "global_bytes, need_bytes, expected",
    [
        (0, 0, 100.0),  # An empty folder is complete, not 0%.
        (100, 0, 100.0),
        (100, 25, 75.0),
        (100, 100, 0.0),
        (100, 250, 0.0),  # needBytes can exceed globalBytes mid-scan; clamp.
    ],
)
def test_completion_from_summary(global_bytes, need_bytes, expected):
    folder = FolderState(id="a")
    _apply_folder_summary(
        folder,
        {"state": "idle", "globalBytes": global_bytes, "needBytes": need_bytes},
    )
    assert folder.completion == pytest.approx(expected)


def test_summary_missing_fields_does_not_raise():
    folder = FolderState(id="a", state="syncing")
    _apply_folder_summary(folder, {})

    assert folder.state == "syncing"  # Kept, because the summary said nothing.
    assert folder.completion == 100.0


# ---------------------------------------------------------------- display names
def test_folder_label_falls_back_to_id():
    assert FolderState(id="abcd").display_name == "abcd"
    assert FolderState(id="abcd", label="Photos").display_name == "Photos"


def test_device_name_falls_back_to_a_short_id():
    assert DeviceState(id="ABCDEFGHIJK").display_name == "ABCDEFG"
    assert DeviceState(id="ABCDEFGHIJK", name="Laptop").display_name == "Laptop"


# --------------------------------------------------------------- event handling
@pytest.fixture
def model():
    """A state model that believes Syncthing is up and reachable.

    Set directly rather than through ``set_process_running``, which would kick
    off a real snapshot over HTTP.
    """
    state = SyncthingState(Config(api_key="test"))
    state._is_connected = True
    state._process_running = True
    state._recompute_status()
    return state


def _add_folder(model, **kwargs):
    folder = FolderState(**kwargs)
    model._snapshot.folders[folder.id] = folder
    return folder


def test_state_changed_updates_the_folder(model):
    _add_folder(model, id="docs", state="idle")

    model.handle_event(
        {"type": "StateChanged", "data": {"folder": "docs", "from": "idle", "to": "syncing"}}
    )

    assert model.snapshot.folders["docs"].state == "syncing"
    assert model.status == SyncStatus.SYNCING


def test_syncing_to_idle_reports_a_finished_folder(model):
    _add_folder(model, id="docs", label="Documents", state="syncing")
    finished = []
    model.folder_sync_finished.connect(finished.append)

    model.handle_event(
        {"type": "StateChanged", "data": {"folder": "docs", "from": "syncing", "to": "idle"}}
    )

    assert finished == ["Documents"]


def test_idle_to_idle_reports_nothing(model):
    """Only a transition out of a busy state counts as 'finished'."""
    _add_folder(model, id="docs", state="idle")
    finished = []
    model.folder_sync_finished.connect(finished.append)

    model.handle_event(
        {"type": "StateChanged", "data": {"folder": "docs", "from": "idle", "to": "idle"}}
    )

    assert finished == []


def test_scanning_to_idle_is_not_a_finished_sync(model):
    _add_folder(model, id="docs", state="scanning")
    finished = []
    model.folder_sync_finished.connect(finished.append)

    model.handle_event(
        {"type": "StateChanged", "data": {"folder": "docs", "from": "scanning", "to": "idle"}}
    )

    assert finished == []


def test_conflict_is_detected_from_the_filename(model):
    _add_folder(model, id="docs", label="Documents")
    conflicts = []
    model.conflict_detected.connect(lambda folder, item: conflicts.append((folder, item)))

    item = f"notes{CONFLICT_MARKER}20260101-120000.txt"
    model.handle_event(
        {"type": "ItemFinished", "data": {"folder": "docs", "item": item, "action": "update"}}
    )

    assert conflicts == [("Documents", item)]


def test_ordinary_file_is_not_a_conflict(model):
    _add_folder(model, id="docs")
    conflicts = []
    model.conflict_detected.connect(lambda *args: conflicts.append(args))

    model.handle_event(
        {"type": "ItemFinished", "data": {"folder": "docs", "item": "notes.txt"}}
    )

    assert conflicts == []


def test_device_connect_and_disconnect(model):
    model._snapshot.devices["DEV1"] = DeviceState(id="DEV1", name="Laptop")
    connected, disconnected = [], []
    model.device_connected.connect(connected.append)
    model.device_disconnected.connect(disconnected.append)

    model.handle_event(
        {
            "type": "DeviceConnected",
            "data": {"id": "DEV1", "deviceName": "Laptop", "addr": "1.2.3.4:22000"},
        }
    )
    assert connected == ["Laptop"]
    assert model.snapshot.devices["DEV1"].connected
    assert model.snapshot.devices["DEV1"].address == "1.2.3.4:22000"

    model.handle_event({"type": "DeviceDisconnected", "data": {"id": "DEV1"}})
    assert disconnected == ["Laptop"]
    assert not model.snapshot.devices["DEV1"].connected


def test_connection_from_an_unknown_device_is_recorded(model):
    """A device can be added remotely and pair before the next snapshot."""
    model.handle_event(
        {"type": "DeviceConnected", "data": {"id": "NEW", "deviceName": "Phone"}}
    )

    assert model.snapshot.devices["NEW"].name == "Phone"
    assert model.snapshot.devices["NEW"].connected


def test_folder_errors_report_once_per_onset(model):
    _add_folder(model, id="docs", label="Documents")
    reported = []
    model.folder_out_of_sync.connect(lambda folder, detail: reported.append((folder, detail)))

    errors = [{"path": "a.txt", "error": "permission denied"}]
    model.handle_event({"type": "FolderErrors", "data": {"folder": "docs", "errors": errors}})
    # Still failing on the next report: the user has already been told.
    model.handle_event({"type": "FolderErrors", "data": {"folder": "docs", "errors": errors}})

    assert reported == [("Documents", "a.txt: permission denied")]
    assert model.status == SyncStatus.WARNING


def test_clearing_folder_errors_returns_to_idle(model):
    _add_folder(model, id="docs", error_count=2)

    model.handle_event({"type": "FolderErrors", "data": {"folder": "docs", "errors": []}})

    assert model.snapshot.folders["docs"].error_count == 0
    assert model.status == SyncStatus.IDLE


def test_unknown_event_types_are_ignored(model):
    model.handle_event({"type": "SomethingInventedLater", "data": {"whatever": True}})
    model.handle_event({})

    assert model.status == SyncStatus.IDLE


def test_malformed_event_data_does_not_raise(model):
    _add_folder(model, id="docs")

    # 'errors' arriving as a string instead of a list must not escape.
    model.handle_event({"type": "FolderErrors", "data": {"folder": "docs", "errors": "nope"}})
    model.handle_event({"type": "StateChanged", "data": None})

    assert model.status in set(SyncStatus)


def test_tooltip_reflects_the_stopped_state():
    state = SyncthingState(Config())
    assert "not running" in state.tooltip()


def test_tooltip_reports_progress(model):
    _add_folder(model, id="docs", label="Documents", state="syncing", completion=37.4)
    model._recompute_status()

    tooltip = model.tooltip()
    assert "Documents" in tooltip
    assert "37%" in tooltip
