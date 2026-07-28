"""Guards the rule that the interface never names the upstream project.

Three separate leaks got through by hand — a heading in the offline placeholder,
an authentication error that reaches a tray notification, and two download
failures shown in a dialog. Each was invisible until someone happened to hit
that state, which is exactly what a test is for.

The approach is an approved list rather than a clever heuristic: every string
literal in the package that names the project must appear below, with a reason.
Adding one is then a deliberate act, not an oversight.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "oxeiosync"

#: Strings that may name the upstream project, and why each is allowed.
#: Keep the reasons — they are what makes the next reviewer's decision easy.
APPROVED = {
    # Real filesystem and network names. Renaming these would break things.
    "syncthing": "the engine's directory and executable stem",
    "syncthing.exe": "the engine's filename on Windows",
    "oxeiosync-syncthing-": "temp directory prefix during download",
    "https://api.github.com/repos/syncthing/syncthing/releases": "the release API",
    r"syncthing\s+(v[\d][^\s]*)": "regex matching the engine's --version output",
    # Configuration keys, which are part of the on-disk format.
    "syncthing_path": "config key",
    "syncthing_home": "config key",
    "syncthing_extra_args": "config key",
    # Diagnostics. Log text is for searching, not for reading in the interface.
    "Port %s is in use; Syncthing's GUI will use %s instead": "log message",
    "Installed Syncthing %s at %s": "log message",
    "Syncthing exited unexpectedly (code ": "log message",
    "Syncthing process state: %s -> %s": "log message",
    "Ignoring start(): Syncthing is already %s": "log message",
    "Syncthing ignored the shutdown request; terminating": "log message",
    "Syncthing did not terminate; killing": "log message",
    "Syncthing process could not be killed": "log message",
    "Stopped reading Syncthing output: %s": "log message",
    "SyncthingProcess": "a class name in __all__",
    # The one deliberate mention, in Help -> About, where a reader expects to
    # find out what a program is built on.
    (
        "</p><p style='color:#898781'>Syncing is powered by Syncthing "
        "(MPL-2.0), downloaded separately and not modified.</p>"
    ): "the About dialog's attribution",
}

#: Substrings marking a literal as machinery rather than prose: the injected
#: stylesheet and script must be able to *match* the name in order to remove it.
MACHINERY = ("var BRAND", "color-scheme:")


def _string_constants(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a file, excluding docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _offenders(root: Path | None = None) -> list[tuple[str, int, str]]:
    root = root or PACKAGE
    found = []
    for path in sorted(root.rglob("*.py")):
        for line, text in _string_constants(path):
            if "syncthing" not in text.lower():
                continue
            if text in APPROVED or any(marker in text for marker in MACHINERY):
                continue
            found.append((str(path.relative_to(root.parent)), line, text))
    return found


def test_no_unapproved_mention_of_the_upstream_project():
    offenders = _offenders()
    if offenders:
        listing = "\n".join(
            f"  {path}:{line}\n      {text[:120]!r}" for path, line, text in offenders
        )
        pytest.fail(
            "String literals name the upstream project without being approved.\n"
            "If the string is shown in the interface, reword it. If it is a log "
            "message, a filename or a config key, add it to APPROVED in this file "
            "with a reason.\n\n" + listing
        )


def test_the_approved_list_has_not_gone_stale():
    """An entry that no longer matches anything is a stale exception."""
    every = {text for path in PACKAGE.rglob("*.py") for _line, text in _string_constants(path)}
    unused = [text for text in APPROVED if text not in every]
    assert not unused, f"APPROVED entries that match nothing any more: {unused}"


def test_the_guard_would_catch_a_regression(tmp_path):
    """The check is worth nothing if it cannot fail."""
    package = tmp_path / "pretend_package"
    package.mkdir()
    (package / "leaky.py").write_text(
        'def show():\n    return "Waiting for Syncthing"\n', encoding="utf-8"
    )

    offenders = _offenders(package)

    assert offenders, "the guard failed to notice an obvious leak"
    assert "Waiting for Syncthing" in offenders[0][2]


def test_the_guard_lets_an_approved_string_through(tmp_path):
    package = tmp_path / "pretend_package"
    package.mkdir()
    (package / "logs.py").write_text(
        'import logging\nlogging.info("Installed Syncthing %s at %s", 1, 2)\n',
        encoding="utf-8",
    )

    assert _offenders(package) == []


def test_the_about_dialog_keeps_its_attribution():
    """The one deliberate mention: crediting what the program is built on."""
    about = (PACKAGE / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "powered by Syncthing" in about
    assert "MPL-2.0" in about
