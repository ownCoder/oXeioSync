"""The dashboard — oXeioSync's own view of what is happening.

Composed rather than embedded: the sync engine's web interface is a
configuration surface, not a status one, and it carries its own branding. This
page answers "is everything fine, and what is moving right now" using the
application's own visual language.

Layout, top to bottom: the one status line the view leads with, a row of stat
tiles for the headline numbers, the live throughput chart, folder progress, and
the peers. Each block answers a different question, so none of them is a chart
for the sake of being one.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..syncthing.state import SyncStatus, SyncthingState
from ..syncthing.transfer import TransferSample, TransferSampler
from .charts import (
    Card,
    Meter,
    Series,
    StatTile,
    TimeSeriesChart,
    format_bytes,
    format_duration,
    format_rate,
)
from .theme import Palette, palette_for

log = logging.getLogger(__name__)

#: How much of the sampled history the throughput chart shows.
CHART_WINDOW = 120

#: Columns in the up-to-date list. Three keeps twenty folders to four lines at
#: the width this card has, and a folder name to about twenty characters before
#: it elides — long enough for the names people actually use.
HEALTHY_COLUMNS = 3

STATUS_HEADLINES = {
    SyncStatus.STOPPED: "Not running",
    SyncStatus.CONNECTING: "Connecting…",
    SyncStatus.IDLE: "Up to date",
    SyncStatus.SCANNING: "Scanning for changes",
    SyncStatus.SYNCING: "Syncing",
    SyncStatus.WARNING: "Some folders are out of sync",
    SyncStatus.ERROR: "Something needs attention",
}


def status_color(palette: Palette, status: SyncStatus) -> str:
    """Status colours are reserved for state and never reused as a series hue."""
    return {
        SyncStatus.STOPPED: palette.ink_muted,
        SyncStatus.CONNECTING: palette.ink_muted,
        SyncStatus.IDLE: palette.status_good,
        SyncStatus.SCANNING: palette.series_download,
        SyncStatus.SYNCING: palette.series_download,
        SyncStatus.WARNING: palette.status_warning,
        SyncStatus.ERROR: palette.status_critical,
    }.get(status, palette.ink_muted)


class DashboardPage(QScrollArea):
    """Scrollable dashboard bound to the state model and the transfer sampler."""

    # Class-level defaults: a palette change can reach changeEvent before
    # __init__ has finished building the widgets it wants to recolour.
    _ready = False
    _recolouring = False

    def __init__(
        self,
        state: SyncthingState,
        sampler: TransferSampler,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._sampler = sampler

        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget(self)
        self._body = body
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(14)

        layout.addWidget(self._build_status_header())
        layout.addLayout(self._build_stat_row())
        layout.addWidget(self._build_transfer_card(), 1)
        layout.addWidget(self._build_folders_card())
        layout.addWidget(self._build_devices_card())
        layout.addStretch(0)

        self.setWidget(body)

        state.status_changed.connect(self._on_status_changed)
        state.changed.connect(self._refresh_from_state)
        sampler.sampled.connect(self._on_sample)

        # Keep the relative clocks ("2h 14m") honest between samples.
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._refresh_slow_text)
        self._tick.start()

        self._apply_colors()
        self._on_status_changed(state.status)
        self._refresh_from_state()
        self._ready = True

    # ------------------------------------------------------------------ build
    def _build_status_header(self) -> QWidget:
        card = Card(self)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        self._status_dot = _StatusDot(card)
        layout.addWidget(self._status_dot)

        text_column = QVBoxLayout()
        text_column.setSpacing(1)

        self._status_headline = QLabel("Starting up", card)
        headline_font = QFont(self.font())
        headline_font.setPointSizeF(headline_font.pointSizeF() + 7.0)
        headline_font.setWeight(QFont.Weight.DemiBold)
        self._status_headline.setFont(headline_font)

        self._status_detail = QLabel("", card)
        text_column.addWidget(self._status_headline)
        text_column.addWidget(self._status_detail)
        layout.addLayout(text_column)
        layout.addStretch(1)

        self._engine_label = QLabel("", card)
        self._engine_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._engine_label)
        return card

    def _build_stat_row(self) -> QHBoxLayout:
        palette = palette_for(self)
        row = QHBoxLayout()
        row.setSpacing(12)

        self._tile_down = StatTile(
            "Download", with_sparkline=True, accent=palette.series_download, parent=self
        )
        self._tile_up = StatTile(
            "Upload", with_sparkline=True, accent=palette.series_upload, parent=self
        )
        self._tile_received = StatTile("Received", parent=self)
        self._tile_sent = StatTile("Sent", parent=self)
        self._tile_peers = StatTile("Peers online", parent=self)
        # Memory belongs up here with the other single numbers, not in a card
        # of its own beside the folders: one figure and its trend never needed
        # two fifths of that row.
        self._tile_memory = StatTile(
            "Memory in use",
            with_sparkline=True,
            accent=palette.series_download,
            parent=self,
        )

        for tile in (
            self._tile_down,
            self._tile_up,
            self._tile_received,
            self._tile_sent,
            self._tile_peers,
            self._tile_memory,
        ):
            row.addWidget(tile)
        return row

    def _build_transfer_card(self) -> QWidget:
        card = Card(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)

        self._transfer_chart = TimeSeriesChart("Transfer rate", parent=card)
        self._transfer_chart.setMinimumHeight(230)
        layout.addWidget(self._transfer_chart)
        return card

    def _build_folders_card(self) -> QWidget:
        """Folders, ordered by what wants attention rather than by name.

        Nineteen folders with nothing wrong are nineteen identical rows, and a
        row per folder spends the same ink on each of them as on the one that
        cannot write its files. So the card answers "is anything wrong" first,
        gives the answer room, and lets everything healthy be a name and a size.
        """
        card = Card(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(12)
        self._folders_title = _section_title("Folders", card)
        header.addWidget(self._folders_title)
        header.addStretch(1)
        self._folders_summary = QLabel("", card)
        header.addWidget(self._folders_summary)
        layout.addLayout(header)

        self._folders_chips = QHBoxLayout()
        self._folders_chips.setSpacing(6)
        self._folders_chips.addStretch(1)
        layout.addLayout(self._folders_chips)

        self._attention_column = QVBoxLayout()
        self._attention_column.setSpacing(6)
        layout.addLayout(self._attention_column)

        self._healthy_heading = QLabel("", card)
        layout.addWidget(self._healthy_heading)

        # Three columns of (dot, name, size). Wide enough to read, narrow
        # enough that twenty folders are four rows rather than twenty.
        self._folders_grid = QGridLayout()
        self._folders_grid.setHorizontalSpacing(10)
        self._folders_grid.setVerticalSpacing(4)
        for column in range(HEALTHY_COLUMNS):
            self._folders_grid.setColumnStretch(column * 3 + 1, 4)
            self._folders_grid.setColumnStretch(column * 3 + 2, 2)
        layout.addLayout(self._folders_grid)

        self._folders_empty = QLabel("No folders yet — add one in Configuration.", card)
        layout.addWidget(self._folders_empty)
        layout.addStretch(1)

        self._chips: list[_Pill] = []
        self._attention_rows: list[_AttentionRow] = []
        self._healthy_rows: list[tuple[_StatusDot, QLabel, QLabel]] = []
        return card

    def _build_devices_card(self) -> QWidget:
        card = Card(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)

        self._devices_title = _section_title("Peers", card)
        layout.addWidget(self._devices_title)

        self._devices_grid = QGridLayout()
        self._devices_grid.setHorizontalSpacing(14)
        self._devices_grid.setVerticalSpacing(6)
        self._devices_grid.setColumnStretch(1, 3)
        self._devices_grid.setColumnStretch(2, 4)
        layout.addLayout(self._devices_grid)

        self._devices_empty = QLabel("No peers yet — add one in Configuration.", card)
        layout.addWidget(self._devices_empty)

        self._device_rows: list[tuple[_StatusDot, QLabel, QLabel, QLabel]] = []
        return card

    # ---------------------------------------------------------------- updating
    def _on_status_changed(self, status: object) -> None:
        if not isinstance(status, SyncStatus):
            return
        palette = palette_for(self)
        self._status_headline.setText(STATUS_HEADLINES.get(status, "—"))
        self._status_dot.set_color(status_color(palette, status))
        self._status_headline.setStyleSheet(
            f"color: {palette.ink}; background: transparent;"
        )

    def _refresh_from_state(self) -> None:
        snapshot = self._state.snapshot
        self._engine_label.setText(
            f"engine {snapshot.version}" if snapshot.version else ""
        )
        self._rebuild_folders()
        self._rebuild_devices()
        self._refresh_slow_text()

    def _refresh_slow_text(self) -> None:
        latest = self._sampler.latest()
        folders = self._state.folders()
        active = [f for f in folders if not f.paused]
        behind = [f for f in active if f.completion < 100.0]

        parts: list[str] = []
        if behind:
            worst = min(behind, key=lambda f: f.completion)
            parts.append(f"{worst.display_name} at {worst.completion:.0f}%")
        # Every folder, paused ones included: the card below counts them all,
        # and two different totals on one screen means one of them is wrong.
        parts.append(f"{len(folders)} folder{'s' if len(folders) != 1 else ''}")
        if latest is not None and latest.uptime_seconds:
            parts.append(f"running {format_duration(latest.uptime_seconds)}")
        self._status_detail.setText(" · ".join(parts))

    def _on_sample(self, sample: object) -> None:
        if not isinstance(sample, TransferSample):
            return

        history = self._sampler.history()[-CHART_WINDOW:]
        palette = palette_for(self)

        self._transfer_chart.set_series(
            [
                Series("Download", palette.series_download, [s.down_bps for s in history]),
                Series("Upload", palette.series_upload, [s.up_bps for s in history]),
            ]
        )
        # The two rate tiles share one scale. They sit side by side in the same
        # unit, so independent scales would draw a trickle of upload at the same
        # height as a saturated download.
        recent = history[-40:]
        shared_peak = max(
            (max(s.down_bps, s.up_bps) for s in recent),
            default=0.0,
        )
        self._tile_down.set_value(format_rate(sample.down_bps))
        self._tile_down.set_trend([s.down_bps for s in recent], shared_peak)
        self._tile_up.set_value(format_rate(sample.up_bps))
        self._tile_up.set_trend([s.up_bps for s in recent], shared_peak)
        self._tile_received.set_value(format_bytes(sample.in_total), "since start")
        self._tile_sent.set_value(format_bytes(sample.out_total), "since start")

        total_peers = len(self._state.snapshot.devices)
        self._tile_peers.set_value(
            f"{sample.connected_devices}",
            f"of {total_peers}" if total_peers else "none configured",
        )

        self._tile_memory.set_value(
            format_bytes(sample.heap_bytes),
            f"{format_bytes(sample.memory_bytes)} reserved · {sample.goroutines} tasks",
        )
        self._tile_memory.set_trend([s.heap_bytes for s in recent])
        self._update_device_rates(sample)

    # ------------------------------------------------------------------ tables
    @staticmethod
    def _sort_folders(folders: list) -> tuple[list, list]:
        """Split into the ones that want attention and the ones that do not.

        Ordered by how much they want it, so the first row of the card is
        always the worst thing happening.
        """
        wants: list = []
        healthy: list = []
        for folder in folders:
            if folder.error_count:
                wants.append((0, folder))
            elif folder.paused:
                wants.append((3, folder))
            elif folder.completion < 100.0:
                wants.append((1, folder))
            elif folder.is_scanning:
                wants.append((2, folder))
            else:
                healthy.append(folder)
        wants.sort(key=lambda pair: (pair[0], pair[1].display_name.lower()))
        return [folder for _rank, folder in wants], healthy

    def _rebuild_folders(self) -> None:
        folders = self._state.folders()
        palette = palette_for(self)
        self._folders_empty.setVisible(not folders)

        wants, healthy = self._sort_folders(folders)
        total_bytes = sum(f.global_bytes for f in folders)
        self._folders_summary.setText(
            f"{len(folders)} folder{'s' if len(folders) != 1 else ''}"
            + (f" · {format_bytes(total_bytes)}" if total_bytes else "")
            if folders
            else ""
        )

        self._rebuild_chips(folders, palette)

        while len(self._attention_rows) < len(wants):
            row = _AttentionRow(self._body)
            self._attention_column.addWidget(row)
            self._attention_rows.append(row)

        for index, row in enumerate(self._attention_rows):
            row.setVisible(index < len(wants))
            if index >= len(wants):
                continue
            folder = wants[index]
            if folder.error_count:
                row.show_error(folder, palette)
            elif folder.paused:
                row.show_paused(folder, palette)
            elif folder.completion < 100.0:
                row.show_syncing(folder, palette)
            else:
                row.show_scanning(folder, palette)

        self._rebuild_healthy(healthy, palette)

    def _rebuild_chips(self, folders: list, palette: Palette) -> None:
        """One chip per state that is actually present. No zeroes.

        A row of chips reading "0 with errors · 0 paused" is a row that has to
        be read before it can be dismissed. Absence says the same thing faster.
        """
        healthy = [f for f in folders if not f.error_count and not f.paused]
        counts = [
            (
                sum(1 for f in folders if f.error_count),
                "with errors",
                palette.status_critical,
            ),
            (
                sum(1 for f in healthy if f.completion < 100.0),
                "syncing",
                palette.series_download,
            ),
            (
                sum(1 for f in folders if f.paused and not f.error_count),
                "paused",
                palette.ink_muted,
            ),
            (
                sum(1 for f in healthy if f.completion >= 100.0),
                "up to date",
                palette.status_good,
            ),
        ]
        present = [(n, word, hue) for n, word, hue in counts if n]

        while len(self._chips) < len(present):
            chip = _Pill(self._body)
            self._folders_chips.insertWidget(len(self._chips), chip)
            self._chips.append(chip)

        for index, chip in enumerate(self._chips):
            chip.setVisible(index < len(present))
            if index >= len(present):
                continue
            count, word, hue = present[index]
            chip.set_state(
                f"{count} {word}",
                palette.ink_secondary if hue == palette.ink_muted else hue,
                palette.qcolor(hue, 0.14).name(QColor.NameFormat.HexArgb),
            )

    def _rebuild_healthy(self, healthy: list, palette: Palette) -> None:
        """The quiet majority: a dot, a name, a size, three to a line."""
        self._healthy_heading.setVisible(bool(healthy))
        if healthy:
            self._healthy_heading.setText(f"Up to date · {len(healthy)}")

        while len(self._healthy_rows) < len(healthy):
            index = len(self._healthy_rows)
            row, column = divmod(index, HEALTHY_COLUMNS)
            dot = _StatusDot(self._body)
            name = QLabel(self._body)
            size = QLabel(self._body)
            size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._folders_grid.addWidget(dot, row, column * 3)
            self._folders_grid.addWidget(name, row, column * 3 + 1)
            self._folders_grid.addWidget(size, row, column * 3 + 2)
            self._healthy_rows.append((dot, name, size))

        for index, (dot, name, size) in enumerate(self._healthy_rows):
            visible = index < len(healthy)
            for widget in (dot, name, size):
                widget.setVisible(visible)
            if not visible:
                continue
            folder = healthy[index]
            dot.set_color(palette.status_good)
            name.setText(folder.display_name)
            name.setStyleSheet(f"color: {palette.ink_secondary}; background: transparent;")
            size.setText(format_bytes(folder.global_bytes) if folder.global_bytes else "")
            size.setStyleSheet(f"color: {palette.ink_muted}; background: transparent;")

    def _rebuild_devices(self) -> None:
        devices = self._state.devices()
        self._devices_empty.setVisible(not devices)
        palette = palette_for(self)

        while len(self._device_rows) < len(devices):
            row = len(self._device_rows)
            dot = _StatusDot(self._body)
            name = QLabel(self._body)
            detail = QLabel(self._body)
            rates = QLabel(self._body)
            rates.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._devices_grid.addWidget(dot, row, 0)
            self._devices_grid.addWidget(name, row, 1)
            self._devices_grid.addWidget(detail, row, 2)
            self._devices_grid.addWidget(rates, row, 3)
            self._device_rows.append((dot, name, detail, rates))

        for index, (dot, name, detail, rates) in enumerate(self._device_rows):
            visible = index < len(devices)
            for widget in (dot, name, detail, rates):
                widget.setVisible(visible)
            if not visible:
                continue

            device = devices[index]
            name.setText(device.display_name)
            name.setStyleSheet(f"color: {palette.ink}; background: transparent;")

            # State is carried by a word as well as the colour, never colour alone.
            if device.paused:
                dot.set_color(palette.ink_muted)
                detail.setText("paused")
            elif device.connected:
                dot.set_color(palette.status_good)
                detail.setText(device.address or "connected")
            else:
                dot.set_color(palette.ink_muted)
                detail.setText("offline")
            detail.setStyleSheet(f"color: {palette.ink_secondary}; background: transparent;")
            rates.setStyleSheet(f"color: {palette.ink_muted}; background: transparent;")

    def _update_device_rates(self, sample: TransferSample) -> None:
        devices = self._state.devices()
        for index, (_dot, _name, _detail, rates) in enumerate(self._device_rows):
            if index >= len(devices):
                continue
            measured = sample.devices.get(devices[index].id)
            if measured is None or not measured.connected:
                rates.setText("")
                continue
            rates.setText(
                f"↓ {format_rate(measured.down_bps)}   ↑ {format_rate(measured.up_bps)}"
            )

    # ------------------------------------------------------------------- theme
    def changeEvent(self, event) -> None:  # noqa: N802
        # Applying a stylesheet is itself a palette change, so without this
        # guard reacting to one would recurse until the stack ran out.
        if (
            event.type() == QEvent.Type.PaletteChange
            and self._ready
            and not self._recolouring
        ):
            self._recolouring = True
            try:
                self._apply_colors()
                self._refresh_from_state()
            finally:
                self._recolouring = False
        super().changeEvent(event)

    def _apply_colors(self) -> None:
        palette = palette_for(self)
        self.setStyleSheet(
            f"QScrollArea {{ background: {palette.plane}; border: none; }}"
            f"QScrollArea > QWidget > QWidget {{ background: {palette.plane}; }}"
        )
        muted = f"color: {palette.ink_muted}; background: transparent;"
        secondary = f"color: {palette.ink_secondary}; background: transparent;"
        self._status_detail.setStyleSheet(secondary)
        self._engine_label.setStyleSheet(muted)
        self._folders_summary.setStyleSheet(muted)
        self._healthy_heading.setStyleSheet(muted)
        self._folders_empty.setStyleSheet(muted)
        self._devices_empty.setStyleSheet(muted)
        for title in (self._folders_title, self._devices_title):
            title.setStyleSheet(f"color: {palette.ink}; background: transparent;")


def _section_title(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    font = QFont(parent.font())
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    return label


class _Pill(QLabel):
    """A state, said in a word, on a ground tinted from that state's own hue.

    A pill rather than a coloured edge on the row: an accented border reads as
    decoration at a glance and as an unfinished row at second glance, and it
    cannot carry the word that has to be there anyway for anyone who does not
    separate these colours.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 0.5))
        self.setFont(font)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_state(self, text: str, ink: str, ground: str) -> None:
        self.setText(text)
        self.setStyleSheet(
            f"color: {ink}; background: {ground}; border-radius: 8px; padding: 2px 9px;"
        )


class _AttentionRow(QWidget):
    """One folder that wants something: an error, a transfer, a pause.

    Sized by what it has to say. An error carries a sentence and the way to the
    screen that can act on it; a transfer carries its meter; a pause carries
    neither, and is the shortest row on the card.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ground = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 10)
        layout.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(10)
        self._name = QLabel("", self)
        name_font = QFont(self.font())
        name_font.setWeight(QFont.Weight.DemiBold)
        self._name.setFont(name_font)
        self._pill = _Pill(self)
        # Both on the left, together. Pushed to opposite edges they read as a
        # pair only on a narrow card; this one is as wide as the window.
        head.addWidget(self._name)
        head.addWidget(self._pill)
        head.addStretch(1)
        layout.addLayout(head)

        self._message = QLabel("", self)
        self._message.setWordWrap(True)
        message_font = QFont(self.font())
        message_font.setPointSizeF(max(7.5, message_font.pointSizeF() - 0.5))
        self._message.setFont(message_font)
        layout.addWidget(self._message)

        self._meter = Meter(self)
        layout.addWidget(self._meter)

    def show_error(self, folder, palette: Palette) -> None:
        count = folder.error_count
        self._name.setText(folder.display_name)
        self._pill.set_state(
            f"{count:,} error{'s' if count != 1 else ''}",
            palette.status_critical,
            palette.qcolor(palette.status_critical, 0.16).name(QColor.NameFormat.HexArgb),
        )
        # The count is the fact; the sentence is what the count means. Neither
        # invents a cause — the engine reports how many, not why.
        self._message.setText(
            f"{count:,} file{'s' if count != 1 else ''} could not be synced. "
            "Configuration lists them."
        )
        self._message.setVisible(True)
        self._meter.setVisible(False)
        self._paint(palette, palette.status_critical)

    def show_syncing(self, folder, palette: Palette) -> None:
        self._name.setText(folder.display_name)
        self._pill.set_state(
            f"{folder.completion:.0f}%",
            palette.series_download,
            palette.qcolor(palette.series_download, 0.18).name(QColor.NameFormat.HexArgb),
        )
        left = format_bytes(folder.need_bytes) if folder.need_bytes else ""
        self._message.setText(
            f"{left} left to fetch" if left else "Bringing this folder up to date"
        )
        self._message.setVisible(True)
        self._meter.set_value(folder.completion / 100.0)
        self._meter.setVisible(True)
        self._paint(palette, palette.series_download)

    def show_scanning(self, folder, palette: Palette) -> None:
        self._name.setText(folder.display_name)
        self._pill.set_state(
            "scanning",
            palette.ink_secondary,
            palette.qcolor(palette.ink_muted, 0.18).name(QColor.NameFormat.HexArgb),
        )
        self._message.setText("Looking for local changes")
        self._message.setVisible(True)
        self._meter.setVisible(False)
        self._paint(palette, palette.ink_muted)

    def show_paused(self, folder, palette: Palette) -> None:
        self._name.setText(folder.display_name)
        self._pill.set_state(
            "paused",
            palette.ink_secondary,
            palette.qcolor(palette.ink_muted, 0.18).name(QColor.NameFormat.HexArgb),
        )
        self._message.setVisible(False)
        self._meter.setVisible(False)
        self._paint(palette, palette.ink_muted)

    def _paint(self, palette: Palette, hue: str) -> None:
        self._ground = palette.qcolor(hue, 0.10).name(QColor.NameFormat.HexArgb)
        self.setStyleSheet(f"background: {self._ground}; border-radius: 8px;")
        self._name.setStyleSheet(f"color: {palette.ink}; background: transparent;")
        self._message.setStyleSheet(
            f"color: {palette.ink_secondary}; background: transparent;"
        )


class _StatusDot(QWidget):
    """A small filled circle used beside a state word — never colour alone."""

    SIZE = 12

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = "#898781"
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        palette = palette_for(self)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(palette.qcolor(palette.surface), 2.0))
            painter.setBrush(palette.qcolor(self._color))
            inset = 1.5
            painter.drawEllipse(
                self.rect().adjusted(int(inset), int(inset), -int(inset), -int(inset))
            )
        finally:
            painter.end()
