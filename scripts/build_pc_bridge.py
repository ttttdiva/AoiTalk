"""Build a Windows executable with its extension and optional verified helper."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HELPER_DESTINATION = "resources/agent-browser"
EDGE_EXTENSION_DESTINATION = "resources/edge-browser-extension"
EDGE_EXTENSION_FILES = frozenset({
    "manifest.json", "background.js", "content.js", "media-prepare-youtube.js",
    "popup.html", "popup.js", "popup.css",
})
PROFILE_DRIVER_DESTINATION = "resources/browser-profile-driver"
PROFILE_DRIVER_FILES = frozenset({
    "driver.cjs", "dedicated-manifest.json", "jev-lib.cjs", "jev-provenance.json",
    "jev-browser.patch", "JEV-THIRD-PARTY-NOTICES.txt", "README.md",
})
RUST_DEPENDENCY_NOTICE = "licenses/RUST-DEPENDENCIES-NOTICES.txt"
HELPER_NOTICES = (
    "NOTICE-bridge-modifications.txt",
    "licenses/LICENSE-agent-browser.txt",
    "licenses/LICENSE-axe-core.txt",
    "licenses/LICENSE-axe-core-THIRD-PARTY.txt",
    RUST_DEPENDENCY_NOTICE,
    "licenses/supplemental/rand-0.8.5-COPYRIGHT.txt",
    "licenses/supplemental/rand_chacha-0.3.1-COPYRIGHT.txt",
    "licenses/supplemental/rand_core-0.6.4-COPYRIGHT.txt",
    "licenses/supplemental/rav1e-0.8.1-PATENTS.txt",
    "licenses/supplemental/tiff-0.10.3-tests-COPYRIGHT.txt",
    "licenses/supplemental/utf8_iter-1.0.4-COPYRIGHT.txt",
)
RAW_HELPER_NOTICES = {
    RUST_DEPENDENCY_NOTICE:
        ("ef228f39562540bc5ba40dfb99074f0c03d640ae2b4694a7c0a14b9fab52e18a", "utf-8"),
    "licenses/supplemental/rav1e-0.8.1-PATENTS.txt":
        ("7b14a70c194b19dbd0625e7eac839a63200f67484521ce5e8d8244b127e8907a", "cp1252"),
}
MAX_NOTICE_BYTES = 1024 * 1024
# The reviewer-provided aggregate is larger than the individual upstream notices.
# Bundling it does not replace the separate release-time license review.
MAX_RUST_DEPENDENCY_NOTICE_BYTES = 4 * 1024 * 1024
DISTRIBUTION_NOTICE_DESTINATION = "resources/pc-bridge-notices"


def _is_reparse_point(path: Path) -> bool:
    """Return whether a path is a symlink or Windows junction."""
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def stage_distribution_notices(files: dict[str, bytes], work_root: Path) -> Path:
    """Stage exact, already-validated notice bytes in one directory argument.

    PyInstaller recursively expands a directory passed to ``--add-data``.  A
    dedicated staging tree lets the Windows ``makespec`` subprocess receive a
    single bounded argument while preserving the reviewed manifest's exact
    file set and bytes.  The caller owns the returned directory and must keep
    it until the generated specification has been built.
    """
    staging_root = Path(tempfile.mkdtemp(prefix=".pc-bridge-notices-", dir=work_root))
    try:
        for relative, data in files.items():
            target = staging_root.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        expected = set(files)
        actual = set()
        for current, directories, filenames in os.walk(staging_root, followlinks=False):
            current_path = Path(current)
            if _is_reparse_point(current_path):
                raise SystemExit("Staged distribution notices contain a linked directory")
            for directory in directories:
                if _is_reparse_point(current_path / directory):
                    raise SystemExit("Staged distribution notices contain a linked directory")
            for filename in filenames:
                path = current_path / filename
                if _is_reparse_point(path) or not path.is_file():
                    raise SystemExit("Staged distribution notices contain a linked file")
                relative = path.relative_to(staging_root).as_posix()
                actual.add(relative)
                if path.read_bytes() != files.get(relative):
                    raise SystemExit("Staged distribution notice bytes differ from the reviewed bundle")
        if actual != expected:
            raise SystemExit("Staged distribution notices differ from the reviewed file set")
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return staging_root


def _is_distribution_notice_data(value: str) -> bool:
    """Recognize one ``--add-data`` destination from the reviewed bundle."""
    _, separator, destination = value.rpartition(";")
    return bool(separator) and (
        destination == DISTRIBUTION_NOTICE_DESTINATION
        or destination.startswith(DISTRIBUTION_NOTICE_DESTINATION + "/")
    )


def validate_distribution_environment(manifest, helper_directory=None):
    """Check shipped distribution versions, not unrelated installed packages."""
    import importlib.metadata
    environment = manifest.get("build_environment")
    if not isinstance(environment, dict):
        raise SystemExit("Reviewed distribution build environment is missing")
    expected = environment.get("runtime_distributions")
    if not isinstance(expected, dict) or not expected:
        raise SystemExit("Reviewed runtime distribution versions are missing")
    try:
        actual = {name: importlib.metadata.version(name) for name in expected}
    except importlib.metadata.PackageNotFoundError:
        raise SystemExit("Reviewed runtime distribution is not installed") from None
    if (environment.get("platform") != sys.platform or
            environment.get("python_version") != sys.version.split()[0] or
            expected != actual):
        raise SystemExit("Build runtime differs from the reviewed distribution inventory")
    expected_helper = manifest.get("helper_sha256")
    if expected_helper is not None:
        from pc_bridge.browser_backend import PINNED_HELPER_SHA256
        if expected_helper != PINNED_HELPER_SHA256:
            raise SystemExit("Reviewed helper hash is not the pinned bridge.4 helper")
    if helper_directory is not None:
        from pc_bridge.browser_backend import load_helper_bundle
        if expected_helper != load_helper_bundle(
            helper_directory, require_pinned=True
        ).manifest.sha256:
            raise SystemExit("Helper differs from the reviewed distribution inventory")
    elif expected_helper is not None:
        raise SystemExit("Reviewed distribution inventory requires an agent-browser helper")


def validate_distribution_notice_policy(manifest, files):
    """Original bytes and recorded decisions are both required at release time."""
    import json
    from pc_bridge.distribution_notices import validate_notice_conditions
    from pc_bridge.license_bundle import LicenseBundleError
    try:
        policy = json.loads((ROOT / "resources/pc-bridge-distribution/notice-policy.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LicenseBundleError("Distribution notice policy is missing or invalid") from None
    validate_notice_conditions(manifest, files, policy)


def packaged_inventory(executable):
    """Read the actual onefile archive; never infer inclusion from installed dists."""
    from PyInstaller.archive.readers import CArchiveReader, PKG_ITEM_PYZ
    archive = CArchiveReader(str(executable))
    pkg, pyz = [], {}
    for name, entry in archive.toc.items():
        normalized = name.replace("\\", "/")
        if normalized.startswith(DISTRIBUTION_NOTICE_DESTINATION + "/"):
            continue
        pkg.append(normalized)
        if entry[-1] == PKG_ITEM_PYZ:
            # The first-party source may evolve without changing third-party
            # obligations. Everything else must remain explicitly inventoried.
            pyz[normalized] = sorted(n for n in archive.open_embedded_archive(name).toc
                                     if n != "pc_bridge" and not n.startswith("pc_bridge."))
    return {"archive_entries": sorted(pkg), "python_modules": pyz}


def validate_packaged_profile_driver(executable: Path, *, onedir: bool = False) -> None:
    """Require exact driver, Jev loop, provenance and notices in the EXE."""
    source = ROOT / PROFILE_DRIVER_DESTINATION
    if _is_reparse_point(source) or not source.is_dir():
        raise SystemExit("Profile driver source is missing or linked")
    files = {path.name: path for path in source.iterdir()}
    if set(files) != PROFILE_DRIVER_FILES or any(_is_reparse_point(path) or not path.is_file()
                                                for path in files.values()):
        raise SystemExit("Profile driver source inventory differs from the fixed file set")
    originals = {name: path.read_bytes() for name, path in files.items()}
    if onedir:
        packaged = executable.parent / "_internal" / PROFILE_DRIVER_DESTINATION
        if _is_reparse_point(packaged) or not packaged.is_dir():
            raise SystemExit("Packaged profile driver is missing or linked")
        installed = {path.name: path for path in packaged.iterdir()}
        if set(installed) != PROFILE_DRIVER_FILES or any(_is_reparse_point(path) or not path.is_file()
                                                       for path in installed.values()):
            raise SystemExit("Packaged profile driver inventory differs from source")
        actual = {name: path.read_bytes() for name, path in installed.items()}
    else:
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(executable))
        prefix = PROFILE_DRIVER_DESTINATION + "/"
        entries = [(name.replace("\\", "/")[len(prefix):], name)
                   for name in archive.toc if name.replace("\\", "/").startswith(prefix)]
        if len(entries) != len(PROFILE_DRIVER_FILES) or {name for name, _ in entries} != PROFILE_DRIVER_FILES:
            raise SystemExit("Packaged profile driver inventory differs from source")
        actual = {name: archive.extract(entry) for name, entry in entries}
    if actual != originals:
        raise SystemExit("Packaged profile driver bytes differ from current source")
    print("[OK] Packaged profile driver and Jev: exact source bytes and notices", flush=True)


def validate_packaged_extension(executable: Path, *, onedir: bool = False, include_profile_driver: bool = False) -> None:
    """Verify the distributed extension's exact current source bytes offline."""
    source = ROOT / EDGE_EXTENSION_DESTINATION
    if _is_reparse_point(source) or not source.is_dir():
        raise SystemExit("Edge extension source directory is missing or linked")
    paths = {path.name: path for path in source.iterdir()}
    if set(paths) != EDGE_EXTENSION_FILES or any(
        _is_reparse_point(path) or not path.is_file() for path in paths.values()
    ):
        raise SystemExit("Edge extension source differs from the fixed file inventory")
    originals = {name: path.read_bytes() for name, path in paths.items()}
    if onedir:
        packaged = executable.parent / "_internal" / EDGE_EXTENSION_DESTINATION
        if _is_reparse_point(packaged) or not packaged.is_dir():
            raise SystemExit("Packaged Edge extension is missing or linked")
        installed = {path.name: path for path in packaged.iterdir()}
        if set(installed) != EDGE_EXTENSION_FILES or any(
            _is_reparse_point(path) or not path.is_file() for path in installed.values()
        ):
            raise SystemExit("Packaged Edge extension file inventory differs from source")
        actual = {name: path.read_bytes() for name, path in installed.items()}
    else:
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(executable))
        prefix = EDGE_EXTENSION_DESTINATION + "/"
        entries = [(name.replace("\\", "/")[len(prefix):], name)
                   for name in archive.toc if name.replace("\\", "/").startswith(prefix)]
        if len(entries) != len(EDGE_EXTENSION_FILES) or {name for name, _ in entries} != EDGE_EXTENSION_FILES:
            raise SystemExit("Packaged Edge extension file inventory differs from source")
        actual = {name: archive.extract(entry) for name, entry in entries}
    if actual != originals:
        raise SystemExit("Packaged Edge extension bytes differ from current source")
    print("[OK] Packaged Edge extension: exact source bytes for all 7 files", flush=True)
    if include_profile_driver:
        validate_packaged_profile_driver(executable, onedir=onedir)


def validate_packaged_inventory(executable, manifest):
    expected = manifest.get("packaged_inventory")
    if not isinstance(expected, dict) or not expected.get("archive_entries") or not expected.get("python_modules"):
        raise SystemExit("Reviewed onefile component inventory is missing")
    if packaged_inventory(executable) != expected:
        raise SystemExit("Actual onefile components differ from the reviewed distribution inventory")


def helper_bundle_arguments(
    directory: Path, *, require_pinned: bool = False
) -> list[str]:
    """Validate trusted build input and include only the fixed bundle allowlist."""
    # Direct invocation puts scripts/, rather than the repository, on sys.path.
    # Keep the optional backend import out of extension-only builds.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pc_bridge.browser_backend import load_helper_bundle
    from pc_bridge.agent_browser_supervisor import AgentBrowserPreparationError

    # Do not resolve symlinks: the shared verifier must see and reject them.
    root = directory.absolute()
    try:
        bundle = load_helper_bundle(root, require_pinned=require_pinned)
    except AgentBrowserPreparationError as exc:
        raise SystemExit("Invalid agent-browser build bundle: " + str(exc)) from None

    arguments = [
        "--add-binary", str(bundle.binary) + ";" + HELPER_DESTINATION,
        "--upx-exclude", "agent-browser.exe",
        "--add-data", str(root / "manifest.json") + ";" + HELPER_DESTINATION,
    ]
    if bundle.build_manifest is not None:
        arguments.extend(("--add-data", str(bundle.build_manifest) + ";" + HELPER_DESTINATION))
    for relative in HELPER_NOTICES:
        path = root / relative
        maximum = (
            MAX_RUST_DEPENDENCY_NOTICE_BYTES
            if relative == RUST_DEPENDENCY_NOTICE else MAX_NOTICE_BYTES
        )
        try:
            if any(
                item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                for item in (path, *path.parents)
            ) or not path.is_file():
                raise ValueError
            with path.open("rb") as stream:
                contents = stream.read(maximum + 1)
            if relative in RAW_HELPER_NOTICES:
                import hashlib
                expected, encoding = RAW_HELPER_NOTICES[relative]
                if hashlib.sha256(contents).hexdigest() != expected:
                    raise ValueError
            else:
                encoding = "utf-8"
            if len(contents) > maximum or not contents.decode(encoding).strip():
                raise ValueError
        except (OSError, UnicodeError, ValueError):
            raise SystemExit(
                "Required agent-browser bundle notice is missing or invalid: " + relative
            ) from None
        destination = HELPER_DESTINATION
        if Path(relative).parent != Path("."):
            destination += "/" + Path(relative).parent.as_posix()
        arguments.extend(("--add-data", str(path) + ";" + destination))
    return arguments



def _reject_legacy(data, legacy, label):
    """Inspect plain bytes and nested ZIP payloads, including UTF-16 PE strings."""
    import io
    import zipfile
    lowered = data.lower()
    if any(legacy.lower().encode(encoding) in lowered
           for encoding in ("ascii", "utf-16-le", "utf-16-be")):
        raise SystemExit("Legacy Enterprise brand in " + label)
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as nested:
            names = nested.namelist()
            if len({name.replace("\\", "/").casefold() for name in names}) != len(names):
                raise SystemExit("Duplicate nested ZIP member in " + label)
            for name in names:
                _reject_legacy(name.encode("utf-8"), legacy, label + " member name")
                _reject_legacy(nested.read(name), legacy, label + "/" + name)


def _checked_decompress(data, label):
    """Reject suffixes and truncated streams that the canonical reader ignores."""
    import zlib
    stream = zlib.decompressobj()
    try:
        result = stream.decompress(data) + stream.flush()
    except zlib.error as exc:
        raise SystemExit("Invalid compressed stream in " + label) from exc
    if not stream.eof or stream.unused_data or stream.unconsumed_tail:
        raise SystemExit("Compressed stream was not fully consumed: " + label)
    return result


def _checked_archive(executable):
    """Use the canonical reader, additionally rejecting shadowed/aliased entries."""
    import struct
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    data = archive.raw_pkg_data()
    cookie = struct.unpack(archive._COOKIE_FORMAT, data[-archive._COOKIE_LENGTH:])
    _, length, toc_start, toc_length, pyvers, _ = cookie
    if length != len(data) or pyvers != sys.version_info.major * 100 + sys.version_info.minor:
        raise SystemExit("Invalid onefile archive length or Python version")
    if toc_start + toc_length != len(data) - archive._COOKIE_LENGTH:
        raise SystemExit("Invalid onefile table boundary")
    names, cursor, payload_end = set(), toc_start, 0
    while cursor < toc_start + toc_length:
        size, offset, packed, unpacked, compressed, kind = struct.unpack_from(
            archive._TOC_ENTRY_FORMAT, data, cursor)
        if size < archive._TOC_ENTRY_LENGTH or cursor + size > toc_start + toc_length:
            raise SystemExit("Invalid onefile entry boundary")
        name = data[cursor + archive._TOC_ENTRY_LENGTH:cursor + size].rstrip(b"\0").decode("utf-8")
        normalized = name.replace("\\", "/")
        if (not normalized or normalized.startswith("/") or
                any(part in ("", ".", "..") for part in normalized.split("/")) or
                normalized.casefold() in names or kind in (b"n", b"d")):
            raise SystemExit("Duplicate, linked or noncanonical onefile member: " + normalized)
        names.add(normalized.casefold())
        if kind != b"o":
            if offset != payload_end or offset + packed > toc_start or compressed not in (0, 1):
                raise SystemExit("Overlapping or unaccounted onefile payload")
            payload_end += packed
            payload = data[offset:offset + packed]
            if compressed:
                payload = _checked_decompress(payload, "onefile/" + normalized)
            if len(payload) != unpacked:
                raise SystemExit("Invalid onefile expanded member length")
        cursor += size
    if payload_end != toc_start:
        raise SystemExit("Unaccounted onefile payload")
    return archive


def validate_enterprise_brand(executable, helper, legacy):
    """Hash-bound, build-time validation; original external bytes are never rewritten."""
    import hashlib
    import importlib.util
    import io
    import marshal
    import struct
    import types
    from PyInstaller.archive.readers import PKG_ITEM_PYZ
    from PyInstaller.archive.writers import ZlibArchiveWriter
    from PyInstaller.loader.pyimod01_archive import PYZ_ITEM_MODULE, PYZ_ITEM_PKG, PYZ_ITEM_NSPKG
    external = {}
    if helper is not None:
        # Reuse the same fixed member allowlist and verifier as the build command.
        helper_bundle_arguments(helper, require_pinned=True)
        for relative in ("agent-browser.exe", "manifest.json", *HELPER_NOTICES):
            external[HELPER_DESTINATION + "/" + relative] = (helper / relative).read_bytes()
    archive = _checked_archive(executable)
    actual = {name.replace("\\", "/"): name for name in archive.toc}
    if not set(external) <= set(actual):
        raise SystemExit("Missing original external distribution member")
    for name, original in external.items():
        if (name == HELPER_DESTINATION + "/agent-browser.exe" and
                archive.toc[actual[name]][-1] != "b"):
            raise SystemExit("Unexpected external distribution member type: " + name)
        if archive.extract(actual[name]) != original:
            raise SystemExit("External distribution member differs from original: " + name)
    if any(name.startswith(HELPER_DESTINATION + "/") and name not in external for name in actual):
        raise SystemExit("Unexpected external distribution member")

    binary = executable.read_bytes()
    _reject_legacy(binary[:archive._start_offset], legacy, "outer PE")
    _reject_legacy(binary[archive._end_offset:], legacy, "outer PE overlay")
    pkg = archive.raw_pkg_data()
    toc_start = struct.unpack(archive._COOKIE_FORMAT, pkg[-archive._COOKIE_LENGTH:])[2]
    _reject_legacy(pkg[toc_start:], legacy, "onefile table and cookie")
    for name, entry in archive.toc.items():
        normalized = name.replace("\\", "/")
        _reject_legacy(normalized.encode("utf-8"), legacy, "onefile member name")
        if normalized in external:
            continue
        raw = archive.extract(name)
        if entry[-1] != PKG_ITEM_PYZ:
            _reject_legacy(raw, legacy, normalized)
            continue
        if raw[:8] != b"PYZ\0" + importlib.util.MAGIC_NUMBER:
            raise SystemExit("PYZ Python magic mismatch")
        toc_start = struct.unpack("!i", raw[8:12])[0]
        header_length = ZlibArchiveWriter._HEADER_LENGTH
        if not header_length <= toc_start <= len(raw):
            raise SystemExit("Invalid PYZ table boundary")
        stream = io.BytesIO(raw[toc_start:])
        rows = marshal.load(stream)
        if stream.tell() != len(raw) - toc_start or not isinstance(rows, list):
            raise SystemExit("Invalid PYZ table")
        if len({row[0].casefold() for row in rows}) != len(rows):
            raise SystemExit("Duplicate PYZ module")
        pyz = archive.open_embedded_archive(name)
        cursor = header_length
        for module, (kind, offset, size) in rows:
            _reject_legacy(module.encode("utf-8"), legacy, "PYZ module name")
            if kind not in (PYZ_ITEM_MODULE, PYZ_ITEM_PKG, PYZ_ITEM_NSPKG) or (kind == PYZ_ITEM_NSPKG and size):
                raise SystemExit("Invalid PYZ module type or namespace payload")
            if offset != cursor or offset + size > toc_start:
                raise SystemExit("Overlapping or unaccounted PYZ payload")
            cursor += size
            if kind != PYZ_ITEM_NSPKG:
                _checked_decompress(raw[offset:offset + size], "PYZ/" + module)
            payload = pyz.extract(module, raw=True)
            if payload is None:
                continue
            code_stream = io.BytesIO(payload)
            try:
                code = marshal.load(code_stream)
            except (EOFError, TypeError, ValueError):
                raise SystemExit("Invalid PYZ code object: " + module) from None
            if not isinstance(code, types.CodeType) or code_stream.tell() != len(payload):
                raise SystemExit("PYZ code payload was not fully consumed: " + module)
            _reject_legacy(payload, legacy, "PYZ/" + module)
        if cursor != toc_start:
            raise SystemExit("Unaccounted PYZ payload")
        _reject_legacy(raw[:header_length], legacy, "PYZ header")
        _reject_legacy(raw[toc_start:], legacy, "PYZ table")
    for option in archive.options:
        _reject_legacy(option.encode("utf-8"), legacy, "onefile option")
    return hashlib.sha256(binary).hexdigest()


def apply_windows_runtime_redist(specification, directory):
    """Use Windows 10+ OS UCRT and hash-pinned Microsoft x64 MFC/VC runtime."""
    import hashlib
    import json
    directory = directory.absolute()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    required = {"mfc140u.dll", "vcruntime140.dll", "vcruntime140_1.dll"}
    files = manifest.get("files", [])
    if sys.platform != "win32" or {item.get("name") for item in files} != required or len(files) != 3:
        raise SystemExit("Invalid Windows runtime REDIST inventory")
    replacements = []
    for item in files:
        path = directory / item["name"]
        if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)):
            raise SystemExit("Linked Windows runtime REDIST input")
        data = path.read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise SystemExit("Windows runtime REDIST hash mismatch")
        replacements.append((item["name"], str(path), "BINARY"))
    source = specification.read_text(encoding="utf-8")
    marker = "pyz = PYZ(a.pure)"
    if source.count(marker) != 1:
        raise SystemExit("Unexpected PyInstaller specification layout")
    native = "\n# Windows 10+ resolves API sets/UCRT from the OS; keep MFC functionality.\n"
    native += "_redist = " + repr(replacements) + "\n"
    native += "_replace = {row[0].lower() for row in _redist}\n"
    native += "a.binaries = [row for row in a.binaries if row[0].lower() not in _replace and row[0].lower() != 'ucrtbase.dll' and not row[0].lower().startswith(('api-ms-win-core-', 'api-ms-win-crt-'))] + _redist\n"
    specification.write_text(source.replace(marker, native + marker), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-runtime-redist", type=Path, help="Hash-pinned official x64 Microsoft REDIST; Windows 10+ OS UCRT")
    parser.add_argument("--enterprise-legacy-brand", help="Require exact external identity and decompressed Enterprise branding checks")
    parser.add_argument("--onedir", action="store_true")
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--name", default="AoiTalk-PC-Bridge")
    parser.add_argument("--dist-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--install-path", type=Path)
    parser.add_argument("--distribution-notices", type=Path,
                        help="Reviewed, hash-pinned recipient notice/source bundle")
    parser.add_argument("--require-distribution-notices", action="store_true",
                        help="Require completed component inventory for a distribution build")
    parser.add_argument("--audit-distribution", action="store_true",
                        help="Emit actual onefile inventory for review; requires --no-install")
    parser.add_argument(
        "--agent-browser-bundle", type=Path,
        help="Trusted custom Windows helper bundle to validate and embed (build input only)",
    )
    args = parser.parse_args(argv)
    if (args.require_distribution_notices or args.enterprise_legacy_brand or args.audit_distribution) and args.agent_browser_bundle is None:
        raise SystemExit("PC Bridge distribution candidates require --agent-browser-bundle")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,95}", args.name):
        raise SystemExit("Executable name must match [A-Za-z][A-Za-z0-9_.-]{0,95}")
    if args.enterprise_legacy_brand and (args.distribution_notices is not None or
                                         args.require_distribution_notices or args.audit_distribution or
                                         args.onedir or not args.no_install or
                                         not re.fullmatch(r"[A-Za-z]+", args.enterprise_legacy_brand)):
        raise SystemExit("Enterprise builds use onefile, --no-install, and no additional distribution notices")
    notice_arguments = []
    reviewed_notice_files = None
    if args.require_distribution_notices and args.distribution_notices is None:
        raise SystemExit("Distribution builds require --distribution-notices")
    if args.require_distribution_notices and args.onedir:
        raise SystemExit("Reviewed distribution builds must use the onefile format")
    if args.audit_distribution and (not args.no_install or args.onedir or args.require_distribution_notices):
        raise SystemExit("Distribution audit requires --no-install, onefile, and no release gate flag")
    if args.distribution_notices is not None:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from pc_bridge.license_bundle import LicenseBundleError, build_arguments, load_bundle
        try:
            if args.windows_runtime_redist is not None:
                # Keep the makespec command bounded even when the reviewed
                # bundle contains hundreds of explicitly inventoried files.
                # The exact bytes are staged after the work root exists.
                manifest, reviewed_notice_files = load_bundle(
                    args.distribution_notices,
                    require_complete=args.require_distribution_notices,
                )
                if args.require_distribution_notices:
                    validate_distribution_notice_policy(manifest, reviewed_notice_files)
                    validate_distribution_environment(manifest, args.agent_browser_bundle)
            else:
                notice_arguments = build_arguments(
                    args.distribution_notices,
                    require_complete=args.require_distribution_notices,
                )
                if args.require_distribution_notices:
                    manifest, files = load_bundle(args.distribution_notices, require_complete=True)
                    validate_distribution_notice_policy(manifest, files)
                    validate_distribution_environment(manifest, args.agent_browser_bundle)
        except LicenseBundleError as exc:
            raise SystemExit("Invalid distribution notices: " + str(exc)) from None
    helper_arguments = (
        helper_bundle_arguments(
            args.agent_browser_bundle,
            require_pinned=args.require_distribution_notices or bool(args.enterprise_legacy_brand),
        )
        if args.agent_browser_bundle is not None else []
    )

    default_work = ROOT / ".local" / "portable-bridge" / "pyinstaller"
    dist_root = (args.dist_root or (default_work / ("dist" if args.onedir else "onefile"))).resolve()
    work_root = (args.work_root or (default_work / "build")).resolve()
    spec_root = work_root / "spec"
    dist_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    spec_root.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable, "-B", "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", args.name, "--console",
        "--onedir" if args.onedir else "--onefile",
        "--distpath", str(dist_root),
        "--workpath", str(work_root),
        "--specpath", str(spec_root),
        "--paths", str(ROOT),
        "--collect-submodules", "pywinauto",
        "--hidden-import", "comtypes",
        "--hidden-import", "pythoncom",
        "--hidden-import", "win32security",
        "--hidden-import", "psutil",
        "--collect-data", "playwright",
        "--add-data", str(ROOT / "resources/browser-profile-driver") + ";resources/browser-profile-driver",
        "--hidden-import", "win32crypt",
        "--add-data", str(ROOT / "resources/edge-browser-extension") + ";resources/edge-browser-extension",
        *helper_arguments,
        *notice_arguments,
        str(ROOT / "scripts/pc_bridge_main.py"),
    ]
    staged_notice_root = None
    if args.windows_runtime_redist is not None:
        try:
            if reviewed_notice_files is not None:
                staged_notice_root = stage_distribution_notices(reviewed_notice_files, work_root)
            make = [sys.executable, "-B", "-m", "PyInstaller.utils.cliutils.makespec"]
            # Omit only the build-only flags and the per-file reviewed notice
            # entries.  The latter are replaced by one verified directory
            # entry below; helper and extension data remain explicit.
            index = 5
            while index < len(command) - 1:
                option = command[index]
                if option in ("--noconfirm", "--clean"):
                    index += 1
                elif option in ("--distpath", "--workpath"):
                    index += 2
                elif (
                    staged_notice_root is not None
                    and option == "--add-data"
                    and _is_distribution_notice_data(command[index + 1])
                ):
                    index += 2
                else:
                    make.append(option)
                    index += 1
            if staged_notice_root is not None:
                make.extend((
                    "--add-data",
                    str(staged_notice_root) + ";" + DISTRIBUTION_NOTICE_DESTINATION,
                ))
            make.append(command[-1])
            subprocess.run(make, cwd=ROOT, check=True)
            specification = spec_root / (args.name + ".spec")
            apply_windows_runtime_redist(specification, args.windows_runtime_redist)
            command = [sys.executable, "-B", "-m", "PyInstaller", "--noconfirm", "--clean",
                       "--distpath", str(dist_root), "--workpath", str(work_root), str(specification)]
            subprocess.run(command, cwd=ROOT, check=True)
        finally:
            if staged_notice_root is not None:
                shutil.rmtree(staged_notice_root, ignore_errors=True)
    else:
        subprocess.run(command, cwd=ROOT, check=True)
    exe = dist_root / args.name / f"{args.name}.exe" if args.onedir else dist_root / f"{args.name}.exe"
    validate_packaged_extension(exe, onedir=args.onedir, include_profile_driver=True)
    if args.require_distribution_notices:
        validate_packaged_inventory(exe, manifest)
    enterprise_sha = None
    if args.enterprise_legacy_brand:
        enterprise_sha = validate_enterprise_brand(exe, args.agent_browser_bundle, args.enterprise_legacy_brand)
    subprocess.run([str(exe), "--self-test"], cwd=work_root, check=True)
    subprocess.run([str(exe), "--verify-profile-driver"], cwd=work_root, check=True)
    if args.agent_browser_bundle is not None:
        subprocess.run([str(exe), "--verify-browser-helper"], cwd=work_root, check=True)
    if args.distribution_notices is not None:
        # CLI checks are offline and run before installing the resulting EXE.
        # An existing export must never be overwritten by either code path.
        import zipfile
        from pc_bridge.license_bundle import load_bundle
        _, originals = load_bundle(args.distribution_notices, require_complete=args.require_distribution_notices)
        with tempfile.TemporaryDirectory(prefix="notice-verification-", dir=work_root) as verification:
            exported = Path(verification) / "licenses.zip"
            subprocess.run([str(exe), "--export-licenses", str(exported)], cwd=work_root, check=True)
            with zipfile.ZipFile(exported) as archive:
                if set(archive.namelist()) != set(originals) or len(archive.namelist()) != len(originals):
                    raise SystemExit("Packaged license inventory mismatch")
                if any(archive.read(name) != original for name, original in originals.items()):
                    raise SystemExit("Packaged license bytes do not match originals")

    if args.audit_distribution:
        import hashlib
        import importlib.metadata
        import json
        audit = {
            "status": "audit_only_not_distribution_clearance",
            "executable": exe.name,
            "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
            "packaged_inventory": packaged_inventory(exe),
            "build_environment": {"platform": sys.platform, "python_version": sys.version.split()[0]},
            "installed_distribution_versions": {
                re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower(): dist.version
                for dist in importlib.metadata.distributions() if dist.metadata.get("Name")
            },
            "installed_distributions_are_not_runtime_proof": True,
        }
        audit_path = dist_root / (args.name + ".distribution-audit.json")
        audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if not args.onedir and not args.no_install:
        install_path = (args.install_path or (ROOT / exe.name)).resolve()
        install_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(exe, install_path)
        except PermissionError:
            raise SystemExit(
                "BridgeとEdge拡張の接続を終了してから、ビルド済みexeを配置してください: " + str(exe)
            )
        exe = install_path
    if enterprise_sha is not None:
        print("ENTERPRISE_BRAND_SHA256=" + enterprise_sha, flush=True)
    print("EXE=" + str(exe), flush=True)


if __name__ == "__main__":
    main()
