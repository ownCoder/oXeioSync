"""The parts of the embedded configuration view that can be checked in isolation.

The Browse button spans a page script and a native dialog, and neither half is
much use without the other. What is testable without a browser is the pair they
agree on — the request string — and the rule that decides where the dialog
opens, which is the piece with real branching in it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxeiosync.ui.web_view import (
    PICK_FOLDER_REQUEST,
    _folder_browse_script,
    _start_directory,
)


def _script_source() -> str:
    return _folder_browse_script().sourceCode()


def test_button_asks_for_the_request_the_page_answers() -> None:
    # The two halves only meet through this string, so a typo in either would
    # otherwise show up as a button that silently does nothing.
    assert json.dumps(PICK_FOLDER_REQUEST) in _script_source()


def test_button_does_not_submit_the_dialog() -> None:
    # A button without an explicit type submits the form it sits in, which in
    # this dialog means saving the folder instead of browsing for it.
    assert 'button.type = "button"' in _script_source()


def test_start_directory_uses_an_existing_path_as_given(tmp_path: Path) -> None:
    assert _start_directory(str(tmp_path)) == str(tmp_path)


def test_start_directory_climbs_to_the_nearest_existing_parent(tmp_path: Path) -> None:
    # Typing a path that does not exist yet is normal here: the folder is
    # created on save.
    missing = tmp_path / "not-created-yet" / "nor-this"
    assert _start_directory(str(missing)) == str(tmp_path)


def test_start_directory_expands_the_tilde_shortcut() -> None:
    assert _start_directory("~") == str(Path.home())


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_start_directory_falls_back_to_home_when_empty(value: str) -> None:
    assert _start_directory(value) == str(Path.home())


def test_start_directory_survives_a_nonsense_path() -> None:
    assert _start_directory("\0:/not/a/path") == str(Path.home())
