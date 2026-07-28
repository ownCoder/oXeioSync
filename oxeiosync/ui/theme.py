"""Colour tokens for the dashboard.

These are the values from the reference data-visualisation palette, used
unchanged: categorical slots 1 and 2 for the two throughput series, a one-hue
blue ramp for progress meters, and the fixed status palette for state. Because
the palette and the chart surfaces are the documented ones, the published
validation applies as-is — the hues were not re-derived here.

The application is dark. :func:`apply_dark_theme` installs that at start-up and
nothing changes it afterwards, so :data:`DARK` is the set in force everywhere.

:data:`LIGHT` is kept because the machinery that reads a palette and answers
with a token set is worth keeping honest — ``palette_for`` reports what is
actually installed rather than asserting an answer — and because a light mode,
if it ever comes back, should come back as a *selected* set of steps like this
one rather than as an inversion of the dark values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

#: The style the dark palette is applied through. Windows' native style paints
#: parts of itself from the system theme rather than from the palette it is
#: given — set the application dark under it and the tab bar, the menu and the
#: scrollbars stay light. Fusion draws everything from the palette, which is the
#: only way a supplied one is honoured throughout.
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
    """Whether the palette in force is a dark one.

    Reads the palette rather than a flag of our own, so a widget that has been
    given its own palette gets an answer about itself, and so the answer cannot
    drift from what is actually on screen.
    """
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
    """The token set matching the palette currently in force."""
    return DARK if is_dark_mode(widget) else LIGHT


def dark_qpalette() -> QPalette:
    """Qt's palette, built from the dark tokens above.

    Every colour this application paints itself comes from :data:`DARK`, but the
    widgets it does *not* paint — menus, dialogs, scrollbars, the tab bar — take
    theirs from Qt. Handing Qt the same tokens is what stops the frame around
    the dashboard disagreeing with the dashboard.

    Written out rather than derived: Qt's roles do not map onto the design
    tokens one-to-one, and guessing at the ones that do not (which grey is
    "Button", what a disabled label should be) is how a palette ends up with a
    control nobody can read.
    """
    ink = QColor(DARK.ink)
    ink_secondary = QColor(DARK.ink_secondary)
    ink_muted = QColor(DARK.ink_muted)
    surface = QColor(DARK.surface)
    plane = QColor(DARK.plane)
    raised = QColor(DARK.raised)
    accent = QColor(DARK.series_download)

    palette = QPalette()
    roles = QPalette.ColorRole
    groups = QPalette.ColorGroup

    palette.setColor(roles.Window, plane)
    palette.setColor(roles.WindowText, ink)
    palette.setColor(roles.Base, surface)
    palette.setColor(roles.AlternateBase, raised)
    palette.setColor(roles.Text, ink)
    palette.setColor(roles.PlaceholderText, ink_muted)
    palette.setColor(roles.Button, raised)
    palette.setColor(roles.ButtonText, ink)
    palette.setColor(roles.ToolTipBase, surface)
    palette.setColor(roles.ToolTipText, ink)
    palette.setColor(roles.BrightText, QColor(DARK.status_critical))
    palette.setColor(roles.Link, accent)
    palette.setColor(roles.LinkVisited, accent)
    palette.setColor(roles.Highlight, accent)
    palette.setColor(roles.HighlightedText, QColor("#ffffff"))
    palette.setColor(roles.Light, raised)
    palette.setColor(roles.Midlight, QColor(DARK.border))
    palette.setColor(roles.Mid, QColor(DARK.baseline))
    palette.setColor(roles.Dark, plane)
    palette.setColor(roles.Shadow, QColor("#000000"))

    # Disabled text has to stay legible while still reading as disabled; the
    # muted ink is the step the rest of the interface already uses for that.
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        palette.setColor(groups.Disabled, role, ink_muted)
    palette.setColor(groups.Disabled, roles.Highlight, raised)
    palette.setColor(groups.Disabled, roles.HighlightedText, ink_secondary)

    return palette


def apply_dark_theme(app: QApplication) -> None:
    """Make the application dark, and keep it dark.

    Not "follow the host": this application is dark, full stop. Following would
    mean the dashboard's colours — which are chosen for a dark surface, and
    validated on one — get used on a light one half the time, and it would put
    the appearance of the program in the hands of a setting the person running
    it may have chosen for something else entirely.

    Two steps, both needed. The style, because Windows' native style ignores an
    applied palette in places and would leave light chrome around dark content;
    Fusion honours what it is given. Then the palette itself, because Fusion
    picks light or dark from the *host*, and the host is not the authority here.
    """
    app.setStyle(DARK_CAPABLE_STYLE)
    app.setPalette(dark_qpalette())

    # Qt 6.8 and later can tell the platform too, which is what gets the window
    # frame and the embedded browser's own controls to match. Older Qt simply
    # does not have it, and the palette above is what does the real work.
    setter = getattr(app.styleHints(), "setColorScheme", None)
    if setter is not None:
        setter(Qt.ColorScheme.Dark)

    log.info("Dark theme applied (style: %s)", app.style().objectName())
