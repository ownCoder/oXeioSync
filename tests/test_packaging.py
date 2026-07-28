"""Tests for shipping an engine inside the build rather than downloading one.

The promise this file guards is narrow and worth stating: an installed copy must
be able to start syncing on a machine that cannot reach GitHub. That breaks if
the packaging puts the engine somewhere the application does not look, so the
knowledge of *where a bundled engine lives* — which exists in the app, in two
build tools and in the installer script — is checked here for agreement.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

from oxeiosync import paths
from oxeiosync.syncthing import binary

ROOT = Path(__file__).resolve().parent.parent


def _load_tool(name: str):
    """Import a script from tools/, which is not an importable package."""
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tools_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------ where it lives
def test_candidates_cover_both_bundle_layouts(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "install_dir", lambda: tmp_path)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    candidates = paths.bundled_syncthing_candidates()

    assert tmp_path / paths.syncthing_binary_name() in candidates
    assert tmp_path / "_internal" / paths.syncthing_binary_name() in candidates


def test_meipass_is_searched_when_frozen(monkeypatch, tmp_path):
    """A one-file build unpacks somewhere unrelated to the executable."""
    unpacked = tmp_path / "_MEI12345"
    monkeypatch.setattr(paths, "install_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(sys, "_MEIPASS", str(unpacked), raising=False)

    assert unpacked / paths.syncthing_binary_name() in paths.bundled_syncthing_candidates()


def test_candidates_do_not_repeat_a_location(monkeypatch, tmp_path):
    """_MEIPASS is normally the _internal folder; looking twice is noise."""
    monkeypatch.setattr(paths, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)

    candidates = paths.bundled_syncthing_candidates()

    assert len(candidates) == len(set(candidates))


# ------------------------------------------------------------- finding it
@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """An install folder and a managed data folder, both empty."""
    install = tmp_path / "install"
    managed = tmp_path / "data" / "bin"
    install.mkdir()
    managed.mkdir(parents=True)
    monkeypatch.setattr(paths, "install_dir", lambda: install)
    monkeypatch.setattr(
        paths, "managed_syncthing_path", lambda: managed / paths.syncthing_binary_name()
    )
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    # Nothing on PATH may answer for a test about the bundle.
    monkeypatch.setattr(binary.shutil, "which", lambda _name: None)
    return install, managed


def test_engine_in_the_contents_directory_is_found(sandbox):
    """PyInstaller puts bundled payload in _internal, not beside the exe."""
    install, _managed = sandbox
    internal = install / "_internal"
    internal.mkdir()
    engine = internal / paths.syncthing_binary_name()
    engine.write_bytes(b"MZ")

    assert binary.find_syncthing() == engine


def test_engine_beside_the_executable_is_found(sandbox):
    install, _managed = sandbox
    engine = install / paths.syncthing_binary_name()
    engine.write_bytes(b"MZ")

    assert binary.find_syncthing() == engine


def test_a_downloaded_engine_wins_over_the_shipped_one(sandbox):
    """An update fetched after installation must not be shadowed by the old one."""
    install, managed = sandbox
    (install / paths.syncthing_binary_name()).write_bytes(b"shipped")
    newer = managed / paths.syncthing_binary_name()
    newer.write_bytes(b"downloaded")

    assert binary.find_syncthing() == newer


def test_no_engine_anywhere_is_reported_as_missing(sandbox):
    assert binary.find_syncthing() is None


# --------------------------------------------------- the build tools agree
def test_build_and_smoke_tools_look_where_the_app_looks(monkeypatch, tmp_path):
    """Three copies of one fact. They drift silently unless something checks."""
    build_exe = _load_tool("build_exe")
    smoke_exe = _load_tool("smoke_exe")

    monkeypatch.setattr(paths, "install_dir", lambda: tmp_path)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    app_locations = {p.resolve() for p in paths.bundled_syncthing_candidates()}

    for tool in (build_exe, smoke_exe):
        for relative in ("", "_internal"):
            location = tmp_path / relative / tool.ENGINE_NAME
            location.parent.mkdir(parents=True, exist_ok=True)
            location.write_bytes(b"MZ")
            found = tool.bundled_engine(tmp_path)
            assert found is not None, f"{tool.__name__} misses {location}"
            assert found.resolve() in app_locations, (
                f"{tool.__name__} accepts {found}, which the application never looks at"
            )
            location.unlink()


def test_the_installer_guard_knows_every_bundle_layout():
    """The installer refuses a payload with no engine — in either location."""
    script = (ROOT / "packaging" / "oxeiosync.iss").read_text(encoding="utf-8")

    assert '#error' in script
    assert r'\_internal\syncthing.exe' in script
    assert r'SourceDir + "\syncthing.exe"' in script


def test_the_spec_refuses_to_build_without_an_engine():
    """A release build that quietly needs the network is the bug being fixed."""
    spec = (ROOT / "packaging" / "oxeiosync.spec").read_text(encoding="utf-8")

    assert "OXEIOSYNC_ALLOW_NO_ENGINE" in spec
    assert "raise SystemExit" in spec
    # Added after the drop filters, or a later filter rule could remove it.
    assert spec.index("a.datas += engine_datas") > spec.index("a.datas = [entry for entry")


# ------------------------------------------------------------ the vendor tool
def test_engine_filename_follows_the_target_not_the_host():
    fetch = _load_tool("fetch_engine")

    assert fetch.engine_filename("windows-amd64") == "syncthing.exe"
    assert fetch.engine_filename("linux-arm64") == "syncthing"
    assert fetch.engine_filename("macos-amd64") == "syncthing"


def test_version_is_read_back_from_an_asset_name():
    fetch = _load_tool("fetch_engine")

    assert fetch._version_from_name("syncthing-windows-amd64-v2.1.2.zip") == "v2.1.2"
    assert fetch._version_from_name("syncthing-linux-amd64-v2.1.2.tar.gz") == "v2.1.2"
    assert fetch._version_from_name("something-else.zip") == "unknown"


def test_licence_files_are_taken_out_of_the_archive(tmp_path):
    """Shipping the engine means shipping its terms; they come from the archive."""
    fetch = _load_tool("fetch_engine")
    archive = tmp_path / "syncthing-windows-amd64-v2.1.2.zip"
    root = "syncthing-windows-amd64-v2.1.2"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{root}/", "")
        zf.writestr(f"{root}/LICENSE.txt", "MPL text")
        zf.writestr(f"{root}/AUTHORS.txt", "names")
        zf.writestr(f"{root}/README.txt", "prose")
        zf.writestr(f"{root}/etc/linux-systemd/user/syncthing.service", "unit")
        zf.writestr(f"{root}/syncthing.exe", "MZ")

    written = fetch.extract_legal_files(archive, tmp_path)

    assert sorted(written) == ["AUTHORS-engine.txt", "LICENSE-engine.txt"]
    assert (tmp_path / "LICENSE-engine.txt").read_text(encoding="utf-8") == "MPL text"
    assert not (tmp_path / "README.txt").exists()


def test_the_notice_names_the_version_and_the_source(tmp_path):
    """Distributing the binary means telling the recipient where its source is."""
    fetch = _load_tool("fetch_engine")

    fetch.write_notice(tmp_path, "v2.1.2", ["LICENSE-engine.txt"], "a" * 64)
    notice = (tmp_path / fetch.NOTICE_NAME).read_text(encoding="utf-8")

    assert "MPL-2.0" in notice
    assert "LICENSE-engine.txt" in notice
    assert "a" * 64 in notice
    # The exact version, not the project in general: a pointer at "latest" would
    # stop resolving to the thing that was actually shipped.
    assert "github.com/syncthing/syncthing/releases/tag/v2.1.2" in notice


def test_the_app_and_the_vendor_tool_agree_on_the_notice_filename():
    """The About dialog points people at a file another program writes."""
    fetch = _load_tool("fetch_engine")

    assert fetch.NOTICE_NAME == paths.ENGINE_NOTICE_NAME


def test_the_notice_does_not_promise_files_that_are_not_there(tmp_path):
    fetch = _load_tool("fetch_engine")

    fetch.write_notice(tmp_path, "v2.1.2", [])
    notice = (tmp_path / fetch.NOTICE_NAME).read_text(encoding="utf-8")

    assert "none found" in notice


def test_an_unknown_version_does_not_become_a_link_to_nowhere(tmp_path):
    """The offline path may not know the version; a 404 is worse than saying so."""
    fetch = _load_tool("fetch_engine")

    fetch.write_notice(tmp_path, fetch.UNKNOWN_VERSION, [])
    notice = (tmp_path / fetch.NOTICE_NAME).read_text(encoding="utf-8")

    assert "releases/tag/unknown" not in notice
    assert "did not record which release" in notice
    assert fetch.SOURCE_URL in notice


def test_an_offline_engine_without_a_digest_is_refused(tmp_path):
    """The offline path must not become a laxer way to obtain the same file."""
    fetch = _load_tool("fetch_engine")
    payload = tmp_path / "syncthing.exe"
    payload.write_bytes(b"MZ")

    with pytest.raises(SystemExit, match="--sha256"):
        fetch.check_offline_digest(payload, None, allow_unverified=False)


def test_an_offline_engine_with_the_wrong_digest_is_refused(tmp_path):
    fetch = _load_tool("fetch_engine")
    payload = tmp_path / "syncthing.exe"
    payload.write_bytes(b"MZ")

    with pytest.raises(SystemExit, match="digest mismatch"):
        fetch.check_offline_digest(payload, "0" * 64, allow_unverified=False)


def test_an_offline_engine_with_the_right_digest_is_accepted(tmp_path):
    fetch = _load_tool("fetch_engine")
    payload = tmp_path / "syncthing.exe"
    payload.write_bytes(b"MZ")

    fetch.check_offline_digest(payload, fetch.sha256_of(payload).upper(), False)  # must not raise


def test_unverified_is_possible_but_has_to_be_asked_for(tmp_path):
    fetch = _load_tool("fetch_engine")
    payload = tmp_path / "syncthing.exe"
    payload.write_bytes(b"MZ")

    fetch.check_offline_digest(payload, None, allow_unverified=True)  # must not raise


def _write_manifest(into: Path, fetch, digest: str, version: str = "v2.1.2") -> None:
    (into / fetch.MANIFEST_NAME).write_text(
        json.dumps({"version": version, "binary_sha256": digest}), encoding="utf-8"
    )


def test_a_vendored_engine_that_was_swapped_is_refetched(tmp_path):
    """The manifest is what makes 'already vendored' a safe shortcut."""
    fetch = _load_tool("fetch_engine")
    engine = tmp_path / "syncthing.exe"
    engine.write_bytes(b"MZ")
    _write_manifest(tmp_path, fetch, "0" * 64)

    assert fetch.already_vendored(tmp_path, "windows-amd64", None) is None


def test_a_matching_vendored_engine_is_reused(tmp_path):
    fetch = _load_tool("fetch_engine")
    engine = tmp_path / "syncthing.exe"
    engine.write_bytes(b"MZ")
    _write_manifest(tmp_path, fetch, fetch.sha256_of(engine))

    assert fetch.already_vendored(tmp_path, "windows-amd64", "v2.1.2") is not None
    # A different pinned version is not the one that was asked for.
    assert fetch.already_vendored(tmp_path, "windows-amd64", "v2.0.0") is None
