"""Tests for the folders card: the order it puts things in, and how it draws them.

The card's whole claim is that the first row is the worst thing happening. That
claim lives in one pure function, so it is the one worth pinning down: a screen
full of healthy folders must never push an error below the fold.

The order being right was not enough, though. Both defects the design review
found lived in the rendering — a tint that never painted, and sizes that sat
beside the wrong folder — so the card is also rendered and measured.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from oxeiosync.syncthing.state import FolderState
from oxeiosync.ui.dashboard import DashboardPage

sort = DashboardPage._sort_folders


def folder(name: str, **kwargs) -> FolderState:
    return FolderState(id=name.lower(), label=name, **kwargs)


def names(folders: list[FolderState]) -> list[str]:
    return [f.display_name for f in folders]


def test_a_healthy_folder_is_not_asking_for_anything():
    wants, healthy = sort([folder("Aman", global_bytes=2_400_000_000)])

    assert names(wants) == []
    assert names(healthy) == ["Aman"]


def test_errors_come_before_everything_else():
    wants, _healthy = sort([
        folder("Zeta", paused=True),
        folder("Alpha", completion=38.0),
        folder("Office Files", error_count=2310),
    ])

    assert names(wants)[0] == "Office Files"


def test_the_order_is_errors_then_syncing_then_scanning_then_paused():
    wants, _healthy = sort([
        folder("Paused one", paused=True),
        folder("Scanning one", state="scanning"),
        folder("Syncing one", completion=38.0),
        folder("Broken one", error_count=1),
    ])

    assert names(wants) == ["Broken one", "Syncing one", "Scanning one", "Paused one"]


def test_folders_in_the_same_state_stay_alphabetical():
    wants, _healthy = sort([
        folder("zulu", completion=10.0),
        folder("Alpha", completion=90.0),
        folder("mike", completion=50.0),
    ])

    assert names(wants) == ["Alpha", "mike", "zulu"]


def test_an_error_outranks_a_folder_that_is_also_behind():
    """A folder can be both; the reason shown must be the one that matters."""
    wants, healthy = sort([folder("Office Files", error_count=2310, completion=41.0)])

    assert names(wants) == ["Office Files"]
    assert healthy == []


def test_a_paused_folder_is_never_counted_as_up_to_date():
    """It is at 100% of nothing — reporting it as fine would be a small lie."""
    _wants, healthy = sort([folder("Archive", paused=True, completion=100.0)])

    assert healthy == []


def test_twenty_healthy_folders_and_one_error_put_the_error_first():
    """The shape of the real screenshot that started this."""
    folders = [folder(f"Folder {i}", global_bytes=1_000_000) for i in range(20)]
    folders.insert(11, folder("Office Files", error_count=2310))

    wants, healthy = sort(folders)

    assert names(wants) == ["Office Files"]
    assert len(healthy) == 20


def test_nothing_is_lost_in_the_split():
    folders = [
        folder("a", error_count=3),
        folder("b", paused=True),
        folder("c", completion=12.0),
        folder("d"),
        folder("e", state="scanning"),
    ]

    wants, healthy = sort(folders)

    assert len(wants) + len(healthy) == len(folders)
    assert set(names(wants)) | set(names(healthy)) == {"a", "b", "c", "d", "e"}


# -------------------------------------------------------------------- rendering
@pytest.fixture(scope="module")
def rendered() -> dict:
    """The card drawn at an ordinary width and a very wide one, measured.

    Rendered in a child process: the suite runs on a ``QCoreApplication``, and
    widgets need a ``QApplication``, of which Qt allows one per process. The
    offscreen platform means no display is needed.
    """
    from oxeiosync import paths

    result = subprocess.run(
        [sys.executable, "-c", _CHILD_RENDER.format(root=str(paths.install_dir()))],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("width", ["1500", "2600"])
def test_an_attention_row_is_drawn_on_a_ground_of_its_own(rendered, width):
    """The tint is what makes a folder in trouble read as a contained object.

    It once shipped invisible: set by stylesheet on a plain QWidget, which Qt
    ignores, so the row band was byte-identical to the card around it.
    """
    for name, share in rendered[width]["attention"].items():
        assert share < 0.5, (
            f"{share:.0%} of the {name!r} row is bare card surface — its ground is not painted"
        )


@pytest.mark.parametrize("width", ["1500", "2600"])
def test_each_size_sits_closer_to_its_own_folder_than_to_the_next(rendered, width):
    """Proximity is the only thing pairing a name with its size in this grid.

    Giving the name column the stretch put "Aman" 372px from its size and 13px
    from the next folder's dot, and a wider window made it worse.
    """
    pairs = rendered[width]["healthy"]
    assert pairs, "no up-to-date folders were drawn"
    for pair in pairs:
        if pair["to_next"] is None:
            continue
        assert pair["to_own"] < pair["to_next"], (
            f"{pair['name']!r}: size is {pair['to_own']}px from its name "
            f"but {pair['to_next']}px from the next folder"
        )


_CHILD_RENDER = textwrap.dedent(
    """
    import json, os, sys
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, r"{root}")

    from PySide6.QtCore import QObject, QPoint, Signal
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication

    from oxeiosync.syncthing.state import FolderState
    from oxeiosync.ui.dashboard import HEALTHY_COLUMNS, DashboardPage
    from oxeiosync.ui.theme import DARK, apply_dark_theme

    class Snapshot:
        devices = {{}}

    class State(QObject):
        changed = Signal()
        snapshot = Snapshot()
        def __init__(self, folders):
            super().__init__()
            self._folders = folders
        def folders(self):
            return sorted(self._folders, key=lambda f: f.display_name.lower())
        def devices(self):
            return []

    class Sampler(QObject):
        sampled = Signal(object)
        def history(self):
            return []

    app = QApplication([])
    apply_dark_theme(app)

    labels = ["Aman", "Design Team", "Downloads", "Hafiz", "MBA BackUp", "Munni",
              "Office Files", "Pre Upload", "Rony", "Sadia Akhter", "Upload"]
    folders = [FolderState(id=n.lower(), label=n, global_bytes=(i + 1) * 470_000)
               for i, n in enumerate(labels)]
    folders.append(FolderState(id="video", label="Video", paused=True))
    folders.append(FolderState(id="broken", label="Broken", error_count=2310))

    page = DashboardPage(State(folders), Sampler())
    surface = QColor(DARK.surface).rgb()
    out = {{}}

    for width in (1500, 2600):
        page.resize(width, 980)
        page.show()
        app.processEvents()
        image = page.grab().toImage()

        attention = {{}}
        for row in page._attention_rows:
            if not row.isVisible():
                continue
            origin = row.mapTo(page, QPoint(0, 0))
            bare = total = 0
            for y in range(origin.y() + 2, origin.y() + row.height() - 2):
                for x in range(origin.x() + 10, origin.x() + row.width() - 10):
                    total += 1
                    bare += image.pixelColor(x, y).rgb() == surface
            attention[row._name.text()] = bare / total

        visible = [r for r in page._healthy_rows if r[0].isVisible()]
        healthy = []
        for index, (dot, name, size) in enumerate(visible):
            metrics = name.fontMetrics()
            name_end = name.mapTo(page, QPoint(0, 0)).x() + metrics.horizontalAdvance(name.text())
            size_right = size.mapTo(page, QPoint(0, 0)).x() + size.width()
            size_start = size_right - metrics.horizontalAdvance(size.text())
            to_next = None
            if (index + 1) % HEALTHY_COLUMNS and index + 1 < len(visible):
                to_next = visible[index + 1][0].mapTo(page, QPoint(0, 0)).x() - size_right
            healthy.append({{"name": name.text(), "to_own": size_start - name_end,
                             "to_next": to_next}})

        out[str(width)] = {{"attention": attention, "healthy": healthy}}

    print(json.dumps(out))
    """
)
