"""フロントエンド（Next.js）ビルド指紋・静的アセット検査。

フロントエンドの依存修復、ビルド指紋の算出/保存、`.next` 静的アセット参照の
整合性検査、そして必要時のみ再ビルドを行うヘルパー群。挙動は分割前の
`service_manager.py` と同一（機械的移設）。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

from ._process_utils import _IS_WINDOWS, _read_log_tail

_FRONTEND_BUILD_FINGERPRINT_VERSION = 2
_FRONTEND_BUILD_FINGERPRINT_REL_PATH = Path(".next") / "aoitalk-build-fingerprint.json"
_FRONTEND_BUILD_EXCLUDED_DIR_NAMES = {
    ".git",
    ".next",
    ".turbo",
    "coverage",
    "node_modules",
    "playwright-report",
    "test-results",
}
_FRONTEND_BUILD_EXCLUDED_FILE_NAMES = {
    ".DS_Store",
    # Next.js rewrites this ignored shim to point at the active distDir's
    # generated route declarations.  It is build output, not source input.
    "next-env.d.ts",
}
_FRONTEND_TRANSIENT_QA_FILE_PATTERNS = (
    re.compile(r"^\.playwright-focus[^/]*\.cjs$"),
    re.compile(r"^e2e/\.focus-live[^/]*\.spec\.ts$"),
)
_FRONTEND_BUILD_INPUT_SUFFIXES = {
    ".cjs",
    ".css",
    ".gif",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".md",
    ".mdx",
    ".mjs",
    ".png",
    ".scss",
    ".svg",
    ".ts",
    ".tsx",
    ".ttf",
    ".txt",
    ".webp",
    ".woff",
    ".woff2",
    ".yaml",
    ".yml",
}
_FRONTEND_STATIC_ASSET_SUFFIXES = {
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".map",
    ".png",
    ".svg",
    ".ttf",
    ".txt",
    ".webp",
    ".woff",
    ".woff2",
}
_FRONTEND_STATIC_REF_PATTERN = re.compile(
    r"(?:/_next/|_next/)?static/[^\s\"'`<>)\]}]+"
)
_FRONTEND_NEXT_GENERATED_TYPES_INCLUDE_PATTERN = re.compile(
    r"[\"']\.next(?:-[^/\"'\\]+)?[\\/](?:[^/\"'\\]+[\\/])*"
    r"types[\\/]\*\*[\\/]\*\.ts[\"']"
)


def _npm_command() -> str:
    return "npm.cmd" if _IS_WINDOWS else "npm"


def _next_bin_path(frontend_dir: Path) -> Path:
    bin_name = "next.cmd" if _IS_WINDOWS else "next"
    return frontend_dir / "node_modules" / ".bin" / bin_name


def _ensure_frontend_dependencies(project_root: Path, log_path: Path) -> None:
    """Repair missing npm executable links before starting Next.js."""
    frontend_dir = project_root / "frontend"
    if _next_bin_path(frontend_dir).exists():
        return

    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(
            "Next.js executable link is missing; running npm ci before startup.\n"
        )
        log_file.flush()
        result = subprocess.run(
            [_npm_command(), "ci"],
            cwd=str(frontend_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Frontend dependencies are not ready and npm ci failed.\n"
            f"frontend.log tail:\n{_read_log_tail(log_path)}"
        )

    if not _next_bin_path(frontend_dir).exists():
        raise RuntimeError(
            "npm ci completed, but Next.js executable link is still missing.\n"
            f"frontend.log tail:\n{_read_log_tail(log_path)}"
        )


def _frontend_build_fingerprint_path(frontend_dir: Path) -> Path:
    return frontend_dir / _FRONTEND_BUILD_FINGERPRINT_REL_PATH


def _is_frontend_build_input(path: Path, frontend_dir: Path) -> bool:
    try:
        relative = path.relative_to(frontend_dir)
    except ValueError:
        return False

    if any(
        part in _FRONTEND_BUILD_EXCLUDED_DIR_NAMES or part.startswith(".next-")
        for part in relative.parts
    ):
        return False
    if path.name in _FRONTEND_BUILD_EXCLUDED_FILE_NAMES:
        return False
    # Computer-use/Playwright QA may leave hidden focus harness files in the
    # frontend tree while a live session is being debugged.  They are never
    # imported by the production bundle, and including them here makes every
    # subsequent startup rebuild the otherwise valid .next output.  Keep this
    # exclusion deliberately narrow so ordinary tracked source and test files
    # remain part of the production TypeScript fingerprint.
    if any(
        pattern.fullmatch(relative.as_posix())
        for pattern in _FRONTEND_TRANSIENT_QA_FILE_PATTERNS
    ):
        return False
    if path.name.startswith(".env"):
        return False
    return path.suffix.lower() in _FRONTEND_BUILD_INPUT_SUFFIXES


def _iter_frontend_build_input_files(frontend_dir: Path) -> list[Path]:
    if not frontend_dir.is_dir():
        return []
    input_files: list[Path] = []
    for root, dir_names, file_names in os.walk(frontend_dir):
        dir_names[:] = [
            name
            for name in dir_names
            if name not in _FRONTEND_BUILD_EXCLUDED_DIR_NAMES
            and not name.startswith(".next-")
        ]
        root_path = Path(root)
        for file_name in file_names:
            path = root_path / file_name
            if _is_frontend_build_input(path, frontend_dir):
                input_files.append(path)
    return sorted(input_files)


def _skip_jsonc_trivia(text: str, index: int) -> int:
    """Skip whitespace and JSONC comments beginning at ``index``."""
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        break
    return index


def _scan_jsonc_string_end(text: str, index: int) -> int:
    """Return the first index after a quoted JSONC string."""
    quote = text[index]
    cursor = index + 1
    escaped = False
    while cursor < len(text):
        char = text[cursor]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return cursor + 1
        cursor += 1
    return len(text)


def _scan_jsonc_comment_end(text: str, index: int) -> int | None:
    """Return the first index after a JSONC comment, or ``None``."""
    if text.startswith("//", index):
        newline = text.find("\n", index + 2)
        return len(text) if newline < 0 else newline + 1
    if text.startswith("/*", index):
        end = text.find("*/", index + 2)
        return len(text) if end < 0 else end + 2
    return None


def _find_jsonc_array_end(text: str, opening_index: int) -> int | None:
    """Find the closing bracket for a JSONC array.

    A character scanner is used instead of a regular expression so brackets in
    strings or comments cannot be mistaken for the array terminator.
    """
    depth = 1
    cursor = opening_index + 1
    while cursor < len(text):
        char = text[cursor]
        if char in {"\"", "'"}:
            cursor = _scan_jsonc_string_end(text, cursor)
            continue
        comment_end = _scan_jsonc_comment_end(text, cursor)
        if comment_end is not None:
            cursor = comment_end
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _find_jsonc_include_body(text: str) -> tuple[int, int] | None:
    """Locate the body span of the root ``include`` array in JSONC text."""
    cursor = 0
    object_depth = 0
    while cursor < len(text):
        cursor = _skip_jsonc_trivia(text, cursor)
        if cursor >= len(text):
            break
        char = text[cursor]
        if char in {"\"", "'"}:
            string_end = _scan_jsonc_string_end(text, cursor)
            if (
                object_depth == 1
                and text[cursor + 1 : max(cursor + 1, string_end - 1)]
                == "include"
            ):
                value_cursor = _skip_jsonc_trivia(text, string_end)
                if value_cursor < len(text) and text[value_cursor] == ":":
                    value_cursor = _skip_jsonc_trivia(text, value_cursor + 1)
                    if value_cursor < len(text) and text[value_cursor] == "[":
                        closing_index = _find_jsonc_array_end(text, value_cursor)
                        if closing_index is not None:
                            return value_cursor + 1, closing_index
            cursor = string_end
            continue
        comment_end = _scan_jsonc_comment_end(text, cursor)
        if comment_end is not None:
            cursor = comment_end
            continue
        if char == "{":
            object_depth += 1
        elif char == "}":
            object_depth = max(0, object_depth - 1)
        cursor += 1
    return None


def _iter_jsonc_array_entries(body: str) -> list[str]:
    """Split a JSONC array body at top-level commas."""
    entries: list[str] = []
    entry_start = 0
    cursor = 0
    nested_depth = 0
    while cursor < len(body):
        char = body[cursor]
        if char in {"\"", "'"}:
            cursor = _scan_jsonc_string_end(body, cursor)
            continue
        comment_end = _scan_jsonc_comment_end(body, cursor)
        if comment_end is not None:
            cursor = comment_end
            continue
        if char in "[{":
            nested_depth += 1
        elif char in "]}":
            nested_depth = max(0, nested_depth - 1)
        elif char == "," and nested_depth == 0:
            entries.append(body[entry_start:cursor])
            entry_start = cursor + 1
        cursor += 1
    entries.append(body[entry_start:])
    return entries


def _is_next_generated_types_include_entry(entry: str) -> bool:
    """Return whether one JSONC include entry is a Next generated type glob."""
    cursor = _skip_jsonc_trivia(entry, 0)
    if cursor >= len(entry) or entry[cursor] not in {"\"", "'"}:
        return False
    string_end = _scan_jsonc_string_end(entry, cursor)
    if not _FRONTEND_NEXT_GENERATED_TYPES_INCLUDE_PATTERN.fullmatch(
        entry[cursor:string_end]
    ):
        return False
    return _skip_jsonc_trivia(entry, string_end) == len(entry)


def _normalize_frontend_tsconfig_for_fingerprint(text: str) -> str:
    """Remove only Next.js generated type include entries from tsconfig.

    Next.js adds the active ``distDir``'s route type glob to ``include``.  The
    active distDir may be ``.next`` or any ``.next-*`` QA/verification path, so
    retaining that generated path would make a production fingerprint change
    every time another build profile runs.  Keep all other tsconfig content
    byte-for-byte so genuine compiler/include setting changes remain tracked.
    """
    include_span = _find_jsonc_include_body(text)
    if include_span is None:
        return text

    body_start, body_end = include_span
    body = text[body_start:body_end]
    entries = _iter_jsonc_array_entries(body)
    kept_entries = [
        entry
        for entry in entries
        if not _is_next_generated_types_include_entry(entry)
    ]
    normalized_body = ",".join(kept_entries)
    return (
        text[:body_start]
        + normalized_body
        + text[body_end:]
    )


def _frontend_build_input_bytes(path: Path) -> bytes:
    """Read one fingerprint input, normalizing Next-generated tsconfig paths."""
    content = path.read_bytes()
    if path.name != "tsconfig.json":
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return _normalize_frontend_tsconfig_for_fingerprint(text).encode("utf-8")


def _frontend_build_input_hash(path: Path) -> bytes:
    digest = hashlib.sha256()
    if path.name == "tsconfig.json":
        digest.update(_frontend_build_input_bytes(path))
    else:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.digest()


def _frontend_build_fingerprint(frontend_dir: Path) -> dict[str, object]:
    # Cold Windows file reads dominated startup. Read a bounded number in
    # parallel, retaining content-based checks (including same-size edits with
    # preserved timestamps), and combine hashes in deterministic path order.
    digest = hashlib.sha256()
    paths = _iter_frontend_build_input_files(frontend_dir)
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="frontend-input") as pool:
        for path, file_hash in zip(paths, pool.map(_frontend_build_input_hash, paths)):
            relative = path.relative_to(frontend_dir).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_hash)
            digest.update(b"\0")

    return {
        "version": _FRONTEND_BUILD_FINGERPRINT_VERSION,
        "digest": digest.hexdigest(),
        "file_count": len(paths),
    }


def _read_frontend_build_fingerprint(frontend_dir: Path) -> dict[str, object] | None:
    try:
        return json.loads(
            _frontend_build_fingerprint_path(frontend_dir).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None


def _write_frontend_build_fingerprint(
    frontend_dir: Path,
    fingerprint: dict[str, object],
) -> None:
    path = _frontend_build_fingerprint_path(frontend_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(fingerprint, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _normalize_next_static_asset_ref(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    ref = value.strip().strip("\"'`")
    if ref.startswith("/_next/"):
        ref = ref[len("/_next/") :]
    elif ref.startswith("_next/"):
        ref = ref[len("_next/") :]
    elif ref.startswith("/static/"):
        ref = ref[1:]

    if not ref.startswith("static/"):
        return None

    ref = unquote(ref.split("?", 1)[0].split("#", 1)[0])
    ref = ref.rstrip(";,")
    parts = [part for part in ref.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    if "." not in parts[-1]:
        return None
    if Path(parts[-1]).suffix.lower() not in _FRONTEND_STATIC_ASSET_SUFFIXES:
        return None
    return "/".join(parts)


def _iter_static_refs_from_json(value: object) -> set[str]:
    refs: set[str] = set()
    normalized = _normalize_next_static_asset_ref(value)
    if normalized:
        refs.add(normalized)
        return refs

    if isinstance(value, dict):
        for item in value.values():
            refs.update(_iter_static_refs_from_json(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_iter_static_refs_from_json(item))
    return refs


def _iter_static_refs_from_text(text: str) -> set[str]:
    refs: set[str] = set()
    for candidate_text in {text, text.replace("\\/", "/")}:
        for match in _FRONTEND_STATIC_REF_PATTERN.finditer(candidate_text):
            normalized = _normalize_next_static_asset_ref(match.group(0))
            if normalized:
                refs.add(normalized)
    return refs


def _collect_next_static_asset_refs(next_dir: Path) -> set[str]:
    refs: set[str] = set()
    scan_roots = [
        path
        for path in (
            next_dir / "build-manifest.json",
            next_dir / "app-build-manifest.json",
            next_dir / "server" / "app-build-manifest.json",
        )
        if path.is_file()
    ]
    server_dir = next_dir / "server"
    if server_dir.is_dir():
        scan_roots.extend(
            path
            for path in server_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".html", ".js", ".json", ".rsc"}
        )

    for path in sorted(set(scan_roots)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if path.suffix.lower() == ".json":
            try:
                refs.update(_iter_static_refs_from_json(json.loads(text)))
                continue
            except json.JSONDecodeError:
                pass
        refs.update(_iter_static_refs_from_text(text))

    return refs


def _missing_next_static_assets(next_dir: Path) -> list[str]:
    missing: list[str] = []
    for ref in _collect_next_static_asset_refs(next_dir):
        target = next_dir.joinpath(*ref.split("/"))
        if not target.is_file():
            missing.append(ref)
    return sorted(missing)


def _frontend_static_build_invalid_reason(frontend_dir: Path) -> str | None:
    next_dir = frontend_dir / ".next"
    required_paths = [
        next_dir,
        next_dir / "BUILD_ID",
        next_dir / "server",
        next_dir / "static",
        next_dir / "static" / "chunks",
    ]
    for path in required_paths:
        if not path.exists():
            return f"Next.js build artifact is missing: {path.relative_to(frontend_dir)}"

    missing_assets = _missing_next_static_assets(next_dir)
    if missing_assets:
        shown = ", ".join(missing_assets[:5])
        suffix = "" if len(missing_assets) <= 5 else f" and {len(missing_assets) - 5} more"
        return f"Next.js build references missing static asset(s): {shown}{suffix}"
    return None


def _validate_frontend_startup_artifacts(project_root: Path) -> None:
    """Validate startup prerequisites without installing or building anything."""
    frontend_dir = project_root / "frontend"
    if not _next_bin_path(frontend_dir).exists():
        raise RuntimeError(
            "Frontend dependencies are not installed. "
            "Run setup.bat/setup.sh or `cd frontend && npm ci` before run.bat. "
            "run.bat does not install dependencies."
        )

    invalid_reason = _frontend_static_build_invalid_reason(frontend_dir)
    if invalid_reason:
        raise RuntimeError(
            f"Frontend build is not ready: {invalid_reason}. "
            "Run `cd frontend && npm run build:production` manually, or restart "
            "run.bat so its startup self-repair can retry the production build."
        )


def _frontend_build_rebuild_reason(frontend_dir: Path) -> tuple[str | None, dict[str, object]]:
    fingerprint = _frontend_build_fingerprint(frontend_dir)
    invalid_reason = _frontend_static_build_invalid_reason(frontend_dir)
    if invalid_reason:
        return invalid_reason, fingerprint

    stored = _read_frontend_build_fingerprint(frontend_dir)
    if stored != fingerprint:
        return "Frontend source fingerprint changed since the last verified build", fingerprint

    return None, fingerprint


def _ensure_frontend_build(
    project_root: Path,
    log_path: Path,
    env: dict[str, str],
    *,
    prepared_check: tuple[str | None, dict[str, object]] | None = None,
) -> None:
    """Ensure the canonical ``.next`` production build matches the frontend tree."""
    frontend_dir = project_root / "frontend"
    reason, fingerprint = (
        prepared_check if prepared_check is not None
        else _frontend_build_rebuild_reason(frontend_dir)
    )
    if not reason:
        return

    next_dir = frontend_dir / ".next"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(
            f"{reason}; running npm run build:production before startup.\n"
        )
        log_file.flush()

        try:
            # Invalidate all served artifacts and the verified marker,
            # but preserve Next's compiler cache for incremental builds.
            # Refuse redirected output roots; never traverse links while
            # removing children of the known frontend/.next directory.
            expected = frontend_dir.resolve() / ".next"
            if next_dir.is_symlink() or next_dir.is_junction() or next_dir.resolve() != expected:
                raise OSError("Frontend output directory is redirected")
            if next_dir.exists():
                for child in next_dir.iterdir():
                    if child.is_junction():
                        raise OSError("Frontend output child is redirected")
                    if child.name == "cache" and child.is_dir() and not child.is_symlink():
                        if not child.resolve().is_relative_to(expected):
                            raise OSError("Frontend cache directory is redirected")
                        continue
                    if child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        if not child.resolve().is_relative_to(expected):
                            raise OSError("Frontend output child is redirected")
                        shutil.rmtree(child)
                    else:
                        child.unlink()
        except OSError as exc:
            raise RuntimeError(
                "Failed to remove stale Next.js build artifacts before rebuild.\n"
                f"frontend.log tail:\n{_read_log_tail(log_path)}"
            ) from exc

        build_env = dict(env)
        build_env["NEXT_DIST_DIR"] = ".next"
        build_env["NODE_ENV"] = "production"
        result = subprocess.run(
            [_npm_command(), "run", "build:production"],
            cwd=str(frontend_dir),
            env=build_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Frontend build is stale or broken and npm run build:production failed.\n"
            f"frontend.log tail:\n{_read_log_tail(log_path)}"
        )

    post_build_reason = _frontend_static_build_invalid_reason(frontend_dir)
    if post_build_reason:
        raise RuntimeError(
            f"Frontend build completed but remains invalid: {post_build_reason}.\n"
            f"frontend.log tail:\n{_read_log_tail(log_path)}"
        )

    _write_frontend_build_fingerprint(frontend_dir, fingerprint)
