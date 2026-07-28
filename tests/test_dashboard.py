"""Tests for the order the folders card puts things in.

The card's whole claim is that the first row is the worst thing happening. That
claim lives in one pure function, so it is the one worth pinning down: a screen
full of healthy folders must never push an error below the fold.
"""

from __future__ import annotations

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
