"""Hand-painted chart widgets for the dashboard.

Everything is drawn with QPainter rather than pulled from a charting library, for
the same reasons the icons are: no extra dependency, exact control over the mark
specs, and correct rendering at any DPI.

The specs applied throughout: 2px lines with round joins, area fills as a ~10%
wash of the series hue, markers at least 8px carrying a 2px surface ring,
hairline solid gridlines one step off the surface, and text always in an ink
token — identity comes from a coloured key *beside* the text, never from
colouring the text itself.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .theme import Palette, palette_for

# --- mark specs, from the data-visualisation reference ---------------------
LINE_WIDTH = 2.0
AREA_OPACITY = 0.10
MARKER_RADIUS = 4.0  # ≥ 8px across
SURFACE_RING = 2.0
HAIRLINE = 1.0
#: Horizontal gridline count, giving clean quarter divisions.
Y_DIVISIONS = 4


# ---------------------------------------------------------------- formatting
_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(value: float) -> str:
    """A byte count as a short human string, e.g. ``4.3 GiB``."""
    if value <= 0:
        return "0 B"
    index = min(int(math.log(value, 1024)), len(_BYTE_UNITS) - 1)
    scaled = value / (1024**index)
    if index == 0:
        return f"{int(scaled)} B"
    unit = _BYTE_UNITS[index]
    # Three significant figures: "512 MiB" reads better than "512.0 MiB".
    return f"{scaled:.1f} {unit}" if scaled < 100 else f"{scaled:.0f} {unit}"


def format_rate(bytes_per_second: float) -> str:
    """A throughput as a short human string, e.g. ``1.2 MiB/s``."""
    if bytes_per_second < 1:
        return "0 B/s"
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float) -> str:
    """A duration as the two largest useful units, e.g. ``2h 14m``."""
    seconds = int(max(0, seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def byte_axis_max(value: float) -> tuple[float, float, str]:
    """Choose a clean y-axis ceiling for a byte rate.

    Returns ``(ceiling, unit_divisor, unit_name)``. Ticks are then plain numbers
    in that unit, which is what keeps the axis readable — mixing KiB and MiB
    across four labels does not.
    """
    if value <= 0:
        return 1024.0, 1024.0, "KiB"

    index = min(int(math.log(max(value, 1), 1024)), len(_BYTE_UNITS) - 1)
    index = max(index, 1)  # Never label an axis in bare bytes; KiB is the floor.
    divisor = float(1024**index)
    scaled = value / divisor

    for step in (1, 2, 4, 5, 8, 10, 20, 40, 50, 80, 100, 200, 400, 500, 1024):
        if step >= scaled:
            return step * divisor, divisor, _BYTE_UNITS[index]
    return 1024 * divisor, divisor, _BYTE_UNITS[index]


def _format_tick(value: float) -> str:
    """A tick number with no trailing noise: 0, 0.5, 1, 1.5, 2."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _tabular(font: QFont) -> QFont:
    """Equal-width digits, for columns of numbers that must line up."""
    tabular = QFont(font)
    if hasattr(tabular, "setFeature"):
        # Not every Qt build exposes OpenType features; proportional digits are
        # a perfectly acceptable fallback.
        with contextlib.suppress(TypeError, ValueError):
            tabular.setFeature("tnum", 1)
    return tabular


# -------------------------------------------------------------------- series
@dataclass
class Series:
    """One plotted line."""

    label: str
    color: str
    values: list[float] = field(default_factory=list)


# ------------------------------------------------------------------- sparkline
class Sparkline(QWidget):
    """A bare trend line for a stat tile — no axes, no labels, no interaction."""

    def __init__(self, color: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: list[float] = []
        self._ceiling = 0.0
        self._color = color
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values: list[float], ceiling: float = 0.0) -> None:
        """Plot ``values``, optionally against a scale shared with other tiles.

        Two sparklines of the same unit sitting side by side get compared
        whether or not that was intended, so when the caller supplies a common
        ceiling a small series stays visibly small instead of being inflated to
        fill its own box.
        """
        self._values = values
        self._ceiling = ceiling
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        palette = palette_for(self)
        if len(self._values) < 2:
            return

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(1, 3, -1, -3)
            peak = self._ceiling if self._ceiling > 0 else max(self._values)
            if peak <= 0:
                # Flat at zero: a baseline says "nothing happening" honestly.
                pen = QPen(palette.qcolor(palette.gridline))
                pen.setWidthF(LINE_WIDTH)
                painter.setPen(pen)
                painter.drawLine(
                    QPointF(rect.left(), rect.bottom()),
                    QPointF(rect.right(), rect.bottom()),
                )
                return

            points = _plot_points(self._values, rect, peak)
            colour = QColor(self._color or palette.series_download)

            fill = QPainterPath()
            fill.moveTo(points[0].x(), rect.bottom())
            for point in points:
                fill.lineTo(point)
            fill.lineTo(points[-1].x(), rect.bottom())
            fill.closeSubpath()
            painter.fillPath(fill, palette.qcolor(colour.name(), AREA_OPACITY))

            pen = QPen(colour)
            pen.setWidthF(LINE_WIDTH)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawPath(_line_path(points))
        finally:
            painter.end()


def _plot_points(values: list[float], rect: QRectF, ceiling: float) -> list[QPointF]:
    if len(values) == 1:
        return [QPointF(rect.right(), rect.bottom())]
    step = rect.width() / (len(values) - 1)
    return [
        QPointF(
            rect.left() + index * step,
            rect.bottom() - (min(value, ceiling) / ceiling) * rect.height(),
        )
        for index, value in enumerate(values)
    ]


def _line_path(points: list[QPointF]) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(points[0])
    for point in points[1:]:
        path.lineTo(point)
    return path


# ------------------------------------------------------------ time series chart
class TimeSeriesChart(QWidget):
    """A live line-and-area chart with a crosshair readout.

    Built for one or two series sharing a single y axis and unit — never two
    scales on one plot, which would invent a relationship the data does not
    have.
    """

    #: Emitted with the hovered sample index, or -1 when the cursor leaves.
    hovered = Signal(int)

    #: Recognised axis kinds: a byte rate, a plain byte size, or a percentage.
    AXIS_RATE = "rate"
    AXIS_BYTES = "bytes"
    AXIS_PERCENT = "percent"

    def __init__(
        self,
        title: str,
        *,
        axis: str = AXIS_RATE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._axis_kind = axis
        self._series: list[Series] = []
        self._seconds_per_sample = 1.0
        self._hover_index = -1

        self.setMouseTracking(True)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------------ public
    def set_series(self, series: list[Series], seconds_per_sample: float = 1.0) -> None:
        self._series = series
        self._seconds_per_sample = seconds_per_sample
        if self._hover_index >= self._sample_count():
            self._hover_index = -1
        self.update()

    # ------------------------------------------------------------------ events
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        plot = self._plot_rect()
        count = self._sample_count()
        position = event.position()

        if count < 2 or not plot.contains(position):
            self._set_hover(-1)
            return

        ratio = (position.x() - plot.left()) / plot.width()
        self._set_hover(max(0, min(count - 1, round(ratio * (count - 1)))))

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._set_hover(-1)

    def _set_hover(self, index: int) -> None:
        if index == self._hover_index:
            return
        self._hover_index = index
        self.hovered.emit(index)
        self.update()

    # ----------------------------------------------------------------- geometry
    def _sample_count(self) -> int:
        return max((len(s.values) for s in self._series), default=0)

    def _header_height(self) -> int:
        return QFontMetrics(self._title_font()).height() + 16

    def _y_labels(self) -> list[str]:
        """Tick labels, bottom to top.

        The unit rides the topmost label only, so the rest stay short numbers
        and the axis does not repeat "MiB/s" five times.
        """
        ceiling, divisor, unit = self._axis()
        labels = []
        for division in range(Y_DIVISIONS + 1):
            text = _format_tick((ceiling * division / Y_DIVISIONS) / divisor)
            labels.append(f"{text} {unit}" if division == Y_DIVISIONS else text)
        return labels

    def _plot_rect(self) -> QRectF:
        metrics = QFontMetrics(_tabular(self._label_font()))
        # Measure the labels that will actually be drawn. Sizing the gutter to a
        # guess clipped the top tick, which carries the unit and so is always
        # the widest.
        widest = max(
            (metrics.horizontalAdvance(text) for text in self._y_labels()), default=40
        )
        left = widest + 14
        bottom = metrics.height() + 10
        return QRectF(self.rect()).adjusted(left, self._header_height(), -12.0, -bottom)

    def _title_font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF()))
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def _label_font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1.0))
        return font

    # ----------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:  # noqa: N802
        palette = palette_for(self)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_header(painter, palette)

            plot = self._plot_rect()
            if plot.width() <= 10 or plot.height() <= 10:
                return

            ceiling, _divisor, _unit = self._axis()
            self._paint_grid(painter, palette, plot)

            if self._sample_count() < 2:
                self._paint_empty(painter, palette, plot)
                return

            for series in self._series:
                self._paint_series(painter, palette, plot, series, ceiling)
            self._paint_crosshair(painter, palette, plot, ceiling)
        finally:
            painter.end()

    def _axis(self) -> tuple[float, float, str]:
        if self._axis_kind == self.AXIS_PERCENT:
            return 100.0, 1.0, "%"
        peak = max((max(s.values, default=0.0) for s in self._series), default=0.0)
        ceiling, divisor, unit = byte_axis_max(peak)
        # A rate axis has to say so in the label: "100 MiB" and "100 MiB/s" are
        # very different claims.
        suffix = "/s" if self._axis_kind == self.AXIS_RATE else ""
        return ceiling, divisor, f"{unit}{suffix}"

    def _format_value(self, value: float) -> str:
        if self._axis_kind == self.AXIS_PERCENT:
            return f"{value:.0f}%"
        if self._axis_kind == self.AXIS_BYTES:
            return format_bytes(value)
        return format_rate(value)

    def _paint_header(self, painter: QPainter, palette: Palette) -> None:
        """Title on the left, legend on the right carrying the current values.

        The legend doubles as the direct-value readout. Labelling the line ends
        on the plot instead would collide whenever the two series sit close
        together, which they do whenever the link is idle.
        """
        painter.setFont(self._title_font())
        painter.setPen(palette.qcolor(palette.ink))
        metrics = QFontMetrics(self._title_font())
        painter.drawText(QPointF(0, metrics.ascent() + 2), self._title)

        if not self._series:
            return

        label_font = self._label_font()
        label_metrics = QFontMetrics(label_font)
        painter.setFont(label_font)

        entries = [(s, self._legend_text(s)) for s in self._series]
        widths = [label_metrics.horizontalAdvance(text) + 26 for _s, text in entries]

        x = self.width() - sum(widths)
        baseline = metrics.ascent() + 2

        for (series, text), width in zip(entries, widths, strict=True):
            key_y = baseline - label_metrics.ascent() / 2 - 1
            pen = QPen(QColor(series.color))
            pen.setWidthF(LINE_WIDTH)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, key_y), QPointF(x + 14, key_y))

            # Text stays in an ink token; the coloured key beside it carries
            # the identity.
            painter.setPen(palette.qcolor(palette.ink_secondary))
            painter.drawText(QPointF(x + 20, baseline), text)
            x += width

    def _legend_text(self, series: Series) -> str:
        current = series.values[-1] if series.values else 0.0
        return f"{series.label}  {self._format_value(current)}"

    def _paint_grid(self, painter: QPainter, palette: Palette, plot: QRectF) -> None:
        label_font = _tabular(self._label_font())
        metrics = QFontMetrics(label_font)
        painter.setFont(label_font)

        grid_pen = QPen(palette.qcolor(palette.gridline))
        grid_pen.setWidthF(HAIRLINE)
        grid_pen.setStyle(Qt.PenStyle.SolidLine)  # never dashed

        for division, text in enumerate(self._y_labels()):
            y = plot.bottom() - (division / Y_DIVISIONS) * plot.height()

            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

            painter.setPen(palette.qcolor(palette.ink_muted))
            painter.drawText(
                QPointF(
                    plot.left() - 8 - metrics.horizontalAdvance(text),
                    y + metrics.ascent() / 2 - 1,
                ),
                text,
            )

        baseline_pen = QPen(palette.qcolor(palette.baseline))
        baseline_pen.setWidthF(HAIRLINE)
        painter.setPen(baseline_pen)
        painter.drawLine(
            QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom())
        )

        self._paint_time_axis(painter, palette, plot, metrics)

    def _paint_time_axis(
        self, painter: QPainter, palette: Palette, plot: QRectF, metrics: QFontMetrics
    ) -> None:
        count = self._sample_count()
        if count < 2:
            return
        span = (count - 1) * self._seconds_per_sample
        painter.setPen(palette.qcolor(palette.ink_muted))

        left_text = f"-{format_duration(span)}"
        painter.drawText(
            QPointF(plot.left(), plot.bottom() + metrics.height()), left_text
        )
        now_text = "now"
        painter.drawText(
            QPointF(
                plot.right() - metrics.horizontalAdvance(now_text),
                plot.bottom() + metrics.height(),
            ),
            now_text,
        )

    def _paint_empty(self, painter: QPainter, palette: Palette, plot: QRectF) -> None:
        painter.setFont(self._label_font())
        painter.setPen(palette.qcolor(palette.ink_muted))
        painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Collecting data…")

    def _paint_series(
        self,
        painter: QPainter,
        palette: Palette,
        plot: QRectF,
        series: Series,
        ceiling: float,
    ) -> None:
        if len(series.values) < 2:
            return
        points = _plot_points(series.values, plot, ceiling)
        colour = QColor(series.color)

        fill = QPainterPath()
        fill.moveTo(points[0].x(), plot.bottom())
        for point in points:
            fill.lineTo(point)
        fill.lineTo(points[-1].x(), plot.bottom())
        fill.closeSubpath()
        painter.fillPath(fill, palette.qcolor(colour.name(), AREA_OPACITY))

        pen = QPen(colour)
        pen.setWidthF(LINE_WIDTH)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(_line_path(points))

        # End marker, ringed in the surface colour so it stays legible where the
        # two series cross.
        self._paint_marker(painter, palette, points[-1], colour)

    def _paint_marker(
        self, painter: QPainter, palette: Palette, point: QPointF, colour: QColor
    ) -> None:
        ring = QPen(palette.qcolor(palette.surface))
        ring.setWidthF(SURFACE_RING)
        painter.setPen(ring)
        painter.setBrush(colour)
        painter.drawEllipse(point, MARKER_RADIUS, MARKER_RADIUS)

    def _paint_crosshair(
        self, painter: QPainter, palette: Palette, plot: QRectF, ceiling: float
    ) -> None:
        index = self._hover_index
        count = self._sample_count()
        if index < 0 or count < 2:
            return

        x = plot.left() + (index / (count - 1)) * plot.width()
        pen = QPen(palette.qcolor(palette.baseline))
        pen.setWidthF(HAIRLINE)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

        rows: list[tuple[str, str, QColor]] = []
        for series in self._series:
            if index >= len(series.values):
                continue
            value = series.values[index]
            y = plot.bottom() - (min(value, ceiling) / ceiling) * plot.height()
            self._paint_marker(painter, palette, QPointF(x, y), QColor(series.color))
            rows.append((series.label, self._format_value(value), QColor(series.color)))

        seconds_ago = (count - 1 - index) * self._seconds_per_sample
        heading = "now" if seconds_ago < 0.5 else f"{format_duration(seconds_ago)} ago"
        self._paint_tooltip(painter, palette, plot, x, heading, rows)

    def _paint_tooltip(
        self,
        painter: QPainter,
        palette: Palette,
        plot: QRectF,
        x: float,
        heading: str,
        rows: list[tuple[str, str, QColor]],
    ) -> None:
        font = self._label_font()
        metrics = QFontMetrics(font)
        painter.setFont(font)

        lines = [heading] + [f"{label}  {value}" for label, value, _c in rows]
        width = max(metrics.horizontalAdvance(line) for line in lines) + 32
        height = len(lines) * (metrics.height() + 2) + 12

        left = x + 12
        if left + width > plot.right():
            left = x - 12 - width
        top = min(plot.top() + 8, plot.bottom() - height - 4)

        box = QRectF(left, top, width, height)
        painter.setPen(QPen(palette.qcolor(palette.border), HAIRLINE))
        painter.setBrush(palette.qcolor(palette.surface, 0.97))
        painter.drawRoundedRect(box, 6, 6)

        y = box.top() + 6 + metrics.ascent()
        painter.setPen(palette.qcolor(palette.ink_muted))
        painter.drawText(QPointF(box.left() + 10, y), heading)

        for label, value, colour in rows:
            y += metrics.height() + 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawEllipse(QPointF(box.left() + 14, y - metrics.ascent() / 2 + 1), 3.5, 3.5)
            painter.setPen(palette.qcolor(palette.ink))
            painter.drawText(QPointF(box.left() + 24, y), f"{label}  {value}")


# ------------------------------------------------------------------- meter
class Meter(QWidget):
    """A single ratio against its limit — a folder's progress toward complete.

    The unfilled track is a lighter step of the fill's own ramp, so the state
    reads across the whole bar rather than only where it is filled.
    """

    TRACK_HEIGHT = 8.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fraction = 0.0
        self._color_override = ""
        self.setFixedHeight(int(self.TRACK_HEIGHT))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, fraction: float, color: str = "") -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        self._color_override = color
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        palette = palette_for(self)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)

            radius = self.TRACK_HEIGHT / 2
            track = QRectF(0, 0, self.width(), self.TRACK_HEIGHT)
            painter.setBrush(palette.qcolor(palette.meter_track))
            painter.drawRoundedRect(track, radius, radius)

            if self._fraction <= 0:
                return
            width = max(self.TRACK_HEIGHT, track.width() * self._fraction)
            painter.setBrush(palette.qcolor(self._color_override or palette.meter_fill))
            painter.drawRoundedRect(QRectF(0, 0, width, self.TRACK_HEIGHT), radius, radius)
        finally:
            painter.end()


# ---------------------------------------------------------------------- cards
class Card(QFrame):
    """A panel drawn on the chart surface with a hairline ring.

    Painted rather than styled with a stylesheet so the surface, border and
    radius come from the same tokens the charts inside them use.
    """

    RADIUS = 10.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def paintEvent(self, _event) -> None:  # noqa: N802
        palette = palette_for(self)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(palette.qcolor(palette.border), HAIRLINE))
            painter.setBrush(palette.qcolor(palette.surface))
            painter.drawRoundedRect(
                QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS
            )
        finally:
            painter.end()


class StatTile(Card):
    """Label, current value, an optional sub-line, and an optional sparkline.

    The value uses the font's proportional figures: ``tabular-nums`` gives every
    digit the width of a zero, which makes a large standalone number look loose.
    """

    # Class-level defaults: changeEvent can fire before __init__ has built the
    # labels it wants to recolour.
    _ready = False
    _recolouring = False

    def __init__(
        self,
        label: str,
        *,
        with_sparkline: bool = False,
        accent: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._accent = accent

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)

        self._label = QLabel(label, self)
        label_font = QFont(self.font())
        label_font.setPointSizeF(max(7.5, label_font.pointSizeF() - 0.5))
        self._label.setFont(label_font)

        self._value = QLabel("—", self)
        value_font = QFont(self.font())
        value_font.setPointSizeF(value_font.pointSizeF() + 6.0)
        value_font.setWeight(QFont.Weight.DemiBold)
        self._value.setFont(value_font)

        self._sub = QLabel("", self)
        self._sub.setFont(label_font)

        layout.addWidget(self._label)
        layout.addWidget(self._value)
        layout.addWidget(self._sub)

        self._sparkline: Sparkline | None = None
        if with_sparkline:
            self._sparkline = Sparkline(accent, self)
            layout.addSpacing(2)
            layout.addWidget(self._sparkline)

        self.setMinimumWidth(140)
        self._apply_colors()
        self._ready = True

    def set_value(self, value: str, sub: str = "") -> None:
        self._value.setText(value)
        self._sub.setText(sub)
        self._sub.setVisible(bool(sub))

    def set_trend(self, values: list[float], ceiling: float = 0.0) -> None:
        if self._sparkline is not None:
            self._sparkline.set_values(values, ceiling)

    def changeEvent(self, event) -> None:  # noqa: N802
        # Setting a stylesheet raises another palette change; guard against
        # recursing on our own side effect.
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
        self._label.setStyleSheet(f"color: {palette.ink_muted}; background: transparent;")
        self._value.setStyleSheet(f"color: {palette.ink}; background: transparent;")
        self._sub.setStyleSheet(f"color: {palette.ink_secondary}; background: transparent;")


__all__ = [
    "Card",
    "Meter",
    "Series",
    "Sparkline",
    "StatTile",
    "TimeSeriesChart",
    "byte_axis_max",
    "format_bytes",
    "format_duration",
    "format_rate",
]
