"""Runtime tools for the persistent Apps feature.

The runtime receives an already server-resolved App context.  File and Git
operations therefore resolve only ``workspaces/_apps/app_<uuid>`` and reject a
different App ID or an unsafe relative path.  DB-facing operations repeat the
permission check before touching an App row; display names are never used as
authorization input.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import re
import json
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from uuid import UUID

from sqlalchemy import and_, delete, select
from sqlalchemy.exc import IntegrityError

from ..memory.database import get_database_manager
from ..memory.models import (
    App,
    AppArtifact,
    AppJob,
    AppRelease,
    AppTarget,
    Project,
    ProjectApp,
    Task,
    TaskAppLink,
)
from ..services.app_git_service import AppGitError, AppGitService
from ..services.app_business_analysis_service import (
    analyze_app_workspace,
    collect_source_evidence,
    merge_analysis_into_readme,
    write_analysis_to_manifest,
)
from ..services.app_job_execution_policy import (
    ServerJobExecutionDenied,
    assert_user_may_start_server_job,
)
from ..services.app_job_service import execute_app_job, stop_running_job
from ..services.app_manifest_service import (
    AppManifestError,
    load_app_manifest,
    parse_manifest_text,
    sync_manifest_targets,
    sync_manifest_targets_unlocked,
    validate_manifest_workspace,
)
from ..services.app_operation_lock import app_operation_lock, project_operation_lock
from ..services.app_release_service import (
    AppReleaseError,
    create_app_release as create_app_release_service,
)
from ..services.app_service import AppAccessError, AppService
from ..services.app_storage import (
    AppStorageError,
    AppWorkspaceJournal,
    ensure_app_artifact,
    ensure_app_instance,
    ensure_app_workspace,
    get_app_instance_path,
    get_app_workspace_path,
    list_app_files,
    is_private_app_path,
    is_protected_app_path,
    is_text_app_path,
    iter_app_source_files,
    normalize_app_bundle_member,
    normalize_app_relative_path,
    remove_app_instance,
    remove_app_source_and_artifacts,
    resolve_app_artifact_file,
    resolve_app_file,
    resolve_workspace_file,
    sha256_file,
    stage_app_source_bundle,
    swap_app_workspace_files,
    verify_file_integrity,
)
from ..services.project_context import (
    get_runtime_project_context,
    runtime_project_context_is_bound,
)
from ..services.turn_context import get_turn_context
from .core import ToolDefinition, ToolParam


logger = logging.getLogger(__name__)


class _RuntimeAppContext(dict):
    """Resolve the App/Project principal at invocation time.

    Provider registries can outlive a single turn.  A plain captured dict would
    therefore keep authorizing the App selected when the registry was built.
    Prefer the request-local ContextVar for every call; an explicitly bound
    turn with no runtime context fails closed instead of falling back to a
    stale previous-session principal.  Direct unit/integration calls made
    outside any request binding retain the constructor context for compatibility.
    """

    def __init__(self, initial: dict[str, Any] | None) -> None:
        self._initial_context = dict(initial or {})
        super().__init__(self._initial_context)

    def _current(self) -> dict[str, Any]:
        current = get_runtime_project_context()
        if isinstance(current, dict):
            return current
        if runtime_project_context_is_bound():
            return {}
        # A bound TurnContext with no runtime project is an explicit
        # project/app-off turn (or an unresolved authorization scope).  Do not
        # fall back to the constructor object in that case.  Only legacy
        # direct invocations outside any turn may use the initial context.
        turn = get_turn_context()
        if any(
            getattr(turn, field, None) is not None
            for field in ("user_id", "session_id", "project_id", "message_id", "client_message_id")
        ) or getattr(turn, "include_project_context", None) is not None:
            return {}
        return self._initial_context

    def get(self, key: Any, default: Any = None) -> Any:
        return self._current().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        return self._current()[key]

    def __contains__(self, key: object) -> bool:
        return key in self._current()


def _uuid(value: Any, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{label} が不正です") from exc


def _ctx_app_id(context: dict[str, Any]) -> str | None:
    app = context.get("app_context")
    if isinstance(app, dict) and app.get("id"):
        return str(app["id"])
    value = context.get("app_id")
    return str(value) if value else None


def _ctx_project_id(context: dict[str, Any]) -> str | None:
    value = context.get("id")
    return str(value) if value else None


def _ctx_user_id(context: dict[str, Any]) -> UUID:
    value = context.get("user_id")
    if not value:
        raise ValueError("App contextに認証済みuser_idがありません")
    return _uuid(value, "user_id")


def _ctx_user_role(context: dict[str, Any]) -> str | None:
    value = context.get("user_role") or context.get("role")
    return str(value) if value else None


def _active_app_id(context: dict[str, Any], requested: str | None = None) -> UUID:
    active = _ctx_app_id(context)
    if not active:
        raise ValueError("App contextを選択してください")
    if requested and str(requested) != active:
        raise PermissionError("App context外のAppを操作できません")
    return _uuid(active, "app_id")


def _safe_file(app_id: UUID, path: str, *, workspace_root: str | None = None) -> Path:
    if is_private_app_path(path):
        raise ValueError("private/runtime file はApp Toolから参照・更新できません")
    try:
        return resolve_app_file(app_id, path, workspace_root=workspace_root)
    except AppStorageError as exc:
        raise ValueError(str(exc)) from exc


class AppWriteTransaction:
    """README / Docs Node / App Git / DB をまたぐ更新の補償境界。

    **採用した整合性モデル（README が正本）**

    1. workspace のファイルを更新する。更新前の状態は ``AppWorkspaceJournal``
       へ move で退避する。
    2. 同じ DB session の中で Manifest target 同期と Docs Node 同期を行う。
       Docs Node は README の同期ビューなので、README を書いたトランザクション
       の内側でしか更新しない。
    3. ``commit()`` でファイル更新と DB 更新をまとめて確定する。
    4. ``checkpoint()`` で App Git の revision を作る。**commit の後**に行う。

    1〜3 のどこで失敗しても ``rollback()`` が DB session を rollback し、
    journal を逆順に戻すので、README ファイル・Docs Node・DB は必ず「変更前」
    の状態へ揃う。Git revision もまだ作っていないため増えない。

    4 が失敗した場合はロールバックしない。README と DB は既に確定していて
    互いに整合しており、遅れているのは App Git だけである。Git は追記専用の
    checkpoint なので、次回の checkpoint が取りこぼした差分をまとめて取り込む。
    ここで README を巻き戻すと「正本を Git の都合で壊す」ことになるため、
    あえて「Git だけ 1 revision 遅れる」を許容する。
    """

    def __init__(
        self,
        session: Any,
        app_id: UUID,
        *,
        workspace: Path,
        workspace_root: str | None = None,
        git: Any | None = None,
    ) -> None:
        self.session = session
        self.app_id = app_id
        self.workspace = Path(workspace)
        self.journal = AppWorkspaceJournal(self.workspace)
        self._git = git if git is not None else AppGitService(workspace_root=workspace_root)
        self._settled = False

    def stash(self, *relative_paths: str) -> None:
        """更新前のファイル状態を退避する。書き込みの直前に呼ぶ。"""
        self.journal.stash(*relative_paths)

    async def rollback(self) -> None:
        """DB を rollback してから workspace を変更前へ戻す。"""
        self._settled = True
        try:
            await self.session.rollback()
        except Exception:
            logger.exception("App transaction DB rollback failed (app=%s)", self.app_id)
        self.journal.rollback()

    async def commit(self) -> None:
        try:
            await self.session.commit()
        except BaseException:
            await self.rollback()
            raise
        self._settled = True

    def checkpoint(self, message: str, actor: Any = None) -> str | None:
        """commit 後に App Git revision を作る best-effort な checkpoint。"""
        try:
            return self._git.checkpoint(self.app_id, message, actor=str(actor or "app"))
        except (AppGitError, AppStorageError, OSError) as exc:
            logger.warning("App Git checkpoint failed (app=%s): %s", self.app_id, exc)
            return None

    async def __aenter__(self) -> "AppWriteTransaction":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None and not self._settled:
            await self.rollback()
        self.journal.close()
        return False


async def abort_forked_app(
    session: Any,
    app_id: UUID,
    *,
    workspace_root: str | None = None,
) -> None:
    """Fork 失敗時の補償。DB を rollback し、作成済みのオンディスク成果物を消す。

    Fork は「新 App 行の作成」と「新 workspace / Git repo の作成」を並行して
    行うため、DB を rollback しただけでは workspace だけが孤児として残る。
    この App ID はこの Fork で新規採番したものなので、source / artifact
    namespace ごと削除してよい。
    """
    try:
        await session.rollback()
    except Exception:
        logger.exception("Fork rollback failed (app=%s)", app_id)
    try:
        remove_app_source_and_artifacts(app_id, workspace_root=workspace_root)
    except (AppStorageError, OSError):
        logger.exception("Fork workspace cleanup failed (app=%s)", app_id)


async def _installed_source_bundle(
    session: Any,
    app: App,
    project_id: UUID | None,
    *,
    workspace_root: str | None,
) -> tuple[ProjectApp, AppRelease, AppArtifact, Path] | None:
    if project_id is None:
        return None
    binding = await session.scalar(select(ProjectApp).where(
        ProjectApp.project_id == project_id,
        ProjectApp.app_id == app.id,
    ).limit(1))
    if binding is None or not binding.enabled or binding.binding_mode != "installed":
        return None
    if binding.installed_release_id is None:
        raise PermissionError("固定保存版のReleaseがありません")
    release = await session.scalar(select(AppRelease).where(
        AppRelease.id == binding.installed_release_id,
        AppRelease.app_id == app.id,
        AppRelease.status == "published",
    ).limit(1))
    if release is None:
        raise PermissionError("固定保存版を利用できません")
    artifact = await session.scalar(select(AppArtifact).where(
        AppArtifact.release_id == release.id,
        AppArtifact.artifact_type == "source_bundle",
    ).limit(1))
    if artifact is None:
        raise PermissionError("固定保存版のSource Bundleがありません")
    try:
        archive_path = resolve_app_artifact_file(
            app.id,
            release.id,
            Path(str(artifact.filename)).name,
            workspace_root=workspace_root,
        )
        verify_file_integrity(
            archive_path,
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )
    except (AppStorageError, OSError) as exc:
        raise PermissionError(f"固定保存版のArtifactを検証できません: {exc}") from exc
    return binding, release, artifact, archive_path


def _archive_file_text(archive_path: Path, path: str) -> tuple[str, str]:
    normalized = normalize_app_relative_path(path)
    if is_private_app_path(normalized):
        raise PermissionError("private/runtime file はApp Toolから参照できません")
    if not is_text_app_path(normalized):
        raise ValueError("バイナリまたは未知の形式はApp Toolのread対象外です")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            content = archive.read(normalized)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise FileNotFoundError("固定保存版にファイルがありません") from exc
    try:
        return normalized, content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("UTF-8テキストではありません") from exc


async def _require_development_binding(
    session: Any,
    app: App,
    context: dict[str, Any],
    *,
    workspace_root: str | None,
) -> None:
    if await _installed_source_bundle(
        session,
        app,
        _uuid(_ctx_project_id(context), "project_id") if _ctx_project_id(context) else None,
        workspace_root=workspace_root,
    ) is not None:
        raise PermissionError("固定保存版は読み取り専用です。Project設定でmain（最新）へ切り替えてください")


@asynccontextmanager
async def _authorized_app(
    context: dict[str, Any],
    app_id: str | None = None,
    *,
    required: str = "viewer",
    require_enabled_binding: bool = True,
) -> AsyncIterator[tuple[Any, App, str]]:
    app_uuid = _active_app_id(context, app_id)
    project_id = _ctx_project_id(context)
    project_uuid = _uuid(project_id, "project_id") if project_id else None
    manager = get_database_manager()
    session = await manager.get_session()
    try:
        app = await session.scalar(select(App).where(App.id == app_uuid).limit(1))
        if not app:
            raise ValueError("App not found")
        if project_uuid is not None and require_enabled_binding:
            binding = await session.scalar(select(ProjectApp).where(
                ProjectApp.project_id == project_uuid,
                ProjectApp.app_id == app.id,
                ProjectApp.enabled.is_(True),
            ).limit(1))
            if binding is None:
                raise PermissionError("このProjectではAppが有効化されていません")
        try:
            permission = await AppService().require_permission(
                session,
                app,
                user_id=_ctx_user_id(context),
                required=required,
                user_role=_ctx_user_role(context),
                project_id=project_uuid,
            )
        except AppAccessError as exc:
            raise PermissionError(str(exc)) from exc
        yield session, app, permission
    finally:
        await session.close()


def _tool(
    name: str,
    description: str,
    function: Callable[..., Any],
    parameters: list[ToolParam],
    *,
    risk: str = "low",
    side_effect: str = "none",
    requires_approval: bool = False,
    supports_parallel: bool = True,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        function=function,
        parameters=parameters,
        is_async=inspect.iscoroutinefunction(function),
        owner="apps",
        risk=risk,
        side_effect=side_effect,
        requires_approval=requires_approval,
        supports_parallel=supports_parallel,
    )


def build_app_tool_definitions(
    context: dict[str, Any] | None,
    *,
    workspace_root: str | None = None,
    deployment_config: dict[str, Any] | None = None,
) -> list[ToolDefinition]:
    """Build App tools for one server-resolved runtime context."""
    # Keep a lazy request-local principal instead of freezing the constructor
    # context inside every App Tool closure.
    runtime_context = _RuntimeAppContext(context)

    async def _assert_job_scope(session, job: AppJob) -> None:
        requested_project_id = _ctx_project_id(runtime_context)
        requested_project_uuid = _uuid(requested_project_id, "project_id") if requested_project_id else None
        if job.project_id is None:
            if requested_project_uuid is not None:
                raise PermissionError("JobのProject scopeが一致しません")
            return
        if requested_project_uuid is not None and job.project_id != requested_project_uuid:
            raise PermissionError("JobのProject scopeが一致しません")
        app_service = AppService()
        if not await app_service.project_access(
            session,
            project_id=job.project_id,
            user_id=_ctx_user_id(runtime_context),
            user_role=_ctx_user_role(runtime_context),
        ):
            raise PermissionError("JobのProjectを閲覧できません")
        binding = await session.scalar(select(ProjectApp).where(
            ProjectApp.project_id == job.project_id,
            ProjectApp.app_id == job.app_id,
            ProjectApp.enabled.is_(True),
        ).limit(1))
        if binding is None:
            raise PermissionError("このProjectではAppが有効化されていません")

    async def create_app(name: str, slug: str = "", description: str = "") -> dict[str, Any]:
        user_id = _ctx_user_id(runtime_context)
        session = await get_database_manager().get_session()
        operation_lock = None
        try:
            project_id = _ctx_project_id(runtime_context)
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            if project_uuid and not await AppService().project_access(
                session,
                project_id=project_uuid,
                user_id=user_id,
                user_role=_ctx_user_role(runtime_context),
            ):
                raise PermissionError("Projectへの権限がありません")
            if project_uuid and not await AppService().project_write_access(
                session,
                project_id=project_uuid,
                user_id=user_id,
                user_role=_ctx_user_role(runtime_context),
            ):
                raise PermissionError("ProjectのApp構成を変更する権限がありません")
            if project_uuid:
                operation_lock = project_operation_lock(
                    project_uuid,
                    workspace_root=workspace_root,
                )
                await operation_lock.acquire()
                locked_project_id = await session.scalar(
                    select(Project.id)
                    .where(Project.id == project_uuid, Project.deleted_at.is_(None))
                    .with_for_update()
                )
                if locked_project_id is None:
                    raise PermissionError("Projectが見つかりません")
                if not await AppService().project_write_access(
                    session,
                    project_id=project_uuid,
                    user_id=user_id,
                    user_role=_ctx_user_role(runtime_context),
                ):
                    raise PermissionError("ProjectのApp構成を変更する権限がありません")
            normalized_slug = re.sub(r"[^a-z0-9]+", "-", (slug or name).strip().lower()).strip("-")
            app = await AppService(workspace_root=workspace_root).create_app(
                session,
                owner_user_id=user_id,
                name=name,
                slug=normalized_slug[:120] or "aoitalk-app",
                description=description,
                origin_project_id=project_uuid,
            )
            if project_uuid:
                session.add(ProjectApp(
                    project_id=project_uuid,
                    app_id=app.id,
                    binding_mode="development",
                    created_by=user_id,
                ))
            instance_existed = False
            try:
                if project_uuid:
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
                if project_uuid and not instance_existed:
                    remove_app_instance(project_uuid, app.id, workspace_root=workspace_root)
                raise
            return app.to_dict()
        finally:
            if operation_lock is not None:
                operation_lock.release()
            await session.close()

    async def list_apps() -> list[dict[str, Any]]:
        user_id = _ctx_user_id(runtime_context)
        manager = get_database_manager()
        session = await manager.get_session()
        try:
            project_id = _ctx_project_id(runtime_context)
            project_uuid = _uuid(project_id, "project_id") if project_id else None
            if project_uuid and not await AppService().project_access(
                session,
                project_id=project_uuid,
                user_id=user_id,
                user_role=_ctx_user_role(runtime_context),
            ):
                raise PermissionError("Projectへの権限がありません")
            rows = await AppService().list_accessible_apps(
                session,
                user_id=user_id,
                project_id=project_uuid,
            )
            return [item.to_dict() | {"permission": permission} for item, permission in rows]
        finally:
            await session.close()

    async def get_app(app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None) as (session, app, permission):
            targets = list((await session.scalars(select(AppTarget).where(AppTarget.app_id == app.id).order_by(AppTarget.target_key))).all())
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            bundle = await _installed_source_bundle(session, app, project_uuid, workspace_root=workspace_root)
            if bundle is not None:
                try:
                    with zipfile.ZipFile(bundle[3]) as archive:
                        manifest_text = archive.read("aoitalk.app.yaml").decode("utf-8")
                    manifest = parse_manifest_text(manifest_text)
                    raw_targets = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
                    current_by_key = {target.target_key: target for target in targets}
                    projected: list[dict[str, Any]] = []
                    for key, snapshot in raw_targets.items():
                        if not isinstance(snapshot, dict):
                            continue
                        item = current_by_key.get(str(key))
                        projected.append((item.to_dict() if item else {"id": f"release:{key}", "app_id": str(app.id), "target_key": str(key)}) | {
                            "target_key": str(key),
                            "display_name": snapshot.get("display_name") or str(key),
                            "surface": snapshot.get("surface") or "headless",
                            "runtime": snapshot.get("runtime") or "executable",
                            "execution_host": snapshot.get("execution_host") or "download_only",
                            "entrypoint": snapshot.get("entrypoint") or "",
                            "manifest_snapshot": snapshot,
                        })
                    targets = projected  # type: ignore[assignment]
                except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile, AppManifestError) as exc:
                    raise PermissionError("固定保存版のManifestを読み込めません") from exc
            return app.to_dict() | {"permission": permission, "targets": [target.to_dict() if hasattr(target, "to_dict") else target for target in targets]}

    async def get_app_context(app_id: str = "", target_key: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None) as (session, app, permission):
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            bundle = await _installed_source_bundle(
                session,
                app,
                project_uuid,
                workspace_root=workspace_root,
            )
            if bundle is not None:
                _binding, _release, _artifact, archive_path = bundle
                try:
                    with zipfile.ZipFile(archive_path) as archive:
                        manifest_text = archive.read("aoitalk.app.yaml").decode("utf-8")
                        readme = archive.read("README.md").decode("utf-8") if "README.md" in archive.namelist() else ""
                except (KeyError, UnicodeDecodeError, OSError, zipfile.BadZipFile) as exc:
                    raise PermissionError("固定保存版のSource Bundleを読み込めません") from exc
                manifest = parse_manifest_text(manifest_text)
            else:
                workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
                manifest, manifest_text, _ = load_app_manifest(workspace)
                readme_path = resolve_workspace_file(workspace, "README.md")
                readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
            manifest_hash = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
            if target_key and target_key not in manifest.get("targets", {}):
                raise ValueError("Target not found")
            return {
                "app": app.to_dict(),
                "permission": permission,
                "target_key": target_key or app.default_target_key,
                "manifest": manifest,
                "manifest_text": manifest_text,
                "manifest_hash": manifest_hash,
                "readme": readme,
            }

    async def _refresh_business_overview(session, app: App, workspace: Path, user_id: UUID) -> dict[str, Any]:
        manifest, _manifest_text, _manifest_hash = load_app_manifest(workspace)
        readme_path = resolve_workspace_file(workspace, "README.md")
        readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        # workspace の全走査は重く、App operation lock を保持したまま走るので
        # 1回だけ収集して分析と Manifest 書き戻しの両方で使い回す。
        evidence = collect_source_evidence(workspace, manifest=manifest)
        analysis = await analyze_app_workspace(
            workspace=workspace,
            name=app.name,
            description=app.description or str(manifest.get("description") or ""),
            readme=readme,
            manifest=manifest,
            evidence=evidence,
        )
        _updated, manifest_text = write_analysis_to_manifest(
            workspace=workspace,
            analysis=analysis,
            evidence=evidence,
        )
        normalized = parse_manifest_text(manifest_text)
        validate_manifest_workspace(normalized, workspace)
        await sync_manifest_targets_unlocked(session, app, workspace)
        updated_readme = merge_analysis_into_readme(readme, analysis)
        if updated_readme != readme:
            readme_path.write_text(updated_readme, encoding="utf-8", newline="\n")
            await AppService(workspace_root=workspace_root).sync_readme_to_node(
                session,
                app,
                user_id,
            )
        return analysis

    async def analyze_app_business(app_id: str = "") -> dict[str, Any]:
        """Refresh overview metadata after App source or intent changes.

        README と Manifest を書き換えるので ``AppWriteTransaction`` の補償境界に
        入れる。DB commit まで失敗したら両ファイルとも変更前へ戻す。
        """
        async with _authorized_app(runtime_context, app_id or None, required="developer") as (session, app, _permission):
            await _require_development_binding(session, app, runtime_context, workspace_root=workspace_root)
            user_id = _ctx_user_id(runtime_context)
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                workspace = ensure_app_workspace(app.id, workspace_root=workspace_root)
                async with AppWriteTransaction(
                    session,
                    app.id,
                    workspace=workspace,
                    workspace_root=workspace_root,
                ) as transaction:
                    transaction.stash("README.md", "aoitalk.app.yaml")
                    analysis = await _refresh_business_overview(
                        session,
                        app,
                        workspace,
                        user_id,
                    )
                    await transaction.commit()
                    revision = transaction.checkpoint(
                        "App業務内容を分析して概要を更新",
                        actor=user_id,
                    )
                return {"success": True, "analysis": analysis, "revision": revision}

    async def list_app_files_tool(app_id: str = "") -> list[dict[str, object]]:
        async with _authorized_app(runtime_context, app_id or None, required="viewer") as (session, app, _permission):
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            bundle = await _installed_source_bundle(session, app, project_uuid, workspace_root=workspace_root)
            if bundle is not None:
                _binding, _release, _artifact, archive_path = bundle
                result: list[dict[str, object]] = []
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
                            content = archive.read(info)
                            result.append({
                                "path": normalized,
                                "filename": Path(normalized).name,
                                "size_bytes": len(content),
                                "sha256": hashlib.sha256(content).hexdigest(),
                            })
                except (OSError, zipfile.BadZipFile) as exc:
                    raise PermissionError("固定保存版のSource Bundleを読み込めません") from exc
                return result
            return list_app_files(app.id, workspace_root=workspace_root)

    async def read_app_file(path: str, app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="viewer") as (session, app, _permission):
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            bundle = await _installed_source_bundle(session, app, project_uuid, workspace_root=workspace_root)
            if bundle is not None:
                normalized, content = _archive_file_text(bundle[3], path)
                return {"path": normalized, "content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "release_id": str(bundle[1].id)}
            target = _safe_file(app.id, path, workspace_root=workspace_root)
            if not target.exists() or not target.is_file():
                raise ValueError("ファイルが見つかりません")
            content = target.read_text(encoding="utf-8")
            return {"path": normalize_app_relative_path(path), "content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}

    async def write_app_file(path: str, content: str, expected_sha256: str = "", app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="developer") as (session, app, _permission):
            await _require_development_binding(session, app, runtime_context, workspace_root=workspace_root)
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                return await _write_app_file_unlocked(
                    path,
                    content,
                    expected_sha256=expected_sha256,
                    session=session,
                    app=app,
                )

    async def _write_app_file_unlocked(
        path: str,
        content: str,
        *,
        expected_sha256: str,
        session,
        app: App,
    ) -> dict[str, Any]:
        """Write one workspace file with README/Docs/Git/DB compensation.

        ファイル書き込み → DB 更新 → commit → Git checkpoint の順に固定する。
        commit までに失敗したら journal でファイルを、session rollback で
        Docs Node と Target を変更前へ戻す。checkpoint は commit 後の
        best-effort（``AppWriteTransaction`` の docstring 参照）。
        """
        normalized = normalize_app_relative_path(path)
        if is_protected_app_path(normalized):
            raise ValueError(".gitignore は書き込み保護されているため更新できません")
        target = _safe_file(app.id, normalized, workspace_root=workspace_root)
        if expected_sha256 and target.exists() and sha256_file(target) != expected_sha256:
            raise ValueError("ファイルが競合しています")
        is_manifest = normalized.lower() == "aoitalk.app.yaml"
        if is_manifest:
            validate_manifest_workspace(parse_manifest_text(content), target.parent)
        workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
        user_id = _ctx_user_id(runtime_context)
        async with AppWriteTransaction(
            session,
            app.id,
            workspace=workspace,
            workspace_root=workspace_root,
        ) as transaction:
            transaction.stash(normalized)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            if is_manifest:
                await sync_manifest_targets_unlocked(session, app, target.parent)
            analysis = None
            if normalized.lower() not in {"aoitalk.app.yaml", "readme.md"}:
                analysis = await _try_refresh_business_overview(session, app, workspace, user_id)
            if normalized.lower() == "readme.md":
                await AppService(workspace_root=workspace_root).sync_readme_to_node(
                    session,
                    app,
                    user_id,
                )
            await transaction.commit()
            revision = transaction.checkpoint(f"{normalized} を更新", actor=user_id)
        return {"path": normalized, "sha256": sha256_file(target), "revision": revision, "analysis": analysis}

    async def _try_refresh_business_overview(
        session,
        app: App,
        workspace: Path,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        """Run the optional overview refresh inside its own savepoint.

        概要の再生成は付随処理なので、失敗しても本体のソース編集は残したい。
        ただし README / Manifest を書き換える処理なので、失敗を握り潰すと
        「途中まで書き換わった README」が残る。SAVEPOINT と専用 journal で
        この処理だけを丸ごと巻き戻し、本体の編集には触らない。
        """
        overview_journal = AppWorkspaceJournal(workspace)
        try:
            overview_journal.stash("README.md", "aoitalk.app.yaml")
            async with session.begin_nested():
                return await _refresh_business_overview(session, app, workspace, user_id)
        except Exception as exc:
            # Keep the source edit durable even if an older/custom
            # Manifest prevents the optional overview refresh.
            logger.warning("App business overview refresh failed: %s", exc)
            overview_journal.rollback()
            return None
        finally:
            overview_journal.close()

    async def delete_app_file(path: str, app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="developer") as (session, app, _permission):
            await _require_development_binding(session, app, runtime_context, workspace_root=workspace_root)
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                normalized = normalize_app_relative_path(path)
                if normalized.lower() in {"readme.md", "aoitalk.app.yaml"}:
                    raise ValueError("READMEとManifestは削除できません")
                if is_protected_app_path(normalized):
                    raise ValueError(".gitignore は書き込み保護されているため削除できません")
                target = _safe_file(app.id, normalized, workspace_root=workspace_root)
                if not target.exists() or not target.is_file():
                    raise ValueError("ファイルが見つかりません")
                workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
                user_id = _ctx_user_id(runtime_context)
                async with AppWriteTransaction(
                    session,
                    app.id,
                    workspace=workspace,
                    workspace_root=workspace_root,
                ) as transaction:
                    # stash がそのまま削除になる。commit 前に失敗したら復元される。
                    transaction.stash(normalized)
                    await transaction.commit()
                    revision = transaction.checkpoint(f"{normalized} を削除", actor=user_id)
                return {"success": True, "revision": revision}

    async def validate_app_manifest(app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None) as (session, app, _permission):
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            bundle = await _installed_source_bundle(session, app, project_uuid, workspace_root=workspace_root)
            if bundle is not None:
                _normalized, manifest_text = _archive_file_text(bundle[3], "aoitalk.app.yaml")
                manifest = parse_manifest_text(manifest_text)
                manifest_hash = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
            else:
                manifest, _text, manifest_hash = load_app_manifest(get_app_workspace_path(app.id, workspace_root=workspace_root))
            return {"valid": True, "manifest": manifest, "manifest_hash": manifest_hash}

    async def update_app_manifest(content: str, expected_sha256: str = "", app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="developer") as (session, app, _permission):
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                return await _update_app_manifest_unlocked(
                    content,
                    expected_sha256=expected_sha256,
                    app_id=app_id,
                    session=session,
                    app=app,
                )

    async def _update_app_manifest_unlocked(
        content: str,
        *,
        expected_sha256: str = "",
        app_id: str = "",
        session,
        app,
    ) -> dict[str, Any]:
        await _require_development_binding(session, app, runtime_context, workspace_root=workspace_root)
        workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
        path = resolve_workspace_file(workspace, "aoitalk.app.yaml")
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if expected_sha256 and expected_sha256 != current_hash:
            raise ValueError("Manifestが競合しています")
        validate_manifest_workspace(parse_manifest_text(content), workspace)
        async with AppWriteTransaction(
            session,
            app.id,
            workspace=workspace,
            workspace_root=workspace_root,
        ) as transaction:
            transaction.stash("aoitalk.app.yaml")
            path.write_text(content, encoding="utf-8", newline="\n")
            targets = await sync_manifest_targets_unlocked(session, app, workspace)
            # commit で属性が expire するため、payload は commit 前に確定させる。
            target_payload = [target.to_dict() for target in targets]
            await transaction.commit()
            revision = transaction.checkpoint("Manifestを更新")
        return {"valid": True, "targets": target_payload, "revision": revision}

    async def git_status(app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="viewer") as (session, app, _permission):
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            bundle = await _installed_source_bundle(session, app, project_uuid, workspace_root=workspace_root)
            if bundle is not None:
                release = bundle[1]
                return {
                    "available": True,
                    "fixed_release": True,
                    "clean": True,
                    "dirty": False,
                    "branch": f"release/{release.version}",
                    "revision": release.git_revision,
                    "files": [],
                }
            return AppGitService(workspace_root=workspace_root).status(app.id)

    async def git_history(app_id: str = "", limit: int = 20) -> list[dict[str, str]]:
        async with _authorized_app(runtime_context, app_id or None, required="viewer") as (session, app, _permission):
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            bundle = await _installed_source_bundle(session, app, project_uuid, workspace_root=workspace_root)
            if bundle is not None:
                release = bundle[1]
                return [{
                    "revision": release.git_revision,
                    "message": f"保存版 v{release.version}",
                    "author": "Release",
                    "date": release.created_at.isoformat() if release.created_at else "",
                }]
            return AppGitService(workspace_root=workspace_root).history(app.id, limit=limit)

    async def git_diff(app_id: str = "", rev_a: str = "", rev_b: str = "", path: str = "") -> str:
        async with _authorized_app(runtime_context, app_id or None, required="viewer") as (session, app, _permission):
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            if await _installed_source_bundle(session, app, project_uuid, workspace_root=workspace_root) is not None:
                return ""
            return AppGitService(workspace_root=workspace_root).diff(
                app.id, rev_a or None, rev_b or None, path=path or None
            )

    async def git_restore(path: str, revision: str, app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="developer") as (session, app, _permission):
            await _require_development_binding(session, app, runtime_context, workspace_root=workspace_root)
            normalized = normalize_app_relative_path(path)
            if (
                is_private_app_path(normalized)
                or is_protected_app_path(normalized)
                or normalized.lower() in {"readme.md", "aoitalk.app.yaml"}
            ):
                raise ValueError("README/Manifest/.gitignore/private runtime file はGit復元対象にできません")
            workspace = get_app_workspace_path(app.id, workspace_root=workspace_root)
            git = AppGitService(workspace_root=workspace_root)
            # 復元と checkpoint はセットで初めて意味を持つので、checkpoint が
            # 失敗したら作業ツリーも復元前へ戻して「何も起きなかった」に揃える。
            with AppWorkspaceJournal(workspace) as journal:
                journal.stash(normalized)
                git.restore(app.id, normalized, revision)
                checkpoint = git.checkpoint(app.id, f"{normalized} を復元")
            return {"success": True, "revision": checkpoint}

    async def _job(target_key: str, job_type: str, input_json: dict[str, Any] | None = None, project_id: str = "") -> dict[str, Any]:
        required_permission = (
            "runner" if job_type == "run"
            else "maintainer" if job_type == "package"
            else "developer"
        )
        async with _authorized_app(runtime_context, required=required_permission) as (session, app, _permission):
            try:
                assert_user_may_start_server_job(
                    user_role=_ctx_user_role(runtime_context),
                    config=deployment_config,
                )
            except ServerJobExecutionDenied as exc:
                raise PermissionError(str(exc)) from exc
            async with app_operation_lock(app.id, workspace_root=workspace_root):
                await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
                target = await session.scalar(select(AppTarget).where(
                    AppTarget.app_id == app.id, AppTarget.target_key == target_key
                ).limit(1))
                if not target:
                    raise ValueError("Target not found")
                project_uuid = _uuid(project_id or _ctx_project_id(runtime_context), "project_id") if (project_id or _ctx_project_id(runtime_context)) else None
                release_id: UUID | None = None
                if project_uuid:
                    app_service = AppService()
                    if not await app_service.project_access(
                        session,
                        project_id=project_uuid,
                        user_id=_ctx_user_id(runtime_context),
                        user_role=_ctx_user_role(runtime_context),
                    ):
                        raise PermissionError("Projectへの権限がありません")
                    await app_service.require_permission(
                        session,
                        app,
                        user_id=_ctx_user_id(runtime_context),
                        required=required_permission,
                        user_role=_ctx_user_role(runtime_context),
                        project_id=project_uuid,
                    )
                    binding = await session.scalar(select(ProjectApp).where(
                        ProjectApp.project_id == project_uuid,
                        ProjectApp.app_id == app.id,
                    ).limit(1))
                    if binding is None or not binding.enabled:
                        raise PermissionError("このProjectではAppが有効化されていません")
                    if binding.binding_mode == "installed":
                        if job_type != "run":
                            raise ValueError("固定保存版のProjectではBuild/Test/Packageは実行できません")
                        if binding.installed_release_id is None:
                            raise ValueError("固定保存版が選択されていません")
                        release_id = binding.installed_release_id
                        release = await session.scalar(select(AppRelease).where(
                            AppRelease.id == release_id,
                            AppRelease.app_id == app.id,
                            AppRelease.status == "published",
                        ).limit(1))
                        if release is None:
                            raise ValueError("固定保存版を利用できません")
                        runtime_artifact = await session.scalar(select(AppArtifact.id).where(
                            AppArtifact.release_id == release_id,
                            AppArtifact.target_id == target.id,
                            AppArtifact.artifact_type == "runtime_bundle",
                        ).limit(1))
                        if runtime_artifact is None:
                            raise ValueError("Projectが固定しているReleaseにこのTargetの成果物がありません")
                job = AppJob(
                    app_id=app.id,
                    target_id=target.id,
                    project_id=project_uuid,
                    release_id=release_id if isinstance(release_id, UUID) else None,
                    job_type=job_type,
                    status="queued",
                    input_json=input_json or {},
                    started_by=_ctx_user_id(runtime_context),
                )
                session.add(job)
                await session.commit()
                job_id = job.id
                # Durability is established before execution. The task is linked to
                # the AppJob row and can be stopped through the matching tool/API.
                import asyncio

                asyncio.create_task(execute_app_job(
                    get_database_manager(),
                    job_id,
                    workspace_root=workspace_root,
                    deployment_config=deployment_config,
                ))
                return job.to_dict()

    async def build_app_target(target_key: str, input_json: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _job(target_key, "build", input_json)

    async def test_app_target(target_key: str, input_json: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _job(target_key, "test", input_json)

    async def run_app_target(target_key: str, input_json: dict[str, Any] | None = None, project_id: str = "") -> dict[str, Any]:
        return await _job(target_key, "run", input_json, project_id)

    async def package_app_target(target_key: str, input_json: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _job(target_key, "package", input_json)

    async def stop_app_job(job_id: str) -> dict[str, Any]:
        async with _authorized_app(runtime_context, required="runner") as (session, app, _permission):
            job = await session.scalar(
                select(AppJob)
                .where(AppJob.id == _uuid(job_id, "job_id"), AppJob.app_id == app.id)
                .with_for_update()
                .limit(1)
            )
            if not job:
                raise ValueError("Job not found")
            await _assert_job_scope(session, job)
            if job.status in {"succeeded", "failed", "cancelled"}:
                return {"success": True, "stopped": False, "job": job.to_dict()}
            stopped = stop_running_job(job.id)
            job.status = "cancelled"
            job.ended_at = datetime.utcnow()
            await session.commit()
            return {"success": True, "stopped": stopped, "job": job.to_dict()}

    async def read_app_job_logs(job_id: str) -> dict[str, Any]:
        async with _authorized_app(runtime_context, required="viewer") as (session, app, _permission):
            job = await session.scalar(select(AppJob).where(AppJob.id == _uuid(job_id, "job_id"), AppJob.app_id == app.id).limit(1))
            if not job:
                return {"job_id": job_id, "logs": ""}
            await _assert_job_scope(session, job)
            if not job.log_path:
                return {"job_id": job_id, "logs": ""}
            log_path = Path(job.log_path).resolve()
            roots = [get_app_workspace_path(app.id, workspace_root=workspace_root).resolve()]
            if job.project_id:
                roots.append(ensure_app_instance(job.project_id, app.id, workspace_root=workspace_root).resolve())
            if not any(log_path == root or root in log_path.parents for root in roots):
                raise PermissionError("Job log path is outside App scope")
            return {"job_id": job_id, "logs": log_path.read_text(encoding="utf-8") if log_path.exists() else ""}

    async def create_app_release(version: str, changelog: str = "", app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="maintainer") as (session, app, _permission):
            await _require_development_binding(session, app, runtime_context, workspace_root=workspace_root)
            try:
                release = await create_app_release_service(
                    session,
                    app,
                    version=version,
                    created_by=_ctx_user_id(runtime_context),
                    changelog=changelog,
                    workspace_root=workspace_root,
                    deployment_config=deployment_config,
                )
            except AppReleaseError as exc:
                raise ValueError(str(exc)) from exc
            await session.commit()
            return release.to_dict()

    async def export_app_release(release_id: str, app_id: str = "") -> list[dict[str, Any]]:
        async with _authorized_app(runtime_context, app_id or None, required="viewer") as (session, app, _permission):
            releases = list((await session.scalars(select(AppArtifact).join(AppRelease, AppRelease.id == AppArtifact.release_id).where(
                AppArtifact.release_id == _uuid(release_id, "release_id"), AppRelease.app_id == app.id
            ))).all())
            return [artifact.to_dict() for artifact in releases]

    async def import_app_source_bundle(source_bundle_path: str, app_id: str = "") -> dict[str, Any]:
        """Import a source ZIP that already resides inside the active App workspace.

        既存 App への import は次の順で行う。

        1. 一時 staging へ展開する（本番 workspace には触れない）。``.git``・
           private/runtime file・書き込み保護された `.gitignore` はここで落とす。
        2. staging から workspace へ 1 file ずつ「元をバックアップへ move →
           新しいものを本番位置へ move」で差し替える。すべての move は journal
           に記録する。
        3. 差し替え後の workspace で Manifest を検証し、Target と Docs Node を
           同期して commit する。
        4. commit 後に Git checkpoint を作る。

        2〜3 のどこで失敗しても journal を逆順に戻すので、元の workspace が
        完全に復元される。staging は同じ `_apps` namespace に置き、Windows でも
        move が同一 filesystem 内で完結するようにしている。
        """
        async with _authorized_app(runtime_context, app_id or None, required="developer") as (session, app, _permission):
            await _require_development_binding(session, app, runtime_context, workspace_root=workspace_root)
            source = _safe_file(app.id, source_bundle_path, workspace_root=workspace_root)
            if source.suffix.lower() != ".zip" or not source.exists() or not source.is_file():
                raise ValueError("Source BundleはApp workspace内のZIPで指定してください")
            root = get_app_workspace_path(app.id, workspace_root=workspace_root)
            root.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".app-import-", dir=root.parent))
            user_id = _ctx_user_id(runtime_context)
            operation_lock = app_operation_lock(app.id, workspace_root=workspace_root)
            await operation_lock.acquire()
            try:
                imported = stage_app_source_bundle(source, staging)
                if not imported:
                    raise ValueError("Source Bundleに取り込めるファイルがありません")
                async with AppWriteTransaction(
                    session,
                    app.id,
                    workspace=root,
                    workspace_root=workspace_root,
                ) as transaction:
                    applied = swap_app_workspace_files(root, staging, imported, transaction.journal)
                    manifest, _text, _hash = load_app_manifest(root)
                    validate_manifest_workspace(manifest, root)
                    await sync_manifest_targets_unlocked(session, app, root)
                    await AppService(workspace_root=workspace_root).ensure_readme_node(
                        session,
                        app,
                        user_id,
                        workspace=root,
                    )
                    await transaction.commit()
                    revision = transaction.checkpoint("Source Bundle を取り込み", actor=user_id)
                return {
                    "success": True,
                    "manifest": manifest,
                    "revision": revision,
                    "imported_files": applied,
                }
            except (zipfile.BadZipFile, AppStorageError, AppManifestError) as exc:
                raise ValueError(str(exc)) from exc
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                operation_lock.release()

    async def fork_app(name: str, slug: str = "", app_id: str = "") -> dict[str, Any]:
        """Fork the active App into a brand-new independent App.

        新 App の DB 行と workspace（Git repo 込み）を同時に作るので、途中で
        失敗すると DB rollback だけでは workspace が孤児として残る。commit まで
        の全区間を try で囲み、失敗時は ``abort_forked_app`` で DB rollback と
        オンディスク成果物の削除をまとめて行う。

        コピー対象は ``iter_app_source_files`` / ``normalize_app_bundle_member``
        が決める。``.git``・private/runtime 領域・symlink 経由の別 App 領域・
        書き込み保護された `.gitignore` は絶対にコピーしない（`.gitignore` は
        Fork 先で正本ルールから作り直す）。
        """
        async with _authorized_app(runtime_context, app_id or None, required="developer") as (session, source, _permission):
            user_id = _ctx_user_id(runtime_context)
            normalized_slug = re.sub(r"[^a-z0-9]+", "-", (slug or name).strip().lower()).strip("-")[:120]
            origin_project_id = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            if origin_project_id and not await AppService().project_write_access(
                session,
                project_id=origin_project_id,
                user_id=user_id,
                user_role=_ctx_user_role(runtime_context),
            ):
                raise PermissionError("ProjectのApp構成を変更する権限がありません")
            project_uuid = _uuid(_ctx_project_id(runtime_context), "project_id") if _ctx_project_id(runtime_context) else None
            target_app: App | None = None
            try:
                target_app = await AppService(workspace_root=workspace_root).create_app(
                    session,
                    owner_user_id=user_id,
                    name=name,
                    slug=normalized_slug or "aoitalk-fork",
                    description=source.description or "",
                    origin_project_id=origin_project_id,
                )
                target_app_id = target_app.id
                target_root = get_app_workspace_path(target_app_id, workspace_root=workspace_root)
                operation_lock = app_operation_lock(source.id, workspace_root=workspace_root)
                await operation_lock.acquire()
                try:
                    bundle = await _installed_source_bundle(session, source, project_uuid, workspace_root=workspace_root)
                    if bundle is not None:
                        try:
                            with zipfile.ZipFile(bundle[3]) as archive:
                                for member in archive.infolist():
                                    if member.is_dir():
                                        continue
                                    normalized = normalize_app_bundle_member(member.filename)
                                    if normalized is None:
                                        continue
                                    destination = resolve_workspace_file(target_root, normalized)
                                    destination.parent.mkdir(parents=True, exist_ok=True)
                                    with archive.open(member) as source_file, destination.open("wb") as target_file:
                                        shutil.copyfileobj(source_file, target_file)
                        except (OSError, zipfile.BadZipFile, AppStorageError) as exc:
                            raise ValueError("固定保存版のSource BundleをForkできません") from exc
                    else:
                        source_root = get_app_workspace_path(source.id, workspace_root=workspace_root)
                        for absolute, relative in iter_app_source_files(source_root):
                            destination = resolve_workspace_file(target_root, relative)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(absolute, destination)
                finally:
                    operation_lock.release()
                await sync_manifest_targets(session, target_app, target_root)
                await AppService(workspace_root=workspace_root).ensure_readme_node(
                    session,
                    target_app,
                    user_id,
                    workspace=target_root,
                )
                # commit で属性が expire するため、payload は commit 前に確定させる。
                payload = target_app.to_dict()
                await session.commit()
            except BaseException:
                if target_app is None:
                    # create_app 自体が失敗した場合は App ID を掴めない。
                    # workspace を作る前に落ちるのが通常経路なので DB だけ戻す。
                    await session.rollback()
                else:
                    await abort_forked_app(session, target_app.id, workspace_root=workspace_root)
                raise
            try:
                AppGitService(workspace_root=workspace_root).checkpoint(
                    target_app_id,
                    "AppをFork",
                    actor=str(user_id),
                )
            except (AppGitError, AppStorageError, OSError) as exc:
                # Fork 済みの App と workspace は確定済み。Git だけ次回 checkpoint で追いつく。
                logger.warning("Fork checkpoint failed (app=%s): %s", target_app_id, exc)
            return payload

    async def link_app_to_project(project_id: str, binding_mode: str = "development", installed_release_id: str = "", app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="runner", require_enabled_binding=False) as (session, app, _permission):
            project_uuid = _uuid(project_id, "project_id")
            app_service = AppService()
            async with project_operation_lock(project_uuid, workspace_root=workspace_root):
                locked_project_id = await session.scalar(select(Project.id).where(
                    Project.id == project_uuid,
                    Project.deleted_at.is_(None),
                ).with_for_update())
                if locked_project_id is None:
                    raise PermissionError("Project not found")
                locked_app = await session.scalar(select(App).where(App.id == app.id).with_for_update().limit(1))
                if locked_app is None:
                    raise PermissionError("App not found")
                app = locked_app
                if not await app_service.project_access(
                    session,
                    project_id=project_uuid,
                    user_id=_ctx_user_id(runtime_context),
                    user_role=_ctx_user_role(runtime_context),
                ):
                    raise PermissionError("Projectへの権限がありません")
                if not await app_service.project_write_access(
                    session,
                    project_id=project_uuid,
                    user_id=_ctx_user_id(runtime_context),
                    user_role=_ctx_user_role(runtime_context),
                ):
                    raise PermissionError("ProjectのApp構成を変更する権限がありません")
                await app_service.require_permission(
                    session,
                    app,
                    user_id=_ctx_user_id(runtime_context),
                    required="runner",
                    user_role=_ctx_user_role(runtime_context),
                    project_id=project_uuid,
                )
                active_job = await session.scalar(select(AppJob.id).where(
                    AppJob.app_id == app.id,
                    AppJob.project_id == project_uuid,
                    AppJob.status.in_({"queued", "running"}),
                ).with_for_update().limit(1))
                if active_job is not None:
                    raise PermissionError("実行中または待機中のApp Jobがあるためbindingを変更できません")
                if binding_mode not in {"development", "installed"}:
                    raise ValueError("binding_mode が不正です")
                release_uuid = _uuid(installed_release_id, "installed_release_id") if installed_release_id else None
                if binding_mode == "installed":
                    if not release_uuid:
                        raise ValueError("installed binding には Release が必要です")
                    release = await session.scalar(select(AppRelease).where(
                        AppRelease.id == release_uuid,
                        AppRelease.app_id == app.id,
                        AppRelease.status == "published",
                    ).limit(1))
                    if not release:
                        raise ValueError("published Release not found")
                else:
                    release_uuid = None
                binding = await session.scalar(select(ProjectApp).where(
                    ProjectApp.project_id == project_uuid,
                    ProjectApp.app_id == app.id,
                ).with_for_update().limit(1))
                if binding is None:
                    binding = ProjectApp(project_id=project_uuid, app_id=app.id, created_by=_ctx_user_id(runtime_context))
                    session.add(binding)
                binding.binding_mode = binding_mode
                binding.installed_release_id = release_uuid
                instance_path = get_app_instance_path(project_uuid, app.id, workspace_root=workspace_root)
                instance_existed = instance_path.exists()
                try:
                    ensure_app_instance(project_uuid, app.id, workspace_root=workspace_root)
                    await session.flush()
                    result = binding.to_dict()
                    await session.commit()
                except Exception:
                    await session.rollback()
                    if not instance_existed:
                        remove_app_instance(project_uuid, app.id, workspace_root=workspace_root)
                    raise
                return result

    async def unlink_app_from_project(project_id: str, app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="runner", require_enabled_binding=False) as (session, app, _permission):
            project_uuid = _uuid(project_id, "project_id")
            async with project_operation_lock(project_uuid, workspace_root=workspace_root):
                locked_project_id = await session.scalar(select(Project.id).where(
                    Project.id == project_uuid,
                    Project.deleted_at.is_(None),
                ).with_for_update())
                if locked_project_id is None:
                    raise PermissionError("Project not found")
                locked_app = await session.scalar(select(App).where(App.id == app.id).with_for_update().limit(1))
                if locked_app is None:
                    raise PermissionError("App not found")
                app = locked_app
                app_service = AppService()
                if not await app_service.project_access(
                    session,
                    project_id=project_uuid,
                    user_id=_ctx_user_id(runtime_context),
                    user_role=_ctx_user_role(runtime_context),
                ):
                    raise PermissionError("Projectへの権限がありません")
                if not await app_service.project_write_access(
                    session,
                    project_id=project_uuid,
                    user_id=_ctx_user_id(runtime_context),
                    user_role=_ctx_user_role(runtime_context),
                ):
                    raise PermissionError("ProjectのApp構成を変更する権限がありません")
                await app_service.require_permission(
                    session,
                    app,
                    user_id=_ctx_user_id(runtime_context),
                    required="runner",
                    user_role=_ctx_user_role(runtime_context),
                    project_id=project_uuid,
                )
                active_job = await session.scalar(select(AppJob.id).where(
                    AppJob.app_id == app.id,
                    AppJob.project_id == project_uuid,
                    AppJob.status.in_({"queued", "running"}),
                ).with_for_update().limit(1))
                if active_job is not None:
                    raise PermissionError("実行中または待機中のApp JobがあるためProjectから外せません")
                await session.execute(delete(ProjectApp).where(ProjectApp.project_id == project_uuid, ProjectApp.app_id == app.id))
                await session.commit()
                remove_app_instance(project_uuid, app.id, workspace_root=workspace_root)
            return {"success": True, "project_id": str(project_uuid), "app_id": str(app.id)}

    async def link_app_to_task(task_id: str, relation_type: str = "related", target_id: str = "", app_id: str = "") -> dict[str, Any]:
        async with _authorized_app(runtime_context, app_id or None, required="viewer") as (session, app, _permission):
            task_uuid = _uuid(task_id, "task_id")
            task = await session.scalar(select(Task).where(Task.id == task_uuid).limit(1))
            if not task:
                raise ValueError("Task not found")
            if task.archived_at is not None or task.deleted_at is not None:
                raise ValueError("Task not found")
            if relation_type not in {"develops", "fixes", "tests", "releases", "uses", "related"}:
                raise ValueError("relation_type が不正です")
            app_service = AppService()
            if not await app_service.project_access(
                session,
                project_id=task.project_id,
                user_id=_ctx_user_id(runtime_context),
                user_role=_ctx_user_role(runtime_context),
            ):
                raise PermissionError("TaskのProjectへの権限がありません")
            if not await app_service.project_write_access(
                session,
                project_id=task.project_id,
                user_id=_ctx_user_id(runtime_context),
                user_role=_ctx_user_role(runtime_context),
            ):
                raise PermissionError("TaskのProjectを変更する権限がありません")
            binding = await session.scalar(select(ProjectApp).where(
                ProjectApp.project_id == task.project_id,
                ProjectApp.app_id == app.id,
                ProjectApp.enabled.is_(True),
            ).limit(1))
            if binding is None:
                raise PermissionError("TaskのProjectではAppが有効化されていません")
            await app_service.require_permission(
                session,
                app,
                user_id=_ctx_user_id(runtime_context),
                required="viewer",
                user_role=_ctx_user_role(runtime_context),
                project_id=task.project_id,
            )
            target_uuid = _uuid(target_id, "target_id") if target_id else None
            if target_uuid:
                target = await session.scalar(select(AppTarget).where(
                    AppTarget.id == target_uuid,
                    AppTarget.app_id == app.id,
                ).limit(1))
                if not target:
                    raise ValueError("Target not found")
                if binding.binding_mode == "installed":
                    if binding.installed_release_id is None:
                        raise ValueError("固定保存版が選択されていません")
                    runtime_artifact = await session.scalar(select(AppArtifact.id).where(
                        AppArtifact.release_id == binding.installed_release_id,
                        AppArtifact.target_id == target.id,
                        AppArtifact.artifact_type == "runtime_bundle",
                    ).limit(1))
                    if runtime_artifact is None:
                        raise ValueError("Projectが固定しているReleaseにこのTargetの成果物がありません")
            link = await session.scalar(select(TaskAppLink).where(
                TaskAppLink.task_id == task.id,
                TaskAppLink.app_id == app.id,
                TaskAppLink.target_id == target_uuid,
                TaskAppLink.relation_type == relation_type,
            ).limit(1))
            if link is None:
                link = TaskAppLink(task_id=task.id, app_id=app.id, target_id=target_uuid, relation_type=relation_type, created_by=_ctx_user_id(runtime_context))
                session.add(link)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    link = await session.scalar(select(TaskAppLink).where(
                        TaskAppLink.task_id == task.id,
                        TaskAppLink.app_id == app.id,
                        TaskAppLink.target_id == target_uuid,
                        TaskAppLink.relation_type == relation_type,
                    ).limit(1))
                    if link is None:
                        raise
            return link.to_dict()

    common_app_id = ToolParam("app_id", "string", "App UUID。省略時は選択中App", required=False)
    return [
        _tool("create_app", "永続的なAppを作成し、App workspaceとREADME/Manifestを初期化する", create_app, [ToolParam("name", "string"), ToolParam("slug", "string", required=False, default=""), ToolParam("description", "string", required=False, default="")], risk="medium", side_effect="database", supports_parallel=False),
        _tool("list_apps", "権限のあるApp一覧を取得する", list_apps, []),
        _tool("get_app", "AppのメタデータとTargetを取得する", get_app, [common_app_id]),
        _tool("get_app_context", "選択中AppのREADME/Manifestを参照データとして取得する", get_app_context, [common_app_id, ToolParam("target_key", "string", required=False, default="")]),
        _tool("analyze_app_business", "AppのソースとREADMEから業務内容を分析し、Manifest/READMEの概要UI用メタデータを更新する", analyze_app_business, [common_app_id], risk="high", side_effect="filesystem,database", requires_approval=True, supports_parallel=False),
        _tool("list_app_files", "App workspace内のファイル一覧を取得する", list_app_files_tool, [common_app_id]),
        _tool("read_app_file", "App workspace内の安全な相対パスを読み取る", read_app_file, [ToolParam("path", "string"), common_app_id]),
        _tool("write_app_file", "App workspace内のファイルを更新しGit checkpointを作成する", write_app_file, [ToolParam("path", "string"), ToolParam("content", "string"), ToolParam("expected_sha256", "string", required=False, default=""), common_app_id], risk="high", side_effect="filesystem", requires_approval=True, supports_parallel=False),
        _tool("delete_app_file", "App workspace内のファイルを削除する", delete_app_file, [ToolParam("path", "string"), common_app_id], risk="high", side_effect="filesystem", requires_approval=True, supports_parallel=False),
        _tool("validate_app_manifest", "App Manifestを検証する", validate_app_manifest, [common_app_id]),
        _tool("update_app_manifest", "検証済みManifestを更新しTarget派生データを同期する", update_app_manifest, [ToolParam("content", "string"), ToolParam("expected_sha256", "string", required=False, default=""), common_app_id], risk="high", side_effect="filesystem", requires_approval=True, supports_parallel=False),
        _tool("app_git_status", "App Gitのclean/dirtyとrevisionを取得する", git_status, [common_app_id]),
        _tool("app_git_history", "App Gitのrevision履歴を取得する", git_history, [common_app_id, ToolParam("limit", "integer", required=False, default=20)]),
        _tool("app_git_diff", "App Gitの差分を取得する", git_diff, [common_app_id, ToolParam("rev_a", "string", required=False, default=""), ToolParam("rev_b", "string", required=False, default=""), ToolParam("path", "string", required=False, default="")]),
        _tool("app_git_restore", "App Gitのrevisionからファイルを復元する", git_restore, [ToolParam("path", "string"), ToolParam("revision", "string"), common_app_id], risk="high", side_effect="filesystem", requires_approval=True, supports_parallel=False),
        _tool("build_app_target", "Manifestのbuild commandをAppJobとして実行する", build_app_target, [ToolParam("target_key", "string"), ToolParam("input_json", "object", required=False, default={})], risk="high", side_effect="process", requires_approval=True, supports_parallel=False),
        _tool("test_app_target", "Manifestのtest commandをAppJobとして実行する", test_app_target, [ToolParam("target_key", "string"), ToolParam("input_json", "object", required=False, default={})], risk="high", side_effect="process", requires_approval=True, supports_parallel=False),
        _tool("run_app_target", "Manifestのrun commandをProject instance付きAppJobとして実行する", run_app_target, [ToolParam("target_key", "string"), ToolParam("input_json", "object", required=False, default={}), ToolParam("project_id", "string", required=False, default="")], risk="high", side_effect="process", requires_approval=True, supports_parallel=False),
        _tool("package_app_target", "Manifestのpackage commandをAppJobとして実行する", package_app_target, [ToolParam("target_key", "string"), ToolParam("input_json", "object", required=False, default={})], risk="high", side_effect="process", requires_approval=True, supports_parallel=False),
        _tool("stop_app_job", "実行中のAppJobをプロセスツリー単位で停止する", stop_app_job, [ToolParam("job_id", "string")], risk="high", side_effect="process", requires_approval=True, supports_parallel=False),
        _tool("read_app_job_logs", "AppJobのログを安全に参照する", read_app_job_logs, [ToolParam("job_id", "string")]),
        _tool("create_app_release", "cleanなApp revisionからReleaseとArtifactを作成する", create_app_release, [ToolParam("version", "string"), ToolParam("changelog", "string", required=False, default=""), common_app_id], risk="high", side_effect="database,filesystem", requires_approval=True, supports_parallel=False),
        _tool("export_app_release", "App ReleaseのArtifact情報を取得する", export_app_release, [ToolParam("release_id", "string"), common_app_id]),
        _tool("import_app_source_bundle", "App workspace内のSource Bundleを検証して取り込む", import_app_source_bundle, [ToolParam("source_bundle_path", "string"), common_app_id], risk="high", side_effect="filesystem", requires_approval=True, supports_parallel=False),
        _tool("fork_app", "選択中Appを独立した新AppとしてForkする", fork_app, [ToolParam("name", "string"), ToolParam("slug", "string", required=False, default=""), common_app_id], risk="high", side_effect="database,filesystem", requires_approval=True, supports_parallel=False),
        _tool("link_app_to_project", "AppをProjectへdevelopment/installed bindingとして追加する", link_app_to_project, [ToolParam("project_id", "string"), ToolParam("binding_mode", "string", enum=["development", "installed"], required=False, default="development"), ToolParam("installed_release_id", "string", required=False, default=""), common_app_id], risk="high", side_effect="database", requires_approval=True, supports_parallel=False),
        _tool("unlink_app_from_project", "ProjectからApp bindingだけを外す", unlink_app_from_project, [ToolParam("project_id", "string"), common_app_id], risk="high", side_effect="database", requires_approval=True, supports_parallel=False),
        _tool("link_app_to_task", "TaskへAppとrelation typeを登録する", link_app_to_task, [ToolParam("task_id", "string"), ToolParam("relation_type", "string", enum=["develops", "fixes", "tests", "releases", "uses", "related"], required=False, default="related"), ToolParam("target_id", "string", required=False, default=""), common_app_id], risk="medium", side_effect="database", supports_parallel=False),
    ]


__all__ = ["AppWriteTransaction", "abort_forked_app", "build_app_tool_definitions"]
