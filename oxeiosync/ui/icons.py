"""Status icons, drawn at runtime instead of shipped as image files.

Every icon is the same mark — a circular arrow on a coloured disc — so the tray
entry stays recognisable while colour carries the state. Drawing them means the
application has no binary assets, renders crisply at whatever size the tray asks
for, and can animate the syncing state by rotating the glyph.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from ..syncthing.state import SyncStatus

#: Sizes rendered into each QIcon, covering standard and HiDPI tray metrics.
ICON_SIZES = (16, 20, 24, 32, 48, 64)

#: Number of frames in the syncing animation, i.e. rotation steps per turn.
ANIMATION_FRAMES = 24

STATUS_COLOURS: dict[SyncStatus, str] = {
    SyncStatus.STOPPED: "#8b9099",
    SyncStatus.CONNECTING: "#8b9099",
    SyncStatus.IDLE: "#37a35b",
    SyncStatus.SCANNING: "#3d8bd4",
    SyncStatus.SYNCING: "#3d8bd4",
    SyncStatus.WARNING: "#dd9b28",
    SyncStatus.ERROR: "#d2483c",
}

_cache: dict[tuple[str, int], QIcon] = {}


def status_icon(status: SyncStatus, frame: int = 0) -> QIcon:
    """Icon for ``status``, rotated to ``frame`` of the syncing animation.

    Results are cached, so the animation timer costs nothing after the first
    full rotation.
    """
    frame = frame % ANIMATION_FRAMES
    key = (status.value, frame)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    angle = -360.0 * frame / ANIMATION_FRAMES
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(_render(status, size, angle))
    _cache[key] = icon
    return icon


def app_icon() -> QIcon:
    """The window and taskbar icon."""
    return status_icon(SyncStatus.IDLE)


def render_pixmap(status: SyncStatus, size: int, angle: float = 0.0) -> QPixmap:
    """The icon drawn natively at ``size``, rather than scaled from a cached one.

    Used by the build tooling, which needs sizes larger than the tray ever asks
    for and wants them crisp rather than upscaled.
    """
    return _render(status, size, angle)


def _render(status: SyncStatus, size: int, angle: float) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        disc_inset = size * 0.02
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(STATUS_COLOURS[status]))
        painter.drawEllipse(
            QRectF(disc_inset, disc_inset, size - 2 * disc_inset, size - 2 * disc_inset)
        )

        _draw_arrow_glyph(painter, size, angle)
    finally:
        painter.end()

    return pixmap


def _draw_arrow_glyph(painter: QPainter, size: int, angle: float) -> None:
    """Draw the white circular-arrow mark, rotated by ``angle`` degrees."""
    inset = size * 0.27
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    stroke = max(1.4, size * 0.125)
    white = QColor("#ffffff")

    # A gap is left for the arrowhead, which is drawn as a separate triangle so
    # it stays sharp at small sizes.
    span = 260.0
    start = 90.0 + angle

    # Build the pen from scratch: the disc was filled with NoPen set, and
    # inheriting that style would silently skip the arc.
    pen = QPen(white)
    pen.setStyle(Qt.PenStyle.SolidLine)
    pen.setWidthF(stroke)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # Qt measures arc angles in sixteenths of a degree, counter-clockwise.
    painter.drawArc(rect, int(start * 16), int(span * 16))

    end_angle = math.radians(start + span)
    centre_x = rect.center().x()
    centre_y = rect.center().y()
    radius = rect.width() / 2.0

    # Qt's y axis points down, so the sine term is negated throughout.
    tip_x = centre_x + radius * math.cos(end_angle)
    tip_y = centre_y - radius * math.sin(end_angle)

    # Tangent in the direction of increasing angle, and the outward radial.
    tangent = (-math.sin(end_angle), -math.cos(end_angle))
    radial = (math.cos(end_angle), -math.sin(end_angle))

    length = size * 0.19
    half_width = size * 0.15

    path = QPainterPath()
    path.moveTo(tip_x + tangent[0] * length, tip_y + tangent[1] * length)
    path.lineTo(tip_x + radial[0] * half_width, tip_y + radial[1] * half_width)
    path.lineTo(tip_x - radial[0] * half_width, tip_y - radial[1] * half_width)
    path.closeSubpath()

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(white)
    painter.drawPath(path)
