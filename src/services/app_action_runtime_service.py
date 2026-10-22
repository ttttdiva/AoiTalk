"""Release-bound App Action runtime and API orchestration.

This module is the narrow boundary between the Apps API and the durable
``AppRuntimeActivation``/``AppActionRun`` records.  It deliberately resolves
all executable data from a verified immutable Release Source Bundle.  The
workspace and the current Manifest are never used to execute an Action that
has already been presented to a client.

The execution worker is injected as a callable.  Persisting a run is always
completed before that callable is invoked; a transport failure therefore
leaves a durable status row that can be polled or reconciled without starting
the Action a second time.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import inspect
import json
import logging
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import and_, select

from ..memory.models import (
    App,
    AppArtifact,
    AppRelease,
    AppTarget,
    ProjectApp,
)
from .app_action_contract import (
    AppActionContractError,
    materialize_runtime_ui,
    parse_release_action_id,
    validate_action_contract,
    validate_json_schema_value,
)
from .app_action_run_service import (
    AppActionIdempotencyConflict as ExecutionIdempotencyConflict,
    AppActionRunError as ExecutionRunError,
    AppActionRunService,
    FileAwareAppActionTransport,
    ResolvedAction as ExecutionResolvedAction,
    canonical_json_hash,
)
from .app_action_file_service import (
    AppActionFileError,
    AppActionFileService,
)
from .app_operation_lock import app_operation_lock
from .app_service import AppAccessError, AppService, permission_at_least
from .app_storage import (
    AppStorageError,
    normalize_app_relative_path,
    resolve_app_artifact_file,
    verify_file_integrity,
)

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_FILE_ID_KEYS = {"file_id", "input_file_id", "output_file_id"}
class AppActionRuntimeError(RuntimeError):
    """Base error translated by the HTTP route into a stable response."""

    status_code = 400
    code = "app_action_runtime_error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class AppActionNotFound(AppActionRuntimeError):
    status_code = 404
    code = "not_found"


class AppActionForbidden(AppActionRuntimeError):
    status_code = 403
    code = "forbidden"


class AppActionConflict(AppActionRuntimeError):
    status_code = 409
    code = "conflict"


class AppActionValidationError(AppActionRuntimeError):
    status_code = 422
    code = "validation_error"


class AppActionUnavailable(AppActionRuntimeError):
    status_code = 409
    code = "release_unavailable"


class AppRuntimeNotConfigured(AppActionRuntimeError):
    """The App has no active immutable Action runtime yet."""

    status_code = 409
    code = "app_runtime_not_configured"


@dataclass(frozen=True)
class CatalogAction:
    """One Action copied from the immutable Release catalog."""

    action_key: str
    action_id: str
    definition: dict[str, Any]
    target: dict[str, Any]
    target_id: UUID | None
    action_spec_hash: str
    target_contract_hash: str


@dataclass(frozen=True)
class RuntimeCatalog:
    app: App
    release: AppRelease
    source_artifact: AppArtifact
    source_path: Path
    manifest: dict[str, Any]
    ui: dict[str, Any] | None
    actions: tuple[CatalogAction, ...]
    target: dict[str, Any]
    target_id: UUID | None
    permission: str = "viewer"
    actor_user_id: UUID | None = None
    actor_role: str | None = None

    @property
    def action_by_id(self) -> dict[str, CatalogAction]:
        return {item.action_id: item for item in self.actions}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _uuid(value: Any, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AppActionValidationError(f"{label} が不正です") from exc


def _safe_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _safe_action_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only fields permitted to cross the API/worker boundary."""
    execution = definition.get("execution")
    safe: dict[str, Any] = {
        "label": definition.get("label"),
        "description": definition.get("description"),
        "target_key": definition.get("target_key"),
        "input_schema": copy.deepcopy(definition.get("input_schema") or {}),
        "output_schema": copy.deepcopy(definition.get("output_schema") or {}),
        "execution_target_id": definition.get("execution_target_id"),
        "capabilities": copy.deepcopy(definition.get("capabilities") or []),
    }
    if isinstance(execution, Mapping):
        safe["execution"] = {
            key: copy.deepcopy(execution[key])
            for key in (
                "kind",
                "mode",
                "command_key",
                "saved_action_id",
                "sha256",
                "contract_hash",
            )
            if key in execution
        }
    return safe


def _public_status(status: Any) -> str:
    value = str(status or "unknown")
    if value in {
        "unknown",
        "queued",
        "dispatching",
        "accepted",
        "starting",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "stopping",
        "blocked",
    }:
        return value
    return "unknown"


class AppActionRuntimeService:
    """Resolve immutable App runtime declarations and durable Action Runs."""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        execution_service: AppActionRunService | None = None,
        execution_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.execution_service = execution_service
        self.execution_config = execution_config or {}

    @staticmethod
    def _run_model() -> Any:
        from ..memory.models import AppActionRun

        return AppActionRun

    @staticmethod
    def _activation_model() -> Any:
        from ..memory.models import AppRuntimeActivation

        return AppRuntimeActivation

    async def _load_app(self, session: Any, app_id: UUID | str) -> App:
        app_uuid = _uuid(app_id, "app_id")
        app = await session.scalar(select(App).where(App.id == app_uuid, App.archived_at.is_(None)).limit(1))
        if app is None:
            raise AppActionNotFound("App not found")
        return app

    async def authorize(
        self,
        session: Any,
        app: App | UUID | str,
        *,
        user_id: UUID | str,
        user_role: str | None = None,
        project_id: UUID | str | None = None,
        required: str = "viewer",
        require_binding: bool = True,
    ) -> tuple[App, str, ProjectApp | None]:
        """Resolve current grant and Project binding at invocation time."""
        app_row = app if isinstance(app, App) else await self._load_app(session, app)
        principal = _uuid(user_id, "user_id")
        project_uuid = _uuid(project_id, "project_id") if project_id is not None else None
        app_service = AppService(workspace_root=self.workspace_root)
        if project_uuid is not None and not await app_service.project_access(
            session,
            project_id=project_uuid,
            user_id=principal,
            user_role=user_role,
        ):
            raise AppActionForbidden("Projectへの権限がありません")
        binding: ProjectApp | None = None
        if project_uuid is not None:
            binding = await session.scalar(
                select(ProjectApp).where(
                    ProjectApp.project_id == project_uuid,
                    ProjectApp.app_id == app_row.id,
                ).limit(1)
            )
            if require_binding and (binding is None or not binding.enabled):
                raise AppActionForbidden("このProjectではAppが有効化されていません")
        try:
            permission = await app_service.require_permission(
                session,
                app_row,
                user_id=principal,
                user_role=user_role,
                required=required,
                project_id=project_uuid,
            )
        except AppAccessError as exc:
            raise AppActionForbidden(str(exc)) from exc
        if not permission_at_least(permission, required):
            raise AppActionForbidden("Appの権限が不足しています")
        return app_row, permission, binding

    async def _selected_release(
        self,
        session: Any,
        app: App,
        *,
        project_id: UUID | str | None,
        binding: ProjectApp | None = None,
    ) -> AppRelease:
        """Project installed pin wins; otherwise use explicit activation row."""
        project_uuid = _uuid(project_id, "project_id") if project_id is not None else None
        if project_uuid is not None:
            binding = binding or await session.scalar(
                select(ProjectApp).where(
                    ProjectApp.project_id == project_uuid,
                    ProjectApp.app_id == app.id,
                ).limit(1)
            )
            if binding is not None and binding.enabled and binding.binding_mode == "installed":
                if binding.installed_release_id is None:
                    raise AppRuntimeNotConfigured("Projectの固定Releaseが選択されていません")
                release = await session.scalar(
                    select(AppRelease).where(
                        AppRelease.id == binding.installed_release_id,
                        AppRelease.app_id == app.id,
                        AppRelease.status == "published",
                    ).limit(1)
                )
                if release is None:
                    raise AppActionUnavailable("Projectの固定Releaseを利用できません")
                return release
        activation_model = self._activation_model()
        activation = await session.scalar(
            select(activation_model).where(activation_model.app_id == app.id).limit(1)
        )
        release_id = getattr(activation, "release_id", None) if activation is not None else None
        if release_id is None:
            raise AppRuntimeNotConfigured("Appにactive Releaseが設定されていません")
        release = await session.scalar(
            select(AppRelease).where(
                AppRelease.id == release_id,
                AppRelease.app_id == app.id,
                AppRelease.status == "published",
            ).limit(1)
        )
        if release is None:
            raise AppActionUnavailable("active Releaseを利用できません")
        return release

    @staticmethod
    def _archive_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
        members: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            try:
                normalized = normalize_app_relative_path(info.filename)
            except AppStorageError:
                continue
            # A case-folded duplicate would make a Release depend on the host
            # filesystem's case sensitivity.
            key = normalized.casefold()
            if key in members:
                raise AppActionUnavailable("Source Bundleに重複パスがあります")
            members[key] = info
        return members

    def _read_bundle(self, archive_path: Path, *, release: AppRelease) -> tuple[dict[str, Any], str, str]:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = self._archive_members(archive)
                def read(name: str) -> bytes:
                    info = members.get(name.casefold())
                    if info is None:
                        raise KeyError(name)
                    return archive.read(info)
                manifest_text = read("aoitalk.app.yaml").decode("utf-8")
                readme_text = read("README.md").decode("utf-8") if "readme.md".casefold() in members else ""
        except (KeyError, UnicodeDecodeError, OSError, zipfile.BadZipFile) as exc:
            raise AppActionUnavailable("Release Source Bundleを読み込めません") from exc
        manifest_hash = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
        readme_hash = hashlib.sha256(readme_text.encode("utf-8")).hexdigest()
        if str(release.manifest_hash or "").casefold() != manifest_hash.casefold():
            raise AppActionUnavailable("Release Manifestのhash検証に失敗しました")
        if str(release.readme_hash or "").casefold() != readme_hash.casefold():
            raise AppActionUnavailable("Release READMEのhash検証に失敗しました")
        try:
            from .app_manifest_service import parse_manifest_text

            manifest = parse_manifest_text(manifest_text)
        except Exception as exc:
            raise AppActionUnavailable("Release Manifestを検証できません") from exc
        return manifest, manifest_text, readme_text

    @staticmethod
    def _hydrate_action_schema_from_archive(
        manifest: dict[str, Any], archive_path: Path,
    ) -> dict[str, Any]:
        """Resolve path-based input/output schemas without exposing the path."""
        result = copy.deepcopy(manifest)
        target_map = result.get("targets") if isinstance(result.get("targets"), dict) else {}
        try:
            archive = zipfile.ZipFile(archive_path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise AppActionUnavailable("Release Source Bundleを読み込めません") from exc
        with archive:
            members = AppActionRuntimeService._archive_members(archive)
            def resolve(value: Any, *, label: str) -> Any:
                if not isinstance(value, str):
                    return value
                try:
                    normalized = normalize_app_relative_path(value)
                    info = members.get(normalized.casefold())
                    if info is None:
                        raise KeyError(normalized)
                    parsed = json.loads(archive.read(info).decode("utf-8"))
                except (AppStorageError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AppActionUnavailable(f"{label} の参照先を読み込めません") from exc
                return parsed
            for target_key, target in list(target_map.items()):
                if isinstance(target, dict) and isinstance(target.get("input_schema"), str):
                    target["input_schema"] = resolve(target["input_schema"], label=f"targets.{target_key}.input_schema")
            actions = result.get("actions") if isinstance(result.get("actions"), dict) else {}
            for action_key, action in list(actions.items()):
                if isinstance(action, dict):
                    for schema_key in ("input_schema", "output_schema"):
                        if isinstance(action.get(schema_key), str):
                            action[schema_key] = resolve(action[schema_key], label=f"actions.{action_key}.{schema_key}")
        return result

    async def _source_artifact(
        self, session: Any, app: App, release: AppRelease,
    ) -> tuple[AppArtifact, Path]:
        artifact = await session.scalar(
            select(AppArtifact).where(
                AppArtifact.release_id == release.id,
                AppArtifact.artifact_type == "source_bundle",
            ).limit(1)
        )
        if artifact is None:
            raise AppActionUnavailable("Release Source Bundleがありません")
        try:
            path = resolve_app_artifact_file(
                app.id,
                release.id,
                Path(str(artifact.filename)).name,
                workspace_root=self.workspace_root,
            )
            verify_file_integrity(
                path,
                expected_sha256=artifact.sha256,
                expected_size_bytes=artifact.size_bytes,
            )
        except (AppStorageError, OSError) as exc:
            raise AppActionUnavailable(f"Release Source Bundleの検証に失敗しました: {exc}") from exc
        return artifact, path

    async def _catalog_for_release(
        self, session: Any, app: App, release: AppRelease,
    ) -> RuntimeCatalog:
        artifact, source_path = await self._source_artifact(session, app, release)
        # A published Release is a source/runtime pair.  Verify every
        # immutable Runtime Bundle before exposing its Action catalog so a
        # stale or replaced artifact cannot be used after activation.
        runtime_artifacts = list(
            (
                await session.scalars(
                    select(AppArtifact).where(
                        AppArtifact.release_id == release.id,
                        AppArtifact.artifact_type == "runtime_bundle",
                    )
                )
            ).all()
        )
        for runtime_artifact in runtime_artifacts:
            try:
                runtime_path = resolve_app_artifact_file(
                    app.id,
                    release.id,
                    Path(str(runtime_artifact.filename)).name,
                    workspace_root=self.workspace_root,
                )
                verify_file_integrity(
                    runtime_path,
                    expected_sha256=runtime_artifact.sha256,
                    expected_size_bytes=runtime_artifact.size_bytes,
                )
            except (AppStorageError, OSError) as exc:
                raise AppActionUnavailable(
                    "Release Runtime Bundleの検証に失敗しました"
                ) from exc
        manifest, _manifest_text, _readme_text = self._read_bundle(source_path, release=release)
        manifest = self._hydrate_action_schema_from_archive(manifest, source_path)
        contract = validate_action_contract(manifest, strict=False)
        if not contract.valid:
            raise AppActionUnavailable("Release Action/UI contractを検証できません: " + "; ".join(contract.errors))
        target_map = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
        db_targets = {
            target.target_key: target
            for target in (await session.scalars(select(AppTarget).where(AppTarget.app_id == app.id))).all()
        }
        runtime_by_target_id = {
            str(item.target_id): item
            for item in runtime_artifacts
            if getattr(item, "target_id", None) is not None
        }

        def runtime_metadata(target_model: AppTarget | None) -> dict[str, Any]:
            item = runtime_by_target_id.get(str(target_model.id)) if target_model is not None else None
            if item is None:
                return {}
            return {
                "runtime_bundle_sha256": str(item.sha256),
                "runtime_bundle_size_bytes": int(item.size_bytes or 0),
            }

        action_items: list[CatalogAction] = []
        for action_key, raw in contract.actions.items():
            definition = _safe_action_definition(raw)
            target_key = str(definition.get("target_key") or "")
            snapshot = target_map.get(target_key) if isinstance(target_map, dict) else {}
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            # Capabilities are a target declaration.  They are copied into the
            # Action descriptor but never accepted from a run request.
            definition["capabilities"] = [
                str(item) for item in (snapshot.get("capabilities") or [])
                if isinstance(item, str) and item.strip()
            ]
            target_model = db_targets.get(target_key)
            target = {
                "id": str(target_model.id) if target_model is not None else f"release:{target_key}",
                "target_key": target_key,
                "display_name": snapshot.get("display_name") or target_key,
                "surface": snapshot.get("surface") or (getattr(target_model, "surface", None) or "headless"),
                "runtime": snapshot.get("runtime") or (getattr(target_model, "runtime", None) or "executable"),
                "execution_host": snapshot.get("execution_host") or (getattr(target_model, "execution_host", None) or "download_only"),
                # The executable path is retained only in the server-side
                # catalog.  Public runtime responses use an empty value.
                "entrypoint": snapshot.get("entrypoint") or (getattr(target_model, "entrypoint", None) or ""),
            }
            target.update(runtime_metadata(target_model))
            action_id = f"{release.id}:{action_key}"
            safe_spec = _safe_action_definition(definition)
            # ``AppTarget.id`` is mutable derived metadata.  It must not be
            # part of the immutable Release contract: deleting/recreating a
            # Target row must not change the action fingerprint for the same
            # published Release.  The release snapshot is the authority here.
            immutable_target = {
                key: value
                for key, value in target.items()
                if key != "id"
            }
            action_items.append(
                CatalogAction(
                    action_key=str(action_key),
                    action_id=action_id,
                    definition=definition,
                    target=target,
                    target_id=target_model.id if target_model is not None else None,
                    action_spec_hash=_digest(safe_spec),
                    target_contract_hash=_digest({"target": immutable_target, "action": safe_spec}),
                )
            )
        ui = materialize_runtime_ui(contract.ui, release_id=release.id)
        selected_target_key = None
        if isinstance(ui, Mapping):
            selected_target_key = ui.get("target_key")
        if not selected_target_key and action_items:
            selected_target_key = action_items[0].definition.get("target_key")
        if not selected_target_key:
            selected_target_key = app.default_target_key
        if not selected_target_key and isinstance(target_map, dict) and target_map:
            selected_target_key = next(iter(target_map))
        selected = next((item for item in action_items if item.target.get("target_key") == selected_target_key), None)
        selected_target_model = db_targets.get(str(selected_target_key))
        selected_snapshot = target_map.get(selected_target_key) if isinstance(target_map, dict) else {}
        selected_snapshot = selected_snapshot if isinstance(selected_snapshot, dict) else {}
        selected_target = selected.target if selected is not None else {
            "id": str(selected_target_model.id) if selected_target_model is not None else f"release:{selected_target_key or 'default'}",
            "target_key": str(selected_target_key or "default"),
            "display_name": selected_snapshot.get("display_name") or str(selected_target_key or "default"),
            "surface": selected_snapshot.get("surface") or (getattr(selected_target_model, "surface", None) or "headless"),
            "runtime": selected_snapshot.get("runtime") or (getattr(selected_target_model, "runtime", None) or "executable"),
            "execution_host": selected_snapshot.get("execution_host") or (getattr(selected_target_model, "execution_host", None) or "download_only"),
            "entrypoint": selected_snapshot.get("entrypoint") or (getattr(selected_target_model, "entrypoint", None) or ""),
        }
        selected_target.update(runtime_metadata(selected_target_model))
        return RuntimeCatalog(
            app=app,
            release=release,
            source_artifact=artifact,
            source_path=source_path,
            manifest=manifest,
            ui=ui,
            actions=tuple(action_items),
            target=selected_target,
            target_id=selected_target_model.id if selected_target_model is not None else (selected.target_id if selected else None),
        )

    async def get_runtime(
        self,
        session: Any,
        app_id: UUID | str,
        *,
        user_id: UUID | str,
        user_role: str | None = None,
        project_id: UUID | str | None = None,
    ) -> RuntimeCatalog:
        app, _permission, binding = await self.authorize(
            session,
            app_id,
            user_id=user_id,
            user_role=user_role,
            project_id=project_id,
            required="viewer",
        )
        release = await self._selected_release(session, app, project_id=project_id, binding=binding)
        catalog = await self._catalog_for_release(session, app, release)
        return dataclasses.replace(
            catalog,
            permission=_permission,
            actor_user_id=_uuid(user_id, "user_id"),
            actor_role=user_role,
        )

    async def activate_release(
        self,
        session: Any,
        app_id: UUID | str,
        release_id: UUID | str,
        *,
        user_id: UUID | str,
        user_role: str | None = None,
    ) -> RuntimeCatalog:
        app, _permission, _binding = await self.authorize(
            session,
            app_id,
            user_id=user_id,
            user_role=user_role,
            required="maintainer",
            require_binding=False,
        )
        release_uuid = _uuid(release_id, "release_id")
        async with app_operation_lock(app.id, workspace_root=self.workspace_root):
            await session.scalar(select(App.id).where(App.id == app.id).with_for_update())
            release = await session.scalar(
                select(AppRelease).where(
                    AppRelease.id == release_uuid,
                    AppRelease.app_id == app.id,
                    AppRelease.status == "published",
                ).limit(1)
            )
            if release is None:
                raise AppActionNotFound("Release not found")
            catalog = await self._catalog_for_release(session, app, release)
            catalog = dataclasses.replace(
                catalog,
                permission=_permission,
                actor_user_id=_uuid(user_id, "user_id"),
                actor_role=user_role,
            )
            activation_model = self._activation_model()
            activation = await session.scalar(
                select(activation_model).where(activation_model.app_id == app.id).with_for_update().limit(1)
            )
            if activation is None:
                activation = activation_model(
                    app_id=app.id,
                    release_id=release.id,
                    manifest_hash=release.manifest_hash,
                    activated_by=_uuid(user_id, "user_id"),
                )
                session.add(activation)
            else:
                activation.release_id = release.id
                activation.manifest_hash = release.manifest_hash
                activation.activated_by = _uuid(user_id, "user_id")
                activation.activated_at = datetime.utcnow()
            await session.flush()
            return catalog

    async def runtime_availability(self, catalog: RuntimeCatalog) -> dict[str, Any]:
        """Resolve runner/target availability from server-owned state."""
        unavailable = {
            action.action_id: {
                "available": False,
                "unavailable_reason": "実行Targetを確認できません",
            }
            for action in catalog.actions
        }
        if not permission_at_least(catalog.permission, "runner"):
            for state in unavailable.values():
                state["unavailable_reason"] = "runner権限が必要です"
            return {"available": False, "reason": "runner権限が必要です", "actions": unavailable}
        execution = self.execution_service
        transport = getattr(execution, "transport", None) if execution is not None else None
        if transport is None:
            for state in unavailable.values():
                state["unavailable_reason"] = "Action execution transportが利用できません"
            return {
                "available": False,
                "reason": "Action execution transportが利用できません",
                "actions": unavailable,
            }
        availability_call = getattr(transport, "availability", None)
        if not callable(availability_call):
            for state in unavailable.values():
                state["unavailable_reason"] = "Action execution targetのavailabilityを確認できません"
            return {
                "available": False,
                "reason": "Action execution targetのavailabilityを確認できません",
                "actions": unavailable,
            }
        action_states: dict[str, dict[str, Any]] = {}
        for action in catalog.actions:
            try:
                machine_id, device_id = await self._resolve_execution_identity(
                    catalog,
                    action,
                    machine_id=None,
                    device_id=None,
                )
                result = await availability_call(
                    owner_user_id=str(catalog.app.owner_user_id),
                    actor_user_id=str(catalog.actor_user_id or catalog.app.owner_user_id),
                    actor_user_role=catalog.actor_role,
                    device_id=device_id,
                    machine_id=machine_id,
                    execution_target_id=str(action.definition.get("execution_target_id") or ""),
                    execution_target_kind=(
                        "server"
                        if str(action.target.get("execution_host") or "") in {"aoitalk", "server"}
                        else "pc_bridge"
                    ),
                    execution_mode=str(
                        (action.definition.get("execution") or {}).get("mode") or "finite"
                    ),
                    saved_action_id=str((action.definition.get("execution") or {}).get("saved_action_id") or ""),
                    sha256=str((action.definition.get("execution") or {}).get("sha256") or ""),
                    contract_hash=self._execution_contract_hash(action),
                )
                online = bool(result.get("online")) if isinstance(result, Mapping) else bool(result)
                action_states[action.action_id] = {
                    "available": online,
                    "unavailable_reason": None if online else "実行Targetがofflineです",
                }
            except AppActionRuntimeError as exc:
                action_states[action.action_id] = {
                    "available": False,
                    "unavailable_reason": str(exc),
                }
            except Exception:
                action_states[action.action_id] = {
                    "available": False,
                    "unavailable_reason": "実行Targetを確認できません",
                }
        available = any(item.get("available") for item in action_states.values())
        return {
            "available": available,
            "reason": None if available else "利用可能なAction execution targetがありません",
            "actions": action_states,
        }

    async def _validate_file_refs(
        self,
        session: Any,
        value: Any,
        *,
        app_id: UUID,
        user_id: UUID,
        project_id: UUID | None,
        action_id: str | None = None,
    ) -> None:
        """Require file references to be opaque, owned workspace rows.

        Apps do not accept arbitrary paths or URLs in Action input.  Older
        databases may not have the optional workspace inventory table, so a
        missing table is treated as a failed ownership check rather than a
        reason to trust a caller supplied path.
        """
        def has_file_reference(node: Any) -> bool:
            if isinstance(node, dict):
                if any(str(key).casefold() == "file_id" for key in node):
                    return True
                return any(has_file_reference(child) for child in node.values())
            if isinstance(node, list):
                return any(has_file_reference(child) for child in node)
            return False

        def has_opaque_file_reference(node: Any) -> bool:
            if isinstance(node, dict):
                for key, child in node.items():
                    if str(key).casefold() == "file_id" and str(child).strip().startswith("af_"):
                        return True
                    if has_opaque_file_reference(child):
                        return True
            elif isinstance(node, list):
                return any(has_opaque_file_reference(child) for child in node)
            return False

        def has_legacy_file_reference(node: Any) -> bool:
            if isinstance(node, dict):
                if any(str(key).casefold().endswith("_file_id") for key in node):
                    return True
                return any(has_legacy_file_reference(child) for child in node.values())
            if isinstance(node, list):
                return any(has_legacy_file_reference(child) for child in node)
            return False

        def has_naked_opaque_id(node: Any, *, in_file_ref: bool = False) -> bool:
            if isinstance(node, str):
                return node.strip().startswith("af_") and not in_file_ref
            if isinstance(node, dict):
                for key, child in node.items():
                    if has_naked_opaque_id(
                        child,
                        in_file_ref=in_file_ref or str(key).casefold() == "file_id",
                    ):
                        return True
            elif isinstance(node, list):
                return any(has_naked_opaque_id(child, in_file_ref=in_file_ref) for child in node)
            return False

        # Action-file uploads use only opaque af_* ids and their sidecars are
        # owned by AppActionFileService.  Validate metadata, owner, project,
        # expiry, bytes and hash before a Run is created.  The old UUID
        # WorkspaceFileEntry path is deliberately not accepted here: it did
        # not bind bytes to this App/Action and could create a metadata-only
        # reference that the execution owner could not materialize.
        if action_id and has_file_reference(value):
            file_service = AppActionFileService(workspace_root=self.workspace_root)
            try:
                file_service.validate_input_references(
                    value,
                    app_id=app_id,
                    owner_user_id=user_id,
                    action_id=action_id,
                    project_id=project_id,
                )
                return
            except AppActionFileError as exc:
                # af_* ids must never fall through to a workspace path/UUID
                # lookup.  This keeps a forged file id fail-closed.
                if has_opaque_file_reference(value):
                    if exc.status_code == 403:
                        raise AppActionForbidden(str(exc)) from exc
                    raise AppActionValidationError(str(exc)) from exc
                raise AppActionForbidden(str(exc)) from exc
        if action_id and has_legacy_file_reference(value):
            raise AppActionForbidden("Action inputではopaque file_idのみ指定できます")
        if action_id and has_naked_opaque_id(value):
            raise AppActionForbidden("Action inputではfile_id objectでopaque fileを指定してください")

    async def _action_for_run(
        self,
        session: Any,
        app_id: UUID | str,
        action_id: str,
        *,
        user_id: UUID | str,
        user_role: str | None,
        project_id: UUID | str | None,
    ) -> tuple[RuntimeCatalog, CatalogAction, UUID, ProjectApp | None]:
        try:
            release_id, action_key = parse_release_action_id(action_id)
        except AppActionContractError as exc:
            raise AppActionValidationError(str(exc)) from exc
        app, _permission, binding = await self.authorize(
            session,
            app_id,
            user_id=user_id,
            user_role=user_role,
            project_id=project_id,
            required="runner",
        )
        selected = await self._selected_release(session, app, project_id=project_id, binding=binding)
        if selected.id != release_id:
            raise AppActionConflict("Actionが現在のactive Releaseに属していません")
        catalog = await self._catalog_for_release(session, app, selected)
        action = next((item for item in catalog.actions if item.action_id == action_id and item.action_key == action_key), None)
        if action is None:
            raise AppActionNotFound("Action not found")
        return catalog, action, _uuid(user_id, "user_id"), binding

    @staticmethod
    def _execution_spec(
        catalog: RuntimeCatalog,
        action: CatalogAction,
        *,
        machine_id: str,
        device_id: str,
        target_contract_hash: str | None = None,
    ) -> ExecutionResolvedAction:
        execution = action.definition.get("execution") if isinstance(action.definition.get("execution"), Mapping) else {}
        execution_host = str(action.target.get("execution_host") or "")
        execution_kind = str(execution.get("kind") or "target_command")
        target_kind = "server" if execution_host in {"aoitalk", "server"} else "pc_bridge"
        return ExecutionResolvedAction(
            app_id=catalog.app.id,
            owner_user_id=catalog.app.owner_user_id,
            release_id=catalog.release.id,
            action_ref=action.action_id,
            action_key=action.action_key,
            target_key=str(action.target.get("target_key") or action.definition.get("target_key") or ""),
            action_spec_hash=action.action_spec_hash,
            execution_target_id=str(action.definition.get("execution_target_id") or ""),
            execution_target_kind=target_kind,
            execution_mode=str(execution.get("mode") or "finite"),
            machine_id=str(machine_id),
            device_id=str(device_id),
            target_contract_hash=target_contract_hash or action.target_contract_hash,
            output_schema_json=action.definition.get("output_schema") or {},
            saved_action_id=execution.get("saved_action_id"),
            target_sha256=execution.get("sha256"),
            probe_json={},
        )

    async def _resolve_execution_identity(
        self,
        catalog: RuntimeCatalog,
        action: CatalogAction,
        *,
        machine_id: str | None,
        device_id: str | None,
    ) -> tuple[str, str]:
        """Resolve host identity from server-owned target state."""
        execution = action.definition.get("execution") if isinstance(action.definition.get("execution"), Mapping) else {}
        execution_host = str(action.target.get("execution_host") or "")
        execution_kind = str(execution.get("kind") or "target_command")
        if machine_id or device_id:
            raise AppActionForbidden("execution target identityはserver側で解決されます")
        config: Any = self.execution_config
        if isinstance(config, Mapping):
            config = config.get("apps", config)
            if isinstance(config, Mapping):
                config = config.get("actions", config)
            if not isinstance(config, Mapping) or config.get("enabled") is not True:
                raise AppActionUnavailable("App Action executionがserver設定で有効化されていません")
            targets = config.get("execution_targets", {})
        else:
            targets = {}
        target_id = str(action.definition.get("execution_target_id") or "")
        target = targets.get(target_id) if isinstance(targets, Mapping) else None
        if not isinstance(target, Mapping) or target.get("enabled") is not True:
            raise AppActionUnavailable("Action execution targetがserver設定にありません")
        allowed_apps = target.get("allowed_app_ids") or target.get("app_ids") or []
        if allowed_apps and str(catalog.app.id) not in {str(item) for item in allowed_apps}:
            raise AppActionForbidden("Action execution targetはこのAppに許可されていません")
        configured_owner = str(target.get("owner_user_id") or "").strip()
        if not configured_owner:
            raise AppActionUnavailable("Action execution targetのownerが未設定です")
        if configured_owner != str(catalog.app.owner_user_id):
            raise AppActionForbidden("Action execution targetのownerがApp ownerと一致しません")
        configured_machine = str(target.get("machine_id") or "").strip()
        configured_device = str(target.get("device_id") or "").strip()
        if not configured_machine or not configured_device:
            raise AppActionUnavailable("Action execution targetのmachine/deviceが未設定です")
        if machine_id or device_id:
            raise AppActionForbidden("execution target identityはserver側で解決されます")
        return configured_machine, configured_device

    def _execution_target_config(self, action: CatalogAction) -> Mapping[str, Any] | None:
        """Return one server-owned execution target declaration."""
        config: Any = self.execution_config
        if not isinstance(config, Mapping):
            return None
        config = config.get("apps", config)
        if isinstance(config, Mapping):
            config = config.get("actions", config)
        targets = config.get("execution_targets", {}) if isinstance(config, Mapping) else {}
        target_id = str(action.definition.get("execution_target_id") or "")
        target = targets.get(target_id) if isinstance(targets, Mapping) else None
        return target if isinstance(target, Mapping) else None

    def _execution_contract_hash(
        self,
        action: CatalogAction,
    ) -> str:
        """Resolve the operator-approved target contract fingerprint."""
        execution = (
            action.definition.get("execution")
            if isinstance(action.definition.get("execution"), Mapping)
            else {}
        )
        declared = execution.get("contract_hash")
        target = self._execution_target_config(action)
        configured = (
            target.get("contract_hash") or target.get("target_contract_hash")
            if target is not None
            else None
        )
        for value, label in (
            (declared, "execution.contract_hash"),
            (configured, "execution_target.contract_hash"),
        ):
            if value is not None and not _SHA256_RE.fullmatch(str(value)):
                raise AppActionUnavailable(f"{label} が不正です")
        if declared and configured and str(declared).casefold() != str(configured).casefold():
            raise AppActionConflict(
                "Release Actionとserver execution targetのcontract hashが一致しません"
            )
        execution_kind = str(execution.get("kind") or "target_command")
        if execution_kind == "pc_saved_action" and not configured and not declared:
            raise AppActionUnavailable("PC execution targetのcontract hashが未設定です")
        return str(configured or declared or action.target_contract_hash).lower()

    async def _existing_idempotent_run(
        self,
        session: Any,
        *,
        app_id: UUID | str,
        user_id: UUID | str,
        idempotency_key: str,
    ) -> Any | None:
        """Load the idempotency row before resolving the current Release.

        A retry may arrive after an active Release pointer changed.  The
        existing durable run is the authority for that retry, so checking it
        first prevents a harmless replay from becoming an activation conflict
        and prevents a different action from being dispatched under the same
        key.
        """
        run_model = self._run_model()
        return await session.scalar(
            select(run_model)
            .where(
                run_model.app_id == _uuid(app_id, "app_id"),
                run_model.started_by == _uuid(user_id, "user_id"),
                run_model.idempotency_key == idempotency_key,
            )
            .with_for_update()
            .limit(1)
        )

    @staticmethod
    def _same_project(left: Any, right: UUID | None) -> bool:
        return (getattr(left, "project_id", None) is None and right is None) or (
            getattr(left, "project_id", None) is not None
            and right is not None
            and str(getattr(left, "project_id")) == str(right)
        )

    @staticmethod
    def _file_ref_metadata_map(value: Any) -> dict[str, dict[str, Any]]:
        """Index persisted normalized file metadata without touching bytes."""
        result: dict[str, dict[str, Any]] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                file_id = str(node.get("file_id") or "").strip()
                if file_id:
                    result[file_id] = {
                        str(key): copy.deepcopy(child)
                        for key, child in node.items()
                    }
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)
        return result

    @classmethod
    def _normalize_replay_input(cls, value: Any, persisted: Any) -> Any:
        """Project a retry using stored metadata; never recheck expired bytes."""
        metadata = cls._file_ref_metadata_map(persisted)

        def normalize(node: Any) -> Any:
            if isinstance(node, dict):
                file_id = str(node.get("file_id") or "").strip()
                if file_id and file_id in metadata:
                    return copy.deepcopy(metadata[file_id])
                return {str(key): normalize(child) for key, child in node.items()}
            if isinstance(node, list):
                return [normalize(child) for child in node]
            return node

        return normalize(value)

    async def _normalize_new_input(
        self,
        value: dict[str, Any],
        *,
        app_id: UUID,
        owner_user_id: UUID,
        action_id: str,
        project_id: UUID | None,
    ) -> dict[str, Any]:
        """Validate/project new file refs for the idempotency hash."""
        file_service = getattr(self.execution_service, "file_service", None)
        if file_service is None:
            file_service = AppActionFileService(workspace_root=self.workspace_root)
        if not any(
            isinstance(node, str) and node.startswith("af_")
            for node in self._walk_json_values(value)
        ):
            return value
        try:
            refs = await asyncio.to_thread(
                file_service.validate_input_references,
                value,
                app_id=app_id,
                owner_user_id=owner_user_id,
                action_id=action_id,
                project_id=project_id,
            )
            normalized = file_service.normalize_input_references(value, refs)
        except Exception as exc:
            if isinstance(exc, AppActionFileError):
                raise AppActionForbidden(str(exc)) from exc
            raise AppActionValidationError(str(exc)) from exc
        return normalized

    @staticmethod
    def _walk_json_values(value: Any):
        if isinstance(value, dict):
            for child in value.values():
                yield from AppActionRuntimeService._walk_json_values(child)
        elif isinstance(value, list):
            for child in value:
                yield from AppActionRuntimeService._walk_json_values(child)
        else:
            yield value

    async def _create_execution_run(
        self,
        session: Any,
        *,
        spec: ExecutionResolvedAction,
        input_json: dict[str, Any],
        idempotency_key: str,
        principal: UUID,
        project_id: UUID | None,
        agent_run_id: UUID | str | None,
    ) -> tuple[Any, bool, Any]:
        """Call the execution owner, never constructing AppActionRun here."""
        execution = self.execution_service
        if execution is None:
            raise AppActionUnavailable("App Action execution serviceが設定されていません")
        result = await execution.create_run(
            spec=spec,
            started_by=principal,
            owner_user_id=spec.owner_user_id,
            input_json=input_json,
            idempotency_key=idempotency_key,
            project_id=project_id,
            agent_run_id=agent_run_id,
            session=session,
        )
        run = getattr(result, "run", result)
        replayed = bool(getattr(result, "replayed", False))
        if run is None:
            raise AppActionUnavailable("App Action execution serviceがRunを返しませんでした")
        return run, replayed, execution

    async def create_run(
        self,
        session: Any,
        app_id: UUID | str,
        action_id: str,
        input_json: dict[str, Any],
        *,
        idempotency_key: str,
        user_id: UUID | str,
        user_role: str | None = None,
        project_id: UUID | str | None = None,
        machine_id: str | None = None,
        device_id: str | None = None,
        agent_run_id: UUID | str | None = None,
    ) -> Any:
        if not isinstance(input_json, dict):
            raise AppActionValidationError("input はobjectで指定してください")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 160:
            raise AppActionValidationError("Idempotency-Key は1〜160文字で指定してください")
        principal = _uuid(user_id, "user_id")
        project_uuid = _uuid(project_id, "project_id") if project_id is not None else None

        # Authorize the current caller/project before consulting a previous
        # run, then compare the opaque request identity without consulting the
        # active Release pointer.  This is the replay fence across activation
        # changes; a different project/action/input remains a 409 conflict.
        await self.authorize(
            session,
            app_id,
            user_id=principal,
            user_role=user_role,
            project_id=project_uuid,
            required="runner",
        )
        existing = await self._existing_idempotent_run(
            session,
            app_id=app_id,
            user_id=principal,
            idempotency_key=key,
        )
        if existing is not None:
            # Compare a retry against the persisted normalized projection.
            # This intentionally does not touch the file sidecar or bytes:
            # an expired upload must not invalidate an already durable Run.
            replay_input = self._normalize_replay_input(
                input_json,
                getattr(existing, "input_json", {}) or {},
            )
            try:
                replay_hash = canonical_json_hash(replay_input)
            except ExecutionRunError as exc:
                raise AppActionValidationError(str(exc)) from exc
            if (
                str(getattr(existing, "action_ref", "")) != str(action_id)
                or str(getattr(existing, "input_hash", "")) != replay_hash
                or not self._same_project(existing, project_uuid)
            ):
                raise AppActionConflict("Idempotency-Keyは別Action/input/projectに既に割り当てられています")
            return existing

        catalog, action, principal, binding = await self._action_for_run(
            session,
            app_id,
            action_id,
            user_id=user_id,
            user_role=user_role,
            project_id=project_id,
        )
        try:
            validate_json_schema_value(input_json, action.definition.get("input_schema") or {"type": "object"})
        except AppActionContractError as exc:
            raise AppActionValidationError(str(exc)) from exc
        await self._validate_file_refs(
            session,
            input_json,
            app_id=catalog.app.id,
            user_id=principal,
            project_id=project_uuid,
            action_id=action.action_id,
        )
        normalized_input = await self._normalize_new_input(
            input_json,
            app_id=catalog.app.id,
            owner_user_id=principal,
            action_id=action.action_id,
            project_id=project_uuid,
        )
        target = action.target
        execution = action.definition.get("execution") if isinstance(action.definition.get("execution"), Mapping) else {}
        execution_host = str(target.get("execution_host") or "")
        execution_kind = str(execution.get("kind") or "target_command")
        target_kind = "server" if execution_host in {"aoitalk", "server"} else "pc_bridge"
        if execution_kind == "target_command" and target_kind != "server":
            raise AppActionForbidden("Target Commandはserver実行Targetでのみ利用できます")
        if execution_kind == "pc_saved_action" and target_kind != "pc_bridge":
            raise AppActionForbidden("PC保存ActionはPC Bridge Targetでのみ利用できます")
        machine_id, device_id = await self._resolve_execution_identity(
            catalog,
            action,
            machine_id=machine_id,
            device_id=device_id,
        )
        if not _SAFE_ID_RE.fullmatch(str(machine_id)) or not _SAFE_ID_RE.fullmatch(str(device_id)):
            raise AppActionValidationError("server側で解決したexecution identityが不正です")
        spec = self._execution_spec(
            catalog,
            action,
            machine_id=str(machine_id),
            device_id=str(device_id),
            target_contract_hash=self._execution_contract_hash(action),
        )
        try:
            spec.validate()
        except Exception as exc:
            raise AppActionValidationError(str(exc)) from exc
        try:
            run, replayed, execution = await self._create_execution_run(
                session,
                spec=spec,
                input_json=normalized_input,
                idempotency_key=key,
                principal=principal,
                project_id=project_uuid,
                agent_run_id=agent_run_id,
            )
        except ExecutionIdempotencyConflict as exc:
            raise AppActionConflict(str(exc)) from exc
        except ExecutionRunError as exc:
            raise AppActionValidationError(str(exc)) from exc
        # Dispatch belongs to the execution service.  A replay only returns
        # the durable row; it must never issue a second start.
        if not replayed:
            dispatch_run = getattr(execution, "dispatch_run", None)
            if not callable(dispatch_run):
                raise AppActionUnavailable("App Action execution serviceのdispatch_runが設定されていません")
            if not bool(getattr(execution, "has_transport", getattr(execution, "transport", None) is not None)):
                raise AppActionUnavailable("App Action transportが設定されていません")
            run = await dispatch_run(getattr(run, "id", None)) or run
        return run

    async def list_runs(
        self, session: Any, app_id: UUID | str, *, user_id: UUID | str,
        user_role: str | None = None, project_id: UUID | str | None = None,
        idempotency_key: str | None = None, limit: int = 50,
    ) -> list[Any]:
        app, _permission, _binding = await self.authorize(
            session, app_id, user_id=user_id, user_role=user_role,
            project_id=project_id, required="runner",
        )
        run_model = self._run_model()
        clauses = [run_model.app_id == app.id, run_model.started_by == _uuid(user_id, "user_id")]
        # A request without project_id addresses only App-scoped runs.  Do
        # not let it enumerate a Project-bound run (or reconcile it under a
        # weaker App-only ACL).
        clauses.append(
            run_model.project_id == _uuid(project_id, "project_id")
            if project_id is not None
            else run_model.project_id.is_(None)
        )
        if idempotency_key:
            clauses.append(run_model.idempotency_key == str(idempotency_key))
        runs = list((await session.scalars(select(run_model).where(and_(*clauses)).order_by(run_model.created_at.desc()).limit(max(1, min(int(limit), 200))))).all())
        if self.execution_service is not None:
            for index, run in enumerate(runs):
                if str(getattr(run, "status", "")) in {"succeeded", "failed", "cancelled", "blocked"}:
                    continue
                runs[index] = await self.reconcile_run(run)
        return runs

    async def get_run(
        self, session: Any, app_id: UUID | str, run_id: UUID | str, *, user_id: UUID | str,
        user_role: str | None = None, project_id: UUID | str | None = None,
    ) -> Any:
        app, _permission, _binding = await self.authorize(
            session, app_id, user_id=user_id, user_role=user_role,
            project_id=project_id, required="runner",
        )
        run_model = self._run_model()
        run = await session.scalar(select(run_model).where(run_model.id == _uuid(run_id, "run_id"), run_model.app_id == app.id).limit(1))
        if run is None or run.started_by != _uuid(user_id, "user_id"):
            raise AppActionNotFound("Run not found")
        expected_project = _uuid(project_id, "project_id") if project_id is not None else None
        if not self._same_project(run, expected_project):
            raise AppActionNotFound("Run not found")
        if self.execution_service is not None and str(getattr(run, "status", "")) not in {"succeeded", "failed", "cancelled", "blocked"}:
            run = await self.reconcile_run(run)
        return run

    async def reconcile_run(self, run: Any) -> Any:
        """Refresh one persisted run by status only; never start it."""
        if self.execution_service is None:
            return run
        reconcile = getattr(self.execution_service, "reconcile_run", None)
        if not callable(reconcile):
            return run
        refreshed = await reconcile(getattr(run, "id", None))
        # ``reconcile_run`` uses a separate execution-service session.  Do
        # not copy its values into the caller's stale ORM instance: that can
        # mark fields dirty and later flush an old snapshot over newer state.
        # Return the freshly loaded row and make list/get callers use it.
        return refreshed if refreshed is not None else run

    @staticmethod
    def _typed_logs(response: Mapping[str, Any] | None, *, fallback_text: str = "") -> dict[str, Any]:
        """Normalize PC/server log evidence into the closed API shape."""
        raw = response if isinstance(response, Mapping) else {}
        raw_entries = raw.get("entries")
        entries: list[dict[str, Any]] = []
        if isinstance(raw_entries, list):
            for index, item in enumerate(raw_entries):
                if not isinstance(item, Mapping):
                    continue
                try:
                    sequence = max(0, int(item.get("sequence", index + 1)))
                except (TypeError, ValueError):
                    sequence = index + 1
                created = item.get("created_at")
                if isinstance(created, (int, float)):
                    try:
                        created = datetime.fromtimestamp(float(created), tz=timezone.utc)
                    except (OverflowError, OSError, ValueError):
                        created = None
                entries.append({
                    "sequence": sequence,
                    "stream": str(item.get("stream") or "supervisor")[:32],
                    "line": str(item.get("line") or "")[:16_384],
                    "created_at": created if isinstance(created, datetime) else None,
                })
        data = str(raw.get("data") or raw.get("logs") or fallback_text or "")
        try:
            cursor = max(0, int(raw.get("cursor", 0) or 0))
        except (TypeError, ValueError):
            cursor = 0
        try:
            next_cursor = max(0, int(raw.get("next_cursor", cursor) or cursor))
        except (TypeError, ValueError):
            next_cursor = cursor
        try:
            latest_cursor = max(0, int(raw.get("latest_cursor", next_cursor) or next_cursor))
        except (TypeError, ValueError):
            latest_cursor = next_cursor
        return {
            "cursor": cursor,
            "next_cursor": next_cursor,
            "latest_cursor": latest_cursor,
            "eof": raw.get("eof") if isinstance(raw.get("eof"), bool) else None,
            "entries": entries,
            "data": data or None,
            "status": str(raw.get("status"))[:32] if raw.get("status") is not None else None,
        }

    async def read_logs(self, session: Any, run: Any, *, app: App) -> dict[str, Any]:
        if str(getattr(run, "execution_target_kind", "")) == "pc_bridge" and self.execution_service is not None:
            get_logs = getattr(self.execution_service, "get_logs", None)
            if callable(get_logs):
                try:
                    response = await get_logs(getattr(run, "id", None))
                except Exception as exc:
                    raise AppActionUnavailable("PC BridgeのRun logを取得できません") from exc
                if isinstance(response, Mapping):
                    return self._typed_logs(response)
                return self._typed_logs(None)
        raw = str(getattr(run, "log_path", "") or "")
        if not raw:
            return self._typed_logs(None)
        path = Path(raw).resolve()
        # Logs are worker-owned run files.  Keep the root narrow even when a
        # legacy row contains an absolute path.
        allowed_roots = []
        try:
            from .app_storage import get_app_instance_path, get_app_workspace_path
            allowed_roots = [
                get_app_workspace_path(app.id, workspace_root=self.workspace_root).resolve(),
            ]
            if getattr(run, "project_id", None):
                allowed_roots.append(get_app_instance_path(run.project_id, app.id, workspace_root=self.workspace_root).resolve())
        except Exception:
            allowed_roots = []
        if not any(path == root or root in path.parents for root in allowed_roots):
            raise AppActionForbidden("Run log path is outside App scope")
        try:
            text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            lines = text.splitlines()
            return self._typed_logs(
                {
                    "cursor": len(text.encode("utf-8")),
                    "next_cursor": len(text.encode("utf-8")),
                    "latest_cursor": len(text.encode("utf-8")),
                    "eof": True,
                    "entries": [
                        {"sequence": index, "stream": "server", "line": line}
                        for index, line in enumerate(lines, start=1)
                    ],
                    "status": getattr(run, "status", None),
                },
            )
        except OSError as exc:
            raise AppActionUnavailable("Run logを読み込めません") from exc

    async def stop_run(
        self,
        session: Any,
        app_id: UUID | str,
        run_id: UUID | str,
        *,
        user_id: UUID | str,
        user_role: str | None = None,
        project_id: UUID | str | None = None,
    ) -> Any:
        run = await self.get_run(
            session,
            app_id,
            run_id,
            user_id=user_id,
            user_role=user_role,
            project_id=project_id,
        )
        if self.execution_service is None:
            raise AppActionUnavailable("App Action execution serviceが設定されていません")
        stop = getattr(self.execution_service, "stop_run", None)
        if not callable(stop):
            raise AppActionUnavailable("App Action execution serviceのstop_runが設定されていません")
        return await stop(getattr(run, "id", None)) or run

    @staticmethod
    def result_files(run: Any) -> list[dict[str, Any]]:
        result = getattr(run, "result_json", {})
        raw = result.get("files") if isinstance(result, dict) else None
        if not isinstance(raw, list):
            return []
        entries: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("id") or item.get("file_id") or "").strip()
            filename = str(item.get("filename") or item.get("name") or "").strip()
            digest = str(item.get("sha256") or "").strip().lower()
            if not file_id or not filename or not _SHA256_RE.fullmatch(digest):
                continue
            if not _SAFE_ID_RE.fullmatch(file_id):
                continue
            entries.append({
                "id": file_id,
                "filename": Path(filename).name,
                "content_type": str(item.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"),
                "size_bytes": max(0, int(item.get("size_bytes") or 0)),
                "sha256": digest,
                "path": str(item.get("path") or ""),
            })
        return entries

    async def recover_action_runs(self, session: Any, *, app_id: UUID | str | None = None) -> list[Any]:
        """Return uncertain runs for status reconciliation without starting them."""
        run_model = self._run_model()
        clauses = [run_model.dispatch_state.in_({"sent", "unknown"})]
        if app_id is not None:
            clauses.append(run_model.app_id == _uuid(app_id, "app_id"))
        return list((await session.scalars(select(run_model).where(and_(*clauses)).order_by(run_model.created_at))).all())


def runtime_release_payload(release: AppRelease) -> dict[str, Any]:
    return {
        "id": str(release.id),
        "version": release.version,
        "git_revision": release.git_revision,
        "manifest_hash": release.manifest_hash,
        "readme_hash": release.readme_hash,
        "status": release.status,
        "created_at": _safe_datetime(release.created_at),
    }


def runtime_target_payload(target: Mapping[str, Any], *, hide_entrypoint: bool = True) -> dict[str, Any]:
    payload = {
        "id": str(target.get("id") or ""),
        "target_key": str(target.get("target_key") or ""),
        "display_name": target.get("display_name"),
        "surface": target.get("surface") or "headless",
        "runtime": target.get("runtime") or "executable",
        "execution_host": target.get("execution_host") or "download_only",
        "entrypoint": "" if hide_entrypoint else str(target.get("entrypoint") or ""),
    }
    if target.get("runtime_bundle_sha256"):
        payload["runtime_bundle_sha256"] = str(target["runtime_bundle_sha256"])
    if target.get("runtime_bundle_size_bytes") is not None:
        payload["runtime_bundle_size_bytes"] = int(target["runtime_bundle_size_bytes"] or 0)
    return payload


def runtime_action_payload(action: CatalogAction) -> dict[str, Any]:
    payload = _safe_action_definition(action.definition)
    payload.update({
        "action_id": action.action_id,
        "label": payload.get("label") or action.action_key,
        "target_key": payload.get("target_key") or action.target.get("target_key"),
        "input_schema": copy.deepcopy(payload.get("input_schema") or {}),
        "output_schema": copy.deepcopy(payload.get("output_schema") or {}),
        "execution_target_id": payload.get("execution_target_id") or "",
        # Availability is an observed transport fact, never a Manifest
        # default.  The runtime route replaces these values with the current
        # preflight result when it can check the target.
        "available": False,
        "unavailable_reason": "実行Targetを確認できません",
    })
    return payload


_PRIVATE_RESULT_KEYS = re.compile(
    r"(?:^|_)(?:command|path|cwd|workspace|log|secret|token|credential)(?:$|_)",
    re.IGNORECASE,
)


def _safe_public_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_public_result(child)
            for key, child in value.items()
            if not _PRIVATE_RESULT_KEYS.search(str(key))
        }
    if isinstance(value, list):
        return [_safe_public_result(child) for child in value]
    return value


def run_payload(run: Any, *, target_id: str | None = None) -> dict[str, Any]:
    action_id = str(getattr(run, "action_ref", "") or "")
    result = getattr(run, "result_json", {})
    result = _safe_public_result(copy.deepcopy(result))
    error_code = str(getattr(run, "error_code", "") or "")
    payload = {
        "id": str(getattr(run, "id", "")),
        "app_id": str(getattr(run, "app_id", "")),
        "action_id": action_id,
        "release_id": str(getattr(run, "release_id", "")),
        "target_id": str(target_id or getattr(run, "target_key", "")),
        "project_id": str(getattr(run, "project_id", "")) if getattr(run, "project_id", None) else None,
        "status": _public_status(getattr(run, "status", "unknown")),
        "mode": str(getattr(run, "execution_mode", "finite") or "finite"),
        "readiness_status": str(getattr(run, "readiness_status", "unknown") or "unknown"),
        "process_status": str(getattr(run, "process_status", "unknown") or "unknown"),
        "exit_code": getattr(run, "exit_code", None),
        "idempotency_key": str(getattr(run, "idempotency_key", "")) or None,
        "created_at": _safe_datetime(getattr(run, "created_at", None)),
        "started_at": _safe_datetime(getattr(run, "started_at", None) or getattr(run, "accepted_at", None)),
        "finished_at": _safe_datetime(getattr(run, "ended_at", None)),
        "result": result,
        "error": {"code": error_code} if error_code else None,
        "links": {"logs": f"/api/apps/{getattr(run, 'app_id', '')}/runs/{getattr(run, 'id', '')}/logs"},
    }
    return payload


def build_app_action_execution_service(
    db_manager: Any,
    *,
    config: Mapping[str, Any] | None = None,
    workspace_root: str | Path | None = None,
    pc_transport: Any | None = None,
    server_transport: Any | None = None,
) -> AppActionRunService:
    """Compose the canonical PC/server Action execution service.

    HTTP, Agent tools and startup recovery share this routing boundary.  A
    PC-only service in one caller would silently make server Target Actions
    unavailable or route them to the wrong device.
    """
    from .app_action_run_service import AppActionTransportError

    if pc_transport is None:
        from .pc_bridge_service import get_pc_bridge_app_action_transport

        pc_transport = get_pc_bridge_app_action_transport()
    transports: dict[str, Any] = {"pc_bridge": pc_transport}
    if server_transport is None:
        try:
            from .app_action_server_transport import get_app_action_server_transport

            server_transport = get_app_action_server_transport(
                db_manager=db_manager,
                config=config if isinstance(config, Mapping) else {},
                workspace_root=workspace_root,
            )
        except Exception:
            server_transport = None
    if server_transport is not None:
        transports["server"] = server_transport
    # File references are verified/materialized once at the start fence, then
    # transferred as hash-checked chunks by the selected target transport.
    # This wrapper is outside the idempotency/reconciliation state machine, so
    # replayed or unknown runs never call it.
    file_service_for_execution = None
    try:
        file_service_for_execution = AppActionFileService(workspace_root=workspace_root)
        transports = {
            kind: FileAwareAppActionTransport(transport, file_service_for_execution)
            for kind, transport in transports.items()
        }
    except Exception:
        # File-free Actions remain usable when the optional file store cannot
        # initialize; a file-bearing Action will fail closed at dispatch.
        pass
    execution = AppActionRunService(
        db_manager,
        transports=transports,
        file_service=file_service_for_execution,
    )

    async def _preflight(run: Any) -> Mapping[str, Any]:
        transport = execution._transport_for_run(run)
        if transport is None:
            return {"allowed": False, "error_code": "app_action_preflight_unavailable"}
        availability = getattr(transport, "availability", None)
        if not callable(availability):
            return {"allowed": False, "error_code": "app_action_preflight_unavailable"}
        try:
            result = await availability(
                owner_user_id=execution._transport_owner(run),
                actor_user_id=str(getattr(run, "started_by", "") or ""),
                device_id=str(getattr(run, "device_id", "")),
                machine_id=str(getattr(run, "machine_id", "")),
                execution_target_id=str(getattr(run, "execution_target_id", "")),
                execution_target_kind=str(getattr(run, "execution_target_kind", "")),
                execution_mode=str(getattr(run, "execution_mode", "finite")),
                saved_action_id=getattr(run, "saved_action_id", None),
                sha256=getattr(run, "target_sha256", None),
                contract_hash=getattr(run, "target_contract_hash", None),
                mode=str(getattr(run, "execution_mode", "finite")),
                input_sha256=getattr(run, "input_hash", None),
            )
        except AppActionTransportError:
            return {"allowed": False, "error_code": "app_action_preflight_unavailable"}
        except Exception:
            return {"allowed": False, "error_code": "app_action_preflight_unavailable"}
        if isinstance(result, Mapping):
            online = bool(result.get("online", result.get("available", False)))
            return {
                **dict(result),
                "allowed": online,
                "error_code": None if online else str(result.get("error_code") or "app_action_target_unavailable"),
            }
        online = bool(result)
        return {"allowed": online, "error_code": None if online else "app_action_target_unavailable"}

    execution.preflight = _preflight
    return execution


create_app_action_execution_service = build_app_action_execution_service


__all__ = [
    "AppActionConflict",
    "AppActionForbidden",
    "AppActionNotFound",
    "AppActionRuntimeError",
    "AppActionRuntimeService",
    "AppActionUnavailable",
    "AppActionValidationError",
    "AppRuntimeNotConfigured",
    "CatalogAction",
    "RuntimeCatalog",
    "build_app_action_execution_service",
    "create_app_action_execution_service",
    "run_payload",
    "runtime_action_payload",
    "runtime_release_payload",
    "runtime_target_payload",
]
