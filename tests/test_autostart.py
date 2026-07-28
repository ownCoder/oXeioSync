"""Tests for the login command oXeioSync writes into the registry.

The registry's Run key offers no control over the working directory, so the
command has to be self-sufficient. These tests pin that property, because a
broken command fails silently at login where nobody is watching.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxeiosync import autostart


@pytest.fixture
def source_checkout(monkeypatch, tmp_path):
    """Pretend we are running from a source tree, with nothing pip-installed."""
    monkeypatch.setattr(autostart.paths, "is_frozen", lambda: False)
    monkeypatch.setattr(autostart.paths, "install_dir", lambda: tmp_path / "project")
    monkeypatch.setattr(autostart, "installed_script", lambda: None)
    monkeypatch.setattr(
        autostart, "_windowless_interpreter", lambda: Path("C:/py/pythonw.exe")
    )


def test_frozen_build_runs_the_executable(monkeypatch):
    monkeypatch.setattr(autostart.paths, "is_frozen", lambda: True)
    monkeypatch.setattr(autostart.sys, "executable", r"C:\Apps\oXeioSync.exe")

    command = autostart.launch_command()

    assert command == f'"C:\\Apps\\oXeioSync.exe" {autostart.MINIMIZED_FLAG}'


def test_installed_script_is_preferred(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.paths, "is_frozen", lambda: False)
    script = tmp_path / "oxeiosync.exe"
    monkeypatch.setattr(autostart, "installed_script", lambda: script)

    command = autostart.launch_command()

    assert command == f'"{script}" {autostart.MINIMIZED_FLAG}'
    assert "-m oxeiosync" not in command


def test_source_checkout_does_not_depend_on_the_working_directory(source_checkout):
    """`-m oxeiosync` would fail at login; the path must be made explicit."""
    command = autostart.launch_command()

    assert "-m oxeiosync" not in command
    assert "sys.path.insert" in command
    assert autostart.MINIMIZED_FLAG in command


def test_source_checkout_command_embeds_a_valid_python_expression(source_checkout, tmp_path):
    command = autostart.launch_command()

    # Pull the -c payload back out and check Python can actually parse it,
    # including any backslash escaping of the Windows path.
    start = command.index('-c "') + len('-c "')
    end = command.rindex('"')
    bootstrap = command[start:end]

    compile(bootstrap, "<autostart>", "exec")
    assert "oxeiosync.__main__" in bootstrap


def test_source_checkout_survives_a_path_needing_escapes(monkeypatch, source_checkout):
    monkeypatch.setattr(
        autostart.paths, "install_dir", lambda: Path(r"C:\Users\a b\new\tools")
    )

    command = autostart.launch_command()
    start = command.index('-c "') + len('-c "')
    end = command.rindex('"')

    # A raw "\new" would become a newline if the path were not escaped properly.
    compile(command[start:end], "<autostart>", "exec")
    assert "\n" not in command


def test_description_mentions_the_checkout(source_checkout, monkeypatch):
    monkeypatch.setattr(autostart, "is_supported", lambda: True)

    described = autostart.describe()

    assert "source checkout" in described
    assert "pip install" in described


def test_description_is_honest_when_unsupported(monkeypatch):
    monkeypatch.setattr(autostart, "is_supported", lambda: False)

    assert "only implemented on Windows" in autostart.describe()
