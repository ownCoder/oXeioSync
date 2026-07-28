"""Tests for the instance lock.

The lock is what actually stops two copies running against one Syncthing
database, so its failure modes matter more than its happy path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

from oxeiosync.singleinstance import InstanceLock


def test_first_acquire_succeeds(tmp_path):
    lock = InstanceLock(tmp_path / "app.lock")
    try:
        assert lock.acquire()
        assert lock.path.exists()
    finally:
        lock.release()


def test_acquire_is_idempotent(tmp_path):
    lock = InstanceLock(tmp_path / "app.lock")
    try:
        assert lock.acquire()
        assert lock.acquire()
    finally:
        lock.release()


def test_release_is_safe_to_repeat(tmp_path):
    lock = InstanceLock(tmp_path / "app.lock")
    lock.acquire()
    lock.release()
    lock.release()  # must not raise


def test_a_second_lock_object_is_refused(tmp_path):
    """The lock is per-file, not per-object."""
    path = tmp_path / "app.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)
    try:
        assert first.acquire()
        assert not second.acquire()
    finally:
        first.release()
        second.release()


def test_the_lock_is_reusable_once_released(tmp_path):
    path = tmp_path / "app.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)

    assert first.acquire()
    first.release()
    try:
        assert second.acquire(), "releasing must hand the lock on, not poison it"
    finally:
        second.release()


def test_missing_parent_directory_is_created(tmp_path):
    lock = InstanceLock(tmp_path / "nested" / "deeper" / "app.lock")
    try:
        assert lock.acquire()
        assert lock.path.exists()
    finally:
        lock.release()


def test_lock_is_held_against_another_process(tmp_path):
    """A real second launch is a separate process, so test that case directly."""
    path = tmp_path / "app.lock"
    holder = InstanceLock(path)
    assert holder.acquire()

    try:
        result = _try_lock_in_subprocess(path)
        assert result == "refused", result
    finally:
        holder.release()


def test_lock_is_released_when_the_holder_dies(tmp_path):
    """No stale-lock recovery is needed because the kernel does it for us."""
    path = tmp_path / "app.lock"

    # A child takes the lock and exits without releasing it explicitly.
    subprocess.run(
        [sys.executable, "-c", _CHILD_HOLD_AND_DIE.format(root=_root(), path=str(path))],
        check=True,
        capture_output=True,
        timeout=60,
    )

    survivor = InstanceLock(path)
    try:
        assert survivor.acquire(), "a dead holder must not keep the lock"
    finally:
        survivor.release()


def test_unwritable_location_does_not_block_startup(tmp_path, monkeypatch):
    """A lock we cannot create is not a reason to refuse to run at all."""
    lock = InstanceLock(tmp_path / "app.lock")

    def refuse(*_args, **_kwargs):
        raise PermissionError("nope")

    monkeypatch.setattr(os, "open", refuse)

    assert lock.acquire() is True


# ------------------------------------------------------------------- subprocess
def _root() -> str:
    from oxeiosync import paths

    return str(paths.install_dir())


_CHILD_TRY_LOCK = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, r"{root}")
    from pathlib import Path
    from oxeiosync.singleinstance import InstanceLock
    lock = InstanceLock(Path(r"{path}"))
    print("acquired" if lock.acquire() else "refused", end="")
    """
)

_CHILD_HOLD_AND_DIE = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, r"{root}")
    from pathlib import Path
    from oxeiosync.singleinstance import InstanceLock
    lock = InstanceLock(Path(r"{path}"))
    assert lock.acquire()
    # Exit without release(): the OS must drop the lock for us.
    """
)


def _try_lock_in_subprocess(path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_TRY_LOCK.format(root=_root(), path=str(path))],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()
