"""Tests for turning cumulative byte counters into a rate.

The engine only reports totals, so every number on the dashboard's throughput
chart is derived here. The interesting cases are all the ones where a naive
subtraction would lie.
"""

from __future__ import annotations

import pytest

from oxeiosync.config import Config
from oxeiosync.syncthing.transfer import (
    MAX_SAMPLE_GAP,
    TransferSample,
    TransferSampler,
    _Counters,
    _rate,
    _SampleWorker,
)


# ----------------------------------------------------------------- rate maths
def test_rate_is_delta_over_interval():
    assert _rate(3000, 1000, 2.0) == 1000.0


def test_counter_reset_reads_as_idle_not_negative():
    """A restarted engine zeroes its totals; that is not negative throughput."""
    assert _rate(50, 5000, 1.0) == 0.0


def test_zero_interval_cannot_divide():
    assert _rate(5000, 1000, 0.0) == 0.0


def test_no_change_is_zero():
    assert _rate(1000, 1000, 1.0) == 0.0


# ------------------------------------------------------------------- sampling
def _worker() -> _SampleWorker:
    return _SampleWorker(Config(gui_address="127.0.0.1:1", api_key="k"))


def _connections(in_total: int, out_total: int, devices: dict | None = None) -> dict:
    return {
        "total": {"inBytesTotal": in_total, "outBytesTotal": out_total},
        "connections": devices or {},
    }


def _status(**overrides) -> dict:
    base = {"alloc": 5_000_000, "sys": 20_000_000, "uptime": 42, "goroutines": 80}
    base.update(overrides)
    return base


def test_first_reading_establishes_a_baseline_and_reports_nothing():
    """With one reading there is no interval, so there is no rate to report."""
    worker = _worker()

    assert worker._build_sample(_connections(1000, 500), _status()) is None


def test_second_reading_produces_a_rate(monkeypatch):
    worker = _worker()
    # One reading per _build_sample call, a second apart.
    clock = iter([100.0, 101.0])
    monkeypatch.setattr(
        "oxeiosync.syncthing.transfer.time.monotonic", lambda: next(clock)
    )
    worker._started_at = 100.0

    worker._build_sample(_connections(1000, 500), _status())
    sample = worker._build_sample(_connections(3048, 1524), _status())

    assert isinstance(sample, TransferSample)
    assert sample.down_bps == pytest.approx(2048.0)
    assert sample.up_bps == pytest.approx(1024.0)
    assert sample.in_total == 3048
    assert sample.out_total == 1524


def test_a_long_gap_is_discarded_rather_than_spiking(monkeypatch):
    """Waking from sleep must not draw hours of traffic as one second's worth."""
    worker = _worker()
    clock = iter([100.0, 100.0 + MAX_SAMPLE_GAP + 10])
    monkeypatch.setattr(
        "oxeiosync.syncthing.transfer.time.monotonic", lambda: next(clock)
    )
    worker._started_at = 100.0

    worker._build_sample(_connections(0, 0), _status())
    sample = worker._build_sample(_connections(9_000_000_000, 0), _status())

    assert sample is None


def test_the_gap_still_updates_the_baseline(monkeypatch):
    """After discarding a gap, the next interval must be measured from the new
    reading — not from the stale one before the gap."""
    worker = _worker()
    times = iter([100.0, 200.0, 201.0])
    monkeypatch.setattr(
        "oxeiosync.syncthing.transfer.time.monotonic", lambda: next(times)
    )
    worker._started_at = 100.0

    worker._build_sample(_connections(0, 0), _status())
    assert worker._build_sample(_connections(1_000_000, 0), _status()) is None

    sample = worker._build_sample(_connections(1_001_024, 0), _status())
    assert sample is not None
    assert sample.down_bps == pytest.approx(1024.0)


def test_engine_load_fields_are_carried(monkeypatch):
    worker = _worker()
    # One reading per _build_sample call, a second apart.
    clock = iter([100.0, 101.0])
    monkeypatch.setattr(
        "oxeiosync.syncthing.transfer.time.monotonic", lambda: next(clock)
    )
    worker._started_at = 100.0

    worker._build_sample(_connections(0, 0), _status())
    sample = worker._build_sample(
        _connections(0, 0), _status(alloc=7_000_000, sys=30_000_000, goroutines=91)
    )

    assert sample.heap_bytes == 7_000_000
    assert sample.memory_bytes == 30_000_000
    assert sample.goroutines == 91


def test_per_device_rates(monkeypatch):
    worker = _worker()
    # One reading per _build_sample call, a second apart.
    clock = iter([100.0, 101.0])
    monkeypatch.setattr(
        "oxeiosync.syncthing.transfer.time.monotonic", lambda: next(clock)
    )
    worker._started_at = 100.0

    before = _connections(0, 0, {"DEV1": {"inBytesTotal": 0, "outBytesTotal": 0,
                                          "connected": True, "address": "1.2.3.4:1"}})
    after = _connections(0, 0, {"DEV1": {"inBytesTotal": 4096, "outBytesTotal": 1024,
                                         "connected": True, "address": "1.2.3.4:1"}})

    worker._build_sample(before, _status())
    sample = worker._build_sample(after, _status())

    device = sample.devices["DEV1"]
    assert device.down_bps == pytest.approx(4096.0)
    assert device.up_bps == pytest.approx(1024.0)
    assert device.connected
    assert sample.connected_devices == 1


def test_a_newly_seen_device_reports_zero_not_its_lifetime_total(monkeypatch):
    """A peer that connects mid-window brings a large total with it."""
    worker = _worker()
    # One reading per _build_sample call, a second apart.
    clock = iter([100.0, 101.0])
    monkeypatch.setattr(
        "oxeiosync.syncthing.transfer.time.monotonic", lambda: next(clock)
    )
    worker._started_at = 100.0

    worker._build_sample(_connections(0, 0), _status())
    sample = worker._build_sample(
        _connections(0, 0, {"NEW": {"inBytesTotal": 9_000_000, "outBytesTotal": 0,
                                    "connected": True}}),
        _status(),
    )

    assert sample.devices["NEW"].down_bps == 0.0


def test_missing_fields_do_not_raise(monkeypatch):
    worker = _worker()
    # One reading per _build_sample call, a second apart.
    clock = iter([100.0, 101.0])
    monkeypatch.setattr(
        "oxeiosync.syncthing.transfer.time.monotonic", lambda: next(clock)
    )
    worker._started_at = 100.0

    worker._build_sample({}, {})
    sample = worker._build_sample({}, {})

    assert sample.down_bps == 0.0
    assert sample.heap_bytes == 0


# --------------------------------------------------------------------- history
def test_history_is_bounded_and_ordered(qt_app):
    sampler = TransferSampler(Config())
    for index in range(500):
        sampler._on_sampled(TransferSample(elapsed=float(index), down_bps=index))

    history = sampler.history()
    assert len(history) <= 300
    assert history[-1].down_bps == 499
    assert history[0].elapsed < history[-1].elapsed


def test_peak_rate_spans_the_window(qt_app):
    sampler = TransferSampler(Config())
    for value in (10.0, 900.0, 30.0):
        sampler._on_sampled(TransferSample(down_bps=value))
    sampler._on_sampled(TransferSample(up_bps=1500.0))

    assert sampler.peak_rate() == 1500.0


def test_peak_rate_with_no_history_is_zero(qt_app):
    assert TransferSampler(Config()).peak_rate() == 0.0


def test_clear_drops_the_history(qt_app):
    sampler = TransferSampler(Config())
    sampler._on_sampled(TransferSample(down_bps=1.0))
    sampler.clear()

    assert sampler.history() == []
    assert sampler.latest() is None


def test_reconfigure_invalidates_the_baseline():
    """Counters from a different engine are not comparable with ours."""
    worker = _worker()
    worker._previous = _Counters(at=1.0, in_total=5, out_total=5, per_device={})

    worker.reconfigure("http://127.0.0.1:2", "other-key")

    assert worker._previous is None
