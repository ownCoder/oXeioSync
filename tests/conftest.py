"""Shared fixtures.

A ``QCoreApplication`` has to exist before QObject-derived types are created,
and Qt only allows one per process, so it is built once for the whole session.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication


@pytest.fixture(scope="session", autouse=True)
def qt_app() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app
