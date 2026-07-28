"""Build the application's icon from the icon the app paints at runtime.

The tray icon is drawn with QPainter rather than shipped as a file, so the
platform icon is generated from that same code — one definition, and the icon
Explorer or Finder shows can never drift from the tray icon.

The container format follows the platform: a Windows ``.ico`` assembled here
from per-size PNGs (a single-image .ico downscales muddily at 16px), or a macOS
``.icns`` built with ``iconutil`` from a rendered ``.iconset``. ``iconutil``
ships with every macOS, the same way this build already leans on Inno Setup on
Windows.

    python tools/make_icon.py [output.ico|output.icns]

With no argument it writes the icon its own platform needs.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

#: Sizes Windows asks for: Explorer small/medium/large, taskbar, and the big one
#: used by the shell and installers.
ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)

#: The .iconset members macOS expects, as (size, filename). The @2x entries are
#: the same pixels as the next size up, named for the Retina slot they fill.
ICONSET_MEMBERS = (
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
)

PACKAGING = Path(__file__).resolve().parent.parent / "packaging"


def default_output() -> Path:
    """The icon this platform's packaging wants."""
    if sys.platform == "darwin":
        return PACKAGING / "oxeiosync.icns"
    return PACKAGING / "oxeiosync.ico"


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


def write_ico(output: Path) -> None:
    images = {size: render_png(size) for size in ICO_SIZES}
    output.write_bytes(build_ico(images))
    print(f"wrote {output} ({output.stat().st_size:,} bytes, {len(ICO_SIZES)} sizes)")


def write_icns(output: Path) -> None:
    """Render an ``.iconset`` and let ``iconutil`` fold it into an ``.icns``.

    iconutil is part of macOS, so this needs nothing installed — but it is macOS
    only, which is why it is reached through a platform branch rather than run
    unconditionally.
    """
    with tempfile.TemporaryDirectory(prefix="oxeiosync-iconset-") as tmp:
        iconset = Path(tmp) / "oxeiosync.iconset"
        iconset.mkdir()
        # Render each distinct size once, then write every member that uses it.
        rendered: dict[int, bytes] = {}
        for size, name in ICONSET_MEMBERS:
            if size not in rendered:
                rendered[size] = render_png(size)
            (iconset / name).write_bytes(rendered[size])

        result = subprocess.run(
            ["iconutil", "--convert", "icns", str(iconset), "--output", str(output)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"iconutil failed: {result.stderr.strip() or result.stdout.strip()}")

    print(
        f"wrote {output} ({output.stat().st_size:,} bytes, "
        f"{len({s for s, _ in ICONSET_MEMBERS})} sizes)"
    )


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else default_output()
    output.parent.mkdir(parents=True, exist_ok=True)

    # A GUI application is needed before any QPixmap can be created.
    app = QGuiApplication(argv or ["make_icon"])
    try:
        if output.suffix.lower() == ".icns":
            write_icns(output)
        else:
            write_ico(output)
    finally:
        del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
