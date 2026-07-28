"""Build the application's ``.ico`` from the icon the app paints at runtime.

The tray icon is drawn with QPainter rather than shipped as a file, so the
executable's icon is generated from that same code — one definition, and the
Explorer icon can never drift from the tray icon.

Qt can write a single-image ``.ico``, but Windows then downscales that one image
for every size and 16px comes out muddy. So the container is assembled here from
PNGs rendered natively at each size. PNG-encoded entries are valid ICO from
Windows Vista onwards.

    python tools/make_icon.py [output.ico]
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

#: Sizes Windows asks for: Explorer small/medium/large, taskbar, and the big one
#: used by the shell and installers.
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "packaging" / "oxeiosync.ico"


def render_png(size: int) -> bytes:
    """The app icon at ``size``, PNG-encoded."""
    from oxeiosync.syncthing.state import SyncStatus
    from oxeiosync.ui.icons import render_pixmap

    pixmap = render_pixmap(SyncStatus.IDLE, size)

    # The QByteArray must outlive the QBuffer that writes into it: QBuffer holds
    # a pointer to it, so passing a temporary crashes the interpreter outright.
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not pixmap.save(buffer, "PNG"):
            raise RuntimeError(f"could not encode the {size}px icon")
    finally:
        buffer.close()
    return bytes(storage)


def build_ico(images: dict[int, bytes]) -> bytes:
    """Assemble an ICO container from per-size PNG payloads."""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)  # reserved, type=icon, count

    # Payloads start after the header and one 16-byte directory entry each.
    offset = len(header) + 16 * count
    entries = bytearray()
    payloads = bytearray()

    for size in sorted(images):
        data = images[size]
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 0 means 256 in this field
            size if size < 256 else 0,
            0,  # palette size: 0 for true colour
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(data),
            offset,
        )
        payloads += data
        offset += len(data)

    return header + bytes(entries) + bytes(payloads)


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)

    # A GUI application is needed before any QPixmap can be created.
    app = QGuiApplication(argv or ["make_icon"])
    try:
        images = {size: render_png(size) for size in SIZES}
        output.write_bytes(build_ico(images))
    finally:
        del app

    total = output.stat().st_size
    print(f"wrote {output} ({total:,} bytes, {len(SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
