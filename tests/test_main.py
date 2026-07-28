"""Tests for start-up plumbing, especially the paths only a frozen build takes.

A windowed executable has no console: ``sys.stdout`` and ``sys.stderr`` are both
None. Anything that writes to them then raises, and it raises before there is a
window to show the error in — so these cases are worth pinning.
"""

from __future__ import annotations

import logging

import pytest

from oxeiosync import __main__ as entry


class _Root:
    """The root logger, plus a view of only the handlers a test installed.

    pytest attaches its own capture handlers to the root logger and re-attaches
    them per test, so asserting on the whole handler list would be asserting on
    pytest's behaviour rather than ours.
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger()
        self._before = list(self.logger.handlers)

    @property
    def added(self) -> list[logging.Handler]:
        return [h for h in self.logger.handlers if h not in self._before]

    @property
    def level(self) -> int:
        return self.logger.level


@pytest.fixture
def clean_root_logger():
    """Give each test the root logger back exactly as it found it."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    tracker = _Root()
    yield tracker
    for handler in tracker.added:
        handler.close()
    root.handlers = handlers
    root.setLevel(level)


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    target = tmp_path / "logs"
    target.mkdir()
    monkeypatch.setattr(entry.paths, "log_dir", lambda: target)
    return target


def test_logging_writes_to_a_file(clean_root_logger, log_dir):
    entry.setup_logging(verbose=False)
    logging.getLogger("test").info("hello")

    for handler in clean_root_logger.added:
        handler.flush()
    written = (log_dir / "oxeiosync.log").read_text(encoding="utf-8")
    assert "hello" in written


def test_no_console_means_no_stream_handler(clean_root_logger, log_dir, monkeypatch):
    monkeypatch.setattr(entry.sys, "stderr", None)

    entry.setup_logging(verbose=False)

    added = clean_root_logger.added
    assert any(isinstance(h, logging.FileHandler) for h in added), "file logging is required"
    assert not [
        h
        for h in added
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]


def test_unopenable_log_file_without_a_console_does_not_raise(
    clean_root_logger, monkeypatch, tmp_path
):
    """The failure path used to print to a stream that does not exist."""
    monkeypatch.setattr(entry.paths, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(entry.sys, "stderr", None)
    monkeypatch.setattr(entry.sys, "stdout", None)

    def refuse(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(entry.logging.handlers, "RotatingFileHandler", refuse)

    entry.setup_logging(verbose=False)  # must not raise

    assert clean_root_logger.added == []


def test_verbose_sets_debug_level(clean_root_logger, log_dir):
    entry.setup_logging(verbose=True)
    assert clean_root_logger.level == logging.DEBUG


# ------------------------------------------------------------------- ipc name
def test_ipc_name_is_scoped_to_the_data_folder(monkeypatch, tmp_path):
    """Two installs with separate data folders must not collide."""
    monkeypatch.setattr(entry.paths, "data_dir", lambda: tmp_path / "one")
    first = entry.ipc_name()
    monkeypatch.setattr(entry.paths, "data_dir", lambda: tmp_path / "two")
    second = entry.ipc_name()

    assert first != second
    assert first.startswith("oxeiosync-")


def test_ipc_name_is_stable_for_one_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(entry.paths, "data_dir", lambda: tmp_path / "same")
    assert entry.ipc_name() == entry.ipc_name()


def test_ipc_name_is_a_legal_pipe_name(monkeypatch, tmp_path):
    """Windows named pipes reject path separators and most punctuation."""
    monkeypatch.setattr(entry.paths, "data_dir", lambda: tmp_path / "a b" / "c-d")
    name = entry.ipc_name()

    assert all(ch.isalnum() or ch == "-" for ch in name), name
    assert len(name) < 200


# ---------------------------------------------------------------------- flags
def test_minimized_flag():
    assert entry.parse_args(["--minimized"]).minimized
    assert entry.parse_args(["--minimised"]).minimized
    assert not entry.parse_args([]).minimized


def test_verbose_flag():
    assert entry.parse_args(["--verbose"]).verbose
    assert not entry.parse_args([]).verbose


def test_quit_flag():
    assert entry.parse_args(["--quit"]).quit
    assert not entry.parse_args([]).quit


def test_quit_returns_success_when_nothing_is_running(monkeypatch, tmp_path):
    """Asking a stopped application to stop is not an error."""
    monkeypatch.setattr(entry, "_send_to_running_instance", lambda command: False)
    lock = entry.InstanceLock(tmp_path / "app.lock")

    assert entry._quit_running_instance(lock) == 0


def test_quit_waits_for_the_lock_to_be_released(monkeypatch, tmp_path):
    """The point of --quit is that it returns only once the files are free."""
    sent = []
    monkeypatch.setattr(
        entry, "_send_to_running_instance", lambda command: sent.append(command) or True
    )

    holder = entry.InstanceLock(tmp_path / "app.lock")
    assert holder.acquire()

    attempts = {"n": 0}
    original_sleep = entry.time.sleep

    def release_after_a_moment(seconds):
        attempts["n"] += 1
        if attempts["n"] == 3:
            holder.release()
        original_sleep(0)

    monkeypatch.setattr(entry.time, "sleep", release_after_a_moment)

    waiter = entry.InstanceLock(tmp_path / "app.lock")
    assert entry._quit_running_instance(waiter) == 0
    assert sent == [entry.IPC_QUIT]
    assert attempts["n"] >= 3, "it should have waited rather than returning at once"


def test_quit_reports_failure_if_the_instance_will_not_go(monkeypatch, tmp_path):
    monkeypatch.setattr(entry, "_send_to_running_instance", lambda command: True)
    monkeypatch.setattr(entry, "QUIT_TIMEOUT", 0.05)

    holder = entry.InstanceLock(tmp_path / "app.lock")
    assert holder.acquire()
    try:
        waiter = entry.InstanceLock(tmp_path / "app.lock")
        assert entry._quit_running_instance(waiter) == 1
    finally:
        holder.release()


def test_autostart_flag_matches_what_the_registry_entry_writes():
    """The login entry passes this flag; if they drift, autostart breaks."""
    from oxeiosync import autostart

    assert entry.parse_args([autostart.MINIMIZED_FLAG]).minimized
