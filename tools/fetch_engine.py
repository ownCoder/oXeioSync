"""Vendor a sync engine into the build tree, so the bundle can ship one.

    python tools/fetch_engine.py [--version vX.Y.Z] [--arch windows-amd64]
    python tools/fetch_engine.py --from-archive syncthing-windows-amd64-v2.1.2.zip
    python tools/fetch_engine.py --from-binary C:\\somewhere\\syncthing.exe --sha256 HEX

Downloading the engine on first run means the first run needs the network, and
needs it to be a network that can reach GitHub without being rate-limited. That
is a bad bargain for a machine that is being set up once and then left alone, so
the release build ships an engine instead. This is the step that puts one where
the packaging can find it: ``build/vendor/<os>-<arch>/``.

The download goes through exactly the code path the application uses, digest
check included — vendoring must not be a second, laxer way to obtain the file we
are about to hand to thousands of machines. The offline forms exist for builds
on a machine with no route to GitHub; they still refuse to accept a file without
a digest to check it against unless told to in as many words.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oxeiosync.syncthing import binary  # noqa: E402  (needs the path above)

VENDOR_ROOT = ROOT / "build" / "vendor"
MANIFEST_NAME = "engine.json"
NOTICE_NAME = "ENGINE-NOTICE.txt"

#: Files in the release archive worth keeping beside the binary. Shipping the
#: engine means shipping its licence terms with it, not just its code.
LEGAL_FILES = {"license": "LICENSE-engine.txt", "notice": "NOTICE-engine.txt",
               "authors": "AUTHORS-engine.txt"}

SOURCE_URL = "https://github.com/syncthing/syncthing"
OWN_URL = "https://github.com/ownCoder/oXeioSync"

#: Recorded when an offline source did not say which release it is.
UNKNOWN_VERSION = "unknown"


def engine_filename(infix: str) -> str:
    """What the engine executable is called on the target platform."""
    return "syncthing.exe" if infix.startswith("windows") else "syncthing"


def vendor_dir(infix: str) -> Path:
    return VENDOR_ROOT / infix


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _progress(received: int, total: int) -> None:
    if total:
        print(f"\r   {received / 1_048_576:6.1f} / {total / 1_048_576:.1f} MiB", end="")
    else:
        print(f"\r   {received / 1_048_576:6.1f} MiB", end="")


def _archive_members(archive: Path) -> list[str]:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            return zf.namelist()
    with tarfile.open(archive, "r:*") as tf:
        return tf.getnames()


def _read_member(archive: Path, member: str) -> bytes:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf, zf.open(member) as handle:
            return handle.read()
    with tarfile.open(archive, "r:*") as tf:
        extracted = tf.extractfile(member)
        if extracted is None:
            return b""
        with extracted:
            return extracted.read()


def extract_legal_files(archive: Path, into: Path) -> list[str]:
    """Copy the licence, notice and authors files out of the release archive."""
    written = []
    for member in _archive_members(archive):
        parts = Path(member).parts
        if ".." in parts:
            continue
        stem = Path(member).name.split(".")[0].lower()
        target_name = LEGAL_FILES.get(stem)
        if target_name is None:
            continue
        payload = _read_member(archive, member)
        if not payload:
            continue
        (into / target_name).write_bytes(payload)
        written.append(target_name)
    return written


def write_notice(
    into: Path, version: str, legal: list[str], digest: str | None = None
) -> None:
    """State plainly what the extra executable in this folder is.

    Distributing an MPL-2.0 binary carries an obligation the licence text alone
    does not discharge: telling the recipient where to get the corresponding
    source. That is what the middle of this file is for, and why it names the
    exact version rather than the project in general.
    """
    lines = [
        "This application ships an unmodified copy of Syncthing as its sync engine.",
        "",
        f"    Version:  {version}",
        "    Licence:  Mozilla Public License 2.0 (MPL-2.0)",
    ]
    if digest:
        lines.append(f"    SHA-256:  {digest}")
    if version and version != UNKNOWN_VERSION:
        lines += [
            "",
            "The complete corresponding source for this version is published at:",
            "",
            f"    {SOURCE_URL}/releases/tag/{version}",
            f"    {SOURCE_URL}/tree/{version}",
        ]
    else:
        # A URL built around the word "unknown" resolves to nothing. Saying so
        # is worth more than a link that 404s in the one file whose job is to
        # tell the recipient where the source is.
        lines += [
            "",
            "This build did not record which release the engine came from, so no",
            "direct link can be given. Its source is published at:",
            "",
            f"    {SOURCE_URL}",
        ]
    lines += [
        "",
        "If that is not enough to obtain the corresponding source, a copy can be",
        f"requested at {OWN_URL}/issues, and will be provided by reasonable",
        "means at no more than the cost of distribution.",
        "",
        "Syncthing is not part of oXeioSync and is not modified by it. The full",
        "licence text is reproduced in the file(s) beside this one:",
        "",
    ]
    lines += [f"    {name}" for name in legal] or ["    (none found in the release archive)"]
    lines += [
        "",
        "oXeioSync is not affiliated with, nor endorsed by, the Syncthing project.",
        "oXeioSync itself is MIT-licensed; see its own LICENSE file.",
        "",
    ]
    (into / NOTICE_NAME).write_text("\n".join(lines), encoding="utf-8")


def read_manifest(target: Path) -> dict | None:
    manifest = target / MANIFEST_NAME
    if not manifest.is_file():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def already_vendored(target: Path, infix: str, wanted_version: str | None) -> dict | None:
    """The manifest of a usable existing vendoring, or None to fetch again."""
    manifest = read_manifest(target)
    if not manifest:
        return None
    engine = target / engine_filename(infix)
    if not engine.is_file():
        return None
    if wanted_version and manifest.get("version") != wanted_version:
        return None
    # A vendored binary whose digest no longer matches the manifest is a build
    # tree someone has been editing; refetch rather than ship a mystery.
    if manifest.get("binary_sha256") and sha256_of(engine) != manifest["binary_sha256"]:
        print("   vendored engine does not match its manifest; refetching")
        return None
    return manifest


def vendor_from_network(infix: str, version: str | None, target: Path) -> dict:
    release = binary.latest_release(tag=version, infix=infix)
    print(f"   release {release.version} — {release.asset_name}")

    with tempfile.TemporaryDirectory(prefix="oxeiosync-vendor-") as tmp_name:
        tmp = Path(tmp_name)
        archive = binary.fetch_verified_archive(release, tmp, progress=_progress)
        print("\n   digest verified")
        return _install_from_archive(archive, infix, target, release.version, release.asset_url)


def _install_from_archive(
    archive: Path, infix: str, target: Path, version: str, source: str
) -> dict:
    name = engine_filename(infix)
    extracted = binary.extract_binary(archive, target, name=name)
    # A script where a binary should be means the wrong archive member was
    # picked (release archives carry rc/service files also called "syncthing").
    # This tool cannot run a cross-arch binary to probe it, but it can refuse
    # the one thing the engine is definitely not.
    if extracted.read_bytes()[:2] == b"#!":
        raise binary.SyncthingBinaryError(
            f"extracted {name} is a script, not the engine binary — "
            "the archive layout was not what was expected"
        )
    legal = extract_legal_files(archive, target)
    digest = sha256_of(extracted)
    write_notice(target, version, legal, digest)
    return {
        "version": version,
        "arch": infix,
        "binary": name,
        "binary_sha256": digest,
        "archive": archive.name,
        "archive_sha256": sha256_of(archive),
        "source": source,
        "vendored_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "legal_files": legal,
    }


def check_offline_digest(path: Path, expected: str | None, allow_unverified: bool) -> None:
    """Refuse a local file we have no way to check, unless told to accept it."""
    if expected:
        actual = sha256_of(path)
        if actual.lower() != expected.strip().lower():
            raise SystemExit(
                f"digest mismatch for {path.name}:\n"
                f"  expected {expected.strip().lower()}\n"
                f"  actual   {actual}"
            )
        print("   digest matches --sha256")
        return
    if not allow_unverified:
        raise SystemExit(
            f"{path.name} has no digest to check it against.\n"
            "Pass --sha256 <hex> (the release publishes these in sha256sum.txt.asc), "
            "or --allow-unverified if you are certain of this file's provenance."
        )
    print("   WARNING: shipping an unverified engine because --allow-unverified was given")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", help="release tag to pin, e.g. v2.1.2 (default: newest)")
    parser.add_argument(
        "--arch",
        default=None,
        help="target <os>-<arch>, e.g. windows-amd64 (default: this machine)",
    )
    parser.add_argument("--from-archive", type=Path, help="use a release archive already on disk")
    parser.add_argument("--from-binary", type=Path, help="use an engine executable already on disk")
    parser.add_argument("--sha256", help="expected digest of the --from-* file")
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="accept a --from-* file with no digest to check (say why in the commit)",
    )
    parser.add_argument("--force", action="store_true", help="refetch even if one is vendored")
    parser.add_argument(
        "--print-path",
        action="store_true",
        help="print the vendor directory and exit, for scripting",
    )
    args = parser.parse_args(argv[1:])

    try:
        infix = args.arch or binary.platform_asset_infix()
    except binary.SyncthingBinaryError as exc:
        print(f"{exc}\nName the target explicitly with --arch.", file=sys.stderr)
        return 1
    target = vendor_dir(infix)

    if args.print_path:
        print(target)
        return 0

    if args.from_archive and args.from_binary:
        parser.error("--from-archive and --from-binary are alternatives; pick one")

    print(f"== vendoring the sync engine for {infix}")
    # Naming a file to vendor is an instruction, not a suggestion: it would be a
    # poor kind of obedience to answer it with "there is already one of those".
    if not args.force and not (args.from_archive or args.from_binary):
        existing = already_vendored(target, infix, args.version)
        if existing:
            print(f"   already vendored: {existing.get('version')} in {target}")
            print("   (pass --force to fetch it again)")
            return 0

    target.mkdir(parents=True, exist_ok=True)

    try:
        if args.from_archive:
            archive = args.from_archive.resolve()
            if not archive.is_file():
                raise SystemExit(f"no such archive: {archive}")
            print(f"   using {archive}")
            check_offline_digest(archive, args.sha256, args.allow_unverified)
            version = args.version or _version_from_name(archive.name)
            manifest = _install_from_archive(archive, infix, target, version, str(archive))
        elif args.from_binary:
            source = args.from_binary.resolve()
            if not source.is_file():
                raise SystemExit(f"no such file: {source}")
            print(f"   using {source}")
            check_offline_digest(source, args.sha256, args.allow_unverified)
            name = engine_filename(infix)
            shutil.copy2(source, target / name)
            version = args.version or UNKNOWN_VERSION
            write_notice(target, version, [], sha256_of(target / name))
            print(
                "   NOTE: no licence text came with a bare executable. Put the engine's "
                f"LICENSE in {target / LEGAL_FILES['license']} before shipping this build."
            )
            manifest = {
                "version": version,
                "arch": infix,
                "binary": name,
                "binary_sha256": sha256_of(target / name),
                "source": str(source),
                "vendored_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "legal_files": [],
            }
        else:
            manifest = vendor_from_network(infix, args.version, target)
    except binary.SyncthingBinaryError as exc:
        print(f"\nvendoring failed: {exc}", file=sys.stderr)
        return 1

    (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    engine = target / manifest["binary"]
    print(f"   {engine}  ({engine.stat().st_size / 1_048_576:.1f} MiB)")
    print(f"   version {manifest['version']}, sha256 {manifest['binary_sha256'][:16]}…")
    if manifest["legal_files"]:
        print(f"   licence files: {', '.join(manifest['legal_files'])}")
    return 0


def _version_from_name(asset_name: str) -> str:
    """Pull ``v2.1.2`` out of ``syncthing-windows-amd64-v2.1.2.zip``."""
    stem = asset_name.removesuffix(".zip").removesuffix(".tar.gz")
    tail = stem.rsplit("-", 1)[-1]
    return tail if tail.startswith("v") else UNKNOWN_VERSION


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
