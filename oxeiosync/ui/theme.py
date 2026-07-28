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

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

#: The style to use when the host asks for dark. Windows' native style has no
#: dark palette to give: with the system set to dark it still hands back a
#: #f0f0f0 window, so every dark token below would be unreachable. Fusion
#: follows the host's scheme in both directions.
DARK_CAPABLE_STYLE = "Fusion"


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


def follow_host_colour_scheme(app: QApplication) -> bool:
    """Adopt the host's light/dark setting. Returns True if dark is now active.

    There is no dark-mode switch in this application, by design: the host
    already has one, and a second one that can disagree with it is a worse
    answer than none. What was missing is the part that makes the host's answer
    arrive. Qt reports the scheme faithfully — ``colorScheme()`` says Dark the
    moment Windows is set to dark — but on Windows the native style supplies no
    dark palette to go with it, and everything here reads its colours from the
    palette. The setting was being honoured by nothing.

    So: switch to a style that does honour it, and only in the direction that
    needs it. Light keeps the native look it already had.

    Connected to the host's own change signal as well, because the setting can
    be flipped while the application is running — on a schedule, usually at
    sunset — and every widget here already redraws on a palette change.
    """
    hints = app.styleHints()
    colour_scheme = getattr(hints, "colorScheme", None)
    if colour_scheme is None:  # Qt older than 6.5 cannot be asked.
        return is_dark_mode()

    native_style = app.style().objectName()
    swapped = False

    def apply() -> None:
        # Whether we swapped the style, not whether the palette is dark: once
        # the host has gone light, a Fusion palette is light too, so asking
        # "is it dark now?" would answer no and leave the swap in place for ever.
        nonlocal swapped
        wants_dark = colour_scheme() == Qt.ColorScheme.Dark
        if wants_dark and not is_dark_mode():
            app.setStyle(DARK_CAPABLE_STYLE)
            swapped = True
        elif not wants_dark and swapped:
            app.setStyle(native_style)
            swapped = False

    apply()

    changed = getattr(hints, "colorSchemeChanged", None)
    if changed is not None:
        changed.connect(apply)

    dark = is_dark_mode()
    log.info("Host colour scheme: %s (style: %s)", "dark" if dark else "light",
             app.style().objectName())
    return dark
