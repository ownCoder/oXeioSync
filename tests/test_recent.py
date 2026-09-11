"""Tests for the Recent tab: which images it keeps, and how it draws them.

The feed it reads is noisy — a busy export reports the same few files hundreds
of times, plus every non-image beside them — so most of what matters is what
gets thrown away, and that the one that is kept is the latest.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from oxeiosync.syncthing import events as events_module
from oxeiosync.syncthing import recent
from oxeiosync.syncthing.api import SyncthingUnavailableError
from oxeiosync.syncthing.events import EventPoller
from oxeiosync.syncthing.recent import RecentFiles, fold, format_ago, newest, parse_time

_ids = iter(range(1, 1_000_000))


def change(path: str, action: str = "modified", *, folder: str = "f1", remote: bool = False,
           kind: str = "file", time: str = "2026-09-11T22:36:35.174313+06:00") -> dict:
    return {
        "id": next(_ids),
        "type": "RemoteChangeDetected" if remote else "LocalChangeDetected",
        "time": time,
        "data": {
            "action": action, "folder": folder, "folderID": folder, "label": "Design",
            "modifiedBy": "KKJ6OVY", "path": path, "type": kind,
        },
    }


def names(entries) -> list[str]:
    return [entry.name for entry in newest(entries)]


# ------------------------------------------------------------------ which files
def test_only_images_are_kept():
    entries = {}
    fold(entries, [change("a.png"), change("notes.txt"), change("b.JPG"), change("c.psd")])

    assert sorted(names(entries)) == ["a.png", "b.JPG"]


def test_every_image_format_qt_reads_counts():
    entries = {}
    fold(entries, [change(f"x{suffix}") for suffix in sorted(recent.IMAGE_SUFFIXES)])

    assert len(entries) == len(recent.IMAGE_SUFFIXES)


def test_a_directory_named_like_an_image_is_not_one():
    entries = {}
    fold(entries, [change("exports.png", kind="dir")])

    assert entries == {}


def test_a_file_changed_many_times_is_one_tile_at_the_front():
    """The shape of a real export: the same artboard, over and over."""
    entries = {}
    fold(entries, [change("logo.png"), change("banner.png"), change("logo.png")])

    assert names(entries) == ["logo.png", "banner.png"]


def test_the_newest_change_comes_first():
    entries = {}
    fold(entries, [change("old.png"), change("middle.png"), change("new.png")])

    assert names(entries) == ["new.png", "middle.png", "old.png"]


def test_a_deleted_image_leaves_the_list():
    entries = {}
    fold(entries, [change("gone.png"), change("kept.png"), change("gone.png", "deleted")])

    assert names(entries) == ["kept.png"]


def test_the_same_name_in_two_folders_is_two_files():
    entries = {}
    fold(entries, [change("cover.png", folder="a"), change("cover.png", folder="b")])

    assert len(entries) == 2


def test_a_change_from_another_device_is_marked_as_such():
    entries = {}
    fold(entries, [change("here.png"), change("there.png", remote=True)])

    by_name = {entry.name: entry for entry in entries.values()}
    assert not by_name["here.png"].remote
    assert by_name["there.png"].remote


def test_malformed_events_are_ignored_not_fatal():
    entries = {}
    fold(entries, [
        {"type": "LocalChangeDetected"},
        {"type": "LocalChangeDetected", "data": "nonsense"},
        {"type": "LocalChangeDetected", "id": "x", "data": {"type": "file", "folder": "f",
                                                            "path": "a.png"}},
        {"type": "StateChanged", "data": {"folder": "f"}},
        "not even a dict",
        change("fine.png"),
    ])

    assert names(entries) == ["fine.png"]


def test_a_name_is_the_last_part_of_either_kind_of_path():
    entries = {}
    fold(entries, [change("sub\\dir\\win.png"), change("sub/dir/posix.png")])

    assert sorted(names(entries)) == ["posix.png", "win.png"]


def test_the_list_is_capped():
    model = RecentFiles()
    model.reset([change(f"{n}.png") for n in range(recent.MAX_RECENT + 50)])

    items = model.items()
    assert len(items) == recent.MAX_RECENT
    assert items[0].name == f"{recent.MAX_RECENT + 49}.png"


# ------------------------------------------------------------------ the model
def test_reset_replaces_rather_than_adds():
    model = RecentFiles()
    model.reset([change("from-the-last-engine.png")])
    model.reset([change("current.png")])

    assert [entry.name for entry in model.items()] == ["current.png"]


def test_changes_that_are_not_images_do_not_announce_anything():
    model = RecentFiles()
    fired = []
    model.changed.connect(lambda: fired.append(True))

    model.add(change("readme.md"))
    model.add(change("readme.md", "deleted"))
    assert fired == []

    model.add(change("art.png"))
    assert fired == [True]


# ------------------------------------------------------------------ time
def test_nanosecond_timestamps_are_read():
    """macOS and Linux engines send nine fractional digits."""
    parsed = parse_time("2026-09-11T22:36:35.174313123+06:00")

    assert parsed == datetime(2026, 9, 11, 16, 36, 35, 174313, tzinfo=UTC)


@pytest.mark.parametrize("value", ["2026-09-11T16:36:35Z", "2026-09-11T22:36:35.1+06:00"])
def test_other_rfc3339_forms_are_read(value):
    assert parse_time(value) is not None


def test_an_unreadable_timestamp_is_no_time_rather_than_an_error():
    assert parse_time("yesterday-ish") is None
    assert format_ago(None) == ""


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(5, "just now"), (125, "2 min ago"), (7300, "2 h ago"), (90_000, "yesterday"),
     (3 * 86400 + 5, "3 days ago"), (-30, "just now")],
)
def test_how_long_ago(seconds, expected):
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    assert format_ago(now - timedelta(seconds=seconds), now) == expected


# ------------------------------------------------------------------ the feed
class _FakeApi:
    """Answers polls from a script, and stops the poller when it runs out.

    A scripted answer may be an exception, raised as the engine's would be, and
    each replay first asks which engine run it is talking to.
    """

    def __init__(self, poller: EventPoller, answers: list, start_times: list[str]) -> None:
        self.poller = poller
        self.answers = answers
        self.start_times = start_times
        self.calls: list[dict] = []
        self.status_calls = 0

    def system_status(self):
        started = self.start_times[min(self.status_calls, len(self.start_times) - 1)]
        self.status_calls += 1
        return {"startTime": started}

    def disk_events(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) > len(self.answers):
            # Out of script: stop on a quiet poll, so nothing scripted is cut off.
            self.poller.stop()
            return []
        answer = self.answers[len(self.calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def events(self, **kwargs):  # pragma: no cover - must not be used
        raise AssertionError("a disk poller polled the general feed")

    def close(self):
        pass


def _run(answers: list, start_times: list[str] | None = None) -> tuple[_FakeApi, list, list]:
    poller = EventPoller("http://127.0.0.1:1", "key", disk=True, backlog=500)
    api = _FakeApi(poller, answers, start_times or ["run-1"])
    poller._api = api
    backlogs, events = [], []
    poller.backlog_received.connect(backlogs.append)
    poller.event_received.connect(events.append)
    poller.run()  # synchronously, on this thread
    return api, backlogs, events


@pytest.fixture(autouse=True)
def _no_reconnect_delay(monkeypatch):
    monkeypatch.setattr(events_module, "RECONNECT_DELAY", 0)


def test_a_backlog_poller_replays_what_the_engine_remembers_then_follows():
    remembered = [change("a.png"), change("b.png")]
    later = change("c.png")

    api, backlogs, events = _run([remembered, [later]])

    assert api.calls[0] == {"since": 0, "limit": 500, "timeout": 0}
    assert backlogs == [remembered]
    assert api.calls[1]["since"] == remembered[-1]["id"]
    assert events == [later]


def test_live_polls_for_history_are_not_capped():
    """The engine keeps the newest `limit` events; a cap loses a burst's start."""
    api, _backlogs, _events = _run([[change("a.png")], [change("b.png")]])

    assert api.calls[1]["limit"] is None


def test_an_empty_backlog_is_still_an_answer_and_does_not_spin():
    """Nothing remembered must not mean asking again and again at timeout=0."""
    api, backlogs, _events = _run([[], []])

    assert backlogs == [[]]
    assert api.calls[1]["timeout"] > 0


def test_a_dropped_connection_to_the_same_engine_resumes_instead_of_replacing():
    """A reconnect used to replay over everything, wiping what the tab held."""
    a, b, c, d = (change(name) for name in ("a.png", "b.png", "c.png", "d.png"))

    api, backlogs, events = _run(
        [[a, b], SyncthingUnavailableError("connection dropped"), [a, b, c, d]],
        start_times=["run-1", "run-1"],
    )

    assert backlogs == [[a, b]]
    assert events == [c, d]
    assert api.calls[2] == {"since": 0, "limit": 500, "timeout": 0}


def test_resuming_past_what_the_engine_still_remembers_starts_over():
    """If the buffer moved on while disconnected, deletions may be among the lost."""
    first = [change("a.png"), change("b.png")]
    next(_ids), next(_ids)  # two events the engine forgot while we were away
    remembered = [change("c.png"), change("d.png")]

    _api, backlogs, events = _run(
        [first, SyncthingUnavailableError("connection dropped"), remembered],
        start_times=["run-1", "run-1"],
    )

    assert backlogs == [first, remembered]
    assert events == []


def test_a_restarted_engine_replaces_what_the_last_one_reported():
    """A new run numbers its events from one again; merging would mix two orders."""
    old = [change("old.png"), change("older.png")]
    new = [change("new.png")]

    _api, backlogs, events = _run(
        [old, SyncthingUnavailableError("engine restarted"), new],
        start_times=["run-1", "run-2"],
    )

    assert backlogs == [old, new]
    assert events == []


# ------------------------------------------------------------------ rendering
@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    """The tab drawn from real image files, driven through what a busy folder does.

    In a child process, as the dashboard's rendering tests are: widgets need a
    QApplication, and the suite already runs on a QCoreApplication.
    """
    from oxeiosync import paths

    folder = tmp_path_factory.mktemp("folder")
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_RENDER, str(paths.install_dir()), str(folder)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _near(pixel: list[int], colour: tuple[int, int, int]) -> bool:
    return all(abs(p - c) < 40 for p, c in zip(pixel, colour, strict=True))


def test_the_newest_image_is_the_first_tile_and_is_drawn(rendered):
    assert rendered["names"][:3] == ["blue.png", "green.png", "red.png"]
    assert _near(rendered["first_tile"], (0, 0, 255)), rendered["first_tile"]


def test_a_file_no_longer_on_disk_says_so_instead_of_a_blank(rendered):
    assert rendered["missing_status"] == "missing"


def test_a_re_exported_image_keeps_its_picture_until_the_new_one_is_ready(rendered):
    """It used to go blank on every change, which during an export is always."""
    assert _near(rendered["while_redecoding"], (0, 0, 255)), rendered["while_redecoding"]
    assert _near(rendered["after_redecode"], (255, 255, 0)), rendered["after_redecode"]


def test_many_changes_to_one_file_do_not_queue_a_decode_each(rendered):
    assert rendered["decodes_for_five_changes"] <= 2, rendered


def test_a_restarted_engine_reusing_an_id_does_not_bring_back_the_old_picture(rendered):
    """Same path, same event ID, a later run: the picture must be the new file."""
    assert _near(rendered["after_restart"], (255, 0, 255)), rendered["after_restart"]


def test_a_selected_tile_moving_does_not_scroll_the_grid_back_to_it(rendered):
    scroll = rendered["scroll"]
    assert scroll["after"] == scroll["before"] > 0, scroll
    assert scroll["selected_after"] == scroll["selected_before"], scroll


def test_a_double_click_opens_the_file_that_was_pressed(rendered):
    """Not whichever file a redraw moved into that row between the clicks."""
    click = rendered["double_click"]
    assert click["under_pointer_now"] != click["pressed"], click
    assert click["opened"] == [click["pressed"]], click


def test_the_tiles_share_the_whole_width(rendered):
    """No ragged gap on the right: whatever is left over is less than a tile.

    Drawn at a width where five tiles fit exactly — the case where the view
    wrapped the row and left a whole tile's width empty.
    """
    assert rendered["right_gap"] < recent_tile_width(), rendered


def test_with_nothing_to_show_the_tab_says_why(rendered):
    assert "not running" in rendered["stopped_text"]


def test_one_file_under_two_folders_is_one_tile_and_decodes_once(rendered):
    """A folder shared inside another reports each change twice.

    Two tiles for one path took turns replacing each other's version in the
    cache, decoding the same picture back to back with nothing changing.
    """
    nested = rendered["nested"]
    assert nested["tiles"] == 1, nested
    assert nested["decodes_while_idle"] == 0, nested


def test_a_new_version_that_cannot_be_read_does_not_pass_for_the_old_picture(rendered):
    """Kept briefly, in case the export was still being written; then said plainly."""
    broken = rendered["unreadable"]
    assert _near(broken["at_first"], (0, 255, 0)), broken
    assert broken["after_retry"] == "unreadable", broken
    assert not _near(broken["tile_after_retry"], (0, 255, 0)), broken


def test_a_tile_with_no_picture_is_decoded_before_one_only_being_refreshed(rendered):
    assert rendered["priority"]["third_started"] == "blank.png", rendered["priority"]


def test_the_list_shrinking_under_a_current_tile_does_not_scroll_to_it(rendered):
    assert rendered["shrink"]["after"] == rendered["shrink"]["before"] == 0, rendered["shrink"]


def test_enter_does_not_open_a_file_that_moved_into_a_deleted_files_row(rendered):
    assert rendered["enter_after_delete"] == [], rendered["enter_after_delete"]


def test_where_one_click_opens_a_tile_a_double_click_opens_it_once(rendered):
    assert len(rendered["single_click_style"]) == 1, rendered["single_click_style"]


def test_quitting_drops_queued_thumbnails_instead_of_decoding_them(rendered):
    """Exit used to wait while every queued artboard was decoded for nobody."""
    quit_ = rendered["shutdown"]
    assert quit_["queued_before"] > quit_["workers"], quit_
    # Dropping the queue does not block; the window can go before the wait.
    assert quit_["seconds"] < 0.2, quit_
    assert quit_["in_flight_finished"], quit_
    assert quit_["started_after"] <= quit_["workers"], quit_
    assert not quit_["busy_after_request"], quit_


def recent_tile_width() -> int:
    from oxeiosync.ui.recent import TILE

    return TILE.width()


_CHILD_RENDER = textwrap.dedent(
    """
    import json, os, sys, threading, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    root, folder = sys.argv[1], sys.argv[2]
    sys.path.insert(0, root)

    from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal
    from PySide6.QtGui import QColor, QImage, QMouseEvent, QPixmap
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

    from oxeiosync.syncthing.recent import RecentFiles
    from oxeiosync.syncthing.state import FolderState, Snapshot, SyncStatus
    from oxeiosync.ui import recent as page_module
    from oxeiosync.ui.theme import apply_dark_theme

    T1 = "2026-09-11T10:00:00+00:00"
    T2 = "2026-09-11T11:00:00+00:00"
    LEFT, PLAIN = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    next_id = iter(range(1000, 1_000_000))

    def save(name, colour):
        image = QImage(640, 480, QImage.Format.Format_RGB32)
        image.fill(QColor(colour))
        image.save(os.path.join(folder, name))

    def ev(name, action="added", *, id=None, time=T1, remote=False, folder_id="f"):
        return {"id": next(next_id) if id is None else id,
                "type": "RemoteChangeDetected" if remote else "LocalChangeDetected",
                "time": time,
                "data": {"type": "file", "action": action, "folder": folder_id, "path": name}}

    class State(QObject):
        changed = Signal()
        status_changed = Signal(object)
        def __init__(self):
            super().__init__()
            self.snapshot = Snapshot()
            self.snapshot.folders["f"] = FolderState(id="f", label="Design", path=folder)
            # Shared separately, inside the first.
            self.snapshot.folders["g"] = FolderState(
                id="g", label="Exports", path=os.path.join(folder, "exports"))
            self.status = SyncStatus.IDLE

    # Count decodes, and optionally slow them, by wrapping the real loader.
    real_load = page_module.load_thumbnail
    decodes = []
    lock = threading.Lock()
    delay = {"seconds": 0.0}
    slower = {}  # per file name, overriding the delay
    def counting_load(path, box):
        name = os.path.basename(path)
        with lock:
            decodes.append(name)
        seconds = slower.get(name, delay["seconds"])
        if seconds:
            time.sleep(seconds)
        return real_load(path, box)
    page_module.load_thumbnail = counting_load
    # The page connects double-clicks to this when it is built; a test must not
    # hand files to whatever the machine opens images with. Recorded instead.
    file_opens = []
    page_module.open_file = file_opens.append

    app = QApplication([])
    apply_dark_theme(app)
    model = RecentFiles()
    state = State()
    page = page_module.RecentPage(state, model)
    page.resize(1000, 700)
    page.show()
    view = page._view
    cache = page._cache

    def redraw():
        page._rebuild()
        view.viewport().repaint()
        app.processEvents()

    def settle(timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view.viewport().repaint()
            app.processEvents()
            if not cache.busy():
                break
            time.sleep(0.01)
        view.viewport().repaint()
        app.processEvents()

    def row_of(name):
        return next(i for i in range(page._model.rowCount())
                    if page._model.item(i).text() == name)

    def tile_centre(row):
        image = view.viewport().grab().toImage()
        rect = view.visualRect(page._model.index(row, 0))
        pixel = image.pixelColor(QPoint(
            rect.center().x(),
            rect.top() + page_module.TILE_MARGIN + page_module.TILE_PADDING
            + page_module.THUMB_HEIGHT // 2,
        ))
        return [pixel.red(), pixel.green(), pixel.blue()]

    out = {}

    # -- three images and one that is gone
    save("red.png", "#ff0000"); save("green.png", "#00ff00"); save("blue.png", "#0000ff")
    model.reset([ev("gone.png", id=1, remote=True), ev("red.png", id=2),
                 ev("green.png", id=3), ev("blue.png", id=4)])
    redraw(); settle()
    out["names"] = [page._model.item(i).text() for i in range(page._model.rowCount())]
    out["first_tile"] = tile_centre(0)
    gone = page._model.item(row_of("gone.png")).data(page_module.ROLE_PATH)
    out["missing_status"] = cache.get(gone)[0]

    # -- re-export blue.png as yellow; its tile must not blank while it decodes
    save("blue.png", "#ffff00")
    delay["seconds"] = 0.6
    model.add(ev("blue.png", "modified"))
    redraw()
    out["while_redecoding"] = tile_centre(row_of("blue.png"))
    settle()
    delay["seconds"] = 0.0
    out["after_redecode"] = tile_centre(row_of("blue.png"))

    # -- five quick changes to one file: at most the running one and the newest
    delay["seconds"] = 0.3
    before = len(decodes)
    for _ in range(5):
        model.add(ev("green.png", "modified"))
        redraw()
    settle()
    delay["seconds"] = 0.0
    out["decodes_for_five_changes"] = decodes[before:].count("green.png")

    # -- the engine restarts and reuses red.png's ID for a new version of it
    save("red.png", "#ff00ff")
    model.clear()
    model.reset([ev("red.png", id=2, time=T2)])
    redraw(); settle()
    out["after_restart"] = tile_centre(0)

    # -- a selected tile moving must not scroll the grid back to it
    model.reset([ev(f"s{i:02d}.png", id=i + 1) for i in range(60)])
    redraw()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     view.visualRect(page._model.index(2, 0)).center())
    selected_before = view.selectionModel().selectedIndexes()[0].data()
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum())
    app.processEvents()
    scroll_before = bar.value()
    model.add(ev("s10.png", "modified"))   # an older file jumps to the front
    redraw()
    chosen = view.selectionModel().selectedIndexes()
    out["scroll"] = {"before": scroll_before, "after": bar.value(),
                     "selected_before": selected_before,
                     "selected_after": chosen[0].data() if chosen else None}
    bar.setValue(0)
    app.processEvents()
    view.clearSelection()

    # -- press on a tile, a redraw moves another file into that row, second click
    opened = []
    view.open_requested.connect(opened.append)
    spot = view.visualRect(page._model.index(4, 0)).center()
    pressed = page._model.index(4, 0).data(page_module.ROLE_PATH)
    QTest.mousePress(view.viewport(), LEFT, PLAIN, spot)
    QTest.mouseRelease(view.viewport(), LEFT, PLAIN, spot)
    model.add(ev("s50.png", "modified"))
    redraw()
    now_under = view.indexAt(spot).data(page_module.ROLE_PATH)
    global_spot = view.viewport().mapToGlobal(spot)
    QApplication.sendEvent(view.viewport(), QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(spot), QPointF(global_spot), LEFT, LEFT, PLAIN))
    QTest.mouseRelease(view.viewport(), LEFT, PLAIN, spot)
    out["double_click"] = {"pressed": pressed, "under_pointer_now": now_under, "opened": opened}

    # -- more tiles than fit on one line, at a width five tiles fill exactly
    model.reset([ev(f"x{i}.png", id=i + 1) for i in range(12)])
    redraw()
    bar_width = view.verticalScrollBar().sizeHint().width()
    margins = page.layout().contentsMargins()
    page.resize(5 * page_module.TILE.width() + bar_width + margins.left() + margins.right(), 700)
    for _ in range(5):
        app.processEvents()
    tops = [view.visualRect(page._model.index(i, 0)).top() for i in range(page._model.rowCount())]
    out["right_gap"] = view.viewport().width() - tops.count(tops[0]) * view.gridSize().width()

    # -- nothing to show
    state.status = SyncStatus.STOPPED
    model.reset([])
    redraw()
    out["stopped_text"] = page._empty.text()
    state.status = SyncStatus.IDLE
    page.resize(1000, 700)
    app.processEvents()

    def idle_for(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            view.viewport().repaint()
            app.processEvents()
            time.sleep(0.02)

    # -- one file, reported by both a folder and the folder shared inside it
    os.makedirs(os.path.join(folder, "exports"), exist_ok=True)
    hero = QImage(640, 480, QImage.Format.Format_RGB32)
    hero.fill(QColor("#00ffff"))
    hero.save(os.path.join(folder, "exports", "hero.png"))
    model.reset([ev(os.path.join("exports", "hero.png"), id=1),
                 ev("hero.png", id=2, folder_id="g")])
    redraw(); settle()
    quiet = len(decodes)
    idle_for(1.0)
    out["nested"] = {"tiles": page._model.rowCount(), "decodes_while_idle": len(decodes) - quiet}

    # -- a changed file that cannot be read: old picture briefly, then "No preview"
    page_module.UNREADABLE_RETRY_MS = 300
    save("art.png", "#00ff00")
    art = os.path.join(folder, "art.png")
    model.reset([ev("art.png", id=1)])
    redraw(); settle()
    with open(art, "wb") as broken:
        broken.write(b"not a picture")
    model.add(ev("art.png", "modified"))
    redraw(); settle()
    at_first = tile_centre(0)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and cache.get(art)[0] != "unreadable":
        idle_for(0.05)
    idle_for(0.1)
    out["unreadable"] = {"at_first": at_first, "after_retry": cache.get(art)[0],
                         "tile_after_retry": tile_centre(0)}

    # -- with both workers busy: a tile with no picture before a mere refresh.
    # One worker frees well before the other, so exactly one slot opens first
    # and the order is the cache's choice, not a race between two threads.
    delay["seconds"] = 0.3
    slower["busy2.png"] = 1.5
    other = page_module.ThumbnailCache()
    box = page_module.thumb_box(view)
    here = lambda name: os.path.join(folder, name)
    other.request(here("busy1.png"), "v", box, 0)
    other.request(here("busy2.png"), "v", box, 1)
    other._entries[here("shown.png")] = page_module._Thumb(
        version="old", status="ready", pixmap=QPixmap(4, 4))
    other.request(here("shown.png"), "new", box, 0)
    other.request(here("blank.png"), "v", box, 9)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and other.busy():
        app.processEvents()
        time.sleep(0.01)
    watched = {"busy1.png", "busy2.png", "shown.png", "blank.png"}
    order = [name for name in decodes if name in watched]
    out["priority"] = {"order": order, "third_started": order[2] if len(order) > 2 else None}
    delay["seconds"] = 0.0
    slower.clear()

    # -- the list shrinks while the current tile is the last one
    model.reset([ev(f"k{i:02d}.png", id=i + 1) for i in range(60)])
    redraw()
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum())
    app.processEvents()
    last = page._model.index(page._model.rowCount() - 1, 0)
    QTest.mouseClick(view.viewport(), LEFT, PLAIN, view.visualRect(last).center())
    bar.setValue(0)
    app.processEvents()
    model.add(ev("k59.png", "deleted"))
    redraw()
    out["shrink"] = {"before": 0, "after": bar.value()}

    # -- the selected file is deleted, other files move through its row, Enter
    file_opens.clear()
    third = page._model.index(2, 0)
    QTest.mouseClick(view.viewport(), LEFT, PLAIN, view.visualRect(third).center())
    model.add(ev(third.data(page_module.ROLE_ENTRY).path, "deleted"))
    redraw()
    model.add(ev("k05.png", "modified")); redraw()
    model.add(ev("k06.png", "modified")); redraw()
    QTest.keyClick(view, Qt.Key.Key_Return)
    out["enter_after_delete"] = list(file_opens)

    # -- a style where one click opens an item: a double-click still opens once
    class SingleClick(QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, returnData=None):
            if hint == QStyle.StyleHint.SH_ItemView_ActivateItemOnSingleClick:
                return 1
            return super().styleHint(hint, option, widget, returnData)
    single = SingleClick("Fusion")
    view.setStyle(single)
    file_opens.clear()
    spot = view.visualRect(page._model.index(1, 0)).center()
    QTest.mousePress(view.viewport(), LEFT, PLAIN, spot)
    QTest.mouseRelease(view.viewport(), LEFT, PLAIN, spot)
    QApplication.sendEvent(view.viewport(), QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(spot),
        QPointF(view.viewport().mapToGlobal(spot)), LEFT, LEFT, PLAIN))
    QTest.mouseRelease(view.viewport(), LEFT, PLAIN, spot)
    out["single_click_style"] = list(file_opens)
    view.setStyle(app.style())

    # -- quitting with a screenful of slow decodes queued
    page.resize(1400, 1000)
    delay["seconds"] = 0.3
    model.reset([ev(f"q{i}.png", id=i + 1) for i in range(40)])
    redraw()
    queued_before = len(cache._waiting) + len(cache._running)
    started_before = len(decodes)
    began = time.monotonic()
    page.shutdown()
    seconds = time.monotonic() - began
    in_flight_finished = page.wait_for_thumbnails(3000)
    time.sleep(0.2)
    app.processEvents()
    cache.request(os.path.join(folder, "late.png"), "v", page_module.thumb_box(view), 0)
    out["shutdown"] = {"queued_before": queued_before, "seconds": seconds,
                       "in_flight_finished": in_flight_finished,
                       "started_after": len(decodes) - started_before,
                       "workers": page_module.WORKERS, "busy_after_request": cache.busy()}

    print(json.dumps(out))
    """
)
