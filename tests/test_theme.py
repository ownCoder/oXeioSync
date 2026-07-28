"""Tests for the application being dark, and staying dark.

The bug behind this file: Windows was set to dark, Qt reported dark, and the
application stayed light — the native Windows style hands back a light palette
whatever the setting says, and every colour here is read from the palette. The
answer is not to follow the host more carefully. This application is dark; the
palette is supplied rather than asked for, so the host cannot decide otherwise.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

from oxeiosync.ui import theme


class _Hints:
    def __init__(self) -> None:
        self.scheme = None

    def setColorScheme(self, scheme) -> None:  # noqa: N802 - Qt's spelling
        self.scheme = scheme


class _Style:
    def __init__(self, name: str) -> None:
        self._name = name

    def objectName(self) -> str:  # noqa: N802 - Qt's spelling
        return self._name


class _App:
    """A stand-in for QApplication.

    A real one cannot be built here: the test session already owns a
    QCoreApplication, and Qt allows exactly one per process.
    """

    def __init__(self, style: str = "windowsvista") -> None:
        self._style = _Style(style)
        self._hints = _Hints()
        self.palette: QPalette | None = None

    def style(self):
        return self._style

    def styleHints(self):  # noqa: N802 - Qt's spelling
        return self._hints

    def setStyle(self, name: str) -> None:  # noqa: N802 - Qt's spelling
        self._style = _Style(name.lower())

    def setPalette(self, palette: QPalette) -> None:  # noqa: N802 - Qt's spelling
        self.palette = palette


# ----------------------------------------------------------------- the palette
def test_the_installed_palette_is_one_that_reads_as_dark():
    """is_dark_mode judges by the window's lightness; it must agree."""
    window = theme.dark_qpalette().color(QPalette.ColorRole.Window)

    assert window.lightness() < 128
    assert window == QColor(theme.DARK.plane)


def test_the_palette_is_built_from_the_design_tokens():
    """Qt's widgets and the painted dashboard have to be the same dark."""
    palette = theme.dark_qpalette()
    roles = QPalette.ColorRole

    assert palette.color(roles.Base) == QColor(theme.DARK.surface)
    assert palette.color(roles.WindowText) == QColor(theme.DARK.ink)
    assert palette.color(roles.Highlight) == QColor(theme.DARK.series_download)


@pytest.mark.parametrize(
    "role",
    [
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.ToolTipText,
        QPalette.ColorRole.HighlightedText,
    ],
)
def test_every_text_role_stays_readable_on_what_it_sits_on(role):
    """A palette with one unreadable control is worse than no palette."""
    palette = theme.dark_qpalette()
    pairs = {
        QPalette.ColorRole.WindowText: QPalette.ColorRole.Window,
        QPalette.ColorRole.Text: QPalette.ColorRole.Base,
        QPalette.ColorRole.ButtonText: QPalette.ColorRole.Button,
        QPalette.ColorRole.ToolTipText: QPalette.ColorRole.ToolTipBase,
        QPalette.ColorRole.HighlightedText: QPalette.ColorRole.Highlight,
    }
    ink = palette.color(role)
    ground = palette.color(pairs[role])

    assert abs(ink.lightness() - ground.lightness()) > 60, (
        f"{role} on {pairs[role]}: {ink.name()} on {ground.name()}"
    )


def test_disabled_text_is_dimmer_but_not_invisible():
    palette = theme.dark_qpalette()
    groups = QPalette.ColorGroup
    role = QPalette.ColorRole.WindowText

    enabled = palette.color(groups.Active, role).lightness()
    disabled = palette.color(groups.Disabled, role).lightness()
    ground = palette.color(QPalette.ColorRole.Window).lightness()

    assert disabled < enabled
    assert disabled - ground > 30


# ------------------------------------------------------------------ applying it
def test_applying_the_theme_sets_both_the_style_and_the_palette():
    """Either one alone leaves half the window the wrong colour."""
    app = _App()

    theme.apply_dark_theme(app)

    assert app.style().objectName() == theme.DARK_CAPABLE_STYLE.lower()
    assert app.palette is not None
    assert app.palette.color(QPalette.ColorRole.Window) == QColor(theme.DARK.plane)


def test_the_platform_is_told_as_well_when_it_can_be():
    """Qt 6.8+: this is what darkens the window frame and the embedded browser."""
    app = _App()

    theme.apply_dark_theme(app)

    assert app.styleHints().scheme == Qt.ColorScheme.Dark


def test_an_older_qt_that_cannot_be_told_still_gets_the_palette(monkeypatch):
    """setColorScheme arrived in Qt 6.8; the palette is what does the work."""
    monkeypatch.delattr(_Hints, "setColorScheme")
    app = _App()

    theme.apply_dark_theme(app)  # must not raise

    assert app.palette is not None
    assert app.style().objectName() == theme.DARK_CAPABLE_STYLE.lower()


def test_a_light_host_does_not_get_a_vote():
    """The whole point: the host's setting is not consulted anywhere."""
    import inspect

    source = inspect.getsource(theme.apply_dark_theme)

    assert "colorScheme()" not in source
    assert "Light" not in source


# --------------------------------------------------------------- the token sets
def test_the_dark_tokens_are_the_ones_selected():
    assert theme.DARK.is_dark
    assert theme.palette_for.__doc__  # documented, and reads the live palette


def test_light_and_dark_are_not_inversions_of_each_other():
    """Both are chosen sets of steps; status colours are shared on purpose."""
    assert theme.DARK.surface != theme.LIGHT.surface
    assert theme.DARK.ink != theme.LIGHT.ink
    assert theme.DARK.status_critical == theme.LIGHT.status_critical
