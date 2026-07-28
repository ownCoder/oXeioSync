"""Interactive download of the sync engine binary.

Wraps :mod:`oxeiosync.syncthing.binary` in a modal progress dialog so first run
can offer to fetch the engine instead of just reporting that it is missing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from .. import APP_NAME
from ..syncthing import binary
from ..syncthing.binary import SyncthingBinaryError

log = logging.getLogger(__name__)

#: Only report progress once this many bytes have arrived, to avoid flooding
#: the GUI thread with a signal per 64 KiB chunk.
PROGRESS_STEP = 512 * 1024


class _DownloadWorker(QThread):
    """Resolves the latest release, downloads it and installs the binary."""

    progress = Signal(int, int)
    stage = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cancelled = False
        self._last_reported = 0

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            self.stage.emit("Looking for the latest sync engine release…")
            release = binary.latest_release()

            self.stage.emit(f"Downloading sync engine {release.version}…")
            installed = binary.download_and_install(
                release,
                progress=self._on_progress,
                is_cancelled=lambda: self._cancelled,
            )
        except SyncthingBinaryError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - a dialog is better than a crash
            log.exception("Unexpected failure while downloading the sync engine")
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.done.emit(str(installed))

    def _on_progress(self, received: int, total: int) -> None:
        if received - self._last_reported < PROGRESS_STEP and received != total:
            return
        self._last_reported = received
        self.progress.emit(received, total)


def download_syncthing_interactive(parent: QWidget | None = None) -> Path | None:
    """Download and install the sync engine, showing progress. Returns its path.

    Returns None if the user cancelled or the download failed; in the failure
    case the reason has already been shown to the user.
    """
    dialog = QProgressDialog(parent)
    dialog.setWindowTitle(f"{APP_NAME} — Downloading sync engine")
    dialog.setLabelText("Contacting the release server…")
    dialog.setMinimumWidth(420)
    dialog.setRange(0, 0)  # Indeterminate until the content length is known.
    # Left to its own devices the dialog would vanish the moment the byte count
    # hit the maximum, before verification and install had finished.
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)

    worker = _DownloadWorker(dialog)
    result: dict[str, str] = {}

    def on_progress(received: int, total: int) -> None:
        if total > 0:
            dialog.setRange(0, total)
            dialog.setValue(received)
            dialog.setLabelText(
                f"Downloading sync engine… {received / 1_048_576:.1f} MB "
                f"of {total / 1_048_576:.1f} MB"
            )
        else:
            dialog.setLabelText(
                f"Downloading sync engine… {received / 1_048_576:.1f} MB"
            )

    def on_failed(reason: str) -> None:
        result["error"] = reason
        dialog.reject()

    def on_done(path: str) -> None:
        result["path"] = path
        dialog.accept()

    worker.stage.connect(dialog.setLabelText)
    worker.progress.connect(on_progress)
    worker.failed.connect(on_failed)
    worker.done.connect(on_done)
    dialog.canceled.connect(worker.cancel)

    worker.start()
    dialog.exec()

    # The dialog can close before the thread has unwound; make sure it has.
    worker.cancel()
    worker.wait(15_000)

    if "path" in result:
        return Path(result["path"])

    error = result.get("error")
    if error:
        QMessageBox.warning(
            parent,
            f"{APP_NAME} — Download failed",
            f"The sync engine could not be downloaded.\n\n{error}",
        )
    return None
