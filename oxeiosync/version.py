"""The application's version — counted from git, not written down.

A version typed into a file is a version somebody has to remember to change,
and nobody did: 0.1.0 stayed 0.1.0 through twenty-three commits and every build
made from them. So the number is derived instead. The last release tag gives
major, minor and patch; each commit since adds one to the patch:

    v0.1.0 tagged, 23 commits later  ->  0.1.23
    v0.2.0 tagged on this commit     ->  0.2.0

Every commit is a higher number than the one before it, with nothing to bump.
Starting a new line is tagging it.

A frozen build has no git to ask, so the build writes the number it resolved
into the bundle (:data:`STAMP_NAME`) and the bundle reads that back. The same
number goes into the Windows version resource and the macOS Info.plist, which
is where the installer and the OS read it from.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

#: What a copy with neither git history nor a build stamp reports. Deliberately
#: lower than any real release, so it can never pass for one.
UNKNOWN = "0.0.0"

#: The file a build writes beside the bundled payload, holding the version.
STAMP_NAME = "oxeiosync-version.txt"

#: Release tags look like v1.2.3; anything else (a scratch tag) is not counted.
_TAG_GLOB = "v[0-9]*"
_DESCRIBE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-(\d+)-g[0-9a-f]+$")

ROOT = Path(__file__).resolve().parent.parent


def parse_describe(output: str) -> str | None:
    """``v0.1.0-23-g4f1b382`` → ``0.1.23``; anything unrecognised → None."""
    match = _DESCRIBE.match(output.strip())
    if match is None:
        return None
    major, minor, patch, since = (int(group) for group in match.groups())
    return f"{major}.{minor}.{patch + since}"


def from_git(root: Path = ROOT) -> str | None:
    """The version of the checkout at *root*, or None if it cannot be counted.

    Only asks when *root* is itself a checkout. Without that check, a copy
    installed somewhere inside an unrelated repository would take that
    repository's tags as its own.
    """
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--long", "--match", _TAG_GLOB],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            # Run from a windowed launch (pythonw, or login autostart), git
            # would otherwise flash a console window on the way.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_describe(result.stdout)


def from_stamp(directory: Path) -> str | None:
    """The version a build wrote into *directory*, if it wrote one."""
    try:
        text = (directory / STAMP_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def resolve() -> str:
    """This copy's version: the build stamp when frozen, git otherwise."""
    if getattr(sys, "frozen", False):
        payload = getattr(sys, "_MEIPASS", None)
        stamped = from_stamp(Path(payload)) if payload else None
        return stamped or UNKNOWN
    return from_git() or UNKNOWN


def as_tuple(version: str) -> tuple[int, int, int, int]:
    """The four numbers a Windows version resource wants."""
    parts = [int(part) if part.isdigit() else 0 for part in version.split(".")]
    return tuple((parts + [0, 0, 0, 0])[:4])  # type: ignore[return-value]
