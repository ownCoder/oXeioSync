"""Mutual exclusion between oXeioSync instances.

Two copies must not run at once: they would fight over the same Syncthing
database and the same GUI port.

A ``QLocalServer`` alone is not enough to guarantee that. On Windows the server
name is a named pipe, and several processes can each create their own instance
of the same pipe name — so two launches can both believe they are the first, and
a client's connection goes to whichever pipe instance it happens to get. There
is also a plain race: two launches close together both find nobody listening.

An advisory file lock has neither problem. The kernel grants it to exactly one
process, and releases it automatically when that process dies — including on a
crash, so there is no stale state to clean up. The ``QLocalServer`` is still
used, but only for what it is good at: telling the winner to show its window.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class InstanceLock:
    """An exclusive, crash-safe lock held for the lifetime of the process."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """Try to become the single instance. False means another one is live."""
        if self._fd is not None:
            return True

        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            # Without a lock file we cannot prove exclusivity. Allowing the
            # launch is the lesser evil: refusing would make the application
            # unstartable because of a permissions problem elsewhere.
            log.warning("Could not open the instance lock %s: %s", self._path, exc)
            return True

        try:
            _lock_exclusive(fd)
        except OSError:
            os.close(fd)
            return False

        self._fd = fd
        return True

    def release(self) -> None:
        """Drop the lock. Safe to call more than once."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        except OSError as exc:
            log.debug("Unlocking %s failed: %s", self._path, exc)
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _lock_exclusive(fd: int) -> None:
    """Take a non-blocking exclusive lock, raising OSError if already held."""
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
