#!/usr/bin/env python3
"""Prepare the pinned Qwen3.8-Flash-Next PR #27836 MTP variant.

The normal managed-model downloader intentionally knows only about the
original Unsloth three-shard model.  This helper prepares the *derived*
four-shard embedded variant in a separate directory (the reference graft
script writes beside the supplied base shards).  It downloads and verifies
the exact public reference head and graft implementation before executing
them.  The upstream script's POSIX ``cp --reflink`` path is replaced at
runtime with a checked ``shutil.copyfile`` implementation on every platform,
which is safe on Windows but may consume the full input-shard disk space.

The reference script is not vendored because its standalone file has no
license header.  Provenance is pinned by repository, immutable revision and
SHA-256 below.  The reference repository states that the head is derived
under the Qwen Community License 1.0; users are responsible for complying
with that license and the target model's license.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import runpy
import shutil
import sys
from pathlib import Path
from typing import Any

REFERENCE_REPOSITORY = "jlkivey/Qwen3.8-Flash-Next-MTP-PR27836-GGUF"
REFERENCE_REVISION = "159500b54d79ffc008400c60ba1024bf745042ee"
HEAD_FILENAME = "mtp-Qwen3.8-Flash-Next-Q8_0.gguf"
HEAD_SIZE = 4_135_893_152
HEAD_SHA256 = "ee87df0ecae89d759758667e9a1012d09299806f7805efc266eb5fe84773d4c9"
GRAFT_FILENAME = "graft-mtp-shard.py"
GRAFT_SHA256 = "431ee3ff35306a3710f49f00140d4346b157a5acb5089c53d55218e1ffb88898"

BASE_FILENAMES = (
    "Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf",
)
OUTPUT_FILENAMES = (
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00001-of-00004.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00002-of-00004.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00003-of-00004.gguf",
    "Qwen3.8-Flash-Next-UD-IQ4_XS-MTP-00004-of-00004.gguf",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_regular_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(
            f"{label} must be a regular non-symlink file: {path}"
        )
    return path


def verify_digest(
    path: Path,
    expected_sha256: str,
    *,
    expected_size: int | None = None,
    label: str,
) -> None:
    require_regular_file(path, label)
    if expected_size is not None:
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise RuntimeError(
                f"{label} size mismatch: {path} expected={expected_size} "
                f"actual={actual_size}"
            )
    actual_sha256 = sha256_file(path)
    if actual_sha256.casefold() != expected_sha256.casefold():
        raise RuntimeError(
            f"{label} SHA256 mismatch: {path} expected={expected_sha256} "
            f"actual={actual_sha256}"
        )


def validate_base(primary: Path) -> tuple[Path, ...]:
    """Validate the exact original three-shard input contract."""

    try:
        primary = primary.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"base-primary cannot be resolved: {primary}") from exc
    if primary.name != BASE_FILENAMES[0]:
        raise RuntimeError(
            "base-primary must be the exact Unsloth UD-IQ4_XS first shard: "
            f"{BASE_FILENAMES[0]}"
        )
    paths = tuple(primary.parent / filename for filename in BASE_FILENAMES)
    for path in paths:
        require_regular_file(path, "base shard")
    return paths


def download_reference(cache_dir: Path) -> tuple[Path, Path]:
    """Fetch the pinned head/script and verify immutable bytes."""

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "huggingface_hub is required; install it in the project venv"
        ) from exc

    cache_dir = cache_dir.expanduser()
    if cache_dir.exists() and cache_dir.is_symlink():
        raise RuntimeError(f"reference cache directory must not be a symlink: {cache_dir}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    head = Path(
        hf_hub_download(
            repo_id=REFERENCE_REPOSITORY,
            revision=REFERENCE_REVISION,
            filename=HEAD_FILENAME,
            local_dir=str(cache_dir),
        )
    )
    graft = Path(
        hf_hub_download(
            repo_id=REFERENCE_REPOSITORY,
            revision=REFERENCE_REVISION,
            filename=GRAFT_FILENAME,
            local_dir=str(cache_dir),
        )
    )
    verify_digest(
        head,
        HEAD_SHA256,
        expected_size=HEAD_SIZE,
        label="reference MTP head",
    )
    verify_digest(graft, GRAFT_SHA256, label="reference graft script")
    return head.resolve(strict=True), graft.resolve(strict=True)


def _resolve_gguf_python(
    explicit: Path | None,
    source_root: Path | None,
) -> Path | None:
    """Find the llama.cpp ``gguf-py`` package without changing the venv."""

    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser())
    env_path = os.getenv("LLAMA_CPP_GGUF_PY")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    if source_root is not None:
        candidates.append(source_root.expanduser() / "gguf-py")

    for candidate in candidates:
        if (candidate / "gguf").is_dir():
            return candidate.resolve()
    try:
        import gguf
    except ImportError:
        return None
    package_file = getattr(gguf, "__file__", None)
    if not package_file:
        return None
    try:
        package_root = Path(str(package_file)).resolve().parent
    except (OSError, RuntimeError):
        return None
    # ``gguf-py`` installed as a package exposes ``<root>/gguf``.  Returning
    # its parent lets the reference script import the same package without
    # requiring an explicit checkout path.
    return package_root.parent if package_root.name == "gguf" else package_root


def _safe_clone_file(src: Path, dst: Path) -> None:
    """Clone a source shard without shelling out to an unsafe copy command."""

    require_regular_file(src, "source shard")
    cursor = dst.parent
    while True:
        if cursor.is_symlink():
            raise RuntimeError(
                f"clone destination directory must not be a symlink: {cursor}"
            )
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(dst), flags, 0o666)
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to overwrite clone destination: {dst}") from exc
    try:
        with src.open("rb") as source, os.fdopen(fd, "wb") as destination:
            fd = -1
            shutil.copyfileobj(source, destination, length=16 * 1024 * 1024)
            destination.flush()
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            if dst.is_file() and not dst.is_symlink():
                dst.unlink()
        except OSError:
            pass
        raise
    if dst.is_symlink() or not dst.is_file():
        raise RuntimeError(f"clone destination is not a regular file: {dst}")


def _run_reference_script(
    script: Path,
    first_shard: Path,
    head: Path,
    *,
    verify: bool = False,
    dry_run: bool = False,
    force: bool = False,
    gguf_python: Path | None = None,
) -> None:
    """Run only the hash-verified reference implementation.

    ``runpy`` lets us replace the reference script's POSIX-only ``clone_file``
    function without modifying the downloaded bytes or invoking a shell.
    """

    if gguf_python is not None:
        sys.path.insert(0, str(gguf_python))
    try:
        module: dict[str, Any] = runpy.run_path(str(script))
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "the pinned graft script needs llama.cpp gguf-py and numpy; "
            "pass --gguf-py <llama.cpp>/gguf-py"
        ) from exc
    # ``runpy.run_path`` may return a copy of the globals dictionary on some
    # Python versions.  Patch the actual function globals as well as the
    # returned mapping so ``do_graft`` cannot fall back to its POSIX ``cp``
    # implementation on Windows.
    module["clone_file"] = _safe_clone_file
    for function_name in ("do_graft", "main"):
        function = module.get(function_name)
        function_globals = getattr(function, "__globals__", None)
        if isinstance(function_globals, dict):
            function_globals["clone_file"] = _safe_clone_file
    main = module.get("main")
    if not callable(main):
        raise RuntimeError("pinned graft script did not expose main()")

    argv = [str(script), str(first_shard), str(head)]
    if verify:
        argv.append("--verify")
    if dry_run:
        argv.append("--dry-run")
    if force:
        argv.append("--force")
    previous_argv = sys.argv
    sys.argv = argv
    try:
        try:
            main()
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise RuntimeError(
                    f"pinned graft script failed with exit code {exc.code}"
                ) from exc
    finally:
        sys.argv = previous_argv


def expected_outputs(base_primary: Path) -> tuple[Path, ...]:
    return tuple(base_primary.parent / filename for filename in OUTPUT_FILENAMES)


def prepare(args: argparse.Namespace) -> None:
    base_paths = validate_base(args.base_primary)
    base_snapshots = tuple(
        (path.stat().st_size, sha256_file(path)) for path in base_paths
    ) if not args.dry_run else ()
    head, graft = download_reference(args.reference_cache)
    gguf_python = _resolve_gguf_python(args.gguf_py, args.source_root)
    if gguf_python is None:
        raise RuntimeError(
            "llama.cpp gguf-py was not found; pass --gguf-py <llama.cpp>/gguf-py"
        )

    print(f"reference repository: {REFERENCE_REPOSITORY}")
    print(f"reference revision: {REFERENCE_REVISION}")
    print(f"head: {head} (sha256={HEAD_SHA256})")
    print(f"graft script: {graft} (sha256={GRAFT_SHA256})")
    if base_snapshots:
        print("base snapshots before graft:")
        for path, (size, digest) in zip(base_paths, base_snapshots):
            print(f"  {path.name}: size={size} sha256={digest}")
    outputs = expected_outputs(base_paths[0])

    if args.verify_only:
        _run_reference_script(
            graft,
            base_paths[0],
            head,
            verify=True,
            gguf_python=gguf_python,
        )
    else:
        stale_temps = [
            path.with_name(path.name + ".tmp")
            for path in outputs
            if path.with_name(path.name + ".tmp").exists()
            or path.with_name(path.name + ".tmp").is_symlink()
        ]
        if stale_temps:
            raise RuntimeError(
                "stale derived MTP temporary output exists; verify no graft "
                "process is running, then remove only these files: "
                + ", ".join(str(path) for path in stale_temps)
            )
        existing = [path for path in outputs if path.exists() or path.is_symlink()]
        if existing and not args.force:
            raise RuntimeError(
                "refusing to overwrite existing derived MTP shard(s): "
                + ", ".join(str(path) for path in existing)
                + " (pass --force only for derived outputs)"
            )
        _run_reference_script(
            graft,
            base_paths[0],
            head,
            dry_run=args.dry_run,
            force=args.force,
            gguf_python=gguf_python,
        )
        if not args.dry_run:
            _run_reference_script(
                graft,
                base_paths[0],
                head,
                verify=True,
                gguf_python=gguf_python,
            )
            for path in outputs:
                require_regular_file(path, "derived MTP shard")
            after_snapshots = tuple(
                (path.stat().st_size, sha256_file(path)) for path in base_paths
            )
            if after_snapshots != base_snapshots:
                raise RuntimeError(
                    "base shard bytes changed while preparing the derived variant"
                )
            print("base snapshots after graft: unchanged")
            for path, (size, digest) in zip(base_paths, after_snapshots):
                print(f"  {path.name}: size={size} sha256={digest}")

    print("derived embedded MTP variant:")
    for path in outputs:
        print(f"  {path}")
    print(f"primary: {outputs[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the pinned Qwen3.8-Flash-Next PR27836 MTP variant"
    )
    parser.add_argument(
        "--base-primary",
        required=True,
        type=Path,
        help="exact Unsloth UD-IQ4_XS 00001-of-00003 GGUF",
    )
    parser.add_argument(
        "--reference-cache",
        type=Path,
        default=Path.home() / ".cache" / "aoitalk" / "qwen38-flash-next-mtp-pr27836",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="llama.cpp checkout containing gguf-py (optional)",
    )
    parser.add_argument(
        "--gguf-py",
        type=Path,
        help="explicit llama.cpp gguf-py directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the pinned head and graft plan without writing outputs",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify an already-generated four-shard variant",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow replacing existing derived outputs (never the base shards)",
    )
    args = parser.parse_args()
    if args.dry_run and args.verify_only:
        parser.error("--dry-run and --verify-only are mutually exclusive")
    prepare(args)
    return 0


# FLASH_MTP_REVIEW_FIX_V3_PROCESS_LOCK
class _ExclusivePrepareProcessLock:
    """Cross-process lock for the Flash-Next MTP preparation CLI.

    The helper uses deterministic temporary output names directly or through
    the pinned upstream graft script. Only one CLI invocation for this helper
    may manipulate those names at a time. The lock file is persistent; the OS
    lock itself is released automatically on process exit/crash.
    """

    def __init__(self):
        import hashlib
        import tempfile

        digest = hashlib.sha256(
            str(Path(__file__).resolve()).encode("utf-8")
        ).hexdigest()[:20]
        self.path = (
            Path(tempfile.gettempdir())
            / f"aoitalk-qwen38-flash-next-mtp-{digest}.lock"
        )
        self.handle = None

    def __enter__(self):
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError(
                        "another Qwen3.8-Flash-Next MTP preparation "
                        "is already running"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except OSError as exc:
                    raise RuntimeError(
                        "another Qwen3.8-Flash-Next MTP preparation "
                        "is already running"
                    ) from exc
        except Exception:
            handle.close()
            raise
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc, tb):
        import os

        handle = self.handle
        self.handle = None
        if handle is None:
            return False
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        return False


if __name__ == "__main__":
    with _ExclusivePrepareProcessLock():
        raise SystemExit(main())
