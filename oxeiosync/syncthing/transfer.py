"""Once-a-second sampling of throughput and engine load, for the dashboard.

The sync engine only reports cumulative byte counters, so a *rate* has to be
derived here: the difference between two readings divided by the time between
them. That makes the sampling interval part of the measurement, which is why this
lives on its own thread with its own cadence rather than riding the much slower
state snapshot.

Two counter hazards are handled explicitly. A restarted engine resets its
totals, which would otherwise read as a large negative rate; and a gap in
sampling (engine down, machine asleep) would otherwise read as one enormous
spike when it comes back. Both reset the baseline instead of producing a number.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QThread, Signal

from ..config import Config
from .api import SyncthingApi, SyncthingApiError

log = logging.getLogger(__name__)

#: Target seconds between samples.
SAMPLE_INTERVAL = 1.0
#: How many samples to retain — the chart's visible history.
HISTORY_SIZE = 300
#: A gap longer than this means the baseline is stale; start again rather than
#: reporting the whole backlog as one second's worth of traffic.
MAX_SAMPLE_GAP = 5.0


@dataclass(frozen=True)
class DeviceRates:
    """Throughput for one peer."""

    device_id: str
    down_bps: float = 0.0
    up_bps: float = 0.0
    in_total: int = 0
    out_total: int = 0
    connected: bool = False
    paused: bool = False
    address: str = ""
    client: str = ""


@dataclass(frozen=True)
class TransferSample:
    """One second's reading of everything the dashboard plots."""

    #: Seconds since the sampler started — the chart's x axis.
    elapsed: float = 0.0
    down_bps: float = 0.0
    up_bps: float = 0.0
    in_total: int = 0
    out_total: int = 0
    #: Heap actually in use. This moves; it is what the memory chart plots.
    heap_bytes: int = 0
    #: Address space reserved from the OS — a steadier, larger number.
    memory_bytes: int = 0
    uptime_seconds: int = 0
    goroutines: int = 0
    devices: dict[str, DeviceRates] = field(default_factory=dict)

    @property
    def connected_devices(self) -> int:
        return sum(1 for d in self.devices.values() if d.connected)


class TransferSampler(QObject):
    """Keeps a rolling second-by-second history of throughput and engine load."""

    #: A new reading arrived; carries a :class:`TransferSample`.
    sampled = Signal(object)
    #: Sampling stopped producing readings, with the reason.
    stalled = Signal(str)

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._history: deque[TransferSample] = deque(maxlen=HISTORY_SIZE)
        self._worker: _SampleWorker | None = None

    # -------------------------------------------------------------- properties
    def history(self) -> list[TransferSample]:
        """Every retained sample, oldest first."""
        return list(self._history)

    def latest(self) -> TransferSample | None:
        return self._history[-1] if self._history else None

    def peak_rate(self) -> float:
        """The largest rate seen in the retained window, for axis scaling."""
        if not self._history:
            return 0.0
        return max(max(s.down_bps, s.up_bps) for s in self._history)

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        worker = _SampleWorker(self._config, self)
        worker.sampled.connect(self._on_sampled)
        worker.stalled.connect(self.stalled)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.stop()
        if not worker.wait(5000):
            log.warning("Transfer sampler did not stop in time")

    def reconfigure(self, base_url: str, api_key: str) -> None:
        if self._worker is not None:
            self._worker.reconfigure(base_url, api_key)

    def clear(self) -> None:
        """Drop the history, e.g. when pointed at a different engine."""
        self._history.clear()

    # ----------------------------------------------------------------- handlers
    def _on_sampled(self, sample: object) -> None:
        if not isinstance(sample, TransferSample):
            return
        self._history.append(sample)
        self.sampled.emit(sample)


class _SampleWorker(QThread):
    """Polls the two cheap endpoints once a second and turns counters into rates."""

    sampled = Signal(object)
    stalled = Signal(str)

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api = SyncthingApi(config.gui_url(), config.api_key, timeout=SAMPLE_INTERVAL * 3)
        self._stop = threading.Event()
        self._started_at = 0.0
        self._previous: _Counters | None = None
        self._was_stalled = False

    def reconfigure(self, base_url: str, api_key: str) -> None:
        self._api.reconfigure(base_url, api_key)
        # A different engine has its own counters; ours are meaningless now.
        self._previous = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        self._started_at = time.monotonic()

        while not self._stop.is_set():
            cycle_started = time.monotonic()
            try:
                connections = self._api.connections() or {}
                status = self._api.system_status() or {}
            except SyncthingApiError as exc:
                self._note_stall(str(exc))
            else:
                sample = self._build_sample(connections, status)
                self._was_stalled = False
                if sample is not None:
                    self.sampled.emit(sample)

            # Sleep for what is left of the second, so the cadence does not
            # drift by however long the HTTP calls took.
            remaining = SAMPLE_INTERVAL - (time.monotonic() - cycle_started)
            self._stop.wait(max(0.05, remaining))

        self._api.close()

    # ----------------------------------------------------------------- internals
    def _note_stall(self, reason: str) -> None:
        # Discard the baseline: whatever comes next is not one second on from it.
        self._previous = None
        if not self._was_stalled:
            self._was_stalled = True
            self.stalled.emit(reason)

    def _build_sample(
        self, connections: dict, status: dict
    ) -> TransferSample | None:
        now = time.monotonic()
        total = connections.get("total") or {}
        current = _Counters(
            at=now,
            in_total=int(total.get("inBytesTotal") or 0),
            out_total=int(total.get("outBytesTotal") or 0),
            per_device={
                device_id: (
                    int((entry or {}).get("inBytesTotal") or 0),
                    int((entry or {}).get("outBytesTotal") or 0),
                )
                for device_id, entry in (connections.get("connections") or {}).items()
            },
        )

        previous, self._previous = self._previous, current
        if previous is None:
            # First reading after a start or a gap: it establishes the baseline
            # and has no rate to report yet.
            return None

        interval = current.at - previous.at
        if interval <= 0 or interval > MAX_SAMPLE_GAP:
            log.debug("Discarding a %.1fs sampling gap", interval)
            return None

        devices: dict[str, DeviceRates] = {}
        for device_id, entry in (connections.get("connections") or {}).items():
            entry = entry or {}
            in_total, out_total = current.per_device.get(device_id, (0, 0))
            prev_in, prev_out = previous.per_device.get(device_id, (in_total, out_total))
            devices[device_id] = DeviceRates(
                device_id=device_id,
                down_bps=_rate(in_total, prev_in, interval),
                up_bps=_rate(out_total, prev_out, interval),
                in_total=in_total,
                out_total=out_total,
                connected=bool(entry.get("connected")),
                paused=bool(entry.get("paused")),
                address=str(entry.get("address") or ""),
                client=str(entry.get("clientVersion") or ""),
            )

        return TransferSample(
            elapsed=now - self._started_at,
            down_bps=_rate(current.in_total, previous.in_total, interval),
            up_bps=_rate(current.out_total, previous.out_total, interval),
            in_total=current.in_total,
            out_total=current.out_total,
            # cpuPercent is reported but always zero on current engine builds,
            # so it is deliberately not surfaced — a chart pinned at 0 tells the
            # user nothing and looks broken.
            heap_bytes=int(status.get("alloc") or 0),
            memory_bytes=int(status.get("sys") or 0),
            uptime_seconds=int(status.get("uptime") or 0),
            goroutines=int(status.get("goroutines") or 0),
            devices=devices,
        )


@dataclass(frozen=True)
class _Counters:
    at: float
    in_total: int
    out_total: int
    per_device: dict[str, tuple[int, int]]


def _rate(current: int, previous: int, interval: float) -> float:
    """Bytes per second between two cumulative readings.

    A negative difference means the engine restarted and its counters went back
    to zero. Reporting that as a rate would put a large negative spike on the
    chart, so it is reported as idle instead.
    """
    delta = current - previous
    if delta < 0 or interval <= 0:
        return 0.0
    return delta / interval
