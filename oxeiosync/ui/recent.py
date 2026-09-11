"""The Recent tab: images that changed in the shared folders, newest first.

A grid of tiles — the picture, its name, which folder and how long ago — so the
answer to "did that export make it across?" is something you can see. Opening a
tile opens the file; its menu can also show it in the file manager.

Thumbnails are decoded on a small pool of worker threads. An artboard export is
easily several thousand pixels a side, and the feed this draws from can report
hundreds of changes a minute; decoding on the GUI thread would freeze the window
exactly when there is most to look at.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QProcess,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QDesktopServices,
    QFont,
    QFontMetrics,
    QImage,
    QImageReader,
    QPainter,
    QPen,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..syncthing.recent import RecentFile, RecentFiles, format_ago
from ..syncthing.state import SyncStatus, SyncthingState
from .charts import HAIRLINE
from .theme import palette_for

log = logging.getLogger(__name__)

#: One tile, including the gap around it.
TILE = QSize(204, 212)
#: The gap on each side of a tile's card.
TILE_MARGIN = 6
#: Inside the card.
TILE_PADDING = 8
#: The picture's box, above the two lines of text.
THUMB_HEIGHT = 136
RADIUS = 10.0

#: Redraw at most this often while changes pour in.
COALESCE_MS = 400
#: "2 min ago" has to keep up with the clock even when nothing changes.
CLOCK_MS = 30_000
#: Decoded thumbnails kept. Twice the grid, so scrolling back is instant.
CACHE_SIZE = 240
#: Decodes at once. Two keeps a busy export from taking every core.
WORKERS = 2
#: How long a changed file that fails to decode keeps its previous picture
#: before it is tried once more — long enough for an export to finish writing.
UNREADABLE_RETRY_MS = 2000

# Item data roles.
ROLE_ENTRY = Qt.ItemDataRole.UserRole + 1
ROLE_PATH = Qt.ItemDataRole.UserRole + 2
ROLE_META = Qt.ItemDataRole.UserRole + 3
#: Which change of the file the tile is showing — see :func:`thumb_version`.
ROLE_VERSION = Qt.ItemDataRole.UserRole + 4

# Thumbnail states.
LOADING = "loading"
READY = "ready"
MISSING = "missing"
UNREADABLE = "unreadable"


def load_thumbnail(path: str, box: QSize) -> tuple[QImage | None, str]:
    """Decode *path* no larger than *box*. Safe to call off the GUI thread."""
    if not os.path.isfile(path):
        return None, MISSING
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and (size.width() > box.width() or size.height() > box.height()):
        # Formats that can decode at a reduced size do; the rest are scaled after.
        reader.setScaledSize(size.scaled(box, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return None, UNREADABLE
    if image.width() > box.width() or image.height() > box.height():
        image = image.scaled(
            box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    return image, READY


def thumb_version(entry: RecentFile) -> str:
    """Names one change of one file, uniquely across engine restarts.

    The event ID alone is not enough: the engine numbers its feed from one again
    each time it starts, so a file could come back under an ID an earlier run
    already used, and be shown with that run's picture. Events from one scan can
    share a timestamp, so the time alone is not enough either.
    """
    stamp = entry.time.isoformat() if entry.time is not None else ""
    return f"{stamp}|{entry.event_id}"


@dataclass
class _Thumb:
    #: The change the picture shows, or None before any has been decoded.
    version: str | None = None
    status: str = LOADING
    pixmap: QPixmap | None = None
    #: A version that failed to decode once, and when it may be tried again.
    failed_version: str | None = None
    retry_at: float = 0.0


class ThumbnailCache(QObject):
    """Decoded thumbnails by file, filled in the background.

    Built for a feed that changes the same files over and over. One entry per
    file, not per change, so a re-exported file keeps showing its last picture
    until the new one is ready instead of going blank. At most one decode per
    file is waiting at a time — a newer change replaces an older one not yet
    started — and the waiting work is dropped whenever the grid is redrawn or
    scrolled, so only tiles actually on screen ask again. What is waiting is
    therefore never more than a screenful, however fast the files change.

    Everything here runs on the GUI thread except :func:`load_thumbnail`.
    """

    #: A thumbnail has finished loading (or failed to).
    updated = Signal()
    _loaded = Signal(str, object, object, str)  # path, version, image, status

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Created before anything the workers report back to, so it is also
        # destroyed first. Its destructor waits for the decodes in flight —
        # only those, once shutdown() has dropped the rest.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(WORKERS)
        self._entries: OrderedDict[str, _Thumb] = OrderedDict()
        #: Not started yet: path -> (version, box, priority). One per file.
        self._waiting: dict[str, tuple[str, QSize, tuple[int, int]]] = {}
        #: Being decoded: path -> version.
        self._running: dict[str, str] = {}
        self._closed = False
        self._loaded.connect(self._on_loaded)

    def get(self, path: str) -> tuple[str, QPixmap | None] | None:
        entry = self._entries.get(path)
        if entry is None:
            return None
        self._entries.move_to_end(path)
        return entry.status, entry.pixmap

    def request(self, path: str, version: str, box: QSize, row: int = 0) -> None:
        """Ask for *version* of *path*, for the tile in *row*.

        A tile with no picture at all goes ahead of one that only has an older
        picture, then higher rows first. Otherwise, while the top of the grid
        keeps changing, the tiles below it would never get a turn.
        """
        if self._closed or not path:
            return
        entry = self._entries.get(path)
        if entry is None:
            entry = self._entries[path] = _Thumb()
            self._trim()
        if entry.version == version or self._running.get(path) == version:
            return
        if entry.failed_version == version and time.monotonic() < entry.retry_at:
            return
        self._waiting[path] = (version, box, (0 if entry.pixmap is None else 1, row))
        self._pump()

    def retain(self, paths: set[str]) -> None:
        """Forget files that are no longer listed, and drop all waiting work."""
        self._waiting.clear()
        for path in [p for p in self._entries if p not in paths]:
            del self._entries[path]

    def drop_waiting(self) -> None:
        """Drop work not yet started; the next paint asks again for what it shows."""
        self._waiting.clear()

    def busy(self) -> bool:
        return bool(self._waiting or self._running)

    def shutdown(self) -> None:
        """Stop for good: drop all work not yet started. Does not block.

        Without this the pool's destructor would run every queued decode first,
        for a window that is already gone. What is already running (at most
        WORKERS decodes) is left to :meth:`wait`.
        """
        self._closed = True
        self._waiting.clear()
        self._pool.clear()

    def wait(self, msecs: int) -> bool:
        """Wait up to *msecs* for decodes in flight; True if none are left."""
        return self._pool.waitForDone(msecs)

    def _pump(self) -> None:
        while len(self._running) < WORKERS:
            # Never two decodes of one file at once: a newer version waits for
            # the older one to land.
            ready = [path for path in self._waiting if path not in self._running]
            if not ready:
                return
            path = min(ready, key=lambda p: self._waiting[p][2])
            version, box, _priority = self._waiting.pop(path)
            self._running[path] = version
            self._pool.start(self._job(path, version, box))

    def _job(self, path: str, version: str, box: QSize):
        loaded = self._loaded

        def work() -> None:
            image, status = load_thumbnail(path, box)
            loaded.emit(path, version, image, status)

        return work

    def _on_loaded(self, path: str, version: object, image: object, status: str) -> None:
        self._running.pop(path, None)
        entry = self._entries.get(path)
        if entry is not None and not self._closed:
            version = str(version)
            if (
                status == UNREADABLE
                and entry.pixmap is not None
                and entry.failed_version != version
            ):
                # Perhaps caught mid-write: keep the last good picture for now
                # and try this version once more shortly. Only once — a file
                # that still cannot be read must say so, not pass off the old
                # picture as the new export.
                entry.failed_version = version
                entry.retry_at = time.monotonic() + UNREADABLE_RETRY_MS / 1000
                # The context object: a cache destroyed meanwhile cancels it.
                QTimer.singleShot(UNREADABLE_RETRY_MS + 50, self, self.updated.emit)
            else:
                entry.version = version
                entry.status = status
                entry.pixmap = QPixmap.fromImage(image) if isinstance(image, QImage) else None
                entry.failed_version = None
            self.updated.emit()
        if not self._closed:
            self._pump()

    def _trim(self) -> None:
        for path in list(self._entries):
            if len(self._entries) <= CACHE_SIZE:
                return
            if path not in self._running:
                del self._entries[path]
                self._waiting.pop(path, None)


def thumb_box(widget: QWidget | None) -> QSize:
    """The size to decode a thumbnail at, in device pixels."""
    ratio = (widget.devicePixelRatioF() if widget is not None else 1.0) or 1.0
    return QSize(
        int((TILE.width() - 2 * (TILE_MARGIN + TILE_PADDING)) * ratio),
        int(THUMB_HEIGHT * ratio),
    )


class _TileDelegate(QStyledItemDelegate):
    """Draws one tile: a card holding the picture, the name, and where and when."""

    def __init__(self, cache: ThumbnailCache, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache = cache

    def sizeHint(self, option, _index) -> QSize:  # noqa: N802
        view = option.widget
        grid = view.gridSize() if isinstance(view, QListView) else QSize()
        return grid if grid.isValid() else TILE

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        palette = palette_for(option.widget)
        card = QRectF(option.rect).adjusted(
            TILE_MARGIN + 0.5, TILE_MARGIN + 0.5, -TILE_MARGIN - 0.5, -TILE_MARGIN - 0.5
        )
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            ring = palette.series_download if selected else (
                palette.baseline if hovered else palette.border
            )
            painter.setPen(QPen(palette.qcolor(ring), 2.0 if selected else HAIRLINE))
            painter.setBrush(palette.qcolor(palette.raised if hovered else palette.surface))
            painter.drawRoundedRect(card, RADIUS, RADIUS)

            inner = card.adjusted(TILE_PADDING, TILE_PADDING, -TILE_PADDING, -TILE_PADDING)
            box = QRectF(inner.left(), inner.top(), inner.width(), THUMB_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.qcolor(palette.plane))
            painter.drawRoundedRect(box, 6.0, 6.0)

            # Asked for here, while painting, so only tiles actually on screen
            # queue a decode; the newest (top) rows first.
            path = str(index.data(ROLE_PATH) or "")
            self._cache.request(
                path, str(index.data(ROLE_VERSION) or ""), thumb_box(option.widget), index.row()
            )
            cached = self._cache.get(path)
            status, pixmap = cached if cached is not None else (LOADING, None)
            if pixmap is not None and not pixmap.isNull():
                # Decoded at device pixels; laid out in logical ones, and never
                # beyond the box even if the screen's scale changed since.
                ratio = option.widget.devicePixelRatioF() if option.widget else 1.0
                width, height = pixmap.width() / ratio, pixmap.height() / ratio
                fit = min(1.0, box.width() / width, box.height() / height)
                width, height = width * fit, height * fit
                target = QRectF(
                    box.center().x() - width / 2, box.center().y() - height / 2, width, height
                )
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
            elif status in (MISSING, UNREADABLE):
                painter.setPen(palette.qcolor(palette.ink_muted))
                painter.drawText(
                    box,
                    Qt.AlignmentFlag.AlignCenter,
                    "No longer on disk" if status == MISSING else "No preview",
                )

            font = QFont(option.font)
            metrics = QFontMetrics(font)
            name_top = box.bottom() + 8
            name_rect = QRect(
                int(inner.left()), int(name_top), int(inner.width()), metrics.height()
            )
            painter.setFont(font)
            painter.setPen(palette.qcolor(palette.ink))
            painter.drawText(
                name_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(
                    str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
                    Qt.TextElideMode.ElideMiddle,
                    name_rect.width(),
                ),
            )

            small = QFont(font)
            small.setPointSizeF(max(7.5, font.pointSizeF() - 0.5))
            small_metrics = QFontMetrics(small)
            meta_rect = QRect(
                name_rect.left(), name_rect.bottom() + 3, name_rect.width(), small_metrics.height()
            )
            painter.setFont(small)
            painter.setPen(palette.qcolor(palette.ink_muted))
            painter.drawText(
                meta_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                small_metrics.elidedText(
                    str(index.data(ROLE_META) or ""), Qt.TextElideMode.ElideRight, meta_rect.width()
                ),
            )
        finally:
            painter.restore()


class _TileGrid(QListView):
    """An icon-mode list whose tiles share the full width between them.

    Left to itself the view packs fixed-size tiles from the left and leaves the
    remainder as a ragged gap on the right, up to a whole tile wide.
    """

    #: A tile was double-clicked; carries the path of the file that was pressed.
    open_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pressed_path = ""

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Remembered at the press, because the rows are reused: by the second
        # click of a double-click a redraw may have moved another file into the
        # row under the pointer, and opening that one would be a surprise.
        index = self.indexAt(event.position().toPoint())
        self._pressed_path = str(index.data(ROLE_PATH) or "") if index.isValid() else ""
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._pressed_path:
            super().mouseDoubleClickEvent(event)
            return
        path = self._pressed_path
        # The base class still has to see the double-click — it is what stops
        # the release that follows counting as another click — but its own
        # doubleClicked/activated would open the row's file, whichever that is.
        self.blockSignals(True)
        try:
            super().mouseDoubleClickEvent(event)
        finally:
            self.blockSignals(False)
        # Where one click already opens an item, the first click has done it.
        single_click = self.style().styleHint(
            QStyle.StyleHint.SH_ItemView_ActivateItemOnSingleClick, None, self
        )
        if not single_click:
            self.open_requested.emit(path)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Measured as if the scroll bar were always there. Measuring the live
        # viewport would change the column count when the bar appears, which
        # changes the height, which can take the bar away again.
        width = self.contentsRect().width() - self.verticalScrollBar().sizeHint().width()
        # One pixel short of an exact fit: the view wraps a row that fills the
        # width exactly, which drops a whole column.
        usable = max(1, width - 1)
        columns = max(1, usable // TILE.width())
        cell = QSize(usable // columns, TILE.height())
        if cell != self.gridSize():
            self.setGridSize(cell)


class RecentPage(QWidget):
    """The grid of recently changed images, bound to the change feed."""

    _ready = False
    _recolouring = False

    def __init__(
        self, state: SyncthingState, recent: RecentFiles, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._recent = recent
        self._dirty = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)

        header = QHBoxLayout()
        self._title = QLabel("Recently changed images", self)
        title_font = QFont(self._title.font())
        title_font.setWeight(QFont.Weight.DemiBold)
        self._title.setFont(title_font)
        header.addWidget(self._title)
        header.addStretch(1)
        self._summary = QLabel("", self)
        header.addWidget(self._summary)
        layout.addLayout(header)

        self._cache = ThumbnailCache(self)
        self._model = QStandardItemModel(self)

        self._view = _TileGrid(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(_TileDelegate(self._cache, self._view))
        self._view.setViewMode(QListView.ViewMode.IconMode)
        self._view.setMovement(QListView.Movement.Static)
        self._view.setResizeMode(QListView.ResizeMode.Adjust)
        self._view.setUniformItemSizes(True)
        self._view.setSpacing(0)
        self._view.setWrapping(True)
        self._view.setFrameShape(QListView.Shape.NoFrame)
        self._view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._view.setMouseTracking(True)
        self._view.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # Double-clicks come through open_requested; activated is left with the
        # keyboard (Enter), where the current index already follows its file.
        self._view.open_requested.connect(open_file)
        self._view.activated.connect(self._open_index)
        self._view.customContextMenuRequested.connect(self._show_menu)
        # Scrolling changes what is on screen: drop the decodes queued for what
        # was, and let the tiles now painted ask for themselves.
        self._view.verticalScrollBar().valueChanged.connect(self._cache.drop_waiting)
        layout.addWidget(self._view, 1)

        self._empty = QLabel("", self)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty, 1)

        self._coalesce = QTimer(self)
        self._coalesce.setSingleShot(True)
        self._coalesce.setInterval(COALESCE_MS)
        self._coalesce.timeout.connect(self._rebuild)

        self._clock = QTimer(self)
        self._clock.setInterval(CLOCK_MS)
        self._clock.timeout.connect(self._schedule)
        self._clock.start()

        self._cache.updated.connect(self._view.viewport().update)
        recent.changed.connect(self._schedule)
        state.changed.connect(self._schedule)
        state.status_changed.connect(self._schedule)

        self._apply_colors()
        self._rebuild()
        self._ready = True

    # ---------------------------------------------------------------- updating
    def _schedule(self, *_args: object) -> None:
        self._dirty = True
        # Hidden, there is nothing to draw; showEvent catches up. Visible, a
        # timer already running is left alone so a steady stream of changes
        # still redraws every COALESCE_MS instead of never.
        if self.isVisible() and not self._coalesce.isActive():
            self._coalesce.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._dirty:
            self._rebuild()

    def _rebuild(self) -> None:
        self._dirty = False
        snapshot = self._state.snapshot
        # The feed names a device by the first block of its ID.
        devices = {
            device_id[:7]: device.display_name for device_id, device in snapshot.devices.items()
        }

        rows: list[tuple[RecentFile, str, str]] = []
        seen: set[str] = set()
        for entry in self._recent.items():
            folder = snapshot.folders.get(entry.folder_id)
            if folder is None or not folder.path:
                continue  # not in a snapshot yet; the next one brings it
            path = os.path.normpath(
                os.path.join(os.path.expanduser(folder.path), entry.path)
            )
            # A folder shared inside another reports each change twice, once
            # per folder. One file is one tile — the newest report, which comes
            # first — or the two would take turns redecoding the same picture.
            if os.path.normcase(path) in seen:
                continue
            seen.add(os.path.normcase(path))
            meta = [folder.display_name]
            ago = format_ago(entry.time)
            if ago:
                meta.append(ago)
            if entry.remote:
                meta.append(f"from {devices.get(entry.modified_by, entry.modified_by)}")
            rows.append((entry, path, " · ".join(meta)))

        self._fill(rows)

        count = len(rows)
        self._summary.setText(f"{count} image{'s' if count != 1 else ''}" if count else "")
        self._view.setVisible(bool(rows))
        self._empty.setVisible(not rows)
        if not rows:
            self._empty.setText(self._empty_text())

    def _fill(self, rows: list[tuple[RecentFile, str, str]]) -> None:
        """Update rows in place, keeping the selection on the same file.

        Rows are reused, so a selection left where it was would sit on whichever
        file moved into that row. It follows its file instead, or goes.
        """
        selection = self._view.selectionModel()
        chosen = selection.selectedIndexes()
        selected_key = _key_at(chosen[0]) if chosen else None
        current_key = _key_at(selection.currentIndex())

        # None of what follows is the user moving anywhere. With auto-scroll on,
        # every change of the current index — Qt moves it itself when rows are
        # removed from under it — scrolls the view to it, several times a
        # second while a folder is busy.
        self._view.setAutoScroll(False)
        try:
            self._model.setRowCount(len(rows))
            row_of: dict[tuple[str, str], int] = {}
            for row, (entry, path, meta) in enumerate(rows):
                item = self._model.item(row)
                if item is None:
                    item = QStandardItem()
                    item.setEditable(False)
                    self._model.setItem(row, item)
                item.setText(entry.name)
                item.setToolTip(path)
                item.setData(entry, ROLE_ENTRY)
                item.setData(path, ROLE_PATH)
                item.setData(meta, ROLE_META)
                item.setData(thumb_version(entry), ROLE_VERSION)
                row_of[entry.key] = row

            # Selection and current index follow their files. A file that has
            # gone takes both with it: a current index left on its row would
            # have Enter open whichever file moves in there next.
            if selected_key in row_of:
                selection.setCurrentIndex(
                    self._model.index(row_of[selected_key], 0),
                    QItemSelectionModel.SelectionFlag.ClearAndSelect,
                )
            else:
                if selected_key is not None:
                    selection.clearSelection()
                if current_key in row_of:
                    selection.setCurrentIndex(
                        self._model.index(row_of[current_key], 0),
                        QItemSelectionModel.SelectionFlag.NoUpdate,
                    )
                else:
                    selection.clearCurrentIndex()
        finally:
            self._view.setAutoScroll(True)

        # The repaint below asks again for whatever is on screen now; anything
        # queued for the old order would only be decoded to be thrown away.
        self._cache.retain({path for _entry, path, _meta in rows})
        self._view.viewport().update()

    def _empty_text(self) -> str:
        status = self._state.status
        if status is SyncStatus.STOPPED:
            return "The sync engine is not running."
        if status is SyncStatus.CONNECTING:
            return "Connecting to the sync engine…"
        return (
            "No images have changed recently.\n"
            "New and edited images in your folders appear here as they sync."
        )

    def shutdown(self) -> None:
        """Stop redrawing and drop thumbnail work not yet started. Does not block."""
        self._coalesce.stop()
        self._clock.stop()
        self._cache.shutdown()

    def wait_for_thumbnails(self, msecs: int) -> bool:
        """After :meth:`shutdown`: wait for the decodes still in flight."""
        return self._cache.wait(msecs)

    # ----------------------------------------------------------------- actions
    def _open_index(self, index: QModelIndex) -> None:
        open_file(str(index.data(ROLE_PATH) or ""))

    def _show_menu(self, position) -> None:
        index = self._view.indexAt(position)
        if not index.isValid():
            return
        # The path, not the index: the grid keeps redrawing while the menu is
        # open, and by the time an entry is chosen that row may be another file.
        path = str(index.data(ROLE_PATH) or "")
        menu = QMenu(self)
        menu.addAction("Open", lambda: open_file(path))
        menu.addAction("Show in Folder", lambda: reveal(path))
        menu.exec(self._view.viewport().mapToGlobal(position))

    # ------------------------------------------------------------------- theme
    def changeEvent(self, event) -> None:  # noqa: N802
        if (
            event.type() == QEvent.Type.PaletteChange
            and self._ready
            and not self._recolouring
        ):
            self._recolouring = True
            try:
                self._apply_colors()
            finally:
                self._recolouring = False
        super().changeEvent(event)

    def _apply_colors(self) -> None:
        palette = palette_for(self)
        self._title.setStyleSheet(f"color: {palette.ink}; background: transparent;")
        muted = f"color: {palette.ink_muted}; background: transparent;"
        self._summary.setStyleSheet(muted)
        self._empty.setStyleSheet(muted)
        self._view.setStyleSheet(
            f"QListView {{ background: {palette.plane}; border: none; }}"
        )
        self.setAutoFillBackground(True)
        qpalette = self.palette()
        qpalette.setColor(self.backgroundRole(), palette.qcolor(palette.plane))
        self.setPalette(qpalette)


def _key_at(index: QModelIndex) -> tuple[str, str] | None:
    entry = index.data(ROLE_ENTRY) if index.isValid() else None
    return entry.key if isinstance(entry, RecentFile) else None


def open_file(path: str) -> None:
    if path:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def reveal(path: str) -> None:
    """Show *path* selected in the platform's file manager."""
    if not path:
        return
    if sys.platform == "win32" and os.path.exists(path):
        process = QProcess()
        process.setProgram("explorer.exe")
        # Explorer parses its own command line: /select, and the path must
        # reach it as one token, quoted, which QProcess's own quoting breaks.
        process.setNativeArguments(f'/select,"{path}"')
        process.startDetached()
    elif sys.platform == "darwin" and os.path.exists(path):
        QProcess.startDetached("open", ["-R", path])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
