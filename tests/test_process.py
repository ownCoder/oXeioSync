"""Tests for the process supervisor's decision-making.

No Syncthing is launched here. What matters is the bookkeeping around a launch:
which endpoint a shutdown is aimed at, and when a restart is and is not the right
response to an exit.
"""

from __future__ import annotations

import os
import time

import pytest

from oxeiosync import paths
from oxeiosync.config import Config
from oxeiosync.syncthing import process as process_module
from oxeiosync.syncthing.process import (
    FATAL_LOG_MARKERS,
    RESTART_DELAYS,
    STABLE_UPTIME,
    ProcessState,
    SyncthingProcess,
)


@pytest.fixture
def supervisor():
    return SyncthingProcess(Config(gui_address="127.0.0.1:8384", api_key="original-key"))


def test_shutdown_targets_the_instance_that_is_actually_running(supervisor):
    """Settings can change after launch; the running process has not moved."""
    supervisor._launched_gui_url = "http://127.0.0.1:8384"
    supervisor._launched_api_key = "original-key"

    # The user edits the address and key, but Syncthing has not restarted yet.
    supervisor._config.gui_address = "127.0.0.1:9999"
    supervisor._config.api_key = "brand-new-key"

    api = supervisor._running_api()

    assert api.base_url == "http://127.0.0.1:8384"
    assert api.api_key == "original-key"


def test_shutdown_falls_back_to_config_before_a_first_launch(supervisor):
    api = supervisor._running_api()

    assert api.base_url == "http://127.0.0.1:8384"
    assert api.api_key == "original-key"


def test_gui_address_and_key_reach_syncthing_through_the_environment(supervisor, tmp_path):
    env = supervisor._build_env(tmp_path / "syncthing.exe")

    assert env["STGUIADDRESS"] == "127.0.0.1:8384"
    assert env["STGUIAPIKEY"] == "original-key"
    # We supervise the process, so it must not respawn itself behind our back.
    assert env["STNORESTART"] == "1"


def test_empty_gui_address_does_not_become_an_empty_override(supervisor, tmp_path):
    """An empty STGUIADDRESS would make Syncthing fall back to its config.xml."""
    supervisor._config.gui_address = ""

    assert supervisor._build_env(tmp_path / "syncthing.exe")["STGUIADDRESS"] != ""


def test_a_managed_engine_may_still_upgrade_itself(supervisor, monkeypatch, tmp_path):
    """The copy the app downloads lives where no installer will overwrite it."""
    # The environment is inherited, so a build machine that happens to set this
    # would make the assertion pass for the wrong reason — or fail for one.
    monkeypatch.delenv("STNOUPGRADE", raising=False)

    env = supervisor._build_env(tmp_path / "bin" / "syncthing.exe")

    assert "STNOUPGRADE" not in env


def test_the_shipped_engine_is_stopped_from_upgrading_itself(supervisor, monkeypatch, tmp_path):
    """It sits in the program folder, which the next install replaces wholesale."""
    shipped = tmp_path / "_internal" / "syncthing.exe"
    monkeypatch.setattr(paths, "bundled_syncthing_candidates", lambda: (shipped,))

    assert supervisor._build_env(shipped)["STNOUPGRADE"] == "1"


def test_command_line_uses_only_long_lived_flags(supervisor, tmp_path):
    supervisor._config.syncthing_home = str(tmp_path)
    command = supervisor._build_command(tmp_path / "syncthing.exe")

    assert command[0].endswith("syncthing.exe")
    assert f"--home={tmp_path}" in command
    assert "--no-browser" in command


def test_extra_arguments_are_appended(supervisor, tmp_path):
    supervisor._config.syncthing_extra_args = ["--no-default-folder"]

    assert supervisor._build_command(tmp_path / "syncthing.exe")[-1] == "--no-default-folder"


# ---------------------------------------------------------------- exit handling
def _fake_launch(supervisor, uptime: float = 0.0):
    """Put the supervisor into the state a successful launch would leave it in.

    ``uptime`` is how long ago the launch happened, which is what decides
    whether a later exit resets the restart backoff.
    """
    supervisor._set_state(ProcessState.RUNNING)
    supervisor._stop_requested = False
    supervisor._restart_after_stop = False
    supervisor._started_at = time.monotonic() - uptime


def test_a_requested_stop_is_not_treated_as_a_crash(supervisor):
    _fake_launch(supervisor)
    supervisor._stop_requested = True
    errors = []
    supervisor.error.connect(errors.append)

    supervisor._on_exited(0)

    assert supervisor.state is ProcessState.STOPPED
    assert errors == []
    assert supervisor._restart_attempt == 0


@pytest.mark.parametrize("marker", FATAL_LOG_MARKERS)
def test_a_fatal_log_line_stops_the_restart_loop(supervisor, marker):
    """Retrying a bound port or a locked database just hides the real problem."""
    _fake_launch(supervisor)
    errors = []
    supervisor.error.connect(errors.append)

    supervisor._on_log_line(f"2026-01-01 12:00:00 ERR {marker} (something)")
    supervisor._on_exited(1)

    assert supervisor.state is ProcessState.STOPPED
    assert len(errors) == 1
    assert marker in errors[0]
    assert supervisor._restart_attempt == 0


def test_an_ordinary_crash_schedules_a_retry(supervisor):
    _fake_launch(supervisor)
    errors = []
    supervisor.error.connect(errors.append)

    supervisor._on_exited(1)

    assert supervisor._restart_attempt == 1
    assert errors == []  # A first retry is not worth interrupting the user for.


def test_repeated_quick_crashes_eventually_give_up(supervisor):
    """A process that dies immediately, over and over, is not worth retrying forever."""
    errors = []
    supervisor.error.connect(errors.append)

    for _ in range(len(RESTART_DELAYS) + 2):
        _fake_launch(supervisor)
        supervisor._on_exited(1)
        if errors:
            break

    assert errors, "the supervisor should stop retrying and report the failure"
    assert "keeps exiting" in errors[0]


def test_the_backoff_grows_with_each_quick_crash(supervisor):
    for expected in range(1, len(RESTART_DELAYS) + 1):
        _fake_launch(supervisor)
        supervisor._on_exited(1)
        assert supervisor._restart_attempt == expected


def test_a_crash_after_a_long_uptime_gets_a_fresh_budget(supervisor):
    """An instance that ran for hours before dying is not in a restart loop."""
    _fake_launch(supervisor)
    supervisor._on_exited(1)
    assert supervisor._restart_attempt == 1

    _fake_launch(supervisor, uptime=STABLE_UPTIME + 1)
    supervisor._on_exited(1)

    assert supervisor._restart_attempt == 1  # reset, then counted once again


def test_readiness_is_detected_from_syncthing_s_own_log(supervisor):
    supervisor._set_state(ProcessState.STARTING)

    supervisor._on_log_line("2026-01-01 12:00:00 INF GUI and API listening (address=...)")

    assert supervisor.state is ProcessState.RUNNING


def test_mark_running_confirms_readiness_independently(supervisor):
    """A log-wording change between releases must not leave us stuck starting."""
    supervisor._set_state(ProcessState.STARTING)

    supervisor.mark_running()

    assert supervisor.state is ProcessState.RUNNING


def test_mark_running_does_not_resurrect_a_stopped_process(supervisor):
    supervisor.mark_running()

    assert supervisor.state is ProcessState.STOPPED


# ------------------------------------------------------------- forced shutdown
class _FakePopen:
    """Just enough of Popen for the signalling helpers."""

    def __init__(self, pid: int = 4242, exited: bool = False) -> None:
        self.pid = pid
        self._exited = exited
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self._exited else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


@pytest.mark.skipif(os.name != "nt", reason="the taskkill path is Windows-only")
@pytest.mark.parametrize(
    "force, expect_force_flag", [(False, False), (True, True)]
)
def test_forced_shutdown_targets_the_whole_tree(monkeypatch, force, expect_force_flag):
    """Signalling only the launched process would orphan Syncthing's worker."""
    calls = []
    monkeypatch.setattr(
        process_module.subprocess, "run", lambda cmd, **kw: calls.append(cmd)
    )

    popen = _FakePopen(pid=1234)
    process_module._signal_tree(popen, force=force)

    assert len(calls) == 1
    command = calls[0]
    assert command[:3] == ["taskkill", "/PID", "1234"]
    assert "/T" in command, "without /T the child process survives"
    assert ("/F" in command) is expect_force_flag


@pytest.mark.skipif(os.name != "nt", reason="the taskkill path is Windows-only")
def test_signalling_falls_back_if_taskkill_is_unavailable(monkeypatch):
    def explode(*_args, **_kwargs):
        raise FileNotFoundError("taskkill missing")

    monkeypatch.setattr(process_module.subprocess, "run", explode)

    popen = _FakePopen()
    process_module._signal_tree(popen, force=True)

    assert popen.killed, "a missing taskkill must not leave the process running"


def test_an_already_dead_process_is_not_signalled(monkeypatch):
    calls = []
    monkeypatch.setattr(
        process_module.subprocess, "run", lambda cmd, **kw: calls.append(cmd)
    )

    popen = _FakePopen(exited=True)
    process_module._signal_tree(popen, force=True)

    assert calls == []
    assert not popen.killed


def test_log_is_bounded(supervisor):
    supervisor._config.log_lines_kept = 100
    for index in range(5000):
        supervisor._append_log(f"line {index}")

    assert len(supervisor.log_tail()) <= max(100, supervisor._log.maxlen)
    assert supervisor.log_tail()[-1] == "line 4999"
