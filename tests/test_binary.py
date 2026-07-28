"""Tests for release-asset selection, checksum parsing and archive extraction."""

from __future__ import annotations

import hashlib
import zipfile

import pytest

from oxeiosync.syncthing import binary
from oxeiosync.syncthing.binary import SyncthingBinaryError, SyncthingRelease

# ------------------------------------------------------------- checksum listing
SHA256_LISTING = """-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA512

aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111  syncthing-linux-amd64-v2.1.2.tar.gz
BBBB2222BBBB2222BBBB2222BBBB2222BBBB2222BBBB2222BBBB2222BBBB2222  syncthing-windows-amd64-v2.1.2.zip
cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333  syncthing-macos-arm64-v2.1.2.zip
-----BEGIN PGP SIGNATURE-----
"""


def test_digest_is_found_for_the_right_asset():
    digest = binary._find_digest(SHA256_LISTING, "syncthing-windows-amd64-v2.1.2.zip")
    assert digest == "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"


def test_digest_lookup_does_not_match_a_different_asset():
    digest = binary._find_digest(SHA256_LISTING, "syncthing-linux-amd64-v2.1.2.tar.gz")
    assert digest.startswith("aaaa1111")


def test_missing_asset_has_no_digest():
    assert binary._find_digest(SHA256_LISTING, "syncthing-freebsd-riscv-v9.zip") is None


def test_digest_lookup_ignores_a_partial_name_match():
    """A prefix must not be accepted as the asset we asked for."""
    assert binary._find_digest(SHA256_LISTING, "syncthing-windows-amd64") is None


# --------------------------------------------------------------- checksum gate
def _release(**overrides) -> SyncthingRelease:
    defaults = dict(
        version="v2.1.2",
        asset_name="syncthing-windows-amd64-v2.1.2.zip",
        asset_url="https://example.invalid/asset.zip",
        asset_size=1234,
        checksums_url="https://example.invalid/sha256sum.txt.asc",
    )
    defaults.update(overrides)
    return SyncthingRelease(**defaults)


def test_release_without_checksums_is_refused(tmp_path):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(b"anything")

    with pytest.raises(SyncthingBinaryError, match="no checksums"):
        binary._verify_checksum(archive, _release(checksums_url=None))


def test_checksum_mismatch_is_refused(tmp_path, monkeypatch):
    archive = tmp_path / "syncthing-windows-amd64-v2.1.2.zip"
    archive.write_bytes(b"tampered payload")

    listing = f"{'0' * 64}  syncthing-windows-amd64-v2.1.2.zip\n"
    monkeypatch.setattr(binary, "requests", _FakeRequests(listing))

    with pytest.raises(SyncthingBinaryError, match="Checksum mismatch"):
        binary._verify_checksum(archive, _release())


def test_matching_checksum_passes(tmp_path, monkeypatch):
    payload = b"the real thing"
    archive = tmp_path / "syncthing-windows-amd64-v2.1.2.zip"
    archive.write_bytes(payload)

    listing = f"{hashlib.sha256(payload).hexdigest()}  {archive.name}\n"
    monkeypatch.setattr(binary, "requests", _FakeRequests(listing))

    binary._verify_checksum(archive, _release())  # must not raise


class _FakeRequests:
    """Minimal stand-in for the ``requests`` module used by _verify_checksum."""

    # Named to mirror requests.RequestException, which is what the code under
    # test catches; renaming it would stop the stand-in standing in.
    class RequestException(Exception):  # noqa: N818
        pass

    def __init__(self, text: str) -> None:
        self._text = text

    def get(self, *_args, **_kwargs):
        return self

    @property
    def text(self) -> str:
        return self._text

    def raise_for_status(self) -> None:
        return None


# -------------------------------------------------------------- release lookup
def _github_listing(tag: str = "v2.1.2") -> list[dict]:
    """A release list shaped the way GitHub's API returns it."""
    def asset(name: str) -> dict:
        return {
            "name": name,
            "size": 1234,
            "url": f"https://api.github.com/repos/syncthing/syncthing/releases/assets/{name}",
            "browser_download_url": f"https://github.com/download/{tag}/{name}",
        }

    return [
        {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "assets": [
                asset("sha256sum.txt.asc"),
                asset(f"syncthing-linux-amd64-{tag}.tar.gz"),
                asset(f"syncthing-windows-amd64-{tag}.zip"),
            ],
        }
    ]


def _mirror_listing(tag: str = "v2.1.2") -> list[dict]:
    """The same list as the mirror serves it: one ``url`` key, no sizes."""
    return [
        {
            "tag_name": tag,
            "prerelease": False,
            "assets": [
                {"name": name, "url": f"https://github.com/download/{tag}/{name}"}
                for name in (
                    "sha256sum.txt.asc",
                    f"syncthing-windows-amd64-{tag}.zip",
                )
            ],
        }
    ]


class _FakeReleaseRequests:
    """Serves a canned response per URL, or raises for URLs mapped to an error."""

    class RequestException(Exception):  # noqa: N818 - mirrors requests.RequestException
        pass

    def __init__(self, by_url: dict) -> None:
        self._by_url = by_url
        self.urls: list[str] = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        payload = self._by_url.get(url)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            raise self.RequestException(f"unexpected URL {url}")
        return _FakeResponse(payload)


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


@pytest.fixture
def on_windows_amd64(monkeypatch):
    monkeypatch.setattr(binary.platform, "system", lambda: "Windows")
    monkeypatch.setattr(binary.platform, "machine", lambda: "AMD64")


def test_release_is_read_from_github_when_it_answers(monkeypatch, on_windows_amd64):
    fake = _FakeReleaseRequests({binary.GITHUB_RELEASES_API: _github_listing()})
    monkeypatch.setattr(binary, "requests", fake)

    release = binary.latest_release()

    assert release.version == "v2.1.2"
    assert release.asset_name == "syncthing-windows-amd64-v2.1.2.zip"
    # The browsable link, not the API handle, which would download JSON.
    assert release.asset_url == "https://github.com/download/v2.1.2/syncthing-windows-amd64-v2.1.2.zip"
    assert release.checksums_url.endswith("sha256sum.txt.asc")
    assert binary.UPGRADE_META_URL not in fake.urls


def test_rate_limited_github_falls_back_to_the_mirror(monkeypatch, on_windows_amd64):
    """The reported bug: GitHub answers 403 once 60 requests an hour are spent."""
    rate_limited = _FakeReleaseRequests.RequestException("403 Client Error: rate limit exceeded")
    fake = _FakeReleaseRequests(
        {
            binary.GITHUB_RELEASES_API: rate_limited,
            binary.UPGRADE_META_URL: _mirror_listing(),
        }
    )
    monkeypatch.setattr(binary, "requests", fake)

    release = binary.latest_release()

    assert release.asset_name == "syncthing-windows-amd64-v2.1.2.zip"
    assert release.asset_url.startswith("https://github.com/download/")
    assert release.asset_size == 0  # The mirror publishes no sizes.
    assert release.checksums_url.endswith("sha256sum.txt.asc")


def test_both_sources_failing_is_reported_with_advice(monkeypatch, on_windows_amd64):
    broken = _FakeReleaseRequests.RequestException("no route to host")
    monkeypatch.setattr(
        binary,
        "requests",
        _FakeReleaseRequests(
            {binary.GITHUB_RELEASES_API: broken, binary.UPGRADE_META_URL: broken}
        ),
    )

    with pytest.raises(SyncthingBinaryError) as caught:
        binary.latest_release()

    assert "no route to host" in str(caught.value)
    assert "engine path in Settings" in str(caught.value)


def test_prereleases_are_skipped_unless_asked_for(monkeypatch, on_windows_amd64):
    listing = _mirror_listing("v2.2.0-rc.1") + _mirror_listing("v2.1.2")
    listing[0]["prerelease"] = True
    monkeypatch.setattr(
        binary, "requests", _FakeReleaseRequests({binary.GITHUB_RELEASES_API: listing})
    )

    assert binary.latest_release().version == "v2.1.2"
    assert binary.latest_release(include_prereleases=True).version == "v2.2.0-rc.1"


def test_a_source_without_this_platform_falls_through_to_the_next(monkeypatch, on_windows_amd64):
    """An answer that parses but has no build for us is not a usable answer."""
    linux_only = [{"tag_name": "v2.1.2", "assets": [{"name": "syncthing-linux-amd64-v2.1.2.tar.gz",
                                                    "url": "https://github.com/download/x.tar.gz"}]}]
    fake = _FakeReleaseRequests(
        {
            binary.GITHUB_RELEASES_API: linux_only,
            binary.UPGRADE_META_URL: _mirror_listing(),
        }
    )
    monkeypatch.setattr(binary, "requests", fake)

    assert binary.latest_release().asset_name == "syncthing-windows-amd64-v2.1.2.zip"
    assert binary.UPGRADE_META_URL in fake.urls


def test_a_non_https_asset_url_is_ignored():
    assert binary._asset_url({"url": "http://insecure.invalid/syncthing.zip"}) is None
    assert binary._asset_url({"url": "https://ok.invalid/syncthing.zip"}) is not None


# ------------------------------------------------------------ archive extraction
def test_binary_is_extracted_from_a_nested_archive(tmp_path):
    """Laid out like a real release: one top-level directory, extras beside it."""
    archive = tmp_path / "syncthing-windows-amd64-v2.1.2.zip"
    root = "syncthing-windows-amd64-v2.1.2"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{root}/", "")
        zf.writestr(f"{root}/LICENSE.txt", "text")
        zf.writestr(f"{root}/AUTHORS.txt", "names")
        zf.writestr(f"{root}/README.txt", "text")
        zf.writestr(f"{root}/metadata/release.sig", b"\x00sig")
        zf.writestr(f"{root}/etc/linux-systemd/user/syncthing.service", "unit")
        zf.writestr(f"{root}/syncthing.exe", b"MZ binary")

    extracted = binary.extract_binary(archive, tmp_path / "out")

    assert extracted.read_bytes() == b"MZ binary"


def test_archive_without_the_binary_is_rejected(tmp_path):
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("README.md", "nothing useful here")

    with pytest.raises(SyncthingBinaryError, match="No syncthing"):
        binary.extract_binary(archive, tmp_path / "out")


def test_unknown_archive_format_is_rejected(tmp_path):
    archive = tmp_path / "asset.7z"
    archive.write_bytes(b"not a zip")

    with pytest.raises(SyncthingBinaryError, match="unpack"):
        binary.extract_binary(archive, tmp_path / "out")


def test_path_traversal_entries_are_skipped():
    """A malicious archive must not be able to write outside the target."""
    with pytest.raises(SyncthingBinaryError):
        binary._match_member(["../../syncthing.exe"], "syncthing.exe")


def test_match_member_finds_a_top_level_entry():
    assert binary._match_member(["syncthing.exe"], "syncthing.exe") == "syncthing.exe"


# ------------------------------------------------------------------- platform
def test_asset_infix_is_recognised(monkeypatch):
    monkeypatch.setattr(binary.platform, "system", lambda: "Windows")
    monkeypatch.setattr(binary.platform, "machine", lambda: "AMD64")

    assert binary.platform_asset_infix() == "windows-amd64"


@pytest.mark.parametrize(
    "system, machine, expected",
    [
        ("Linux", "x86_64", "linux-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Windows", "x86", "windows-386"),
    ],
)
def test_asset_infix_across_platforms(monkeypatch, system, machine, expected):
    monkeypatch.setattr(binary.platform, "system", lambda: system)
    monkeypatch.setattr(binary.platform, "machine", lambda: machine)

    assert binary.platform_asset_infix() == expected


def test_unsupported_architecture_is_reported(monkeypatch):
    monkeypatch.setattr(binary.platform, "system", lambda: "Linux")
    monkeypatch.setattr(binary.platform, "machine", lambda: "sparc64")

    with pytest.raises(SyncthingBinaryError, match="architecture"):
        binary.platform_asset_infix()


# --------------------------------------------------------------------- locating
def test_explicit_override_wins(tmp_path):
    override = tmp_path / "custom-syncthing.exe"
    override.write_bytes(b"MZ")

    assert binary.find_syncthing(str(override)) == override


def test_missing_override_is_not_silently_replaced(tmp_path):
    """An override that does not exist must fail loudly, not fall back."""
    assert binary.find_syncthing(str(tmp_path / "nope.exe")) is None
