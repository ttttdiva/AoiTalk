"""ログ保持・ローテーションの共通 housekeeping（fail-open）。"""

from __future__ import annotations

import gzip
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from src.utils.log_layout import LogLayout

logger = logging.getLogger(__name__)

# 10 MiB
_DEFAULT_ROTATE_BYTES = 10 * 1024 * 1024
_CADDY_ROLL_KEEP = 5
_FRONTEND_GENERATION_KEEP = 10
_MODEL_GENERATION_KEEP = 3
_DESKTOP_GENERATION_KEEP = 3
_APP_MAX_FILES = 20
_APP_MAX_AGE_DAYS = 14
_STARTUP_MAX_FILES = 20
_DISCORD_MAX_FILES = 20


def run_log_housekeeping(
    layout: LogLayout,
    *,
    active_paths: set[Path] | None = None,
) -> None:
    """起動時に古いログを整理する。アクティブファイルは削除しない。"""
    try:
        layout.ensure_dirs()
        active = _normalize_active_paths(active_paths or set())
        _housekeep_app_logs(layout, active)
        _housekeep_startup_logs(layout, active)
        _housekeep_frontend_generations(layout, active)
        _housekeep_caddy_leftovers(layout, active)
        _housekeep_model_logs(layout, active)
        _housekeep_discord_logs(layout, active)
        rotate_log_if_over_size(layout.desktop_backend_log())
        _housekeep_desktop_generations(layout, active)
    except Exception as exc:
        logger.warning("ログ housekeeping に失敗しました（続行します）: %s", exc)


def rotate_frontend_log_if_exists(path: Path) -> None:
    """既存 frontend.log を世代ファイルへ rename する（起動時削除はしない）。"""
    if not path.is_file():
        return
    try:
        if path.stat().st_size == 0:
            return
    except OSError:
        return
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    destination = path.parent / f"frontend-{timestamp}.log"
    _safe_rename(path, destination)


def rotate_log_if_over_size(
    path: Path,
    *,
    max_bytes: int = _DEFAULT_ROTATE_BYTES,
) -> None:
    """サイズ超過時に timestamp 付きへ rename してから追記開始できるようにする。"""
    if not path.is_file():
        return
    try:
        if path.stat().st_size <= max_bytes:
            return
    except OSError:
        return
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    destination = path.parent / f"{path.stem}-{timestamp}{path.suffix}"
    _safe_rename(path, destination)


def _normalize_active_paths(active_paths: set[Path]) -> set[Path]:
    return {path.resolve() for path in active_paths}


def _is_active(path: Path, active_paths: set[Path]) -> bool:
    try:
        return path.resolve() in active_paths
    except OSError:
        return False


def _safe_delete(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except PermissionError:
        return False
    except OSError as exc:
        if getattr(exc, "winerror", None) == 32:
            return False
        logger.debug("ログ削除をスキップしました: %s (%s)", path, exc)
        return False


def _safe_rename(source: Path, destination: Path) -> bool:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return False
        os.replace(source, destination)
        return True
    except PermissionError:
        return False
    except OSError as exc:
        if getattr(exc, "winerror", None) == 32:
            return False
        logger.debug("ログ rename をスキップしました: %s -> %s (%s)", source, destination, exc)
        return False


def _housekeep_app_logs(layout: LogLayout, active_paths: set[Path]) -> None:
    app_dir = layout.app_dir
    if not app_dir.is_dir():
        return
    candidates = [
        path
        for path in app_dir.glob("app_*.log")
        if path.is_file() and not _is_active(path, active_paths)
    ]
    cutoff = time.time() - (_APP_MAX_AGE_DAYS * 86400)
    candidates.sort(key=lambda p: p.stat().st_mtime)
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                _safe_delete(path)
        except OSError:
            continue
    remaining = [
        path
        for path in app_dir.glob("app_*.log")
        if path.is_file() and not _is_active(path, active_paths)
    ]
    remaining.sort(key=lambda p: p.stat().st_mtime)
    while len(remaining) > _APP_MAX_FILES:
        oldest = remaining.pop(0)
        _safe_delete(oldest)


def _housekeep_startup_logs(layout: LogLayout, active_paths: set[Path]) -> None:
    startup_dir = layout.startup_dir
    if not startup_dir.is_dir():
        return
    candidates = [
        path
        for path in startup_dir.glob("startup_timing_*.jsonl")
        if path.is_file() and not _is_active(path, active_paths)
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime)
    while len(candidates) > _STARTUP_MAX_FILES:
        oldest = candidates.pop(0)
        _safe_delete(oldest)


def _housekeep_frontend_generations(layout: LogLayout, active_paths: set[Path]) -> None:
    web_dir = layout.web_dir
    if not web_dir.is_dir():
        return
    active_frontend = layout.frontend_log_path()
    candidates = [
        path
        for path in web_dir.glob("frontend-*.log")
        if path.is_file() and not _is_active(path, active_paths)
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime)
    while len(candidates) > _FRONTEND_GENERATION_KEEP:
        oldest = candidates.pop(0)
        _safe_delete(oldest)
    if active_frontend.is_file() and _is_active(active_frontend, active_paths):
        return


def _housekeep_caddy_leftovers(layout: LogLayout, active_paths: set[Path]) -> None:
    web_dir = layout.web_dir
    if not web_dir.is_dir():
        return
    for stem, active_name in (
        ("caddy-access", "caddy-access.log"),
        ("caddy-runtime", "caddy-runtime.log"),
    ):
        active_path = web_dir / active_name
        _compress_and_prune_caddy_family(
            web_dir,
            stem=stem,
            active_path=active_path,
            active_paths=active_paths,
            keep=_CADDY_ROLL_KEEP,
        )


def _compress_and_prune_caddy_family(
    web_dir: Path,
    *,
    stem: str,
    active_path: Path,
    active_paths: set[Path],
    keep: int,
) -> None:
    keep_candidates: dict[Path, float] = {}

    def _track(path: Path) -> None:
        if _is_active(path, active_paths):
            return
        try:
            keep_candidates[path.resolve()] = path.stat().st_mtime
        except OSError:
            return

    for path in web_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".tmp"):
            continue
        if name == active_path.name:
            continue
        if not name.startswith(f"{stem}-") and name != f"{stem}.log":
            continue
        if name.endswith(".gz"):
            if not _is_valid_gzip(path):
                _safe_delete(path)
                continue
            _track(path)
            uncompressed = path.with_name(path.name[:-3])
            if uncompressed.is_file() and not _is_active(uncompressed, active_paths):
                if not _safe_delete(uncompressed):
                    _track(uncompressed)
            continue
        if name.endswith(".log"):
            gz_path = path.with_suffix(path.suffix + ".gz")
            if _gzip_file_fail_open(path, gz_path):
                if gz_path.is_file():
                    _track(gz_path)
            if path.is_file():
                _track(path)

    sorted_candidates = sorted(keep_candidates.items(), key=lambda item: item[1])
    while len(sorted_candidates) > keep:
        oldest_path, _ = sorted_candidates.pop(0)
        oldest = Path(oldest_path)
        if _is_active(oldest, active_paths):
            continue
        if oldest.name.endswith(".log.gz"):
            uncompressed = oldest.with_name(oldest.name[:-3])
            if uncompressed.is_file():
                _safe_delete(uncompressed)
        _safe_delete(oldest)


def _is_valid_gzip(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(65536):
                pass
        return True
    except (OSError, EOFError, gzip.BadGzipFile):
        return False


def _gzip_file_fail_open(source: Path, destination: Path) -> bool:
    if destination.exists():
        if _is_valid_gzip(destination):
            _safe_delete(source)
            return True
        _safe_delete(destination)

    tmp_path = destination.with_name(destination.name + ".tmp")
    if tmp_path.exists():
        _safe_delete(tmp_path)

    try:
        with source.open("rb") as src, gzip.open(tmp_path, "wb") as dst:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
        if not _is_valid_gzip(tmp_path):
            _safe_delete(tmp_path)
            return False
        os.replace(tmp_path, destination)
    except PermissionError:
        _safe_delete(tmp_path)
        return False
    except OSError as exc:
        _safe_delete(tmp_path)
        if getattr(exc, "winerror", None) == 32:
            return False
        logger.debug("Caddy leftover の gzip に失敗しました: %s (%s)", source, exc)
        return False
    _safe_delete(source)
    return True


def _housekeep_model_logs(layout: LogLayout, active_paths: set[Path]) -> None:
    models_dir = layout.models_dir
    if not models_dir.is_dir():
        return
    active_names = {
        layout.llama_cpp_log().name,
        layout.exo_log().name,
        layout.mlx_lm_log().name,
        layout.sglang_server_log().name,
        layout.sglang_server_error_log().name,
    }
    for active_name in active_names:
        active_path = models_dir / active_name
        prefix = active_name.removesuffix(".log")
        candidates = [
            path
            for path in models_dir.glob(f"{prefix}-*.log")
            if path.is_file() and not _is_active(path, active_paths)
        ]
        candidates.sort(key=lambda p: p.stat().st_mtime)
        while len(candidates) > _MODEL_GENERATION_KEEP:
            oldest = candidates.pop(0)
            _safe_delete(oldest)


def _housekeep_discord_logs(layout: LogLayout, active_paths: set[Path]) -> None:
    discord_dir = layout.discord_dir
    if not discord_dir.is_dir():
        return
    latest_pointer = discord_dir / "latest.log"
    candidates = [
        path
        for path in discord_dir.glob("bot_*.log")
        if path.is_file() and not _is_active(path, active_paths)
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime)
    while len(candidates) > _DISCORD_MAX_FILES:
        oldest = candidates.pop(0)
        _safe_delete(oldest)
    if latest_pointer.is_file() and _is_active(latest_pointer, active_paths):
        return


def _housekeep_desktop_generations(layout: LogLayout, active_paths: set[Path]) -> None:
    desktop_dir = layout.desktop_dir
    if not desktop_dir.is_dir():
        return
    active_path = layout.desktop_backend_log()
    candidates = [
        path
        for path in desktop_dir.glob("desktop-tauri-backend-*.log")
        if path.is_file() and not _is_active(path, active_paths)
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime)
    while len(candidates) > _DESKTOP_GENERATION_KEEP:
        oldest = candidates.pop(0)
        _safe_delete(oldest)
    if active_path.is_file() and _is_active(active_path, active_paths):
        return
