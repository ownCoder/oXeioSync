"""Entry point for the frozen build.

PyInstaller runs its entry script as a top-level module with no package context,
so ``oxeiosync/__main__.py`` cannot be used directly: every ``from . import ...``
in it would raise *attempted relative import with no known parent package* —
before logging exists, and in a windowed build with nowhere to print it.

This launcher imports the package by name instead, which gives the relative
imports the parent they need.
"""

from __future__ import annotations

import sys


def main() -> int:
    from oxeiosync.__main__ import main as run

    return run()


if __name__ == "__main__":
    sys.exit(main())
