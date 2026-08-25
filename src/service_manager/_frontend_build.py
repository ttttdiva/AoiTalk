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
from pathlib import Path
from urllib.parse import unquote

from ._process_utils import _IS_WINDOWS, _read_log_tail

_FRONTEND_BUILD_FINGERPRINT_VERSION = 1
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
}
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


def _frontend_build_fingerprint(frontend_dir: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    file_count = 0
    for path in _iter_frontend_build_input_files(frontend_dir):
        relative = path.relative_to(frontend_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
        file_count += 1

    return {
        "version": _FRONTEND_BUILD_FINGERPRINT_VERSION,
        "digest": digest.hexdigest(),
        "file_count": file_count,
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
) -> None:
    """Ensure the canonical ``.next`` production build matches the frontend tree."""
    frontend_dir = project_root / "frontend"
    reason, fingerprint = _frontend_build_rebuild_reason(frontend_dir)
    if not reason:
        return

    next_dir = frontend_dir / ".next"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(
            f"{reason}; running npm run build:production before startup.\n"
        )
        log_file.flush()

        if next_dir.exists():
            try:
                shutil.rmtree(next_dir)
            except OSError as exc:
                raise RuntimeError(
                    "Failed to remove stale Next.js build artifacts before rebuild.\n"
                    f"frontend.log tail:\n{_read_log_tail(log_path)}"
                ) from exc

        result = subprocess.run(
            [_npm_command(), "run", "build:production"],
            cwd=str(frontend_dir),
            env=env,
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
