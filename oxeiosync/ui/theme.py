"""Colour tokens for the dashboard, in light and dark.

These are the values from the reference data-visualisation palette, used
unchanged: categorical slots 1 and 2 for the two throughput series, a one-hue
blue ramp for progress meters, and the fixed status palette for state. Because
the palette and the chart surfaces are the documented ones, the published
validation applies as-is — the hues were not re-derived here.

Dark mode is a *selected* set of steps for the dark surface, not an automatic
inversion of the light values.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget


@dataclass(frozen=True)
class Palette:
    """Every colour the dashboard draws with, for one mode."""

    is_dark: bool

    # --- surfaces ---------------------------------------------------------
    surface: str  # card / chart surface
    plane: str  # page behind the cards
    raised: str  # hover / pressed state, one step off the surface

    # --- ink --------------------------------------------------------------
    ink: str
    ink_secondary: str
    ink_muted: str

    # --- chart chrome -----------------------------------------------------
    gridline: str
    baseline: str
    border: str

    # --- categorical series (fixed order, never cycled) -------------------
    series_download: str  # slot 1, blue
    series_upload: str  # slot 2, orange

    # --- one-hue ramp for meters -----------------------------------------
    meter_fill: str
    meter_track: str

    # --- status (identical in both modes, by design) ----------------------
    status_good: str = "#0ca30c"
    status_warning: str = "#fab219"
    status_serious: str = "#ec835a"
    status_critical: str = "#d03b3b"

    def qcolor(self, hex_value: str, alpha: float = 1.0) -> QColor:
        colour = QColor(hex_value)
        if alpha < 1.0:
            colour.setAlphaF(alpha)
        return colour


LIGHT = Palette(
    is_dark=False,
    surface="#fcfcfb",
    plane="#f9f9f7",
    raised="#f0efec",
    ink="#0b0b0b",
    ink_secondary="#52514e",
    ink_muted="#898781",
    gridline="#e1e0d9",
    baseline="#c3c2b7",
    border="#e3e2dc",
    series_download="#2a78d6",
    series_upload="#eb6834",
    meter_fill="#2a78d6",
    meter_track="#cde2fb",
)

DARK = Palette(
    is_dark=True,
    surface="#1a1a19",
    plane="#0d0d0d",
    raised="#242423",
    ink="#ffffff",
    ink_secondary="#c3c2b7",
    ink_muted="#898781",
    gridline="#2c2c2a",
    baseline="#383835",
    border="#2c2c2a",
    series_download="#3987e5",
    series_upload="#d95926",
    meter_fill="#3987e5",
    # The darkest step of the same blue ramp. A track any closer to the fill
    # made an empty bar (a paused folder) read as a full one.
    meter_track="#0d366b",
)


def is_dark_mode(widget: QWidget | None = None) -> bool:
    """Whether the host is running a dark colour scheme."""
    palette: QPalette | None = None
    if widget is not None:
        palette = widget.palette()
    else:
        app = QApplication.instance()
        if app is not None:
            palette = app.palette()
    if palette is None:
        return False
    window = palette.color(QPalette.ColorRole.Window)
    return window.lightness() < 128


def palette_for(widget: QWidget | None = None) -> Palette:
    """The token set matching the host's current colour scheme."""
    return DARK if is_dark_mode(widget) else LIGHT
