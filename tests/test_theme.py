"""Tests for adopting the host's light/dark setting.

The bug these exist for: Windows was set to dark, Qt said dark, and the
application stayed light — because the native Windows style hands back a light
palette regardless, and every colour here is read from the palette. Nothing was
broken in a way anything could see; the setting simply reached nothing.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from oxeiosync.ui import theme

NATIVE = "windowsvista"


class _Signal:
    """Just enough of a Qt signal to record and fire one connection."""

    def __init__(self) -> None:
        self._slots = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self) -> None:
        for slot in self._slots:
            slot()


class _Hints:
    def __init__(self, scheme) -> None:
        self.scheme = scheme
        self.colorSchemeChanged = _Signal()

    def colorScheme(self):
        return self.scheme


class _Style:
    def __init__(self, name: str) -> None:
        self._name = name

    def objectName(self) -> str:
        return self._name


class _App:
    """A stand-in for QApplication.

    A real one cannot be built here — the test session already owns a
    QCoreApplication, and Qt allows exactly one.
    """

    def __init__(self, scheme, style: str = NATIVE) -> None:
        self._hints = _Hints(scheme)
        self._style = _Style(style)
        self.styles_set: list[str] = []

    def styleHints(self):  # noqa: N802 - Qt's spelling
        return self._hints

    def style(self):
        return self._style

    def setStyle(self, name: str) -> None:  # noqa: N802 - Qt's spelling
        self.styles_set.append(name)
        self._style = _Style(name.lower())


@pytest.fixture
def host(monkeypatch):
    """Wire is_dark_mode to what the styles actually do on Windows.

    Fusion follows the host's scheme; the native style is light whatever the
    host says. That asymmetry is the entire subject of these tests, so the fake
    has to reproduce it rather than assume it away.
    """

    def make(scheme, style: str = NATIVE) -> _App:
        app = _App(scheme, style)
        monkeypatch.setattr(
            theme,
            "is_dark_mode",
            lambda widget=None: (
                app.style().objectName() == theme.DARK_CAPABLE_STYLE.lower()
                and app.styleHints().colorScheme() == Qt.ColorScheme.Dark
            ),
        )
        return app

    return make


def test_a_dark_host_gets_a_style_that_can_be_dark(host):
    app = host(Qt.ColorScheme.Dark)

    assert theme.follow_host_colour_scheme(app) is True
    assert app.styles_set == [theme.DARK_CAPABLE_STYLE]


def test_a_light_host_keeps_the_native_style(host):
    """Light already looked right; changing it would be a change nobody asked for."""
    app = host(Qt.ColorScheme.Light)

    assert theme.follow_host_colour_scheme(app) is False
    assert app.styles_set == []


def test_switching_the_host_to_dark_while_running_is_followed(host):
    app = host(Qt.ColorScheme.Light)
    theme.follow_host_colour_scheme(app)

    app.styleHints().scheme = Qt.ColorScheme.Dark
    app.styleHints().colorSchemeChanged.emit()

    assert app.styles_set == [theme.DARK_CAPABLE_STYLE]
    assert theme.is_dark_mode() is True


def test_switching_back_to_light_restores_the_native_style(host):
    app = host(Qt.ColorScheme.Dark)
    theme.follow_host_colour_scheme(app)

    app.styleHints().scheme = Qt.ColorScheme.Light
    app.styleHints().colorSchemeChanged.emit()

    assert app.styles_set == [theme.DARK_CAPABLE_STYLE, NATIVE]
    assert theme.is_dark_mode() is False


def test_a_host_that_cannot_be_asked_is_left_alone(host, monkeypatch):
    """Qt older than 6.5 has no colorScheme(); guessing would be worse."""
    app = host(Qt.ColorScheme.Dark)
    monkeypatch.delattr(type(app.styleHints()), "colorScheme")

    assert theme.follow_host_colour_scheme(app) is False
    assert app.styles_set == []


def test_the_two_palettes_do_not_share_a_surface():
    """A 'dark mode' that is not visibly darker is not one."""
    assert theme.DARK.is_dark and not theme.LIGHT.is_dark
    assert theme.DARK.surface != theme.LIGHT.surface
    assert theme.DARK.ink != theme.LIGHT.ink
    # Status colours are deliberately identical in both — red means red.
    assert theme.DARK.status_critical == theme.LIGHT.status_critical
