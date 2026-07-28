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
from PySide6.QtGui import QFont, QPainter, QPen
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

        lower = QHBoxLayout()
        lower.setSpacing(14)
        lower.addWidget(self._build_folders_card(), 3)
        lower.addWidget(self._build_load_card(), 2)
        layout.addLayout(lower)

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

        for tile in (
            self._tile_down,
            self._tile_up,
            self._tile_received,
            self._tile_sent,
            self._tile_peers,
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

    def _build_load_card(self) -> QWidget:
        card = Card(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)

        self._load_chart = TimeSeriesChart(
            "Memory in use", axis=TimeSeriesChart.AXIS_BYTES, parent=card
        )
        self._load_chart.setMinimumHeight(150)
        layout.addWidget(self._load_chart)

        self._load_detail = QLabel("", card)
        self._load_detail.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._load_detail)
        return card

    def _build_folders_card(self) -> QWidget:
        card = Card(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)

        self._folders_title = _section_title("Folders", card)
        layout.addWidget(self._folders_title)

        self._folders_grid = QGridLayout()
        self._folders_grid.setHorizontalSpacing(12)
        self._folders_grid.setVerticalSpacing(7)
        self._folders_grid.setColumnStretch(0, 3)
        self._folders_grid.setColumnStretch(1, 4)
        layout.addLayout(self._folders_grid)

        self._folders_empty = QLabel("No folders yet — add one in Configuration.", card)
        layout.addWidget(self._folders_empty)
        layout.addStretch(1)

        self._folder_rows: list[tuple[QLabel, Meter, QLabel]] = []
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
        parts.append(f"{len(active)} folder{'s' if len(active) != 1 else ''}")
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
        # A single series needs no legend box — the chart title already names
        # what is plotted — but the legend doubles as the current-value readout,
        # so it stays.
        self._load_chart.set_series(
            [Series("In use", palette.series_download, [s.heap_bytes for s in history])]
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

        self._load_detail.setText(
            f"{format_bytes(sample.memory_bytes)} reserved · {sample.goroutines} tasks"
        )
        self._update_device_rates(sample)

    # ------------------------------------------------------------------ tables
    def _rebuild_folders(self) -> None:
        folders = self._state.folders()
        self._folders_empty.setVisible(not folders)
        palette = palette_for(self)

        while len(self._folder_rows) < len(folders):
            row = len(self._folder_rows)
            name = QLabel(self._body)
            meter = Meter(self._body)
            detail = QLabel(self._body)
            detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._folders_grid.addWidget(name, row, 0)
            self._folders_grid.addWidget(meter, row, 1)
            self._folders_grid.addWidget(detail, row, 2)
            self._folder_rows.append((name, meter, detail))

        for index, (name, meter, detail) in enumerate(self._folder_rows):
            visible = index < len(folders)
            for widget in (name, meter, detail):
                widget.setVisible(visible)
            if not visible:
                continue

            folder = folders[index]
            name.setText(folder.display_name)
            name.setStyleSheet(f"color: {palette.ink}; background: transparent;")

            if folder.paused:
                meter.set_value(0.0)
                detail.setText("paused")
            elif folder.error_count:
                meter.set_value(folder.completion / 100.0, palette.status_warning)
                detail.setText(f"{folder.error_count} error(s)")
            else:
                meter.set_value(folder.completion / 100.0)
                size = format_bytes(folder.global_bytes) if folder.global_bytes else ""
                detail.setText(
                    f"{folder.completion:.0f}%" + (f" of {size}" if size else "")
                )
            detail.setStyleSheet(f"color: {palette.ink_secondary}; background: transparent;")

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
        self._load_detail.setStyleSheet(muted)
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
