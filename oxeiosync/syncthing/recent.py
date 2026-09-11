"""The images that changed in the shared folders most recently.

Built from the engine's file-change feed, which reports every file added,
modified or deleted — here on this machine (``LocalChangeDetected``) or pulled
in from a peer (``RemoteChangeDetected``). Only images are kept, each file once,
newest first: a folder that re-exports the same artwork forty times is one tile
that keeps moving to the front, not forty.

The feed is the engine's memory, not a disk scan: on connecting it replays only
the newest changes the engine still holds (its last thousand or so, of every
kind of file), then follows live. That is also what keeps this cheap: nothing
here walks a folder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import QObject, Signal

#: What counts as an image: the formats Qt reads without extra plugins.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

#: The most files kept. A grid of more than this is not "recent" any more.
MAX_RECENT = 120

#: How many remembered changes to ask for on connecting. Generous, because one
#: busy export can be hundreds of changes to the same few files.
BACKLOG = 1000

_CHANGE_TYPES = {"LocalChangeDetected", "RemoteChangeDetected"}
_FRACTION = re.compile(r"(\.\d{6})\d+")


@dataclass(frozen=True)
class RecentFile:
    """One image, as of its latest change."""

    folder_id: str
    #: Relative to the folder root, as the engine reports it.
    path: str
    action: str
    event_id: int
    time: datetime | None = None
    #: Short ID of the device that made the change.
    modified_by: str = ""
    #: True when the change arrived from another device.
    remote: bool = False

    @property
    def name(self) -> str:
        return re.split(r"[\\/]", self.path)[-1]

    @property
    def key(self) -> tuple[str, str]:
        return (self.folder_id, self.path)


def parse_time(value: str) -> datetime | None:
    """The engine's RFC 3339 timestamp, which may carry nanoseconds.

    ``datetime.fromisoformat`` stops at microseconds on the Pythons this runs
    on, and the engine sends nine digits on macOS and Linux.
    """
    if not value:
        return None
    text = _FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_image(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(suffix) for suffix in IMAGE_SUFFIXES)


def fold(entries: dict[tuple[str, str], RecentFile], events: list[dict[str, Any]]) -> bool:
    """Apply file-change events, in order, to *entries* in place.

    A deletion removes the file; an addition or modification (re)places it with
    the newer event. Directories, non-images and malformed events are ignored.
    Returns whether anything changed — most of a busy feed is not images.
    """
    changed = False
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in _CHANGE_TYPES:
            continue
        data = event.get("data")
        if not isinstance(data, dict) or data.get("type") != "file":
            continue
        folder_id = str(data.get("folderID") or data.get("folder") or "")
        path = str(data.get("path") or "")
        if not folder_id or not path:
            continue

        key = (folder_id, path)
        action = str(data.get("action") or "")
        if action == "deleted":
            changed |= entries.pop(key, None) is not None
            continue
        if not is_image(path):
            continue

        try:
            event_id = int(event.get("id") or 0)
        except (TypeError, ValueError):
            continue
        entries[key] = RecentFile(
            folder_id=folder_id,
            path=path,
            action=action,
            event_id=event_id,
            time=parse_time(str(event.get("time") or "")),
            modified_by=str(data.get("modifiedBy") or ""),
            remote=event.get("type") == "RemoteChangeDetected",
        )
        changed = True
    return changed


def newest(entries: dict[tuple[str, str], RecentFile], limit: int = MAX_RECENT) -> list[RecentFile]:
    return sorted(entries.values(), key=lambda entry: entry.event_id, reverse=True)[:limit]


def format_ago(then: datetime | None, now: datetime | None = None) -> str:
    """How long ago, in the fewest words that still say it."""
    if then is None:
        return ""
    now = now or datetime.now(UTC)
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    days = seconds // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


class RecentFiles(QObject):
    """The recent images, kept current from the file-change feed."""

    #: The list changed. Can fire many times a second during a big sync, so a
    #: view should coalesce rather than redraw on each one.
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: dict[tuple[str, str], RecentFile] = {}

    def items(self) -> list[RecentFile]:
        return newest(self._entries)

    def reset(self, events: list) -> None:
        """Replace everything with what the engine remembers."""
        had_any = bool(self._entries)
        self._entries = {}
        if self._apply(events) or had_any:
            self.changed.emit()

    def add(self, event: dict) -> None:
        if self._apply([event]):
            self.changed.emit()

    def clear(self) -> None:
        if self._entries:
            self._entries = {}
            self.changed.emit()

    def _apply(self, events: list) -> bool:
        changed = fold(self._entries, events)
        if len(self._entries) > MAX_RECENT:
            self._entries = {entry.key: entry for entry in newest(self._entries)}
        return changed
