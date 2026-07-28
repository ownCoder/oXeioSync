"""Tests for chart formatting and axis scaling.

These are the parts a reader believes without checking, so they have to be
right: an axis that rounds badly or a unit that says MiB when it means MiB/s
misstates the data.
"""

from __future__ import annotations

import pytest

from oxeiosync.ui.charts import (
    byte_axis_max,
    format_bytes,
    format_duration,
    format_rate,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (int(1.25 * 1024 * 1024), "1.2 MiB"),
        (1024**3, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
    ],
)
def test_format_bytes(value, expected):
    assert format_bytes(value) == expected


def test_large_values_drop_the_decimal():
    """Three significant digits is enough; '512.0 MiB' wastes width."""
    assert format_bytes(512 * 1024 * 1024) == "512 MiB"


def test_format_rate_marks_the_unit_as_per_second():
    assert format_rate(1024 * 1024) == "1.0 MiB/s"


def test_sub_byte_rates_read_as_idle():
    """Rounding a trickle to '0 B/s' is honest; '0.4 B/s' is noise."""
    assert format_rate(0.4) == "0 B/s"


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m 0s"),
        (3600, "1h 0m"),
        (3661, "1h 1m"),
        (90000, "1d 1h"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_negative_duration_does_not_produce_nonsense():
    assert format_duration(-5) == "0s"


# ------------------------------------------------------------------ axis scale
@pytest.mark.parametrize(
    "peak, expected_ceiling, expected_unit",
    [
        (0, 1024, "KiB"),
        (900, 1024, "KiB"),  # Never label an axis in bare bytes.
        (1.2 * 1024**2, 2 * 1024**2, "MiB"),
        (90 * 1024**2, 100 * 1024**2, "MiB"),
        (3 * 1024**2, 4 * 1024**2, "MiB"),
        (1.5 * 1024**3, 2 * 1024**3, "GiB"),
    ],
)
def test_byte_axis_max(peak, expected_ceiling, expected_unit):
    ceiling, _divisor, unit = byte_axis_max(peak)
    assert ceiling == expected_ceiling
    assert unit == expected_unit


def test_axis_ceiling_always_covers_the_data():
    for peak in (1, 999, 1025, 5_000_000, 123_456_789, 9_876_543_210):
        ceiling, _divisor, _unit = byte_axis_max(peak)
        assert ceiling >= peak, f"{peak} would be clipped by a {ceiling} axis"


def test_axis_divisions_land_on_clean_numbers():
    """Four gridlines must not produce labels like 0.33 or 1.67."""
    for peak in (1.2 * 1024**2, 90 * 1024**2, 7 * 1024**3):
        ceiling, divisor, _unit = byte_axis_max(peak)
        for division in range(5):
            value = (ceiling * division / 4) / divisor
            # Clean means a whole number or a single decimal place.
            assert round(value, 1) == pytest.approx(value)


def test_axis_does_not_overshoot_wildly():
    """A ceiling far above the data wastes the plot's whole vertical range."""
    for peak in (1.2 * 1024**2, 90 * 1024**2, 3.5 * 1024**3):
        ceiling, _divisor, _unit = byte_axis_max(peak)
        assert ceiling <= peak * 2.5
