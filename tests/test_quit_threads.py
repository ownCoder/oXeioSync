"""Tests for what quitting knows about background threads still running.

Destroying a QThread that is still running makes Qt abort the process, and quit
can only avoid that for threads it knows about. So what matters here is that a
running thread is found wherever it hangs in the object tree, and that the slow
workers stop promptly when told to.
"""

from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QObject, QThread

from oxeiosync import app
from oxeiosync.config import Config
from oxeiosync.syncthing import state as state_module
from oxeiosync.syncthing.api import SyncthingApiError, SyncthingUnavailableError


class _Blocking(QThread):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.release = threading.Event()
        self.running = threading.Event()

    def run(self) -> None:
        self.running.set()
        self.release.wait(10)


def test_a_running_thread_is_found_wherever_it_hangs_in_the_tree():
    root = QObject()
    thread = _Blocking(QObject(root))  # a grandchild, as a dialog's worker is
    thread.start()
    try:
        assert thread.running.wait(5)
        assert app.running_threads(root) == ["_Blocking"]
    finally:
        thread.release.set()
        assert thread.wait(5000)

    assert app.running_threads(root) == []


def test_a_root_that_does_not_exist_is_skipped():
    assert app.running_threads(None, QObject()) == []


# ------------------------------------------------------------------ snapshots
class _FakeApi:
    def __init__(self, folder_status) -> None:
        self._folder_status = folder_status

    def system_config(self):
        return {"folders": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "devices": []}

    def system_status(self):
        return {"myID": "ME"}

    def system_version(self):
        return {"version": "v2"}

    def connections(self):
        return {"connections": {}}

    def system_errors(self):
        return []

    def folder_status(self, folder_id):
        return self._folder_status(folder_id)

    def close(self):
        pass


def _worker(folder_status) -> state_module._SnapshotWorker:
    worker = state_module._SnapshotWorker("http://127.0.0.1:1", "key")
    worker._api = _FakeApi(folder_status)
    return worker


def test_a_folder_that_has_gone_is_skipped_and_the_snapshot_goes_on():
    def status(folder_id):
        if folder_id == "b":
            raise SyncthingApiError("GET /rest/db/status: HTTP 404")
        return {"state": "idle"}

    assert set(_worker(status)._read().folders) == {"a", "b", "c"}


def test_an_engine_that_stops_answering_ends_the_snapshot_at_once():
    """Every folder left would otherwise wait out a timeout of its own."""
    asked = []

    def status(folder_id):
        asked.append(folder_id)
        raise SyncthingUnavailableError("connection refused")

    with pytest.raises(SyncthingUnavailableError):
        _worker(status)._read()
    assert asked == ["a"]


def test_a_stopped_snapshot_ends_at_the_next_folder_and_reports_nothing():
    asked = []
    holder = {}

    def status(folder_id):
        asked.append(folder_id)
        holder["worker"].stop()
        return {"state": "idle"}

    worker = holder["worker"] = _worker(status)
    reported = []
    worker.ready.connect(lambda _snapshot: reported.append("ready"))
    worker.failed.connect(lambda _reason: reported.append("failed"))
    worker.run()  # synchronously, on this thread

    assert asked == ["a"]
    assert reported == []


class _StubWorker:
    def __init__(self, finishes: bool) -> None:
        self.finishes = finishes
        self.told_to_stop = False

    def stop(self) -> None:
        self.told_to_stop = True

    def wait(self, _msecs: int) -> bool:
        return self.finishes


@pytest.mark.parametrize("finishes", [True, False])
def test_stopping_the_model_tells_the_snapshot_and_says_whether_it_ended(finishes):
    model = state_module.SyncthingState(Config())
    worker = _StubWorker(finishes)
    model._worker = worker

    assert model.stop() is finishes
    assert worker.told_to_stop


def test_stopping_a_model_with_no_snapshot_in_flight_is_simply_done():
    assert state_module.SyncthingState(Config()).stop() is True
