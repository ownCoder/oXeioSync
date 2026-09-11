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
    #: The events the engine still remembered when the connection came up, in
    #: order. Only for a poller built with ``backlog``, and only on reaching an
    #: engine run it has not followed before — the first connection, an engine
    #: restart, a reconfigure — so a receiver should replace what it holds. A
    #: reconnection to the same run delivers the missed events one by one instead.
    backlog_received = Signal(list)
    #: Emitted once each time the connection comes up.
    connected = Signal()
    #: Emitted once each time the connection goes down, with the reason.
    disconnected = Signal(str)

    def __init__(
        self,
        base_url: str,
        api_key: str,
        parent: Any = None,
        *,
        disk: bool = False,
        backlog: int = 0,
    ) -> None:
        """``disk`` polls the file-change feed instead of the general one.

        ``backlog`` replays up to that many remembered events on connecting, for
        a view that shows history rather than only what happens from now on. The
        default of zero skips them, which is right for notifications: replaying
        would announce syncs that finished hours ago.
        """
        super().__init__(parent)
        self._api = SyncthingApi(base_url, api_key)
        self._disk = disk
        self._backlog = backlog
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
        #: For a backlog poller: which engine run the events so far came from,
        #: by its start time. A reconnection to the same run carries on; a
        #: different run numbers its events from one again, and replaces.
        instance: str | None = None

        while not self._stop.is_set():
            if self._rebaseline.is_set():
                self._rebaseline.clear()
                since = 0
                first_poll = True
                instance = None

            poll = self._api.disk_events if self._disk else self._api.events
            replaying = first_poll and self._backlog > 0
            started = ""
            try:
                if replaying:
                    started = str((self._api.system_status() or {}).get("startTime") or "")
                    # Everything remembered, at once: timeout=0 answers
                    # immediately even when there is nothing to answer with.
                    events = poll(since=0, limit=self._backlog, timeout=0)
                else:
                    events = poll(
                        since=since,
                        # The engine keeps the *newest* `limit` events and drops
                        # the rest, so a cap loses whatever piled up between
                        # polls. The general feed can afford that — a snapshot
                        # repairs it — but a history cannot; its size is bounded
                        # by the engine's own buffer instead.
                        limit=None if self._backlog else (1 if first_poll else MAX_EVENTS_PER_POLL),
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
                if not self._backlog:
                    # A backlog poller keeps its place, to resume from it.
                    since = 0
                self._wait(RECONNECT_DELAY)
                continue

            self._note_connected()

            if replaying:
                first_poll = False
                ids = [int(event.get("id", 0)) for event in events]
                # Resuming needs the engine to still remember the event after
                # the last one delivered. If its buffer has moved past that,
                # changes were lost — deletions among them — and merging would
                # keep what they removed. Start over from what it remembers.
                continuous = not ids or min(ids) <= since + 1
                if started and started == instance and not continuous:
                    log.debug("File-change feed skipped from %s to %s", since, min(ids))
                if started and started == instance and continuous:
                    # The same engine, after a dropped connection: hand over only
                    # what is new since the last event delivered, as ordinary
                    # events, so what the receiver already holds is kept.
                    fresh = [event for event in events if int(event.get("id", 0)) > since]
                    since = max(ids, default=since)
                    for event in fresh:
                        if self._stop.is_set():
                            break
                        self.event_received.emit(event)
                else:
                    # Emitted even when empty: "nothing remembered" is an answer,
                    # and the receiver has to drop what it held from before.
                    instance = started or None
                    since = max(ids, default=0)
                    self.backlog_received.emit(list(events))
                continue

            if not events:
                continue

            if self._backlog and int(events[0].get("id", 0)) > since + 1:
                # The feed's IDs are consecutive; a jump means the engine's
                # buffer moved on before this poll. Nothing can bring those
                # back, so carry on with what there is.
                log.debug(
                    "File-change feed skipped from %s to %s", since, events[0].get("id")
                )

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
