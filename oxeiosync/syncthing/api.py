"""A thin, synchronous client for Syncthing's REST API.

Only the endpoints oXeioSync actually needs are wrapped. Calls raise
:class:`SyncthingApiError` (or a subclass) rather than returning error
sentinels, so callers cannot accidentally treat a failure as valid state.

Everything here blocks. Call it from a worker thread, never from the GUI
thread — :mod:`oxeiosync.syncthing.events` and
:mod:`oxeiosync.syncthing.state` do exactly that.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
#: How long the server holds an event request open before returning empty.
#:
#: Short, because it is also how long a poller takes to notice it has been told
#: to stop: a request blocked in a read cannot be broken off from another thread
#: on Windows — shutting the socket down does not wake it. At 55 seconds, quit
#: gave up waiting and the interpreter destroyed QThreads that were still
#: running, which Qt answers by aborting the process. A few seconds costs one
#: cheap loopback request per poller in that time, next to the transfer
#: sampler's one a second.
EVENT_POLL_TIMEOUT = 3.0


class SyncthingApiError(Exception):
    """Any failure while talking to the Syncthing API."""


class SyncthingUnavailableError(SyncthingApiError):
    """Syncthing could not be reached at all — not running, or still starting."""


class SyncthingAuthError(SyncthingApiError):
    """Syncthing rejected our API key."""


class SyncthingApi:
    """Wraps one Syncthing instance's REST API.

    The base URL and API key can be swapped at runtime via :meth:`reconfigure`,
    which is what happens when the user edits them in the settings dialog.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._session = requests.Session()
        # Syncthing's GUI is loopback-only by default; a system proxy would
        # break every call, so opt out explicitly.
        self._session.trust_env = False
        self._apply_headers()

    # ------------------------------------------------------------- plumbing
    def _apply_headers(self) -> None:
        self._session.headers.update(
            {
                "X-API-Key": self._api_key,
                "Accept": "application/json",
            }
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str:
        return self._api_key

    def reconfigure(self, base_url: str, api_key: str) -> None:
        """Re-point this client at a different address or key."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._apply_headers()

    def close(self) -> None:
        self._session.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=timeout if timeout is not None else self._timeout,
            )
        except requests.RequestException as exc:
            raise SyncthingUnavailableError(f"{method} {path}: {exc}") from exc

        if response.status_code in (401, 403):
            # Wording matters: this reaches the user through a tray
            # notification when a rescan fails.
            raise SyncthingAuthError(
                f"{method} {path}: the sync engine rejected the API key "
                f"(HTTP {response.status_code})"
            )
        if not response.ok:
            raise SyncthingApiError(
                f"{method} {path}: HTTP {response.status_code} "
                f"{response.text[:200].strip()}"
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            # Every endpoint called here answers in JSON, so anything else means
            # the engine is not itself yet: while it migrates its database on
            # start it serves a stand-in page on this address for a few seconds.
            # Handing that text back let three worker threads call .get() on a
            # string at once, and the resulting error dialogs took the whole
            # application down.
            raise SyncthingUnavailableError(
                f"{method} {path}: the sync engine is not ready yet"
            ) from exc

    def _get(self, path: str, **params: Any) -> Any:
        # Drop unset optional query parameters rather than sending "None".
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", path, params=clean or None)

    def _post(self, path: str, *, json_body: Any = None, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("POST", path, params=clean or None, json_body=json_body)

    # ---------------------------------------------------------------- system
    def ping(self) -> bool:
        """True if Syncthing answers and accepts our key."""
        try:
            self._get("/rest/system/ping")
        except SyncthingApiError:
            return False
        return True

    def system_status(self) -> dict[str, Any]:
        """Device ID, uptime, discovery state, and similar runtime facts."""
        return self._get("/rest/system/status")

    def system_version(self) -> dict[str, Any]:
        """Syncthing's version, OS and architecture."""
        return self._get("/rest/system/version")

    def system_config(self) -> dict[str, Any]:
        """The full Syncthing configuration, including folders and devices."""
        return self._get("/rest/system/config")

    def connections(self) -> dict[str, Any]:
        """Per-device connection state plus aggregate transfer totals."""
        return self._get("/rest/system/connections")

    def system_errors(self) -> list[dict[str, Any]]:
        """Syncthing's internal error list (the bell icon in its web UI)."""
        payload = self._get("/rest/system/error") or {}
        return payload.get("errors") or []

    def clear_system_errors(self) -> None:
        self._post("/rest/system/error/clear")

    def restart(self) -> None:
        """Ask Syncthing to restart itself."""
        self._post("/rest/system/restart")

    def shutdown(self) -> None:
        """Ask Syncthing to exit cleanly, flushing its database."""
        self._post("/rest/system/shutdown")

    def pause_device(self, device_id: str | None = None) -> None:
        """Pause one device, or every device when ``device_id`` is None."""
        self._post("/rest/system/pause", device=device_id)

    def resume_device(self, device_id: str | None = None) -> None:
        self._post("/rest/system/resume", device=device_id)

    # -------------------------------------------------------------- database
    def folder_status(self, folder_id: str) -> dict[str, Any]:
        """Sync state and byte counts for one folder."""
        return self._get("/rest/db/status", folder=folder_id)

    def completion(
        self, folder_id: str | None = None, device_id: str | None = None
    ) -> dict[str, Any]:
        """Completion percentage for a folder, a device, or the whole setup."""
        return self._get("/rest/db/completion", folder=folder_id, device=device_id)

    def folder_errors(self, folder_id: str) -> list[dict[str, Any]]:
        """Files Syncthing has given up on for a folder."""
        payload = self._get("/rest/folder/errors", folder=folder_id) or {}
        return payload.get("errors") or []

    def rescan(self, folder_id: str | None = None) -> None:
        """Trigger a rescan of one folder, or all of them."""
        self._post("/rest/db/scan", folder=folder_id)

    # ---------------------------------------------------------------- events
    def events(
        self,
        since: int = 0,
        limit: int | None = None,
        timeout: float = EVENT_POLL_TIMEOUT,
    ) -> list[dict[str, Any]]:
        """Long-poll the event stream.

        Blocks for up to ``timeout`` seconds and returns an empty list if
        nothing happened. Pass ``limit=1`` on the first call to learn the
        current event ID without replaying Syncthing's whole backlog.
        """
        return self._events("/rest/events", since, limit, timeout)

    def disk_events(
        self,
        since: int = 0,
        limit: int | None = None,
        timeout: float = EVENT_POLL_TIMEOUT,
    ) -> list[dict[str, Any]]:
        """Long-poll the file-change feed: files added, modified and deleted.

        A separate stream from :meth:`events`, which leaves these out. With
        ``since=0`` and a ``limit`` it returns the newest changes the engine
        still remembers, and ``timeout=0`` returns at once rather than holding.
        """
        return self._events("/rest/events/disk", since, limit, timeout)

    def _events(
        self, path: str, since: int, limit: int | None, timeout: float
    ) -> list[dict[str, Any]]:
        # The read timeout must outlast the server-side hold, or every quiet
        # poll would surface as a connection error.
        payload = self._request(
            "GET",
            path,
            params={
                k: v
                for k, v in {"since": since, "limit": limit, "timeout": int(timeout)}.items()
                if v is not None
            },
            timeout=timeout + DEFAULT_TIMEOUT,
        )
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise SyncthingUnavailableError(f"GET {path}: the sync engine is not ready yet")
        return [event for event in payload if isinstance(event, dict)]


def normalise_gui_address(address: str) -> str:
    """Turn a Syncthing GUI address into a URL we can actually connect to.

    Syncthing accepts bind addresses that are not usable as client targets:
    ``:8384`` and ``0.0.0.0:8384`` mean "every interface". Both are rewritten
    to loopback. Bare ``host:port`` strings gain an ``http://`` scheme.
    """
    address = address.strip()
    if not address:
        return ""

    if "://" in address:
        parts = urlsplit(address)
        scheme = parts.scheme or "http"
        host = parts.hostname or "127.0.0.1"
        port = f":{parts.port}" if parts.port else ""
    else:
        scheme = "http"
        if address.startswith(":"):
            host, port = "", address
        elif address.count(":") == 1:
            host, _, port_part = address.partition(":")
            port = f":{port_part}"
        else:
            host, port = address, ""

    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"

    return f"{scheme}://{host}{port}"
