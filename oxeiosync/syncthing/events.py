"""Background poller for Syncthing's event stream.

Syncthing exposes changes as a long-polled event feed. This module keeps that
poll on a worker thread and re-emits everything as Qt signals, so the GUI
thread only ever sees already-parsed dictionaries.

The poller owns its own :class:`~oxeiosync.syncthing.api.SyncthingApi`
instance: a 55-second poll would otherwise monopolise the connection pool that
ordinary API calls share.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import QThread, Signal

from .api import EVENT_POLL_TIMEOUT, SyncthingApi, SyncthingApiError, SyncthingAuthError

log = logging.getLogger(__name__)

#: Delay before retrying after a failed poll, in seconds.
RECONNECT_DELAY = 2.0
#: Cap on events returned per poll, so a long backlog cannot flood the GUI.
MAX_EVENTS_PER_POLL = 200


class EventPoller(QThread):
    """Long-polls ``/rest/events`` until stopped.

    Signals are emitted from the worker thread; Qt queues them onto the GUI
    thread automatically because the receivers live there.
    """

    #: One Syncthing event, as returned by the API.
    event_received = Signal(dict)
    #: Emitted once each time the connection comes up.
    connected = Signal()
    #: Emitted once each time the connection goes down, with the reason.
    disconnected = Signal(str)

    def __init__(self, base_url: str, api_key: str, parent: Any = None) -> None:
        super().__init__(parent)
        self._api = SyncthingApi(base_url, api_key)
        self._stop = threading.Event()
        #: Set when the target changed, so the next poll starts from scratch.
        self._rebaseline = threading.Event()
        self._was_connected = False

    def reconfigure(self, base_url: str, api_key: str) -> None:
        """Re-point at a new address or key.

        Safe to call while running: it takes effect once the in-flight poll
        returns. The event ID baseline is reset explicitly, because a different
        Syncthing instance numbers its events from zero and a carried-over
        ``since`` would silently skip everything below it.
        """
        self._api.reconfigure(base_url, api_key)
        self._rebaseline.set()

    def stop(self) -> None:
        """Ask the thread to finish. Does not block; ``wait()`` for that."""
        self._stop.set()

    # ------------------------------------------------------------------ thread
    def run(self) -> None:  # noqa: D102 - QThread entry point
        since = 0
        # A fresh connection starts from the newest event only. Current state
        # is fetched over the REST API instead; replaying the backlog would
        # raise stale notifications for syncs that finished hours ago.
        first_poll = True

        while not self._stop.is_set():
            if self._rebaseline.is_set():
                self._rebaseline.clear()
                since = 0
                first_poll = True

            try:
                events = self._api.events(
                    since=since,
                    limit=1 if first_poll else MAX_EVENTS_PER_POLL,
                    timeout=EVENT_POLL_TIMEOUT,
                )
            except SyncthingAuthError as exc:
                # Retrying with the same rejected key will not help, but the
                # user may fix it in settings, so keep the thread alive.
                self._note_disconnected(str(exc))
                first_poll = True
                self._wait(RECONNECT_DELAY * 5)
                continue
            except SyncthingApiError as exc:
                log.debug("Event poll failed: %s", exc)
                self._note_disconnected(str(exc))
                first_poll = True
                since = 0
                self._wait(RECONNECT_DELAY)
                continue

            self._note_connected()

            if not events:
                continue

            since = max(int(event.get("id", 0)) for event in events)
            if first_poll:
                # That single event was only used to learn the baseline ID.
                first_poll = False
                continue

            for event in events:
                if self._stop.is_set():
                    break
                self.event_received.emit(event)

        self._api.close()
        log.debug("Event poller stopped")

    # ------------------------------------------------------------------ helpers
    def _wait(self, seconds: float) -> None:
        """Sleep, but wake immediately if asked to stop."""
        self._stop.wait(seconds)

    def _note_connected(self) -> None:
        if not self._was_connected:
            self._was_connected = True
            self.connected.emit()

    def _note_disconnected(self, reason: str) -> None:
        if self._was_connected:
            self._was_connected = False
            self.disconnected.emit(reason)
