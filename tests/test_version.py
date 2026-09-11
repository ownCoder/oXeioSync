"""Tests for the version number.

It used to be typed into a file, and stayed 0.1.0 through twenty-three commits.
Now it is counted, so what matters is that the counting only ever goes up, and
that a copy with nothing to count from says so rather than claiming a release.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from oxeiosync import APP_VERSION, version


# --------------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    ("described", "expected"),
    [
        ("v0.1.0-23-g4f1b382", "0.1.23"),
        ("v0.2.0-0-gabcdef0", "0.2.0"),
        ("v1.4.7-3-g0123456", "1.4.10"),
        ("v0.1.0-23-g4f1b382\n", "0.1.23"),
    ],
)
def test_the_patch_is_the_tag_plus_the_commits_since(described, expected):
    assert version.parse_describe(described) == expected


@pytest.mark.parametrize("described", ["", "4f1b382", "nightly-3-g4f1b382", "v0.1-3-g4f1b382"])
def test_something_that_is_not_a_release_tag_is_not_a_version(described):
    assert version.parse_describe(described) is None


def test_the_windows_resource_gets_four_numbers():
    assert version.as_tuple("0.1.23") == (0, 1, 23, 0)
    assert version.as_tuple("2.0") == (2, 0, 0, 0)


def test_the_running_version_has_three_numbers():
    parts = APP_VERSION.split(".")
    assert len(parts) == 3 and all(part.isdigit() for part in parts), APP_VERSION


# ------------------------------------------------------------------ real git
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo, *args: str) -> None:
    subprocess.run(
        [
            "git",
            # A throwaway repository: identity and signing are set here so the
            # test does not depend on, or prompt for, the machine's own config.
            "-c", "user.name=test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
            *args,
        ],
        cwd=repo, check=True, capture_output=True,
    )


def _commit(repo, message: str) -> None:
    _git(repo, "commit", "--allow-empty", "-q", "-m", message)


@needs_git
def test_every_commit_after_a_release_is_a_higher_number(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "first")
    _git(tmp_path, "tag", "v0.1.0")
    assert version.from_git(tmp_path) == "0.1.0"

    seen = []
    for n in range(3):
        _commit(tmp_path, f"change {n}")
        seen.append(version.from_git(tmp_path))

    assert seen == ["0.1.1", "0.1.2", "0.1.3"]


@needs_git
def test_tagging_a_new_line_starts_its_count_again(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "first")
    _git(tmp_path, "tag", "v0.1.0")
    _commit(tmp_path, "second")
    _git(tmp_path, "tag", "v0.2.0")
    _commit(tmp_path, "third")

    assert version.from_git(tmp_path) == "0.2.1"


@needs_git
def test_a_checkout_with_no_release_tag_has_no_version(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "first")
    _git(tmp_path, "tag", "scratch")

    assert version.from_git(tmp_path) is None


def test_a_folder_that_is_not_a_checkout_has_no_version(tmp_path):
    """Not even when an unrelated repository encloses it."""
    assert version.from_git(tmp_path) is None


# ---------------------------------------------------------------- frozen build
def test_a_frozen_build_reads_the_number_it_was_built_with(tmp_path, monkeypatch):
    (tmp_path / version.STAMP_NAME).write_text("0.1.24\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert version.resolve() == "0.1.24"


def test_a_frozen_build_without_a_stamp_does_not_pass_for_a_release(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert version.resolve() == version.UNKNOWN
