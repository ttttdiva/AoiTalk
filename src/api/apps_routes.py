"""FastAPI routes for the persistent Apps feature."""

from __future__ import annotations

import asyncio
import html
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from collections import OrderedDict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask
from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from ..memory.models import (
    App,
    AppArtifact,
    AppJob,
    AgentRun,
    AppRelease,
    AppTarget,
    Project,
    ProjectApp,
    Task,
    TaskAppLink,
)
from ..services.app_git_service import AppGitError, AppGitService
from ..services.app_bridge_service import (
    AppBridgeTokenError,
    DEFAULT_TTL_SECONDS,
    issue_app_bridge_token,
    verify_app_bridge_token,
)
from ..services.app_job_execution_policy import (
    ServerJobExecutionDenied,
    assert_user_may_start_server_job,
)
from ..services.app_job_service import execute_app_job, stop_running_job
from ..services.app_manifest_service import (
    AppManifestError,
    ValidationMode,
    load_app_manifest,
    parse_manifest_text,
    sync_manifest_targets,
    sync_manifest_targets_unlocked,
    validate_manifest_text,
    validate_manifest_workspace,
)
from ..services.app_operation_lock import app_operation_lock, project_operation_lock
from ..services.app_release_service import AppReleaseError, create_app_release
from ..services.app_business_analysis_service import (
    analyze_app_workspace,
    collect_source_evidence,
    merge_analysis_into_readme,
    write_analysis_to_manifest,
)
from ..services.project_workspace_cleanup import get_project_workspace_path
from ..services.app_source_import_service import generate_import_metadata
from ..services.app_source_update_service import (
    MAX_FILE_SIZE,
    MAX_FILES,
    MAX_TOTAL_SIZE,
    AppSourceUpdateError,
    AppSourceUpdateService,
)
from ..services.app_service import (
    AppAccessError,
    AppService,
    permission_at_least,
    validate_capability_grants,
)
from ..services.app_storage import (
    APP_IGNORED_PATHS,
    AppStorageError,
    AppWorkspaceJournal,
    canonical_app_source_path,
    ensure_app_instance,
    get_app_instance_path,
    get_app_workspace_path,
    is_credential_app_path,
    is_embedded_app_path,
    is_private_app_path,
    is_protected_app_path,
    is_sensitive_app_path,
    is_text_app_path,
    iter_app_source_files,
    list_app_files,
    normalize_app_relative_path,
    remove_app_instance,
    resolve_app_artifact_file,
    resolve_app_file,
    sha256_file,
    verify_file_integrity,
)
from .routes.apps import AppGrantPayload, AppRouterContext, register_app_grant_routes

logger = logging.getLogger(__name__)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,119}$")
# ダウンロード zip の folder 名 / file 名から落とす文字。Windows の禁止文字と
# 制御文字を潰しておかないと、展開先や Content-Disposition が壊れる。
_ARCHIVE_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_ARCHIVE_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ARCHIVE_DEPENDENCY_DIRS = APP_IGNORED_PATHS - {"logs", "runtime data", "secrets"}
_ARCHIVE_RUNTIME_DIRS = {"logs", "runtime data"}

# 入力上限はアップロード/取り込み経路 (app_source_update_service) と同じ定数から
# 導出する。max_length は文字数なので、byte 上限の MAX_FILE_SIZE をそのまま
# 文字数上限として使う（1文字 >= 1byte なので実効的に同等以下の許容量）。
MAX_APP_TEXT_CONTENT_CHARS = MAX_FILE_SIZE
#: App file API の path 上限。AppGitRestorePayload.path と揃える。
MAX_APP_PATH_CHARS = 500
#: description は DB 上 Text だが、無制限入力を避けるため上限を設ける。
MAX_APP_DESCRIPTION_CHARS = 100_000
#: sha256 hex の桁数。
MAX_SHA256_CHARS = 64
#: 任意 dict (config_json / capability_grants_json) のシリアライズ後サイズ上限。
MAX_APP_JSON_BYTES = 256 * 1024
#: 任意 dict のキー総数上限。
MAX_APP_JSON_KEYS = 1_000
#: 任意 dict のネスト深さ上限。
MAX_APP_JSON_DEPTH = 16
#: Windows の環境変数は 1 個あたり 32767 文字が上限。
#: AppJob.input_json は AOITALK_APP_INPUT_JSON へ json.dumps されるため、
#: 実行時に環境変数構築で落ちる前に 422 で弾く。
MAX_APP_INPUT_JSON_ENV_CHARS = 32_767


def _reject_oversized_json(value: dict[str, Any] | None, label: str) -> dict[str, Any] | None:
    """Reject unbounded free-form JSON before it reaches the database."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} はオブジェクトである必要があります")

    keys = 0

    def _walk(node: Any, depth: int) -> None:
        nonlocal keys
        if depth > MAX_APP_JSON_DEPTH:
            raise ValueError(f"{label} のネストが深すぎます（最大{MAX_APP_JSON_DEPTH}段）")
        if isinstance(node, dict):
            keys += len(node)
            if keys > MAX_APP_JSON_KEYS:
                raise ValueError(f"{label} のキー数が上限（{MAX_APP_JSON_KEYS}）を超えています")
            for child in node.values():
                _walk(child, depth + 1)
        elif isinstance(node, list):
            keys += len(node)
            if keys > MAX_APP_JSON_KEYS:
                raise ValueError(f"{label} の要素数が上限（{MAX_APP_JSON_KEYS}）を超えています")
            for child in node:
                _walk(child, depth + 1)

    _walk(value, 1)
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} をJSONへ変換できません") from exc
    if len(serialized.encode("utf-8")) > MAX_APP_JSON_BYTES:
        raise ValueError(f"{label} のサイズが上限（{MAX_APP_JSON_BYTES} bytes）を超えています")
    return value


def _require_non_blank_name(value: str | None) -> str | None:
    """Reject whitespace-only App names so ``.strip()`` cannot persist ``""``."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("name を空白のみにはできません")
    return stripped


class AppCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=120)
    description: str = Field(default="", max_length=MAX_APP_DESCRIPTION_CHARS)
    origin_project_id: str | None = None
    visibility: str = "private"

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _require_non_blank_name(value)  # type: ignore[return-value]


class AppProjectSourceImportPayload(AppCreatePayload):
    project_id: str
    source_path: str = Field(min_length=1, max_length=500)


class AppPatchPayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=MAX_APP_DESCRIPTION_CHARS)
    visibility: str | None = None
    default_target_key: str | None = None
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        return _require_non_blank_name(value)


class ManifestValidatePayload(BaseModel):
    content: str | None = Field(default=None, max_length=MAX_APP_TEXT_CONTENT_CHARS)


class AppAnalysisPayload(BaseModel):
    expected_manifest_sha256: str | None = Field(default=None, max_length=MAX_SHA256_CHARS)


class ManifestUpdatePayload(BaseModel):
    content: str = Field(max_length=MAX_APP_TEXT_CONTENT_CHARS)
    expected_sha256: str | None = Field(default=None, max_length=MAX_SHA256_CHARS)


class ReadmeUpdatePayload(BaseModel):
    content: str = Field(max_length=MAX_APP_TEXT_CONTENT_CHARS)
    expected_sha256: str | None = Field(default=None, max_length=MAX_SHA256_CHARS)


class AppFileWritePayload(BaseModel):
    path: str = Field(min_length=1, max_length=MAX_APP_PATH_CHARS)
    content: str = Field(max_length=MAX_APP_TEXT_CONTENT_CHARS)
    expected_sha256: str | None = Field(default=None, max_length=MAX_SHA256_CHARS)


class AppSourceApplyPayload(BaseModel):
    expected_revision: str | None = Field(default=None, max_length=128)
    delete_paths: list[str] = Field(default_factory=list, max_length=MAX_FILES)


class AppJobPayload(BaseModel):
    target_key: str
    job_type: str
    project_id: str | None = None
    release_id: str | None = None
    agent_run_id: str | None = None
    input_json: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=900, ge=1, le=86400)

    @field_validator("input_json")
    @classmethod
    def _validate_input_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_oversized_json(value, "input_json")
        serialized = json.dumps(value or {}, ensure_ascii=False)
        if len(serialized) > MAX_APP_INPUT_JSON_ENV_CHARS:
            raise ValueError(
                "input_json が大きすぎます"
                f"（AOITALK_APP_INPUT_JSON は最大 {MAX_APP_INPUT_JSON_ENV_CHARS} 文字）"
            )
        return value


class AppGitRestorePayload(BaseModel):
    path: str | None = Field(default=None, max_length=500)
    revision: str = Field(min_length=1, max_length=128)


class AppReleasePayload(BaseModel):
    version: str = Field(min_length=1, max_length=80)
    changelog: str = ""


class ProjectAppPayload(BaseModel):
    app_id: str
    binding_mode: str = "development"
    installed_release_id: str | None = None
    enabled: bool = True
    pinned: bool = False
    display_alias: str | None = Field(default=None, max_length=255)
    config_json: dict[str, Any] = Field(default_factory=dict)
    capability_grants_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config_json", "capability_grants_json")
    @classmethod
    def _validate_json(cls, value: dict[str, Any], info) -> dict[str, Any]:
        return _reject_oversized_json(value, info.field_name)  # type: ignore[return-value]


class ProjectAppPatchPayload(BaseModel):
    binding_mode: str | None = None
    installed_release_id: str | None = None
    enabled: bool | None = None
    pinned: bool | None = None
    display_alias: str | None = Field(default=None, max_length=255)
    config_json: dict[str, Any] | None = None
    capability_grants_json: dict[str, Any] | None = None

    @field_validator("config_json", "capability_grants_json")
    @classmethod
    def _validate_json(cls, value: dict[str, Any] | None, info) -> dict[str, Any] | None:
        return _reject_oversized_json(value, info.field_name)


class TaskAppLinkPayload(BaseModel):
    app_id: str
    target_id: str | None = None
    relation_type: str = "related"


class AppBridgeInvokePayload(BaseModel):
    token: str
    method: str
    input_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("input_json")
    @classmethod
    def _validate_input_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _reject_oversized_json(value, "input_json")  # type: ignore[return-value]


def _uuid(value: str | None, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} が不正です") from exc


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:120] or "aoitalk-app"


def _user_id(user: dict[str, Any]) -> UUID:
    return _uuid(str(user.get("id")), "user_id")


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


_EMBED_PROJECT_SCOPE_SEGMENT = "__aoitalk_project__"

#: Release artifact の SHA-256 全読みは数十MBのバンドルで致命的に遅い。
#: 埋め込みAssetは1リクエストごとに同じ artifact を検証するため、
#: (path, mtime_ns, size, 期待メタデータ) が一致する間だけ結果を再利用する。
#: mtime か size が変われば再検証されるので、差し替え検知は失われない。
_ARTIFACT_VERIFY_CACHE_LIMIT = 256
_ARTIFACT_VERIFY_CACHE: "OrderedDict[tuple[str, int, int, str, int], bool]" = OrderedDict()


def _force_remove_tree(path: Path) -> bool:
    """Remove an App workspace even when Git left read-only objects behind.

    Windows では ``.git/objects`` が read-only 属性で作られるため、素の
    ``shutil.rmtree`` は ``PermissionError`` になり、``ignore_errors=True``
    だと補償したつもりで workspace がまるごと残る。書き込み属性を戻して
    再試行し、実際に消えたかどうかを返す。
    """
    if not path.exists():
        return True

    def _retry(func, target, _exception=None):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            logger.warning("App workspace cleanup failed: %s", target)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_retry)
        else:  # pragma: no cover - 3.11 以前の互換
            shutil.rmtree(path, onerror=lambda func, target, info: _retry(func, target))
    except OSError:
        logger.exception("App workspace cleanup failed: %s", path)
    return not path.exists()


def _verify_artifact_file_cached(
    path: Path,
    *,
    expected_sha256: str | None,
    expected_size_bytes: int | None,
) -> None:
    """Verify a Release artifact, reusing the result while the file is unchanged."""
    try:
        stat_result = path.stat()
    except OSError:
        # 実在しない/読めない場合は元の検証関数に同じ例外を出させる。
        verify_file_integrity(
            path,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
        )
        return
    key = (
        str(path),
        int(stat_result.st_mtime_ns),
        int(stat_result.st_size),
        str(expected_sha256 or ""),
        int(expected_size_bytes if expected_size_bytes is not None else -1),
    )
    if key in _ARTIFACT_VERIFY_CACHE:
        _ARTIFACT_VERIFY_CACHE.move_to_end(key)
        return
    verify_file_integrity(
        path,
        expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes,
    )
    _ARTIFACT_VERIFY_CACHE[key] = True
    while len(_ARTIFACT_VERIFY_CACHE) > _ARTIFACT_VERIFY_CACHE_LIMIT:
        _ARTIFACT_VERIFY_CACHE.popitem(last=False)


def resolve_runtime_bundle_entrypoint(archive_path: Path, declared_entrypoint: str) -> str:
    """Resolve a manifest entrypoint against the staged Runtime Bundle.

    Runtime bundles may be staged from ``build.output`` and therefore omit
    the output-directory prefix that appears in the manifest entrypoint.  A
    unique suffix match keeps that valid layout while refusing ambiguous
    basename matches.

    The caller validates the *declared* entrypoint, but suffix/basename
    fallbacks may resolve to a completely different path inside the bundle.
    Re-check the resolved path so a fallback cannot serve a private/runtime
    file that the declared entrypoint check would have rejected.
    """

    def _accept(candidate: str) -> str:
        if is_embedded_app_path(candidate, allow_build_output=True):
            raise _error(403, "private/runtime entrypoint はembedded Appから配信できません")
        return candidate

    try:
        normalized = normalize_app_relative_path(declared_entrypoint)
        with zipfile.ZipFile(archive_path) as archive:
            names = {
                normalize_app_relative_path(name)
                for name in archive.namelist()
                if not name.endswith("/")
            }
    except (AppStorageError, OSError, zipfile.BadZipFile) as exc:
        raise _error(422, "Runtime Bundleを読み込めません") from exc
    if normalized in names:
        return _accept(normalized)
    normalized_parts = PurePosixPath(normalized).parts
    suffix_matches = [
        name for name in names
        if len(PurePosixPath(name).parts) >= len(normalized_parts)
        and PurePosixPath(name).parts[-len(normalized_parts):] == normalized_parts
    ]
    if len(suffix_matches) == 1:
        return _accept(suffix_matches[0])
    basename = PurePosixPath(normalized).name
    basename_matches = [name for name in names if PurePosixPath(name).name == basename]
    if len(basename_matches) == 1:
        return _accept(basename_matches[0])
    raise _error(404, "固定ReleaseのTarget entrypoint がRuntime Bundleにありません")


def _safe_archive_component(value: Any, *, limit: int = 80) -> str:
    """Return a Windows/zip 安全な名前。使えない文字は落として空なら空文字を返す。"""
    cleaned = _ARCHIVE_UNSAFE_RE.sub("_", str(value or "")).strip().strip(".").strip()
    return cleaned[:limit].strip().strip(".").strip()


def _archive_root_name(app: App) -> str:
    """Return the single top-level folder name inside a downloaded App archive.

    展開すると App 単位のフォルダが 1 つできる形にして、利用者の Downloads へ
    ファイルが散らばらないようにする。表示名を優先し、使えない文字で全部
    落ちたら slug、それも駄目なら固定名へ落とす。
    """
    for candidate in (app.name, app.slug):
        cleaned = _safe_archive_component(candidate)
        if cleaned:
            return cleaned
    return "app"


def _new_temp_zip() -> Path:
    handle, name = tempfile.mkstemp(prefix="aoitalk-app-", suffix=".zip")
    os.close(handle)
    return Path(name)


def _archive_path_is_excluded(
    relative: str,
    *,
    exclude_git: bool,
    exclude_dependencies: bool,
    exclude_runtime: bool,
    exclude_credentials: bool,
) -> bool:
    """Apply only the archive exclusions explicitly selected by the user."""
    normalized = _normalize_archive_relative_path(relative)
    if normalized is None:
        return True
    parts = {part.casefold() for part in normalized.split("/")}
    return (
        (exclude_git and ".git" in parts)
        or (exclude_dependencies and bool(parts & _ARCHIVE_DEPENDENCY_DIRS))
        or (exclude_runtime and bool(parts & _ARCHIVE_RUNTIME_DIRS))
        or (exclude_credentials and is_credential_app_path(normalized))
    )


def _archive_path_is_included(relative: str, include_paths: tuple[str, ...] | None) -> bool:
    """Return whether a normalized archive path is inside a selected scope."""
    if include_paths is None:
        return True
    normalized = _normalize_archive_relative_path(relative)
    if normalized is None:
        return False
    key = normalized.casefold()
    return any(key == scope.casefold() or key.startswith(f"{scope.casefold()}/") for scope in include_paths)


def _archive_directory_may_contain_scope(relative: str, include_paths: tuple[str, ...] | None) -> bool:
    """Keep an ancestor directory while walking toward a selected scope."""
    if include_paths is None:
        return True
    normalized = _normalize_archive_relative_path(relative)
    if normalized is None:
        return False
    key = normalized.casefold()
    return any(
        key == scope.casefold()
        or key.startswith(f"{scope.casefold()}/")
        or scope.casefold().startswith(f"{key}/")
        for scope in include_paths
    )


def _parse_archive_include_paths(raw: str | None) -> tuple[str, ...] | None:
    """Parse the JSON array used by the download UI to select archive roots."""
    if not raw:
        return None
    if len(raw.encode("utf-8")) > MAX_APP_JSON_BYTES:
        raise _error(400, "ダウンロード範囲の指定が大きすぎます")
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _error(400, "ダウンロード範囲が不正です") from exc
    if not isinstance(values, list):
        raise _error(400, "ダウンロード範囲は配列で指定してください")
    if len(values) > MAX_FILES:
        raise _error(400, f"ダウンロード範囲は{MAX_FILES}件以内で指定してください")

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise _error(400, "ダウンロード範囲に不正な値があります")
        if len(value) > MAX_APP_PATH_CHARS:
            raise _error(400, f"ダウンロード範囲のパスは{MAX_APP_PATH_CHARS}文字以内で指定してください")
        path = _normalize_archive_relative_path(value)
        if path is None:
            raise _error(400, "ダウンロード範囲に不正なパスがあります")
        key = path.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(path)
    return tuple(normalized)


def _normalize_archive_relative_path(value: str) -> str | None:
    """Normalize a ZIP member while allowing the user-selectable ``.git`` path."""
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or raw.startswith(("/", "//")) or _ARCHIVE_DRIVE_RE.match(raw):
        return None
    parts = tuple(part for part in raw.split("/") if part)
    if not parts or any(part in {".", ".."} or "\x00" in part for part in parts):
        return None
    return "/".join(parts)


def _iter_workspace_archive_files(
    workspace: Path,
    *,
    exclude_git: bool,
    exclude_dependencies: bool,
    exclude_runtime: bool,
    exclude_credentials: bool,
    include_paths: tuple[str, ...] | None = None,
):
    """Yield archive files without silently applying App source filters.

    Symlinks are never followed or stored because their targets may leave the App
    workspace.  Every ordinary file is included unless its category was selected
    for exclusion or it falls outside the selected include_paths scope.
    """
    if not workspace.is_dir():
        return
    root_resolved = workspace.resolve()
    for current, directories, filenames in os.walk(workspace, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            if path.is_symlink():
                continue
            relative = path.relative_to(workspace).as_posix()
            if _archive_path_is_excluded(
                relative,
                exclude_git=exclude_git,
                exclude_dependencies=exclude_dependencies,
                exclude_runtime=exclude_runtime,
                exclude_credentials=exclude_credentials,
            ):
                continue
            if not _archive_directory_may_contain_scope(relative, include_paths):
                continue
            kept_directories.append(name)
        directories[:] = kept_directories
        for filename in sorted(filenames):
            path = current_path / filename
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(workspace).as_posix()
            if _archive_path_is_excluded(
                relative,
                exclude_git=exclude_git,
                exclude_dependencies=exclude_dependencies,
                exclude_runtime=exclude_runtime,
                exclude_credentials=exclude_credentials,
            ):
                continue
            if not _archive_path_is_included(relative, include_paths):
                continue
            try:
                path.resolve(strict=True).relative_to(root_resolved)
            except (OSError, ValueError):
                continue
            yield path, relative


def _archive_path_is_release_supplement(relative: str) -> bool:
    """Return whether a workspace path is omitted from immutable source bundles."""
    normalized = _normalize_archive_relative_path(relative)
    if normalized is None:
        return False
    parts = {part.casefold() for part in normalized.split("/")}
    return bool(
        ".git" in parts
        or parts & _ARCHIVE_DEPENDENCY_DIRS
        or parts & _ARCHIVE_RUNTIME_DIRS
        or is_credential_app_path(normalized)
    )


def _remove_temp_file(path: Path) -> None:
    """Delete a streamed temp archive.  Failure must not break the response."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("一時アーカイブを削除できませんでした (%s): %s", path, exc)


def _build_workspace_archive(
    app_id: UUID,
    root_name: str,
    workspace_root: str | Path | None,
    *,
    exclude_git: bool = False,
    exclude_dependencies: bool = False,
    exclude_runtime: bool = False,
    exclude_credentials: bool = False,
    include_paths: tuple[str, ...] | None = None,
) -> tuple[Path, int]:
    """Zip the current App workspace for download.

    通常ファイルは既定ですべて含める。include_paths があればその配下へ絞り込み、
    Git履歴、依存・build/cache、runtime、credentialの各カテゴリは、ダウンロードGUIで
    利用者が選択した場合だけ除外する。
    symlinkだけはworkspace外参照を防ぐため常に対象外とする。
    """
    workspace = get_app_workspace_path(app_id, workspace_root=workspace_root)
    destination = _new_temp_zip()
    written = 0
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            written_names: set[str] = set()
            for path, relative in _iter_workspace_archive_files(
                workspace,
                exclude_git=exclude_git,
                exclude_dependencies=exclude_dependencies,
                exclude_runtime=exclude_runtime,
                exclude_credentials=exclude_credentials,
                include_paths=include_paths,
            ):
                name_key = relative.casefold()
                if name_key in written_names:
                    raise _error(409, "大文字小文字だけが異なる重複パスがあるためZIPを作成できません")
                try:
                    archive.write(path, f"{root_name}/{relative}")
                except OSError as exc:
                    # 収集中に消えた・ロックされた 1 file で一括取得ごと落とさない。
                    logger.warning("App archive skipped %s: %s", relative, exc)
                    continue
                written_names.add(name_key)
                written += 1
    except BaseException:
        _remove_temp_file(destination)
        raise
    return destination, written


def _build_release_archive(
    source_bundle: Path,
    root_name: str,
    workspace: Path | None = None,
    *,
    exclude_git: bool = False,
    exclude_dependencies: bool = False,
    exclude_runtime: bool = False,
    exclude_credentials: bool = False,
    include_paths: tuple[str, ...] | None = None,
) -> tuple[Path, int]:
    """Repack a fixed Release Source Bundle under one top-level folder.

    Releaseの通常ソースを正本にしつつ、Release作成時に固定除外されていたGit履歴・
    runtime・credential等は現在のworkspaceから補完する。補完対象もGUIで選択された
    除外だけを適用し、通常ソースを開発中workspaceで上書きしない。
    """
    destination = _new_temp_zip()
    written = 0
    try:
        with zipfile.ZipFile(source_bundle) as bundle:
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                written_names: set[str] = set()
                for info in bundle.infolist():
                    if info.is_dir():
                        continue
                    if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                        continue
                    normalized = _normalize_archive_relative_path(info.filename)
                    if normalized is None:
                        continue
                    if _archive_path_is_excluded(
                        normalized,
                        exclude_git=exclude_git,
                        exclude_dependencies=exclude_dependencies,
                        exclude_runtime=exclude_runtime,
                        exclude_credentials=exclude_credentials,
                    ):
                        continue
                    if not _archive_path_is_included(normalized, include_paths):
                        continue
                    name_key = normalized.casefold()
                    if name_key in written_names:
                        raise _error(409, "固定ReleaseのSource Bundleに重複パスがあります")
                    archive.writestr(f"{root_name}/{normalized}", bundle.read(info))
                    written_names.add(name_key)
                    written += 1
                if workspace is not None:
                    for path, relative in _iter_workspace_archive_files(
                        workspace,
                        exclude_git=exclude_git,
                        exclude_dependencies=exclude_dependencies,
                        exclude_runtime=exclude_runtime,
                        exclude_credentials=exclude_credentials,
                        include_paths=include_paths,
                    ):
                        if (relative.casefold() in written_names or
                                not _archive_path_is_included(relative, include_paths) or
                                not _archive_path_is_release_supplement(relative)):
                            continue
                        try:
                            archive.write(path, f"{root_name}/{relative}")
                        except OSError as exc:
                            logger.warning("App release archive skipped %s: %s", relative, exc)
                            continue
                        written_names.add(relative.casefold())
                        written += 1
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        _remove_temp_file(destination)
        raise _error(409, "固定ReleaseのSource Bundleを読み込めません") from exc
    except BaseException:
        _remove_temp_file(destination)
        raise
    return destination, written


def create_apps_router(
    *,
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    workspace_root: str | Path | None = None,
    get_llm_client: Callable[[], Any] | None = None,
    get_app_config: Callable[[], dict[str, Any]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["apps"])
    service = AppService(workspace_root=workspace_root)
    source_update_service = AppSourceUpdateService(workspace_root=workspace_root)

    def _app_config() -> dict[str, Any]:
        if get_app_config is None:
            return {}
        try:
            value = get_app_config()
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    async def current_user(request: Request) -> dict[str, Any]:
        user = await get_user_from_request(request)
        if not user:
            raise _error(401, "Not authenticated")
        return user

    async def app_or_404(session, app_id: str, *, include_archived: bool = False) -> App:
        conditions = [App.id == _uuid(app_id, "app_id")]
        if not include_archived:
            conditions.append(App.archived_at.is_(None))
        app = await session.scalar(select(App).where(and_(*conditions)).limit(1))
        if not app:
            raise _error(404, "App not found")
        return app

    async def require_app(
        session,
        app_id: str,
        user: dict[str, Any],
        required: str = "viewer",
        project_id: UUID | None = None,
        include_archived: bool = False,
        require_enabled_binding: bool = True,
    ) -> tuple[App, str]:
        app = await app_or_404(session, app_id, include_archived=include_archived)
        if project_id is not None and not await service.project_access(
            session,
            project_id=project_id,
            user_id=_user_id(user),
            user_role=user.get("role"),
        ):
            raise _error(403, "Projectへの権限がありません")
        if project_id is not None and require_enabled_binding:
            binding = await session.scalar(
                select(ProjectApp)
                .where(
                    ProjectApp.project_id == project_id,
                    ProjectApp.app_id == app.id,
                )
                .limit(1)
            )
            if binding is None or not binding.enabled:
                raise _error(403, "このProjectではAppが有効化されていません")
        try:
            permission = await service.require_permission(
                session,
                app,
                user_id=_user_id(user),
                required=required,
                user_role=user.get("role"),
                project_id=project_id,
                allow_archived=include_archived,
            )
        except AppAccessError as exc:
            raise _error(403, str(exc)) from exc
        return app, permission

    async def project_write_required(session, project_id: UUID, user: dict[str, Any]) -> None:
        if not await service.project_write_access(
            session,
            project_id=project_id,
            user_id=_user_id(user),
            user_role=user.get("role"),
        ):
            raise _error(403, "ProjectのApp構成を変更する権限がありません")

    async def ensure_no_active_jobs(
        session,
        app_id: UUID,
        *,
        project_id: UUID | None = None,
    ) -> None:
        conditions = [
            AppJob.app_id == app_id,
            AppJob.status.in_({"queued", "running"}),
        ]
        if project_id is not None:
            conditions.append(AppJob.project_id == project_id)
        active = await session.scalar(select(AppJob.id).where(and_(*conditions)).limit(1))
        if active is not None:
            raise _error(409, "実行中または待機中のApp Jobがあるため変更できません。Jobを停止してから再試行してください")

    async def project_required(session, project_id: str, user: dict[str, Any]) -> UUID:
        project_uuid = _uuid(project_id, "project_id")
        if not await service.project_access(
            session,
            project_id=project_uuid,
            user_id=_user_id(user),
            user_role=user.get("role"),
        ):
            raise _error(403, "Projectへの権限がありません")
        return project_uuid

    def job_required_permission(job: AppJob) -> str:
        """Return the App permission needed to see or mutate a Job."""
        return (
            "runner" if job.job_type == "run"
            else "maintainer" if job.job_type == "package"
            else "developer"
        )

    async def job_project_visible(session, job: AppJob, user: dict[str, Any], requested_project_id: UUID | None) -> bool:
        """Apply the Job's persisted Project scope before returning or mutating it."""
        app = await session.scalar(select(App).where(App.id == job.app_id).limit(1))
        if app is None:
            return False
        required = job_required_permission(job)
        if job.project_id is None:
            # Project スコープを持たない Job も build/run のログ全文を含む。
            # require_app の既定は viewer なので、ここで job_type 相当の
            # App 権限を必ず検査する（viewer が素通りしない）。
            if requested_project_id is not None:
                return False
            permission = await service.permission_for_app(
                session,
                app,
                user_id=_user_id(user),
                user_role=user.get("role"),
                project_id=None,
            )
            return permission_at_least(permission, required)
        if requested_project_id is not None and job.project_id != requested_project_id:
            return False
        if not await service.project_access(
            session,
            project_id=job.project_id,
            user_id=_user_id(user),
            user_role=user.get("role"),
        ):
            return False
        binding = await session.scalar(select(ProjectApp).where(and_(
            ProjectApp.project_id == job.project_id,
            ProjectApp.app_id == job.app_id,
            ProjectApp.enabled.is_(True),
        )).limit(1))
        if binding is None:
            return False
        permission = await service.permission_for_app(
            session,
            app,
            user_id=_user_id(user),
            user_role=user.get("role"),
            project_id=job.project_id,
        )
        return permission_at_least(permission, required)

    def app_payload(app: App, permission: str, *, targets: list[AppTarget | dict[str, Any]] | None = None) -> dict[str, Any]:
        result = app.to_dict()
        result["permission"] = permission
        if targets is not None:
            result["targets"] = [target.to_dict() if isinstance(target, AppTarget) else target for target in targets]
        return result

    def task_app_payload(link: TaskAppLink) -> dict[str, Any]:
        result = link.to_dict()
        if link.app is not None:
            result["app"] = link.app.to_dict()
        if link.target is not None:
            result["target"] = link.target.to_dict()
        return result

    async def targets_for_apps(session, app_ids: list[UUID]) -> dict[UUID, list[AppTarget]]:
        """Load every App's Targets in one query (list endpoints are N+1 otherwise)."""
        if not app_ids:
            return {}
        rows = list((await session.scalars(
            select(AppTarget)
            .where(AppTarget.app_id.in_(app_ids))
            .order_by(AppTarget.app_id, AppTarget.target_key)
        )).all())
        grouped: dict[UUID, list[AppTarget]] = {}
        for target in rows:
            grouped.setdefault(target.app_id, []).append(target)
        return grouped

    async def latest_row_by_app(
        session,
        model,
        app_ids: list[UUID],
        *,
        order_column,
        extra_conditions: tuple[Any, ...] = (),
        options: tuple[Any, ...] = (),
    ) -> dict[UUID, Any]:
        """Return the newest row per ``app_id`` using a single window query."""
        if not app_ids:
            return {}
        ranked = (
            select(
                model.id.label("row_id"),
                func.row_number()
                .over(partition_by=model.app_id, order_by=order_column.desc())
                .label("row_rank"),
            )
            .where(model.app_id.in_(app_ids), *extra_conditions)
            .subquery()
        )
        statement = select(model).where(
            model.id.in_(select(ranked.c.row_id).where(ranked.c.row_rank == 1))
        )
        for option in options:
            statement = statement.options(option)
        rows = list((await session.scalars(statement)).all())
        return {row.app_id: row for row in rows}

    async def installed_source_bundle(
        session,
        app: App,
        project_id: UUID | None,
        *,
        binding: ProjectApp | None = None,
    ):
        """Resolve the immutable source bundle selected by a Project binding.

        ``binding`` lets a caller that already loaded the Project binding skip
        the extra query (list endpoints resolve one binding per App).
        """
        if project_id is None:
            return None
        if binding is None:
            binding = await session.scalar(select(ProjectApp).where(
                and_(ProjectApp.project_id == project_id, ProjectApp.app_id == app.id)
            ).limit(1))
        if binding is None or not binding.enabled or binding.binding_mode != "installed":
            return None
        if binding.installed_release_id is None:
            raise _error(409, "固定保存版が選択されていますがReleaseがありません")
        release = await session.scalar(
            select(AppRelease)
            .options(selectinload(AppRelease.artifacts))
            .where(
                AppRelease.id == binding.installed_release_id,
                AppRelease.app_id == app.id,
                AppRelease.status == "published",
            ).limit(1)
        )
        if release is None:
            raise _error(409, "Projectが固定しているReleaseを利用できません")
        artifact = next((item for item in (release.artifacts or []) if item.artifact_type == "source_bundle"), None)
        if artifact is None:
            raise _error(409, "固定ReleaseにSource Bundleがありません")
        try:
            archive_path = resolve_app_artifact_file(
                app.id,
                release.id,
                Path(str(artifact.filename)).name,
                workspace_root=workspace_root,
            )
        except AppStorageError as exc:
            raise _error(400, str(exc)) from exc
        try:
            _verify_artifact_file_cached(
                archive_path,
                expected_sha256=artifact.sha256,
                expected_size_bytes=artifact.size_bytes,
            )
        except (AppStorageError, OSError) as exc:
            raise _error(409, f"固定ReleaseのSource Bundleを利用できません: {exc}") from exc
        return binding, release, archive_path

    async def require_development_binding(session, app: App, project_id: UUID | None) -> None:
        if await installed_source_bundle(session, app, project_id):
            raise _error(409, "固定保存版は編集できません。Project設定でmain（最新）へ切り替えてください")

    def release_manifest_and_readme(archive_path: Path) -> tuple[dict[str, Any], str, str]:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                manifest_text = archive.read("aoitalk.app.yaml").decode("utf-8")
                readme = archive.read("README.md").decode("utf-8") if "README.md" in archive.namelist() else ""
        except (KeyError, UnicodeDecodeError, OSError, zipfile.BadZipFile) as exc:
            raise _error(422, "Release Source Bundleを読み込めません") from exc
        try:
            manifest = parse_manifest_text(manifest_text)
        except AppManifestError as exc:
            raise _error(422, str(exc)) from exc
        return manifest, manifest_text, readme

    def release_target_payloads(
        manifest: dict[str, Any],
        current_targets: list[AppTarget],
    ) -> list[dict[str, Any]]:
        current_by_key = {target.target_key: target for target in current_targets}
        result: list[dict[str, Any]] = []
        raw_targets = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
        for key, snapshot in raw_targets.items():
            if not isinstance(snapshot, dict):
                continue
            current = current_by_key.get(str(key))
            payload = current.to_dict() if current is not None else {
                "id": f"release:{key}",
                "app_id": None,
                "target_key": str(key),
                "created_at": None,
                "updated_at": None,
            }
            payload.update({
                "target_key": str(key),
                "display_name": snapshot.get("display_name") or str(key),
                "surface": snapshot.get("surface") or "headless",
                "runtime": snapshot.get("runtime") or "executable",
                "execution_host": snapshot.get("execution_host") or "download_only",
                "entrypoint": snapshot.get("entrypoint") or "",
                "manifest_snapshot": dict(snapshot),
            })
            result.append(payload)
        return result

    def release_file_entries(archive_path: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    try:
                        normalized = normalize_app_relative_path(info.filename)
                    except AppStorageError:
                        continue
                    if is_private_app_path(normalized):
                        continue
                    entries.append({
                        "path": normalized,
                        "filename": Path(normalized).name,
                        "size_bytes": info.file_size,
                    })
        except (OSError, zipfile.BadZipFile) as exc:
            raise _error(422, "Release Source Bundleを読み込めません") from exc
        return entries

    def release_file_bytes(archive_path: Path, relative: str) -> bytes:
        try:
            normalized = normalize_app_relative_path(relative)
            with zipfile.ZipFile(archive_path) as archive:
                return archive.read(normalized)
        except (AppStorageError, KeyError, OSError, zipfile.BadZipFile) as exc:
            raise _error(404, "固定Releaseに指定ファイルがありません") from exc

    async def installed_runtime_bundle(
        session,
        app: App,
        project_id: UUID | None,
        target: AppTarget,
        *,
        source_bundle: tuple[Any, AppRelease, Path] | None = None,
    ) -> tuple[Any, AppRelease, AppArtifact, Path] | None:
        """Resolve the Target Runtime Bundle of the Project's fixed Release.

        ``source_bundle`` reuses a bundle the caller already resolved.  Without
        it every call re-runs the full SHA-256 of the Source Bundle archive,
        which is prohibitive for per-asset embedded requests.
        """
        if source_bundle is None:
            source_bundle = await installed_source_bundle(session, app, project_id)
        if source_bundle is None:
            return None
        binding, release, _source_path = source_bundle
        artifact = await session.scalar(select(AppArtifact).where(
            AppArtifact.release_id == release.id,
            AppArtifact.target_id == target.id,
            AppArtifact.artifact_type == "runtime_bundle",
        ).limit(1))
        if artifact is None:
            raise _error(409, "固定ReleaseのTarget Runtime Bundleがありません")
        try:
            path = resolve_app_artifact_file(
                app.id,
                release.id,
                Path(str(artifact.filename)).name,
                workspace_root=workspace_root,
            )
            _verify_artifact_file_cached(
                path,
                expected_sha256=artifact.sha256,
                expected_size_bytes=artifact.size_bytes,
            )
        except (AppStorageError, OSError) as exc:
            raise _error(409, f"固定ReleaseのRuntime Bundleを利用できません: {exc}") from exc
        return binding, release, artifact, path

    route_context = AppRouterContext(
        get_db_manager=get_db_manager,
        require_auth_dependency=require_auth_dependency,
        current_user=current_user,
        require_app=require_app,
        uuid=_uuid,
        user_id=_user_id,
        error=_error,
    )

    @router.get("/api/apps")
    async def list_apps(
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        manager = get_db_manager()
        session = await manager.get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            if project_uuid is not None:
                await project_required(session, str(project_uuid), user)
            apps = await service.list_accessible_apps(
                session,
                user_id=_user_id(user),
                user_role=user.get("role"),
                project_id=project_uuid,
            )
            app_ids = [app.id for app, _permission in apps]
            # App 1件ごとの targets / bindings / project_access クエリを
            # まとめて引き、Appの件数に比例したラウンドトリップを無くす。
            targets_by_app = await targets_for_apps(session, app_ids)
            bindings_by_app: dict[UUID, list[ProjectApp]] = {}
            if app_ids:
                for binding in (await session.scalars(
                    select(ProjectApp).where(
                        ProjectApp.app_id.in_(app_ids),
                        ProjectApp.enabled.is_(True),
                    )
                )).all():
                    bindings_by_app.setdefault(binding.app_id, []).append(binding)
            project_access_cache: dict[UUID, bool] = {}

            async def _project_visible(candidate_project_id: UUID) -> bool:
                cached = project_access_cache.get(candidate_project_id)
                if cached is None:
                    cached = await service.project_access(
                        session,
                        project_id=candidate_project_id,
                        user_id=_user_id(user),
                        user_role=user.get("role"),
                    )
                    project_access_cache[candidate_project_id] = cached
                return cached

            payload = []
            for app, permission in apps:
                visible_project_ids = [
                    str(binding.project_id)
                    for binding in bindings_by_app.get(app.id, [])
                    if await _project_visible(binding.project_id)
                ]
                item = app_payload(app, permission, targets=targets_by_app.get(app.id, []))
                item["related_project_ids"] = visible_project_ids
                payload.append(item)
            return {"apps": payload}
        finally:
            await session.close()

    @router.post("/api/apps")
    async def create_app(
        payload: AppCreatePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        manager = get_db_manager()
        if manager is None:
            raise _error(503, "Database not available")
        session = await manager.get_session()
        operation_lock = None
        created_workspace: Path | None = None
        committed = False
        try:
            origin = _uuid(payload.origin_project_id, "origin_project_id") if payload.origin_project_id else None
            if origin is not None:
                await project_required(session, str(origin), user)
                operation_lock = project_operation_lock(origin, workspace_root=workspace_root)
                await operation_lock.acquire()
                locked_project_id = await session.scalar(
                    select(Project.id)
                    .where(Project.id == origin, Project.deleted_at.is_(None))
                    .with_for_update()
                )
                if locked_project_id is None:
                    raise _error(404, "Project not found")
                await project_write_required(session, origin, user)
            slug = _slugify(payload.slug or payload.name)
            if not _SLUG_RE.fullmatch(slug):
                raise _error(400, "slug が不正です")
            if payload.visibility not in {"private", "shared", "public"}:
                raise _error(400, "visibility が不正です")
            try:
                app = await service.create_app(
                    session,
                    owner_user_id=_user_id(user),
                    name=payload.name,
                    slug=slug,
                    description=payload.description,
                    origin_project_id=origin,
                    visibility=payload.visibility,
                )
                # service.create_app は workspace をファイルシステムへ作る。
                # DBへ確定できなかった場合に孤児 workspace が残らないよう、
                # import_project_source と同じ補償対象として控えておく。
                created_workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
                if origin is not None:
                    session.add(
                        ProjectApp(
                            project_id=origin,
                            app_id=app.id,
                            binding_mode="development",
                            created_by=_user_id(user),
                        )
                )
                instance_existed = False
                try:
                    if origin is not None:
                        instance_path = get_app_instance_path(
                            origin,
                            app.id,
                            workspace_root=workspace_root,
                        )
                        instance_existed = instance_path.exists()
                        ensure_app_instance(origin, app.id, workspace_root=workspace_root)
                    await session.commit()
                    committed = True
                except Exception:
                    await session.rollback()
                    if origin is not None and not instance_existed:
                        remove_app_instance(origin, app.id, workspace_root=workspace_root)
                    raise
                targets = list((await session.scalars(
                    select(AppTarget)
                    .where(AppTarget.app_id == app.id)
                    .order_by(AppTarget.target_key)
                )).all())
                return JSONResponse(
                    {"success": True, "app": app_payload(app, "admin", targets=targets)},
                    status_code=201,
                )
            except IntegrityError as exc:
                await session.rollback()
                raise _error(409, "同じ slug の App が既に存在します") from exc
        except Exception:
            if not committed and created_workspace is not None:
                _force_remove_tree(created_workspace)
            raise
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    @router.post("/api/apps/import-project-source")
    async def import_project_source(
        payload: AppProjectSourceImportPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Create an App from a folder or file inside a Project workspace."""
        user = await current_user(request)
        manager = get_db_manager()
        if manager is None:
            raise _error(503, "Database not available")
        session = await manager.get_session()
        app_workspace: Path | None = None
        operation_lock = None
        try:
            project_uuid = await project_required(session, payload.project_id, user)
            await project_write_required(session, project_uuid, user)
            operation_lock = project_operation_lock(project_uuid, workspace_root=workspace_root)
            await operation_lock.acquire()
            locked_project_id = await session.scalar(
                select(Project.id)
                .where(Project.id == project_uuid, Project.deleted_at.is_(None))
                .with_for_update()
            )
            if locked_project_id is None:
                raise _error(404, "Project not found")
            await project_write_required(session, project_uuid, user)
            project_root = get_project_workspace_path(project_uuid, workspace_root=workspace_root)
            requested = payload.source_path.replace("\\", "/").strip("/")
            if not requested or requested.startswith("/") or re.match(r"^[A-Za-z]:", requested):
                raise _error(400, "source_path はProject workspaceからの相対パスで指定してください")
            parts = PurePosixPath(requested).parts
            if any(part in {"", ".", ".."} for part in parts):
                raise _error(400, "source_path が不正です")
            source = (project_root / Path(*parts)).resolve()
            try:
                source.relative_to(project_root.resolve())
            except ValueError as exc:
                raise _error(400, "Project workspaceの外側は取り込めません") from exc
            if not source.exists() or not (source.is_file() or source.is_dir()):
                raise _error(404, "指定されたProject workspace内のファイルまたはフォルダが見つかりません")

            slug = _slugify(payload.slug or payload.name)
            if not _SLUG_RE.fullmatch(slug):
                raise _error(400, "slug が不正です")
            if payload.visibility not in {"private", "shared", "public"}:
                raise _error(400, "visibility が不正です")

            app = await service.create_app(
                session,
                owner_user_id=_user_id(user),
                name=payload.name,
                slug=slug,
                description=payload.description,
                origin_project_id=project_uuid,
                visibility=payload.visibility,
            )
            session.add(
                ProjectApp(
                    project_id=project_uuid,
                    app_id=app.id,
                    binding_mode="development",
                    created_by=_user_id(user),
                )
            )
            app_workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
            candidates: list[tuple[Path, Path]] = []
            if source.is_file():
                candidates.append((source, Path(source.name)))
            else:
                for item in source.rglob("*"):
                    if item.is_symlink():
                        raise _error(400, "シンボリックリンクを含むProject sourceは取り込めません")
                    if not item.is_file():
                        continue
                    relative = item.relative_to(source).as_posix()
                    if is_private_app_path(relative):
                        continue
                    candidates.append((item, Path(relative)))
            if not candidates:
                raise _error(400, "取り込めるファイルがありません")
            if len(candidates) > 5000:
                raise _error(400, "取り込みファイル数が上限を超えています")

            total_size = 0
            imported_files: list[str] = []
            imported_path_keys: set[str] = set()
            for source_file, relative_path in candidates:
                normalized = canonical_app_source_path(relative_path.as_posix())
                if normalized.casefold() in imported_path_keys:
                    raise _error(400, f"同じpathが重複しています: {normalized}")
                imported_path_keys.add(normalized.casefold())
                destination = resolve_app_file(app.id, normalized, workspace_root=workspace_root)
                size = source_file.stat().st_size
                total_size += size
                if size > 50 * 1024 * 1024 or total_size > 250 * 1024 * 1024:
                    raise _error(400, "取り込みサイズが上限を超えています")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination)
                imported_files.append(normalized)

            generate_import_metadata(
                workspace=app_workspace,
                app_name=app.name,
                description=app.description or "",
                source_path=requested,
                imported_files=imported_files,
            )
            imported_manifest, _imported_manifest_text, _imported_manifest_hash = load_app_manifest(app_workspace)
            imported_readme_path = resolve_app_file(app.id, "README.md", workspace_root=workspace_root)
            imported_readme = imported_readme_path.read_text(encoding="utf-8") if imported_readme_path.exists() else ""
            # workspace の全走査は重い（rglob 全列挙 + 最大48ファイル読み込み +
            # xlsm の VBA 展開）。App operation lock を保持したままなので、
            # 1回だけ収集して分析と Manifest 書き戻しの両方で使い回す。
            imported_evidence = collect_source_evidence(
                app_workspace, manifest=imported_manifest
            )
            imported_analysis = await analyze_app_workspace(
                workspace=app_workspace,
                name=app.name,
                description=app.description or str(imported_manifest.get("description") or ""),
                readme=imported_readme,
                llm_client=get_llm_client() if get_llm_client is not None else None,
                manifest=imported_manifest,
                evidence=imported_evidence,
            )
            _analysis_manifest, analysis_manifest_text = write_analysis_to_manifest(
                workspace=app_workspace,
                analysis=imported_analysis,
                evidence=imported_evidence,
            )
            analysis_manifest = parse_manifest_text(analysis_manifest_text)
            validate_manifest_workspace(analysis_manifest, app_workspace)
            updated_imported_readme = merge_analysis_into_readme(imported_readme, imported_analysis)
            if updated_imported_readme != imported_readme:
                imported_readme_path.write_text(
                    updated_imported_readme,
                    encoding="utf-8",
                    newline="\n",
                )
            manifest, _manifest_text, _manifest_hash = load_app_manifest(app_workspace)
            validate_manifest_workspace(manifest, app_workspace)
            await sync_manifest_targets(session, app, app_workspace)
            await service.ensure_readme_node(session, app, _user_id(user), workspace=app_workspace)
            instance_existed = False
            try:
                instance_path = get_app_instance_path(
                    project_uuid,
                    app.id,
                    workspace_root=workspace_root,
                )
                instance_existed = instance_path.exists()
                ensure_app_instance(project_uuid, app.id, workspace_root=workspace_root)
                await session.commit()
            except Exception:
                await session.rollback()
                if not instance_existed:
                    remove_app_instance(project_uuid, app.id, workspace_root=workspace_root)
                raise
            # checkpoint は commit の後。取り込みが DB へ確定できなかった場合に
            # App Git だけ revision が進む（phantom revision）のを防ぐ。
            try:
                AppGitService(workspace_root=workspace_root).checkpoint(
                    app.id,
                    "Project workspaceからsourceを取り込み",
                    actor=str(_user_id(user)),
                )
            except AppGitError:
                pass
            targets = list((await session.scalars(
                select(AppTarget).where(AppTarget.app_id == app.id).order_by(AppTarget.target_key)
            )).all())
            result = app_payload(app, "admin", targets=targets)
            result["imported_files"] = imported_files
            result["analysis"] = imported_analysis
            return JSONResponse({"success": True, "app": result}, status_code=201)
        except IntegrityError as exc:
            await session.rollback()
            if app_workspace is not None:
                _force_remove_tree(app_workspace)
            raise _error(409, "同じ slug の App が既に存在します") from exc
        except HTTPException:
            await session.rollback()
            if app_workspace is not None:
                _force_remove_tree(app_workspace)
            raise
        except (AppManifestError, AppStorageError, OSError) as exc:
            await session.rollback()
            if app_workspace is not None:
                _force_remove_tree(app_workspace)
            raise _error(400, str(exc)) from exc
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    @router.post("/api/apps/{app_id}/source-imports/preview")
    async def preview_source_import(
        app_id: str,
        request: Request,
        files: list[UploadFile] = File(...),
        relative_paths: list[str] | None = Form(default=None),
        expected_revision: str | None = Form(default=None),
        root_mode: str = Form(default="strip_common"),
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        """Stage a folder/ZIP upload and return a read-only App diff preview."""
        user = await current_user(request)
        manager = get_db_manager()
        if manager is None:
            raise _error(503, "Database not available")
        session = await manager.get_session()
        temp_root = Path(tempfile.mkdtemp(prefix="aoitalk-app-upload-"))
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="developer", project_id=project_uuid)
            await require_development_binding(session, app, project_uuid)
            if not files:
                raise _error(400, "取り込むファイルを指定してください")
            if relative_paths and len(relative_paths) != len(files):
                raise _error(400, "relative_paths と files の件数が一致しません")
            if len(files) > MAX_FILES:
                raise _error(413, "取り込みファイル数が上限を超えています")
            uploaded: list[tuple[Path, str]] = []
            total_size = 0
            for index, upload in enumerate(files):
                filename = (relative_paths[index] if relative_paths else upload.filename) or f"upload-{index}"
                temporary = temp_root / f"{index:05d}.upload"
                file_size = 0
                with temporary.open("wb") as output:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        file_size += len(chunk)
                        total_size += len(chunk)
                        if file_size > MAX_FILE_SIZE or total_size > MAX_TOTAL_SIZE:
                            raise _error(413, "アップロードサイズが上限を超えています")
                        output.write(chunk)
                uploaded.append((temporary, filename))
            try:
                result = source_update_service.create_preview(
                    app_id,
                    uploaded,
                    expected_revision=expected_revision,
                    root_mode=root_mode,
                )
            except AppSourceUpdateError as exc:
                raise _error(exc.status_code, str(exc)) from exc
            return result
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
            await session.close()

    @router.post("/api/apps/{app_id}/source-imports/{import_id}/apply")
    async def apply_source_import(
        app_id: str,
        import_id: str,
        payload: AppSourceApplyPayload,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        """Apply a previously previewed source update as one App revision."""
        user = await current_user(request)
        manager = get_db_manager()
        if manager is None:
            raise _error(503, "Database not available")
        session = await manager.get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _permission = await require_app(
                session,
                app_id,
                user,
                required="developer",
                project_id=project_uuid,
            )
            await require_development_binding(session, app, project_uuid)
            try:
                result = await source_update_service.apply_async(
                    app_id,
                    import_id,
                    expected_revision=payload.expected_revision,
                    delete_paths=payload.delete_paths,
                    session=session,
                    app=app,
                    app_service=service,
                    actor=str(_user_id(user)),
                    actor_user_id=_user_id(user),
                )
            except AppSourceUpdateError as exc:
                raise _error(exc.status_code, str(exc)) from exc
            return result
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}")
    async def get_app(app_id: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, permission = await require_app(session, app_id, user, project_id=project_uuid)
            targets = list((await session.scalars(select(AppTarget).where(AppTarget.app_id == app.id).order_by(AppTarget.target_key))).all())
            releases = list((await session.scalars(
                select(AppRelease)
                .options(selectinload(AppRelease.artifacts))
                .where(AppRelease.app_id == app.id)
                .order_by(AppRelease.created_at.desc())
                .limit(10)
            )).all())
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                release_manifest, _release_manifest_text, _release_readme = release_manifest_and_readme(selected_bundle[2])
                targets = release_target_payloads(
                    release_manifest,
                    targets,
                )
            bindings = list((await session.scalars(select(ProjectApp).where(ProjectApp.app_id == app.id, ProjectApp.enabled.is_(True)))).all())
            payload = app_payload(app, permission, targets=targets)
            payload["releases"] = [release.to_dict() for release in releases]
            payload["binding_mode"] = selected_bundle[0].binding_mode if selected_bundle else "development"
            payload["selected_release"] = selected_bundle[1].to_dict() if selected_bundle else None
            payload["related_project_ids"] = [
                str(binding.project_id)
                for binding in bindings
                if await service.project_access(
                    session,
                    project_id=binding.project_id,
                    user_id=_user_id(user),
                    user_role=user.get("role"),
                )
            ]
            return {"app": payload}
        finally:
            await session.close()

    @router.patch("/api/apps/{app_id}")
    async def patch_app(app_id: str, payload: AppPatchPayload, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="admin", project_id=project_uuid, include_archived=True)
            if project_uuid is not None and app.owner_user_id != _user_id(user) and user.get("role") != "admin":
                raise _error(403, "Project単位のApp権限ではApp全体設定を変更できません")
            if payload.archived:
                await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
                await ensure_no_active_jobs(session, app.id)
            if payload.name is not None:
                app.name = payload.name.strip()
            if payload.description is not None:
                app.description = payload.description.strip() or None
            if payload.archived is not None:
                app.archived_at = datetime.utcnow() if payload.archived else None
            if payload.visibility is not None:
                if payload.visibility not in {"private", "shared", "public"}:
                    raise _error(400, "visibility が不正です")
                app.visibility = payload.visibility
            if payload.default_target_key is not None:
                target = await session.scalar(select(AppTarget).where(and_(AppTarget.app_id == app.id, AppTarget.target_key == payload.default_target_key)).limit(1))
                if not target:
                    raise _error(400, "default_target_key が存在しません")
                app.default_target_key = payload.default_target_key
            await session.commit()
            return {"success": True, "app": app.to_dict()}
        finally:
            await session.close()

    @router.delete("/api/apps/{app_id}")
    async def delete_app(app_id: str, request: Request, _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            app, _ = await require_app(session, app_id, user, required="admin")
            # Standard DELETE is archive. A future explicit purge endpoint can perform
            # the destructive workspace/artifact removal after all references are checked.
            await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
            await ensure_no_active_jobs(session, app.id)
            app.archived_at = datetime.utcnow()
            await session.commit()
            return {"success": True, "archived": True, "app": app.to_dict()}
        finally:
            await session.close()

    register_app_grant_routes(router, route_context)

    @router.get("/api/apps/{app_id}/targets")
    async def list_targets(app_id: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            targets = list((await session.scalars(select(AppTarget).where(AppTarget.app_id == app.id).order_by(AppTarget.target_key))).all())
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                manifest, _manifest_text, _readme = release_manifest_and_readme(selected_bundle[2])
                return {"targets": release_target_payloads(manifest, targets), "release_id": str(selected_bundle[1].id)}
            return {"targets": [target.to_dict() for target in targets]}
        finally:
            await session.close()

    @router.post("/api/apps/{app_id}/manifest/validate")
    async def validate_manifest(app_id: str, payload: ManifestValidatePayload, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            # 呼び出し側が content を指定した検証は workspace の実在チェックを
            # 伴うため、任意 path の存在オラクルになりうる。Manifest を編集
            # できる developer 以上に限定する。保存済み Manifest の検証
            # (content 未指定) は従来どおり viewer で通す。
            app, _ = await require_app(
                session,
                app_id,
                user,
                required="developer" if payload.content is not None else "viewer",
                project_id=project_uuid,
            )
            workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
            content = payload.content
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if content is None:
                if selected_bundle:
                    content = release_file_bytes(selected_bundle[2], "aoitalk.app.yaml").decode("utf-8")
                else:
                    content = (workspace / "aoitalk.app.yaml").read_text(encoding="utf-8")
            # 開発中の workspace でも保存できるよう DRAFT で検証する。entrypoint /
            # build.output の実在は warning として返し、Release 作成時に STRICT で
            # 改めて必須化する。
            try:
                result = validate_manifest_text(
                    content,
                    mode=ValidationMode.DRAFT,
                    workspace=None if selected_bundle else workspace,
                )
            except (AppManifestError, OSError) as exc:
                return JSONResponse({"valid": False, "errors": [str(exc)]}, status_code=422)
            if not result.valid:
                return JSONResponse(
                    {
                        "valid": False,
                        "errors": list(result.errors),
                        "warnings": list(result.warnings),
                    },
                    status_code=422,
                )
            return {
                "valid": True,
                "manifest": result.manifest,
                "warnings": list(result.warnings),
            }
        finally:
            await session.close()

    @router.post("/api/apps/{app_id}/analysis")
    async def analyze_app(
        app_id: str,
        payload: AppAnalysisPayload,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        """Analyze App business content and update the canonical overview metadata."""
        user = await current_user(request)
        session = await get_db_manager().get_session()
        operation_lock = None
        committed = False
        manifest_path = None
        readme_path = None
        current_manifest_text = ""
        readme = ""
        readme_existed = False
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(
                session,
                app_id,
                user,
                required="developer",
                project_id=project_uuid,
            )
            await require_development_binding(session, app, project_uuid)
            operation_lock = app_operation_lock(app.id, workspace_root=workspace_root)
            await operation_lock.acquire()
            workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
            manifest_path = resolve_app_file(app.id, "aoitalk.app.yaml", workspace_root=workspace_root)
            current_manifest_text = manifest_path.read_text(encoding="utf-8")
            current_manifest_hash = hashlib.sha256(
                current_manifest_text.encode("utf-8")
            ).hexdigest()
            if (
                payload.expected_manifest_sha256
                and payload.expected_manifest_sha256 != current_manifest_hash
            ):
                return JSONResponse(
                    {
                        "detail": "Manifestが競合しています",
                        "current_sha256": current_manifest_hash,
                    },
                    status_code=409,
                )
            manifest, _manifest_text, _manifest_hash = load_app_manifest(workspace)
            readme_path = resolve_app_file(app.id, "README.md", workspace_root=workspace_root)
            readme_existed = readme_path.exists()
            readme = readme_path.read_text(encoding="utf-8") if readme_existed else ""
            llm_client = get_llm_client() if get_llm_client is not None else None
            # 上と同じ理由で、workspace の全走査は1回に集約する。
            evidence = collect_source_evidence(workspace, manifest=manifest)
            analysis = await analyze_app_workspace(
                workspace=workspace,
                name=app.name,
                description=app.description or str(manifest.get("description") or ""),
                readme=readme,
                llm_client=llm_client,
                manifest=manifest,
                evidence=evidence,
            )
            _updated_manifest, updated_manifest_text = write_analysis_to_manifest(
                workspace=workspace,
                analysis=analysis,
                evidence=evidence,
            )
            normalized_manifest = parse_manifest_text(updated_manifest_text)
            validate_manifest_workspace(
                normalized_manifest, workspace, mode=ValidationMode.DRAFT
            )
            targets = await sync_manifest_targets_unlocked(session, app, workspace)
            updated_readme = merge_analysis_into_readme(readme, analysis)
            if updated_readme != readme:
                readme_path.write_text(updated_readme, encoding="utf-8", newline="\n")
                await service.sync_readme_to_node(session, app, _user_id(user))
            else:
                await service.ensure_readme_node(
                    session,
                    app,
                    _user_id(user),
                    workspace=workspace,
                )
            await session.commit()
            committed = True
            try:
                revision = AppGitService(workspace_root=workspace_root).checkpoint(
                    app.id,
                    "App業務内容を分析して概要を更新",
                    actor=str(_user_id(user)),
                )
            except AppGitError as exc:
                revision = None
                logger.warning("App Git analysis checkpoint failed: %s", exc)
            return {
                "success": True,
                "analysis": analysis,
                "manifest": normalized_manifest,
                "manifest_hash": hashlib.sha256(
                    updated_manifest_text.encode("utf-8")
                ).hexdigest(),
                "readme": updated_readme,
                "targets": [target.to_dict() for target in targets],
                "revision": revision,
            }
        except (AppManifestError, OSError, ValueError) as exc:
            if not committed:
                await session.rollback()
                try:
                    if manifest_path is not None:
                        manifest_path.write_text(current_manifest_text, encoding="utf-8", newline="\n")
                    if readme_path is not None:
                        if readme_existed:
                            readme_path.write_text(readme, encoding="utf-8", newline="\n")
                        elif readme_path.exists():
                            readme_path.unlink()
                except OSError:
                    logger.exception("App analysis rollback failed for app %s", app.id)
            raise _error(422, str(exc)) from exc
        except Exception:
            if not committed:
                await session.rollback()
                try:
                    if manifest_path is not None:
                        manifest_path.write_text(current_manifest_text, encoding="utf-8", newline="\n")
                    if readme_path is not None:
                        if readme_existed:
                            readme_path.write_text(readme, encoding="utf-8", newline="\n")
                        elif readme_path.exists():
                            readme_path.unlink()
                except OSError:
                    logger.exception("App analysis rollback failed for app %s", app.id)
            raise
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    @router.put("/api/apps/{app_id}/manifest")
    async def update_manifest(
        app_id: str,
        payload: ManifestUpdatePayload,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        operation_lock = None
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="developer", project_id=project_uuid)
            await require_development_binding(session, app, project_uuid)
            operation_lock = app_operation_lock(app.id, workspace_root=workspace_root)
            await operation_lock.acquire()
            workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
            manifest_path = resolve_app_file(app.id, "aoitalk.app.yaml", workspace_root=workspace_root)
            current_text = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
            current_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
            if payload.expected_sha256 and payload.expected_sha256 != current_hash:
                return JSONResponse(
                    {"detail": "Manifestが競合しています", "current_sha256": current_hash},
                    status_code=409,
                )
            try:
                manifest = parse_manifest_text(payload.content)
                # 保存時は DRAFT。build 前で成果物が無い workspace でも
                # Manifest を保存できるようにする。
                validate_manifest_workspace(manifest, workspace, mode=ValidationMode.DRAFT)
            except AppManifestError as exc:
                raise _error(422, str(exc)) from exc
            committed = False
            try:
                manifest_path.write_text(payload.content, encoding="utf-8", newline="\n")
                targets = await sync_manifest_targets_unlocked(session, app, workspace)
                # Keep the database and canonical file durable before taking
                # the Git checkpoint.  A checkpoint failure must not leave
                # the DB rolled back while the Manifest file is new.
                await session.commit()
                committed = True
                try:
                    revision = AppGitService(workspace_root=workspace_root).checkpoint(
                        app.id,
                        "aoitalk.app.yaml を更新",
                        actor=str(_user_id(user)),
                    )
                except AppGitError as exc:
                    logger.warning("App Git manifest checkpoint failed: %s", exc)
                    revision = None
                return {
                    "success": True,
                    "manifest_hash": hashlib.sha256(payload.content.encode("utf-8")).hexdigest(),
                    "targets": [target.to_dict() for target in targets],
                    "revision": revision,
                }
            except AppManifestError as exc:
                if not committed:
                    await session.rollback()
                    try:
                        manifest_path.write_text(current_text, encoding="utf-8", newline="\n")
                    except OSError:
                        logger.exception("Manifest rollback failed for app %s", app.id)
                raise _error(422, str(exc)) from exc
            except Exception:
                if not committed:
                    await session.rollback()
                    try:
                        manifest_path.write_text(current_text, encoding="utf-8", newline="\n")
                    except OSError:
                        logger.exception("Manifest rollback failed for app %s", app.id)
                raise
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    @router.get("/api/apps/{app_id}/files")
    async def files(app_id: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                return {"files": release_file_entries(selected_bundle[2]), "release_id": str(selected_bundle[1].id)}
            return {"files": list_app_files(app.id, workspace_root=workspace_root)}
        finally:
            await session.close()

    @router.put("/api/apps/{app_id}/readme")
    async def update_readme(
        app_id: str,
        payload: ReadmeUpdatePayload,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        """Canonical Docs editor endpoint: README is written first, then the node/revision."""
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="developer", project_id=project_uuid)
            await require_development_binding(session, app, project_uuid)
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
                readme_path = resolve_app_file(app.id, "README.md", workspace_root=workspace_root)
                current_text = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
                current_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
                if payload.expected_sha256 and payload.expected_sha256 != current_hash:
                    return JSONResponse(
                        {"detail": "READMEが競合しています", "current_sha256": current_hash},
                        status_code=409,
                    )
                # 正本(README)→DB→commit→checkpoint の順に確定させる。
                # commit までに失敗したら journal が README を変更前へ戻すので、
                # ファイルだけ進んで DB が戻る不整合と phantom revision を防ぐ。
                # commit 成功後は補償対象から外し、以降の失敗で確定済みの
                # README を巻き戻さない。
                journal = AppWorkspaceJournal(workspace)
                try:
                    journal.stash("README.md")
                    readme_path.write_text(payload.content, encoding="utf-8", newline="\n")
                    node = await service.sync_readme_to_node(session, app, _user_id(user))
                    try:
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        raise
                except BaseException:
                    journal.rollback()
                    raise
                finally:
                    journal.close()
                node_id = str(node.id)
                # commit 後の best-effort。App Git は追記専用なので、失敗しても
                # README と DB は確定済みで整合しており、次回 checkpoint が拾う。
                try:
                    revision = AppGitService(workspace_root=workspace_root).checkpoint(
                        app.id,
                        "App README を更新",
                        actor=str(_user_id(user)),
                    )
                except AppGitError as exc:
                    revision = None
                    logger.warning("App README checkpoint failed: %s", exc)
                return {
                    "success": True,
                    "sha256": hashlib.sha256(payload.content.encode("utf-8")).hexdigest(),
                    "revision": revision,
                    "node_id": node_id,
                }
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/files/content")
    async def read_file(app_id: str, path: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                try:
                    normalized = normalize_app_relative_path(path)
                except AppStorageError as exc:
                    raise _error(400, str(exc)) from exc
                if is_private_app_path(normalized) or not is_text_app_path(normalized):
                    raise _error(415, "固定Releaseの非テキストファイルはdownload APIを使用してください")
                content_bytes = release_file_bytes(selected_bundle[2], normalized)
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise _error(415, "UTF-8テキストではありません。download APIを使用してください") from exc
                return {"path": normalized, "content": content, "sha256": hashlib.sha256(content_bytes).hexdigest(), "release_id": str(selected_bundle[1].id)}
            try:
                normalized = normalize_app_relative_path(path)
            except AppStorageError as exc:
                raise _error(400, str(exc)) from exc
            if is_private_app_path(normalized):
                raise _error(403, "private/runtime file はApp file APIから参照できません")
            if not is_text_app_path(normalized):
                raise _error(415, "バイナリまたは未知の形式はcontent APIで扱えません。download APIを使用してください")
            try:
                target = resolve_app_file(app.id, normalized, workspace_root=workspace_root)
            except AppStorageError as exc:
                raise _error(400, str(exc)) from exc
            if not target.exists() or not target.is_file():
                raise _error(404, "ファイルが見つかりません")
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise _error(415, "UTF-8テキストではありません。download APIを使用してください") from exc
            return {"path": normalized, "content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/files/download")
    async def download_app_file(
        app_id: str,
        request: Request,
        path: str = Query(..., min_length=1),
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                try:
                    normalized = normalize_app_relative_path(path)
                except AppStorageError as exc:
                    raise _error(400, str(exc)) from exc
                if is_private_app_path(normalized) or is_sensitive_app_path(normalized):
                    raise _error(403, "この固定Releaseファイルはダウンロードできません")
                content = release_file_bytes(selected_bundle[2], normalized)
                return Response(
                    content=content,
                    media_type=mimetypes.guess_type(Path(normalized).name)[0] or "application/octet-stream",
                    headers={
                        "Content-Disposition": f'attachment; filename="{Path(normalized).name}"',
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            try:
                normalized = normalize_app_relative_path(path)
            except AppStorageError as exc:
                raise _error(400, str(exc)) from exc
            if is_private_app_path(normalized) or is_sensitive_app_path(normalized):
                raise _error(403, "このAppファイルはダウンロードできません")
            try:
                file_path = resolve_app_file(app.id, normalized, workspace_root=workspace_root)
            except AppStorageError as exc:
                raise _error(400, str(exc)) from exc
            if not file_path.is_file():
                raise _error(404, "Appファイルが見つかりません")
            media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            return FileResponse(
                path=file_path,
                media_type=media_type,
                filename=file_path.name,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/files/archive")
    async def download_app_archive(
        app_id: str,
        request: Request,
        project_id: str | None = Query(None),
        include_paths: str | None = Query(None),
        exclude_git: bool = Query(False),
        exclude_dependencies: bool = Query(False),
        exclude_runtime: bool = Query(False),
        exclude_credentials: bool = Query(False),
        _: None = Depends(require_auth_dependency),
    ):
        """App のソース一式を zip で返す。

        既定ではworkspace全体を返す。include_paths が指定された場合は、その配下だけを
        返す。Git、依存/build/cache、runtime、credential はGUIで選択されたカテゴリだけを
        除外する。内容の公開可否をサーバー側で推測せず、
        既存どおりrunner以上の利用者が選択する。Projectが保存版を固定している場合、通常ソースはその
        Releaseを使い、Release作成時に除外済みのローカルカテゴリだけworkspaceから
        補完する。
        """
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            selected_paths = _parse_archive_include_paths(include_paths)
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _permission = await require_app(
                session,
                app_id,
                user,
                required="runner",
                project_id=project_uuid,
            )
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            root_name = _archive_root_name(app)
            if selected_bundle:
                version = _safe_archive_component(selected_bundle[1].version, limit=40)
                workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
                archive_path, written = await asyncio.to_thread(
                    _build_release_archive,
                    selected_bundle[2],
                    root_name,
                    workspace,
                    exclude_git=exclude_git,
                    exclude_dependencies=exclude_dependencies,
                    exclude_runtime=exclude_runtime,
                    exclude_credentials=exclude_credentials,
                    include_paths=selected_paths,
                )
                filename = f"{root_name}_{version}.zip" if version else f"{root_name}.zip"
            else:
                archive_path, written = await asyncio.to_thread(
                    _build_workspace_archive,
                    app.id,
                    root_name,
                    workspace_root,
                    exclude_git=exclude_git,
                    exclude_dependencies=exclude_dependencies,
                    exclude_runtime=exclude_runtime,
                    exclude_credentials=exclude_credentials,
                    include_paths=selected_paths,
                )
                filename = f"{root_name}.zip"
            if not written:
                _remove_temp_file(archive_path)
                raise _error(404, "ダウンロードできるファイルがありません")
            # 応答を返し切ってから temp を消す。FileResponse は path を保持する
            # だけなので、ここで削除すると本文が送れない。
            return FileResponse(
                path=archive_path,
                media_type="application/zip",
                filename=filename,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                background=BackgroundTask(_remove_temp_file, archive_path),
            )
        finally:
            await session.close()

    @router.put("/api/apps/{app_id}/files/content")
    async def write_file(app_id: str, payload: AppFileWritePayload, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="developer", project_id=project_uuid)
            await require_development_binding(session, app, project_uuid)
            try:
                normalized = normalize_app_relative_path(payload.path)
            except AppStorageError as exc:
                raise _error(400, str(exc)) from exc
            if is_private_app_path(normalized):
                raise _error(403, "private/runtime file はApp file APIから更新できません")
            if is_protected_app_path(normalized):
                raise _error(403, ".gitignore は書き込み保護されているため更新できません")
            if not is_text_app_path(normalized):
                raise _error(415, "バイナリまたは未知の形式はcontent APIで扱えません")
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
                try:
                    target = resolve_app_file(app.id, normalized, workspace_root=workspace_root)
                except AppStorageError as exc:
                    raise _error(400, str(exc)) from exc
                if payload.expected_sha256 and target.exists():
                    current_hash = sha256_file(target)
                    if current_hash != payload.expected_sha256:
                        return JSONResponse(
                            {"detail": "READMEまたはファイルが競合しています", "current_sha256": current_hash},
                            status_code=409,
                        )
                # ファイル→DB→commit→checkpoint。commit までに失敗したら
                # journal が対象ファイルを変更前へ戻す。commit 成功後は
                # 補償対象から外す。
                journal = AppWorkspaceJournal(workspace)
                try:
                    journal.stash(normalized)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(payload.content, encoding="utf-8", newline="\n")
                    if normalized.lower() == "readme.md":
                        await service.sync_readme_to_node(session, app, _user_id(user))
                    try:
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        raise
                except BaseException:
                    journal.rollback()
                    raise
                finally:
                    journal.close()
                written_hash = sha256_file(target)
                try:
                    revision = AppGitService(workspace_root=workspace_root).checkpoint(
                        app.id,
                        f"{normalized} を更新",
                        actor=str(_user_id(user)),
                    )
                except AppGitError as exc:
                    revision = None
                    logger.warning("App Git checkpoint failed: %s", exc)
                return {"success": True, "path": normalized, "sha256": written_hash, "revision": revision}
        finally:
            await session.close()

    @router.delete("/api/apps/{app_id}/files/content")
    async def delete_file(
        app_id: str,
        path: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="developer", project_id=project_uuid)
            await require_development_binding(session, app, project_uuid)
            if is_private_app_path(path):
                raise _error(403, "private/runtime file はApp file APIから削除できません")
            if is_protected_app_path(path):
                raise _error(403, ".gitignore は書き込み保護されているため削除できません")
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
                try:
                    target = resolve_app_file(app.id, path, workspace_root=workspace_root)
                    normalized = normalize_app_relative_path(path)
                except AppStorageError as exc:
                    raise _error(400, str(exc)) from exc
                if normalized.lower() in {"aoitalk.app.yaml", "readme.md"}:
                    raise _error(400, "ManifestとREADMEは削除できません")
                if not target.exists() or not target.is_file():
                    raise _error(404, "ファイルが見つかりません")
                # stash がそのまま削除になる。commit までに失敗したら journal が
                # 元のファイルを戻すので、削除だけ確定する状態を作らない。
                journal = AppWorkspaceJournal(workspace)
                try:
                    journal.stash(normalized)
                    try:
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        raise
                except BaseException:
                    journal.rollback()
                    raise
                finally:
                    journal.close()
                try:
                    revision = AppGitService(workspace_root=workspace_root).checkpoint(
                        app.id,
                        f"{normalized} を削除",
                        actor=str(_user_id(user)),
                    )
                except AppGitError as exc:
                    logger.warning("App Git delete checkpoint failed: %s", exc)
                    revision = None
                return {"success": True, "path": normalized, "revision": revision}
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/git/status")
    async def git_status(app_id: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                release = selected_bundle[1]
                return {
                    "available": True,
                    "fixed_release": True,
                    "clean": True,
                    "dirty": False,
                    "branch": f"release/{release.version}",
                    "revision": release.git_revision,
                    "files": [],
                }
        finally:
            await session.close()
        try:
            return AppGitService(workspace_root=workspace_root).status(app.id)
        except AppGitError as exc:
            raise _error(503, str(exc)) from exc

    @router.get("/api/apps/{app_id}/git/history")
    async def git_history(app_id: str, request: Request, limit: int = Query(20, ge=1, le=200), project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                release = selected_bundle[1]
                return {"history": [{
                    "revision": release.git_revision,
                    "message": f"保存版 v{release.version}",
                    "author": "Release",
                    "date": release.created_at.isoformat() if release.created_at else "",
                }]}
        finally:
            await session.close()
        try:
            return {"history": AppGitService(workspace_root=workspace_root).history(app.id, limit=limit)}
        except AppGitError as exc:
            raise _error(503, str(exc)) from exc

    @router.get("/api/apps/{app_id}/git/diff")
    async def git_diff(app_id: str, request: Request, rev_a: str | None = None, rev_b: str | None = None, path: str | None = None, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                return {"diff": "", "fixed_release": True}
        finally:
            await session.close()
        try:
            return {"diff": AppGitService(workspace_root=workspace_root).diff(app.id, rev_a, rev_b, path=path)}
        except (AppGitError, AppStorageError) as exc:
            raise _error(400, str(exc)) from exc

    @router.post("/api/apps/{app_id}/git/restore")
    async def git_restore(app_id: str, payload: AppGitRestorePayload, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        operation_lock = None
        restored = False
        checkpoint = None
        before_revision = None
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="developer", project_id=project_uuid)
            await require_development_binding(session, app, project_uuid)
            operation_lock = app_operation_lock(app.id, workspace_root=workspace_root)
            await operation_lock.acquire()
            await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
            normalized = normalize_app_relative_path(payload.path) if payload.path else None
            if normalized and (is_private_app_path(normalized) or normalized.lower() in {"readme.md", "aoitalk.app.yaml"}):
                raise _error(400, "README/Manifest/private runtime file はGit復元対象にできません")
            git = AppGitService(workspace_root=workspace_root)
            current = git.status(app.id)
            before_revision = current.get("revision")
            if current.get("dirty"):
                raise _error(409, "未保存のApp変更があります。先にcheckpointしてから復元してください")
            try:
                resolved = git.resolve_revision(app.id, payload.revision)
                git.restore_revision(app.id, resolved, path=normalized)
                restored = True
                if normalized is None:
                    workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
                    manifest, _manifest_text, _manifest_hash = load_app_manifest(workspace)
                    validate_manifest_workspace(manifest, workspace)
                    await sync_manifest_targets_unlocked(session, app, workspace)
                    await AppService(workspace_root=workspace_root).sync_readme_to_node(
                        session,
                        app,
                        _user_id(user),
                    )
                checkpoint = git.checkpoint(app.id, f"{normalized or 'App全体'} を復元", actor=str(_user_id(user)))
            except (AppGitError, AppManifestError, AppStorageError) as exc:
                raise _error(400, str(exc)) from exc
            if normalized is None:
                await session.commit()
            return {"success": True, "path": normalized or ".", "restored_from_revision": resolved, "revision": checkpoint}
        except Exception:
            await session.rollback()
            if restored and before_revision:
                try:
                    AppGitService(workspace_root=workspace_root).reset_to_revision(app.id, before_revision)
                except Exception:
                    logger.exception("App Git restore compensation failed: app_id=%s", app_id)
            raise
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    @router.get("/api/apps/{app_id}/jobs")
    async def list_jobs(app_id: str, request: Request, limit: int = Query(50, ge=1, le=200), project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            conditions = [AppJob.app_id == app.id]
            if project_uuid is not None:
                conditions.append(AppJob.project_id == project_uuid)
            jobs = list((await session.scalars(
                select(AppJob).where(and_(*conditions)).order_by(AppJob.started_at.desc()).limit(limit)
            )).all())
            visible_jobs = [
                job for job in jobs
                if await job_project_visible(session, job, user, project_uuid)
            ]
            return {"jobs": [job.to_dict() for job in visible_jobs]}
        finally:
            await session.close()

    @router.post("/api/apps/{app_id}/jobs")
    async def create_job(app_id: str, payload: AppJobPayload, request: Request, _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(payload.project_id, "project_id") if payload.project_id else None
            required = "runner" if payload.job_type == "run" else "developer" if payload.job_type in {"build", "test"} else "maintainer"
            app, _ = await require_app(session, app_id, user, required=required, project_id=project_uuid)
            try:
                assert_user_may_start_server_job(
                    user_role=user.get("role"),
                    config=_app_config(),
                )
            except ServerJobExecutionDenied as exc:
                raise _error(403, str(exc)) from exc
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
                if payload.job_type not in {"build", "test", "run", "package"}:
                    raise _error(400, "job_type が不正です")
                target = await session.scalar(select(AppTarget).where(and_(AppTarget.app_id == app.id, AppTarget.target_key == payload.target_key)).limit(1))
                if not target:
                    raise _error(404, "Target not found")
                release_uuid = _uuid(payload.release_id, "release_id") if payload.release_id else None
                release = None
                if release_uuid:
                    release = await session.scalar(select(AppRelease).where(and_(AppRelease.id == release_uuid, AppRelease.app_id == app.id)).limit(1))
                    if not release:
                        raise _error(404, "Release not found")
                    if release.status != "published":
                        raise _error(409, "公開済みではないReleaseではJobを実行できません")
                if project_uuid:
                    binding = await session.scalar(select(ProjectApp).where(
                        and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id)
                    ).limit(1))
                    if binding is None or not binding.enabled:
                        raise _error(403, "このProjectではAppが有効化されていません")
                    if binding.binding_mode == "installed":
                        if payload.job_type != "run":
                            raise _error(409, "固定保存版のProjectではBuild/Test/Packageは実行できません")
                        if binding.installed_release_id is None:
                            raise _error(409, "固定保存版が選択されていません")
                        if release_uuid is not None and release_uuid != binding.installed_release_id:
                            raise _error(409, "Projectに固定された保存版と一致しません")
                        release_uuid = binding.installed_release_id
                        release = await session.scalar(select(AppRelease).where(
                            and_(AppRelease.id == release_uuid, AppRelease.app_id == app.id, AppRelease.status == "published")
                        ).limit(1))
                        if release is None:
                            raise _error(409, "Projectに固定されたReleaseを利用できません")
                        # A newly-created current Target is not automatically
                        # executable from an older fixed Release.  Require the
                        # immutable Runtime Bundle before accepting the Job.
                        await installed_runtime_bundle(session, app, project_uuid, target)
                agent_run_uuid = _uuid(payload.agent_run_id, "agent_run_id") if payload.agent_run_id else None
                if agent_run_uuid:
                    run = await session.scalar(select(AgentRun).where(AgentRun.id == agent_run_uuid).limit(1))
                    if not run or run.app_id != app.id:
                        raise _error(404, "Agent Run not found for this App")
                    if project_uuid is not None and run.project_id != project_uuid:
                        raise _error(403, "Agent RunはJobのProjectに紐付いていません")
                    if run.app_target_id is not None and run.app_target_id != target.id:
                        raise _error(403, "Agent RunはJobのTargetに紐付いていません")
                # 固定Release実行はRelease作成時にSTRICT済みのRuntime Bundleを使う。
                # 開発workspaceを直接動かす run / package だけ実在を必須にする。
                # build / test は成果物を作る側なので、実行前に output が無いのは正常。
                if release_uuid is None and payload.job_type in {"run", "package"}:
                    try:
                        load_app_manifest(
                            get_app_workspace_path(app.id, workspace_root=workspace_root),
                            mode=ValidationMode.STRICT,
                        )
                    except AppManifestError as exc:
                        raise _error(422, f"Manifestが実行可能な状態ではありません: {exc}") from exc
                job = AppJob(
                    app_id=app.id,
                    target_id=target.id,
                    project_id=project_uuid,
                    release_id=release_uuid,
                    agent_run_id=agent_run_uuid,
                    job_type=payload.job_type,
                    status="queued",
                    input_json=payload.input_json,
                    started_by=_user_id(user),
                )
                session.add(job)
                await session.commit()
                job_id = job.id
                asyncio.create_task(execute_app_job(
                    get_db_manager(),
                    job_id,
                    workspace_root=workspace_root,
                    timeout_seconds=payload.timeout_seconds,
                    deployment_config=_app_config(),
                ))
                return JSONResponse({"success": True, "job": job.to_dict()}, status_code=202)
        finally:
            await session.close()

    @router.post("/api/apps/{app_id}/jobs/{job_id}/stop")
    async def stop_job(app_id: str, job_id: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="runner", project_id=project_uuid)
            job = await session.scalar(
                select(AppJob)
                .where(and_(AppJob.id == _uuid(job_id, "job_id"), AppJob.app_id == app.id))
                .with_for_update()
                .limit(1)
            )
            if not job:
                raise _error(404, "Job not found")
            if not await job_project_visible(session, job, user, project_uuid):
                raise _error(404, "Job not found")
            if job.status in {"succeeded", "failed", "cancelled"}:
                return {"success": True, "stopped": False, "job": job.to_dict()}
            stopped = stop_running_job(job.id)
            job.status = "cancelled"
            job.ended_at = datetime.utcnow()
            await session.commit()
            return {"success": True, "stopped": stopped, "job": job.to_dict()}
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/jobs/{job_id}/logs")
    async def job_logs(app_id: str, job_id: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            job = await session.scalar(select(AppJob).where(and_(AppJob.id == _uuid(job_id, "job_id"), AppJob.app_id == app.id)).limit(1))
            if not job:
                raise _error(404, "Job not found")
            if not await job_project_visible(session, job, user, project_uuid):
                raise _error(404, "Job not found")
            if not job.log_path:
                return {"job_id": str(job.id), "logs": ""}
            log_path = Path(job.log_path).resolve()
            # 読み取り専用エンドポイントなので instance ディレクトリを作らない。
            # 未作成なら「そのrootに属するログは無い」というだけで、
            # scope 判定には解決済みのpath比較だけがあればよい。
            allowed_roots = [get_app_workspace_path(app.id, workspace_root=workspace_root).resolve()]
            if job.project_id:
                allowed_roots.append(
                    get_app_instance_path(job.project_id, app.id, workspace_root=workspace_root).resolve()
                )
            if not any(log_path == root or root in log_path.parents for root in allowed_roots):
                raise _error(403, "Job log path is outside App scope")
            return {"job_id": str(job.id), "logs": log_path.read_text(encoding="utf-8") if log_path.exists() else ""}
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/releases")
    async def list_releases(app_id: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            releases = list((await session.scalars(
                select(AppRelease)
                .options(selectinload(AppRelease.artifacts))
                .where(AppRelease.app_id == app.id)
                .order_by(AppRelease.created_at.desc())
            )).all())
            return {"releases": [release.to_dict() for release in releases]}
        finally:
            await session.close()

    @router.post("/api/apps/{app_id}/releases")
    async def create_release(app_id: str, payload: AppReleasePayload, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, _ = await require_app(session, app_id, user, required="maintainer", project_id=project_uuid)
            await require_development_binding(session, app, project_uuid)
            try:
                release = await create_app_release(
                    session,
                    app,
                    version=payload.version,
                    created_by=_user_id(user),
                    changelog=payload.changelog,
                    workspace_root=workspace_root,
                    deployment_config=_app_config(),
                )
                await session.commit()
                await session.refresh(release, attribute_names=["artifacts"])
                return JSONResponse({"success": True, "release": release.to_dict()}, status_code=201)
            except AppReleaseError as exc:
                await session.rollback()
                raise _error(400, str(exc)) from exc
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/artifacts/{artifact_id}/download")
    async def download_artifact(app_id: str, artifact_id: str, request: Request, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            # Release artifact は「Source Bundle 一括」または「download_only の
            # 実行本体」であり、per-path のフィルタが効かない一括取得になる。
            # 単なる閲覧 (viewer) ではなく、実行相当の runner を要求する。
            app, _ = await require_app(session, app_id, user, required="runner", project_id=project_uuid)
            artifact = await session.scalar(select(AppArtifact).join(AppRelease, AppRelease.id == AppArtifact.release_id).where(and_(AppArtifact.id == _uuid(artifact_id, "artifact_id"), AppRelease.app_id == app.id)).limit(1))
            if not artifact:
                raise _error(404, "Artifact not found")
            release = await session.scalar(select(AppRelease).where(AppRelease.id == artifact.release_id).limit(1))
            if not release:
                raise _error(404, "Release not found")
            if release.status != "published":
                raise _error(409, "公開済みではないReleaseのArtifactは取得できません")
            if project_uuid is not None:
                binding = await session.scalar(select(ProjectApp).where(
                    and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id)
                ).limit(1))
                if binding is not None and binding.binding_mode == "installed" and artifact.release_id != binding.installed_release_id:
                    raise _error(403, "このProjectでは固定中の保存版以外を取得できません")
            # 他のartifact解決箇所と同じく、保存済み filename から basename だけを使う。
            artifact_filename = Path(str(artifact.filename)).name
            try:
                path = resolve_app_artifact_file(
                    app.id,
                    release.id,
                    artifact_filename,
                    workspace_root=workspace_root,
                )
                _verify_artifact_file_cached(
                    path,
                    expected_sha256=artifact.sha256,
                    expected_size_bytes=artifact.size_bytes,
                )
            except (AppStorageError, OSError) as exc:
                raise _error(409, f"Artifactを利用できません: {exc}") from exc
            return FileResponse(path, media_type="application/zip", filename=artifact_filename)
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/context")
    async def app_context(app_id: str, request: Request, target_key: str | None = None, project_id: str | None = Query(None), _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            app, permission = await require_app(session, app_id, user, project_id=project_uuid)
            if project_uuid is not None:
                binding = await session.scalar(select(ProjectApp).where(
                    and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id)
                ).limit(1))
                if binding is None or not binding.enabled:
                    raise _error(403, "このProjectではAppが有効化されていません")
            workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
            current_targets = list((await session.scalars(
                select(AppTarget).where(AppTarget.app_id == app.id).order_by(AppTarget.target_key)
            )).all())
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            selected_release = selected_bundle[1] if selected_bundle else None
            if selected_bundle:
                manifest, _text, readme = release_manifest_and_readme(selected_bundle[2])
                manifest_hash = selected_release.manifest_hash
                targets = release_target_payloads(manifest, current_targets)
            else:
                manifest, _text, manifest_hash = load_app_manifest(workspace)
                readme_path = resolve_app_file(app.id, "README.md", workspace_root=workspace_root)
                readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
                targets = [target.to_dict() for target in current_targets]
            releases = list((await session.scalars(
                select(AppRelease)
                .options(selectinload(AppRelease.artifacts))
                .where(AppRelease.app_id == app.id)
                .order_by(AppRelease.created_at.desc())
                .limit(5)
            )).all())
            manifest_target_keys = manifest.get("targets") or {}
            selected_target = target_key or (app.default_target_key if app.default_target_key in manifest_target_keys else next(iter(manifest_target_keys), None))
            if target_key and target_key not in manifest_target_keys:
                raise _error(404, "Target not found")
            return {
                "app": app.to_dict(),
                "permission": permission,
                "target_key": selected_target,
                "targets": targets,
                "releases": [release.to_dict() for release in releases],
                "binding_mode": selected_bundle[0].binding_mode if selected_bundle else "development",
                "selected_release": selected_release.to_dict() if selected_release else None,
                "manifest": manifest,
                "manifest_hash": manifest_hash,
                "readme": readme,
            }
        except AppManifestError as exc:
            raise _error(422, str(exc)) from exc
        finally:
            await session.close()

    async def _embedded_target(
        session,
        app_id: str,
        target_key: str,
        user: dict[str, Any],
        project_id: UUID | None = None,
        *,
        require_static: bool = True,
    ) -> tuple[App, AppTarget]:
        app, _ = await require_app(session, app_id, user, project_id=project_id)
        if project_id is not None:
            binding = await session.scalar(select(ProjectApp).where(
                and_(ProjectApp.project_id == project_id, ProjectApp.app_id == app.id)
            ).limit(1))
            if binding is None or not binding.enabled:
                raise _error(403, "このProjectではAppが有効化されていません")
        target = await session.scalar(select(AppTarget).where(
            and_(AppTarget.app_id == app.id, AppTarget.target_key == target_key)
        ).limit(1))
        if not target:
            raise _error(404, "Target not found")
        if require_static and (target.surface != "embedded_web" or target.runtime != "static_web"):
            raise _error(400, "embedded static web Target ではありません")
        return app, target

    @router.get("/api/apps/{app_id}/targets/{target_key}/embed")
    async def embedded_app_entrypoint(
        app_id: str,
        target_key: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = await project_required(session, project_id, user) if project_id else None
            app, target = await _embedded_target(session, app_id, target_key, user, project_uuid, require_static=False)
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            # 解決済みの source bundle を渡して、Release ZIP の再ハッシュを避ける。
            runtime_bundle = (
                await installed_runtime_bundle(
                    session, app, project_uuid, target, source_bundle=selected_bundle
                )
                if selected_bundle
                else None
            )
            target_snapshot = target.manifest_snapshot if isinstance(target.manifest_snapshot, dict) else {}
            bridge_release_id: str | None = None
            if selected_bundle:
                release_manifest, _release_manifest_text, _release_readme = release_manifest_and_readme(selected_bundle[2])
                release_target = (release_manifest.get("targets") or {}).get(target_key)
                if not isinstance(release_target, dict) or release_target.get("surface") != "embedded_web" or release_target.get("runtime") != "static_web":
                    raise _error(400, "固定ReleaseのTargetはembedded static webではありません")
                entrypoint = str(release_target.get("entrypoint") or "")
                target_snapshot = release_target
                bridge_release_id = str(selected_bundle[1].id)
            else:
                if target.surface != "embedded_web" or target.runtime != "static_web":
                    raise _error(400, "embedded static web Target ではありません")
                entrypoint = target.entrypoint
            if is_embedded_app_path(entrypoint, allow_build_output=True):
                raise _error(403, "private/runtime entrypoint はembedded Appから配信できません")
            if selected_bundle:
                try:
                    if runtime_bundle is None:
                        raise _error(409, "固定ReleaseのRuntime Bundleがありません")
                    bundle_entrypoint = resolve_runtime_bundle_entrypoint(runtime_bundle[3], entrypoint)
                    content = release_file_bytes(runtime_bundle[3], bundle_entrypoint).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise _error(415, "embedded entrypointはUTF-8 HTMLである必要があります") from exc
            else:
                try:
                    path = resolve_app_file(app.id, entrypoint, workspace_root=workspace_root)
                except AppStorageError as exc:
                    raise _error(400, str(exc)) from exc
                if not path.exists() or not path.is_file():
                    raise _error(404, "Target entrypoint がありません")
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise _error(415, "embedded entrypointはUTF-8 HTMLである必要があります") from exc
            declared = {
                str(item) for item in target_snapshot.get("capabilities", []) if isinstance(item, str)
            }
            granted: set[str] = set()
            if project_uuid is not None:
                binding = await session.scalar(select(ProjectApp).where(
                    and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id)
                ).limit(1))
                targets = list((await session.scalars(select(AppTarget).where(AppTarget.app_id == app.id))).all())
                try:
                    granted = validate_capability_grants(binding.capability_grants_json or {}, targets) if binding else set()
                except ValueError as exc:
                    raise _error(409, str(exc)) from exc
            bridge_token = issue_app_bridge_token(
                app_id=str(app.id),
                target_id=str(target.id),
                user_id=str(_user_id(user)),
                project_id=str(project_uuid) if project_uuid else None,
                capabilities=sorted(declared.intersection(granted)),
                release_id=bridge_release_id,
            )
            embed_base = "./embed/"
            if project_uuid is not None:
                # Query strings on a <base> URL are not carried to relative
                # asset requests by browsers.  Put the validated Project
                # scope in the asset path instead, so every CSS/JS/image
                # request reaches the same Project binding check.
                embed_base = f"./embed/{_EMBED_PROJECT_SCOPE_SEGMENT}/{project_uuid}/"
            # An App may already contain a <base> element.  Keeping two base
            # elements makes browsers resolve assets against the first one,
            # bypassing the validated Project-scoped route.
            content = re.sub(r"<base\b[^>]*>", "", content, flags=re.IGNORECASE)
            bridge_meta = (
                f'<base href="{html.escape(embed_base, quote=True)}">'
                f'<meta name="aoitalk-bridge-token" content="{html.escape(bridge_token, quote=True)}">'
                f'<meta name="aoitalk-bridge-target" content="{html.escape(str(target.id), quote=True)}">'
            )
            if "</head>" in content.lower():
                insertion_at = content.lower().index("</head>")
                content = content[:insertion_at] + bridge_meta + content[insertion_at:]
            else:
                content = bridge_meta + content
            return HTMLResponse(
                content=content,
                headers={
                    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/targets/{target_key}/embed/{asset_path:path}")
    async def embedded_app_asset(
        app_id: str,
        target_key: str,
        asset_path: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            path_parts = PurePosixPath(asset_path.replace("\\", "/")).parts
            scoped_project_uuid: UUID | None = None
            if path_parts and path_parts[0] == _EMBED_PROJECT_SCOPE_SEGMENT:
                if len(path_parts) < 3:
                    raise _error(404, "Asset not found")
                scoped_project_uuid = _uuid(path_parts[1], "project_id")
                if project_id and _uuid(project_id, "project_id") != scoped_project_uuid:
                    raise _error(403, "埋め込みAssetのProjectスコープが一致しません")
                project_uuid = await project_required(session, str(scoped_project_uuid), user)
                asset_path = "/".join(path_parts[2:])
            else:
                project_uuid = await project_required(session, project_id, user) if project_id else None
            app, target = await _embedded_target(session, app_id, target_key, user, project_uuid, require_static=False)
            if is_embedded_app_path(asset_path, allow_build_output=True):
                raise _error(403, "private/runtime asset はembedded Appから配信できません")
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            if selected_bundle:
                runtime_bundle = await installed_runtime_bundle(
                    session, app, project_uuid, target, source_bundle=selected_bundle
                )
                if runtime_bundle is None:
                    raise _error(409, "固定ReleaseのRuntime Bundleがありません")
                release_manifest, _release_manifest_text, _release_readme = release_manifest_and_readme(selected_bundle[2])
                release_target = (release_manifest.get("targets") or {}).get(target_key)
                if not isinstance(release_target, dict) or release_target.get("surface") != "embedded_web" or release_target.get("runtime") != "static_web":
                    raise _error(400, "固定ReleaseのTargetはembedded static webではありません")
                entrypoint = resolve_runtime_bundle_entrypoint(runtime_bundle[3], str(release_target.get("entrypoint") or ""))
                entrypoint_parent = PurePosixPath(entrypoint).parent
                relative_asset = str(entrypoint_parent / asset_path) if str(entrypoint_parent) != "." else asset_path
                if is_embedded_app_path(relative_asset, allow_build_output=True):
                    raise _error(403, "private/runtime asset はembedded Appから配信できません")
                content = release_file_bytes(runtime_bundle[3], relative_asset)
                return Response(
                    content=content,
                    media_type=mimetypes.guess_type(Path(asset_path).name)[0] or "application/octet-stream",
                    headers={
                        "Content-Security-Policy": "default-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "no-store",
                    },
                )
            if target.surface != "embedded_web" or target.runtime != "static_web":
                raise _error(400, "embedded static web Target ではありません")
            try:
                entrypoint_parent = PurePosixPath(target.entrypoint).parent
                relative_asset = str(entrypoint_parent / asset_path) if str(entrypoint_parent) != "." else asset_path
                if is_embedded_app_path(relative_asset, allow_build_output=True):
                    raise _error(403, "private/runtime asset はembedded Appから配信できません")
                path = resolve_app_file(app.id, relative_asset, workspace_root=workspace_root)
            except AppStorageError as exc:
                raise _error(400, str(exc)) from exc
            if not path.exists() or not path.is_file():
                raise _error(404, "Asset not found")
            return FileResponse(
                path,
                headers={
                    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/targets/{target_key}/bridge-token")
    async def create_bridge_token(
        app_id: str,
        target_key: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        """Issue a short-lived, target-scoped token for the App Bridge."""
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = await project_required(session, project_id, user) if project_id else None
            app, target = await _embedded_target(
                session,
                app_id,
                target_key,
                user,
                project_uuid,
                require_static=False,
            )
            selected_bundle = await installed_source_bundle(session, app, project_uuid)
            snapshot = target.manifest_snapshot if isinstance(target.manifest_snapshot, dict) else {}
            bridge_release_id: str | None = None
            if selected_bundle:
                release_manifest, _release_manifest_text, _release_readme = release_manifest_and_readme(selected_bundle[2])
                release_target = (release_manifest.get("targets") or {}).get(target_key)
                if not isinstance(release_target, dict) or release_target.get("surface") != "embedded_web" or release_target.get("runtime") != "static_web":
                    raise _error(400, "固定ReleaseのTargetはembedded static webではありません")
                snapshot = release_target
                bridge_release_id = str(selected_bundle[1].id)
            declared = {
                str(item)
                for item in snapshot.get("capabilities", [])
                if isinstance(item, str)
            }
            granted: set[str] = set()
            if project_uuid:
                binding = await session.scalar(select(ProjectApp).where(
                    and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id)
                ).limit(1))
                if not binding or not binding.enabled:
                    raise _error(403, "ProjectにAppが導入されていません")
                targets = list((await session.scalars(select(AppTarget).where(AppTarget.app_id == app.id))).all())
                try:
                    granted = validate_capability_grants(binding.capability_grants_json or {}, targets)
                except ValueError as exc:
                    raise _error(409, str(exc)) from exc
            token = issue_app_bridge_token(
                app_id=str(app.id),
                target_id=str(target.id),
                user_id=str(_user_id(user)),
                project_id=str(project_uuid) if project_uuid else None,
                capabilities=sorted(declared.intersection(granted)),
                release_id=bridge_release_id,
            )
            return {
                "token": token,
                "expires_in": DEFAULT_TTL_SECONDS,
                "app_id": str(app.id),
                "target_id": str(target.id),
                "release_id": bridge_release_id,
                "capabilities": sorted(declared.intersection(granted)),
            }
        finally:
            await session.close()

    @router.post("/api/apps/{app_id}/targets/{target_key}/bridge")
    async def invoke_app_bridge(
        app_id: str,
        target_key: str,
        payload: AppBridgeInvokePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Handle the small, read-only initial App Bridge surface."""
        user = await current_user(request)
        try:
            token = verify_app_bridge_token(payload.token)
        except AppBridgeTokenError as exc:
            raise _error(401, str(exc)) from exc
        if str(token.get("app_id")) != str(app_id):
            raise _error(403, "App bridge token のApp scopeが一致しません")
        if str(token.get("user_id")) != str(_user_id(user)):
            raise _error(403, "App bridge token のuser scopeが一致しません")
        capabilities = {
            str(item) for item in token.get("capabilities", []) if isinstance(item, str)
        }
        if payload.method not in capabilities:
            raise _error(403, "Project Appで許可されていないCapabilityです")

        session = await get_db_manager().get_session()
        try:
            target = await session.scalar(select(AppTarget).where(
                and_(AppTarget.app_id == _uuid(app_id, "app_id"), AppTarget.target_key == target_key)
            ).limit(1))
            if not target or str(token.get("target_id")) != str(target.id):
                raise _error(403, "App bridge token のTarget scopeが一致しません")
            app = await app_or_404(session, app_id)
            project_id = token.get("project_id")
            release_readme: str | None = None
            if project_id:
                project_uuid = _uuid(str(project_id), "project_id")
                binding = await session.scalar(select(ProjectApp).where(
                    and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == target.app_id)
                ).limit(1))
                if binding is None or not binding.enabled:
                    raise _error(403, "このProjectではAppが有効化されていません")
                try:
                    await service.require_permission(
                        session,
                        app,
                        user_id=_user_id(user),
                        required="viewer",
                        user_role=user.get("role"),
                        project_id=project_uuid,
                    )
                except AppAccessError as exc:
                    raise _error(403, "Appを閲覧できません") from exc
            token_release_id = token.get("release_id")
            if token_release_id:
                release_uuid = _uuid(str(token_release_id), "release_id")
                release = await session.scalar(
                    select(AppRelease)
                    .options(selectinload(AppRelease.artifacts))
                    .where(
                        AppRelease.id == release_uuid,
                        AppRelease.app_id == app.id,
                        AppRelease.status == "published",
                    )
                    .limit(1)
                )
                if release is None:
                    raise _error(403, "App bridge token のRelease scopeが無効です")
                if project_id:
                    if binding.binding_mode != "installed" or binding.installed_release_id != release.id:
                        raise _error(403, "App bridge token の固定保存版scopeが一致しません")
                source_artifact = next((item for item in (release.artifacts or []) if item.artifact_type == "source_bundle"), None)
                if source_artifact is None:
                    raise _error(404, "固定ReleaseのSource Bundleがありません")
                try:
                    archive_path = resolve_app_artifact_file(
                        app.id,
                        release.id,
                        Path(str(source_artifact.filename)).name,
                        workspace_root=workspace_root,
                    )
                    verify_file_integrity(
                        archive_path,
                        expected_sha256=source_artifact.sha256,
                        expected_size_bytes=source_artifact.size_bytes,
                    )
                    _manifest, _manifest_text, release_readme = release_manifest_and_readme(archive_path)
                except (AppStorageError, OSError, zipfile.BadZipFile) as exc:
                    raise _error(422, "固定ReleaseのREADMEを読み込めません") from exc
            if payload.method == "docs.read":
                if release_readme is not None:
                    return {
                        "method": payload.method,
                        "data": {"app_id": str(app.id), "readme": release_readme},
                    }
                readme_path = resolve_app_file(app.id, "README.md", workspace_root=workspace_root)
                return {
                    "method": payload.method,
                    "data": {
                        "app_id": str(app.id),
                        "readme": readme_path.read_text(encoding="utf-8") if readme_path.exists() else "",
                    },
                }
            if payload.method == "tasks.read":
                if not project_id:
                    raise _error(400, "tasks.read には Project scope が必要です")
                project_uuid = _uuid(str(project_id), "project_id")
                links = list((await session.scalars(
                    select(TaskAppLink)
                    .options(selectinload(TaskAppLink.task))
                    .join(Task, Task.id == TaskAppLink.task_id)
                    .where(and_(
                        TaskAppLink.app_id == target.app_id,
                        Task.project_id == project_uuid,
                        Task.archived_at.is_(None),
                        Task.deleted_at.is_(None),
                    ))
                )).all())
                return {
                    "method": payload.method,
                    "data": {"tasks": [link.task.to_dict() for link in links if link.task is not None]},
                }
            raise _error(400, "初期App Bridgeではread-onlyのdocs.read/tasks.readだけをサポートします")
        finally:
            await session.close()

    @router.get("/api/projects/{project_id}/apps")
    async def list_project_apps(
        project_id: str,
        request: Request,
        with_git: bool = Query(
            False,
            description="App Git の作業状態も返す。App 1件につき git プロセスを起動するため既定は無効。",
        ),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = await project_required(session, project_id, user)
            # App を join 済みで一度に読み、binding ごとの app_or_404 を無くす。
            binding_rows = list((await session.execute(
                select(ProjectApp, App)
                .join(App, App.id == ProjectApp.app_id)
                .where(ProjectApp.project_id == project_uuid, ProjectApp.enabled.is_(True), App.archived_at.is_(None))
            )).all())
            visible: list[tuple[ProjectApp, App, str]] = []
            for binding, app in binding_rows:
                permission = await service.permission_for_app(
                    session,
                    app,
                    user_id=_user_id(user),
                    user_role=user.get("role"),
                    project_id=project_uuid,
                )
                if not permission:
                    continue
                visible.append((binding, app, permission))

            app_ids = [app.id for _binding, app, _permission in visible]
            # targets / 最新Release / 最新Job / 最新AgentRun / 未完了タスク数を
            # App 件数に依存しない固定回数のクエリでまとめて取得する。
            targets_by_app = await targets_for_apps(session, app_ids)
            latest_release_by_app = await latest_row_by_app(
                session,
                AppRelease,
                app_ids,
                order_column=AppRelease.created_at,
                options=(selectinload(AppRelease.artifacts),),
            )
            latest_job_by_app = await latest_row_by_app(
                session,
                AppJob,
                app_ids,
                order_column=AppJob.started_at,
                extra_conditions=(AppJob.project_id == project_uuid,),
            )
            latest_agent_run_by_app = await latest_row_by_app(
                session,
                AgentRun,
                app_ids,
                order_column=AgentRun.created_at,
                extra_conditions=(AgentRun.project_id == project_uuid,),
            )
            incomplete_task_counts: dict[UUID, int] = {}
            if app_ids:
                count_rows = (await session.execute(
                    select(TaskAppLink.app_id, func.count(TaskAppLink.id))
                    .join(Task, Task.id == TaskAppLink.task_id)
                    .where(
                        TaskAppLink.app_id.in_(app_ids),
                        Task.project_id == project_uuid,
                        Task.archived_at.is_(None),
                        Task.deleted_at.is_(None),
                        Task.status.not_in({"completed", "closed", "done", "cancelled"}),
                    )
                    .group_by(TaskAppLink.app_id)
                )).all()
                incomplete_task_counts = {row[0]: int(row[1] or 0) for row in count_rows}

            git = AppGitService(workspace_root=workspace_root) if with_git else None
            result = []
            for binding, app, permission in visible:
                item = binding.to_dict()
                item["app"] = app.to_dict()
                item["permission"] = permission
                current_targets = targets_by_app.get(app.id, [])
                # 取得済みの binding を渡して ProjectApp の再クエリを避ける。
                selected_bundle = await installed_source_bundle(
                    session, app, project_uuid, binding=binding
                )
                if selected_bundle:
                    release_manifest, _release_manifest_text, _release_readme = release_manifest_and_readme(selected_bundle[2])
                    item["targets"] = release_target_payloads(release_manifest, current_targets)
                    latest_release = selected_bundle[1]
                    release = selected_bundle[1]
                    # 固定Releaseの状態は git プロセスを起動せずに決まる。
                    item["git_status"] = {
                        "available": True,
                        "fixed_release": True,
                        "clean": True,
                        "dirty": False,
                        "branch": f"release/{release.version}",
                        "revision": release.git_revision,
                        "files": [],
                    }
                else:
                    item["targets"] = [target.to_dict() for target in current_targets]
                    latest_release = latest_release_by_app.get(app.id)
                    # AppGitService.status() は initialize() 経由で App 1件ごとに
                    # git プロセスを複数起動する。既定では走らせず、
                    # ?with_git=1 を指定したときだけ展開する。
                    if git is not None:
                        try:
                            item["git_status"] = git.status(app.id)
                        except AppGitError:
                            item["git_status"] = {"available": False, "clean": None, "revision": None}
                latest_job = latest_job_by_app.get(app.id)
                latest_agent_run = latest_agent_run_by_app.get(app.id)
                item["latest_release"] = latest_release.to_dict() if latest_release else None
                item["latest_job"] = latest_job.to_dict() if latest_job else None
                item["latest_agent_run"] = latest_agent_run.to_dict() if latest_agent_run else None
                item["incomplete_task_count"] = incomplete_task_counts.get(app.id, 0)
                result.append(item)
            return {"project_id": str(project_uuid), "apps": result}
        finally:
            await session.close()

    @router.post("/api/projects/{project_id}/apps")
    async def link_project_app(project_id: str, payload: ProjectAppPayload, request: Request, _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        operation_lock = None
        try:
            project_uuid = await project_required(session, project_id, user)
            operation_lock = project_operation_lock(project_uuid, workspace_root=workspace_root)
            await operation_lock.acquire()
            await project_write_required(session, project_uuid, user)
            # Serialize Project deletion against binding creation/update/removal.
            locked_project_id = await session.scalar(select(Project.id).where(
                Project.id == project_uuid,
                Project.deleted_at.is_(None),
            ).with_for_update())
            if locked_project_id is None:
                raise _error(404, "Project not found")
            app, _ = await require_app(
                session,
                payload.app_id,
                user,
                required="runner",
                project_id=project_uuid,
                require_enabled_binding=False,
            )
            await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
            # Re-evaluate App grants after both lifecycle locks are held.  The
            # first check above only establishes the request scope; a grant
            # may have been revoked while waiting for the Project lock.
            app, _ = await require_app(
                session,
                payload.app_id,
                user,
                required="runner",
                project_id=project_uuid,
                require_enabled_binding=False,
            )
            await ensure_no_active_jobs(session, app.id, project_id=project_uuid)
            if payload.binding_mode not in {"development", "installed"}:
                raise _error(400, "binding_mode が不正です")
            targets = list((await session.scalars(select(AppTarget).where(AppTarget.app_id == app.id))).all())
            try:
                validate_capability_grants(payload.capability_grants_json, targets)
            except ValueError as exc:
                raise _error(400, str(exc)) from exc
            release_uuid = _uuid(payload.installed_release_id, "installed_release_id") if payload.installed_release_id else None
            if payload.binding_mode == "installed":
                if not release_uuid:
                    raise _error(400, "installed binding には Release が必要です")
                release = await session.scalar(select(AppRelease).where(and_(AppRelease.id == release_uuid, AppRelease.app_id == app.id, AppRelease.status == "published")).limit(1))
                if not release:
                    raise _error(404, "published Release not found")
            binding = await session.scalar(select(ProjectApp).where(and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id)).limit(1))
            if binding is None:
                binding = ProjectApp(project_id=project_uuid, app_id=app.id, created_by=_user_id(user))
                session.add(binding)
            binding.binding_mode = payload.binding_mode
            binding.installed_release_id = release_uuid
            binding.enabled = payload.enabled
            binding.pinned = payload.pinned
            binding.display_alias = payload.display_alias
            binding.config_json = payload.config_json
            binding.capability_grants_json = payload.capability_grants_json
            instance_path = get_app_instance_path(project_uuid, app.id, workspace_root=workspace_root)
            instance_existed = instance_path.exists()
            try:
                ensure_app_instance(project_uuid, app.id, workspace_root=workspace_root)
                # Keep instance creation inside the Project/App lock and
                # compensate a filesystem-only instance if the DB commit fails.
                await session.commit()
            except Exception:
                await session.rollback()
                if not instance_existed:
                    remove_app_instance(project_uuid, app.id, workspace_root=workspace_root)
                raise
            return {"success": True, "binding": binding.to_dict()}
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    @router.patch("/api/projects/{project_id}/apps/{app_id}")
    async def patch_project_app(project_id: str, app_id: str, payload: ProjectAppPatchPayload, request: Request, _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        operation_lock = None
        try:
            project_uuid = await project_required(session, project_id, user)
            operation_lock = project_operation_lock(project_uuid, workspace_root=workspace_root)
            await operation_lock.acquire()
            await project_write_required(session, project_uuid, user)
            locked_project_id = await session.scalar(select(Project.id).where(
                Project.id == project_uuid,
                Project.deleted_at.is_(None),
            ).with_for_update())
            if locked_project_id is None:
                raise _error(404, "Project not found")
            app, _ = await require_app(
                session,
                app_id,
                user,
                required="runner",
                project_id=project_uuid,
                require_enabled_binding=False,
            )
            await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
            app, _ = await require_app(
                session,
                app_id,
                user,
                required="runner",
                project_id=project_uuid,
                require_enabled_binding=False,
            )
            await ensure_no_active_jobs(session, app.id, project_id=project_uuid)
            binding = await session.scalar(select(ProjectApp).where(and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id)).limit(1))
            if not binding:
                raise _error(404, "Project App binding not found")
            if payload.capability_grants_json is not None:
                targets = list((await session.scalars(select(AppTarget).where(AppTarget.app_id == app.id))).all())
                try:
                    validate_capability_grants(payload.capability_grants_json, targets)
                except ValueError as exc:
                    raise _error(400, str(exc)) from exc
            # 「未指定」と「明示的な null」を model_fields_set で区別する。
            # display_alias は一度設定すると API から解除できなかった。
            provided = payload.model_fields_set
            nullable_fields = {"display_alias", "installed_release_id"}
            for field in ("binding_mode", "installed_release_id", "enabled", "pinned", "display_alias", "config_json", "capability_grants_json"):
                if field not in provided:
                    continue
                value = getattr(payload, field)
                if value is None and field not in nullable_fields:
                    continue
                if value is not None:
                    if field == "binding_mode" and value not in {"development", "installed"}:
                        raise _error(400, "binding_mode が不正です")
                    if field == "installed_release_id":
                        value = _uuid(value, field)
                setattr(binding, field, value)
            if binding.binding_mode == "installed":
                if not binding.installed_release_id:
                    raise _error(400, "installed binding には Release が必要です")
                release = await session.scalar(select(AppRelease).where(
                    and_(
                        AppRelease.id == binding.installed_release_id,
                        AppRelease.app_id == app.id,
                        AppRelease.status == "published",
                    )
                ).limit(1))
                if not release:
                    raise _error(404, "published Release not found")
            else:
                binding.installed_release_id = None
            await session.commit()
            return {"success": True, "binding": binding.to_dict()}
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    @router.delete("/api/projects/{project_id}/apps/{app_id}")
    async def unlink_project_app(project_id: str, app_id: str, request: Request, _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        operation_lock = None
        try:
            project_uuid = await project_required(session, project_id, user)
            operation_lock = project_operation_lock(project_uuid, workspace_root=workspace_root)
            await operation_lock.acquire()
            await project_write_required(session, project_uuid, user)
            locked_project_id = await session.scalar(select(Project.id).where(
                Project.id == project_uuid,
                Project.deleted_at.is_(None),
            ).with_for_update())
            if locked_project_id is None:
                raise _error(404, "Project not found")
            app, _ = await require_app(
                session,
                app_id,
                user,
                required="runner",
                project_id=project_uuid,
                require_enabled_binding=False,
            )
            await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
            app, _ = await require_app(
                session,
                app_id,
                user,
                required="runner",
                project_id=project_uuid,
                require_enabled_binding=False,
            )
            await ensure_no_active_jobs(session, app.id, project_id=project_uuid)
            await session.execute(delete(ProjectApp).where(and_(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id)))
            await session.commit()
            # The Project lifecycle lock is held through cleanup, so a relink
            # cannot recreate the instance between commit and removal.
            #
            # binding は既に削除済みで、instance は次の link_project_app が
            # ensure_app_instance で作り直す。ここで失敗しても API を 500 に
            # せず、残骸を記録して成功を返す（DB が正、instance は派生）。
            instance_removed = True
            try:
                remove_app_instance(project_uuid, app.id, workspace_root=workspace_root)
            except (AppStorageError, OSError):
                instance_removed = False
                logger.exception(
                    "App instance cleanup failed: project_id=%s app_id=%s",
                    project_uuid,
                    app.id,
                )
            return {
                "success": True,
                "app_id": str(app.id),
                "project_id": str(project_uuid),
                "instance_removed": instance_removed,
            }
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    @router.get("/api/tasks/{task_id}/apps")
    async def list_task_apps(task_id: str, request: Request, _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await session.scalar(select(Task).where(Task.id == _uuid(task_id, "task_id")).limit(1))
            if not task:
                raise _error(404, "Task not found")
            if task.archived_at is not None or task.deleted_at is not None:
                raise _error(404, "Task not found")
            await project_required(session, str(task.project_id), user)
            links = list((await session.scalars(
                select(TaskAppLink)
                .options(selectinload(TaskAppLink.app), selectinload(TaskAppLink.target))
                .join(App, App.id == TaskAppLink.app_id)
                .where(
                    TaskAppLink.task_id == task.id,
                    App.archived_at.is_(None),
                )
            )).all())
            visible_links = []
            for link in links:
                if link.app is None:
                    continue
                try:
                    await service.require_permission(
                        session,
                        link.app,
                        user_id=_user_id(user),
                        user_role=user.get("role"),
                        required="viewer",
                        project_id=task.project_id,
                    )
                except AppAccessError:
                    continue
                visible_links.append(task_app_payload(link))
            return {"task_id": str(task.id), "apps": visible_links}
        finally:
            await session.close()

    @router.get("/api/apps/{app_id}/tasks")
    async def list_app_tasks(
        app_id: str,
        request: Request,
        project_id: str = Query(...),
        relation_type: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            project_uuid = await project_required(session, project_id, user)
            app, _ = await require_app(session, app_id, user, project_id=project_uuid)
            conditions = [
                TaskAppLink.app_id == app.id,
                Task.project_id == project_uuid,
                Task.archived_at.is_(None),
                Task.deleted_at.is_(None),
            ]
            if relation_type:
                if relation_type not in {"develops", "fixes", "tests", "releases", "uses", "related"}:
                    raise _error(400, "relation_type が不正です")
                conditions.append(TaskAppLink.relation_type == relation_type)
            links = list((await session.scalars(
                select(TaskAppLink)
                .join(Task, Task.id == TaskAppLink.task_id)
                .options(selectinload(TaskAppLink.app), selectinload(TaskAppLink.target), selectinload(TaskAppLink.task))
                .where(and_(*conditions))
                .order_by(TaskAppLink.created_at.desc())
                .limit(200)
            )).all())
            items = []
            for link in links:
                item = task_app_payload(link)
                if link.task is not None:
                    item["task"] = link.task.to_dict()
                items.append(item)
            return {"app_id": str(app.id), "project_id": str(project_uuid), "tasks": items}
        finally:
            await session.close()

    @router.post("/api/tasks/{task_id}/apps")
    async def link_task_app(task_id: str, payload: TaskAppLinkPayload, request: Request, _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await session.scalar(select(Task).where(Task.id == _uuid(task_id, "task_id")).limit(1))
            if not task:
                raise _error(404, "Task not found")
            if task.archived_at is not None or task.deleted_at is not None:
                raise _error(404, "Task not found")
            project_uuid = await project_required(session, str(task.project_id), user)
            await project_write_required(session, project_uuid, user)
            app, _ = await require_app(session, payload.app_id, user, project_id=project_uuid)
            if payload.relation_type not in {"develops", "fixes", "tests", "releases", "uses", "related"}:
                raise _error(400, "relation_type が不正です")
            target_uuid = _uuid(payload.target_id, "target_id") if payload.target_id else None
            if target_uuid:
                target = await session.scalar(select(AppTarget).where(and_(AppTarget.id == target_uuid, AppTarget.app_id == app.id)).limit(1))
                if not target:
                    raise _error(404, "Target not found")
                binding = await session.scalar(select(ProjectApp).where(and_(
                    ProjectApp.project_id == project_uuid,
                    ProjectApp.app_id == app.id,
                    ProjectApp.enabled.is_(True),
                )).limit(1))
                if binding is not None and binding.binding_mode == "installed":
                    if binding.installed_release_id is None:
                        raise _error(409, "固定保存版が選択されていません")
                    target_artifact = await session.scalar(select(AppArtifact.id).where(and_(
                        AppArtifact.release_id == binding.installed_release_id,
                        AppArtifact.target_id == target.id,
                        AppArtifact.artifact_type == "runtime_bundle",
                    )).limit(1))
                    if target_artifact is None:
                        raise _error(409, "Projectが固定しているReleaseにこのTargetの成果物がありません")
            link = await session.scalar(select(TaskAppLink).where(and_(TaskAppLink.task_id == task.id, TaskAppLink.app_id == app.id, TaskAppLink.relation_type == payload.relation_type, TaskAppLink.target_id == target_uuid)).limit(1))
            if link is None:
                link = TaskAppLink(task_id=task.id, app_id=app.id, target_id=target_uuid, relation_type=payload.relation_type, created_by=_user_id(user))
                session.add(link)
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent identical link is a successful idempotent
                # outcome, not a 500.  The partial no-target index protects
                # the NULL target form; the query handles the winner.
                await session.rollback()
                link = await session.scalar(select(TaskAppLink).where(and_(
                    TaskAppLink.task_id == task.id,
                    TaskAppLink.app_id == app.id,
                    TaskAppLink.relation_type == payload.relation_type,
                    TaskAppLink.target_id == target_uuid,
                )).limit(1))
                if link is None:
                    raise
            await session.refresh(link, attribute_names=["app", "target"])
            return {"success": True, "link": task_app_payload(link)}
        finally:
            await session.close()

    @router.delete("/api/tasks/{task_id}/apps/{app_id}")
    async def unlink_task_app(task_id: str, app_id: str, request: Request, target_id: str | None = None, relation_type: str | None = None, _: None = Depends(require_auth_dependency)):
        user = await current_user(request)
        session = await get_db_manager().get_session()
        try:
            task = await session.scalar(select(Task).where(Task.id == _uuid(task_id, "task_id")).limit(1))
            if not task:
                raise _error(404, "Task not found")
            if task.archived_at is not None or task.deleted_at is not None:
                raise _error(404, "Task not found")
            project_uuid = await project_required(session, str(task.project_id), user)
            await project_write_required(session, project_uuid, user)
            # Removing a stale Task/App reference is allowed even after the
            # Project App binding was disabled or removed.  The Project write
            # check above still protects this cleanup path.
            app, _ = await require_app(
                session,
                app_id,
                user,
                project_id=task.project_id,
                require_enabled_binding=False,
            )
            conditions = [TaskAppLink.task_id == task.id, TaskAppLink.app_id == app.id]
            if target_id:
                conditions.append(TaskAppLink.target_id == _uuid(target_id, "target_id"))
            if relation_type:
                conditions.append(TaskAppLink.relation_type == relation_type)
            await session.execute(delete(TaskAppLink).where(and_(*conditions)))
            await session.commit()
            return {"success": True}
        finally:
            await session.close()

    return router


__all__ = ["create_apps_router"]
