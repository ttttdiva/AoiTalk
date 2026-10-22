"""HTTP transport for App Action input and run-owned output files.

The regular Action route owns release selection and run dispatch.  This module
owns only byte transport: an input upload is bound to an owner/App/Action
scope before a run exists, while output files are bound to one durable run.
Every response contains opaque metadata and every download resolves the blob
through :class:`AppActionFileService`; caller supplied paths and URLs are
never accepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from ....memory.models import AppActionRun
from ....services.app_action_file_service import (
    ActionFileMetadata,
    AppActionFileError,
    AppActionFileService,
)
from ....services.app_action_runtime_service import AppActionRuntimeError
from ...schemas.app_actions import AppRunFileResponse, AppRunFileUploadResponse
from ._shared import AppRouterContext


def _app_uuid(value: str, label: str = "app_id") -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} が不正です") from exc


def _metadata_response(metadata: ActionFileMetadata, *, app_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    if metadata.kind == "output":
        resolved_run_id = str(run_id or metadata.run_id or "")
        return {
            "run_id": resolved_run_id,
            "artifact_id": metadata.file_id,
            "filename": metadata.filename,
            "content_type": metadata.content_type,
            "size_bytes": metadata.size_bytes,
            "sha256": metadata.sha256,
            "download_url": (
                f"/api/apps/{app_id}/runs/{resolved_run_id}/files/{metadata.file_id}/download"
                if app_id and resolved_run_id
                else metadata.download_url or ""
            ),
        }
    return {
        "id": metadata.file_id,
        "artifact_id": None,
        "filename": metadata.filename,
        "content_type": metadata.content_type,
        "size_bytes": metadata.size_bytes,
        "sha256": metadata.sha256,
        "direction": "input",
        "download_url": metadata.download_url,
    }


def _file_route_error(exc: AppActionFileError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


def register_app_action_file_routes(
    router: APIRouter,
    ctx: AppRouterContext,
    *,
    workspace_root: str | Path | None = None,
    action_service: Any | None = None,
    file_service: AppActionFileService | None = None,
) -> AppActionFileService:
    """Register Action file endpoints and return the storage service.

    ``action_service`` is the already-configured immutable runtime service.
    Passing that instance keeps release/ACL decisions on one owner.  The
    optional fallback is useful for focused route tests; production callers
    should pass the same instance used by ``action_routes``.
    """

    files = file_service or AppActionFileService(workspace_root=workspace_root)
    get_db_manager = ctx.get_db_manager
    require_auth_dependency = ctx.require_auth_dependency
    current_user = ctx.current_user
    require_app = ctx.require_app

    async def _session() -> Any:
        return await get_db_manager().get_session()

    def _identity(user: dict[str, Any]) -> tuple[UUID, str | None]:
        try:
            return UUID(str(user.get("id"))), user.get("role")
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=401, detail="認証済みuser_idがありません") from exc

    async def _authorize(
        session: Any,
        app_id: UUID,
        user: dict[str, Any],
        *,
        project_id: str | None,
        required: str = "runner",
    ) -> Any:
        user_id, user_role = _identity(user)
        if action_service is not None and callable(getattr(action_service, "authorize", None)):
            result = await action_service.authorize(
                session,
                app_id,
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
                required=required,
            )
            return result[0] if isinstance(result, tuple) else result
        # Focused tests and legacy callers can supply the existing App auth
        # helper.  It still enforces AppGrant/ProjectApp ACLs.
        return (await require_app(
            session,
            str(app_id),
            user,
            project_id=ctx.uuid(project_id, "project_id") if project_id else None,
            required=required,
        ))[0]

    async def _load_run(
        session: Any,
        app_id: UUID,
        run_id: str,
        user: dict[str, Any],
        *,
        project_id: str | None,
    ) -> Any:
        """Load one run after ACL and exact project scope checks."""
        app = await _authorize(session, app_id, user, project_id=project_id, required="runner")
        actor_id, _role = _identity(user)
        try:
            run_uuid = UUID(str(run_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        run = await session.scalar(
            select(AppActionRun)
            .where(AppActionRun.id == run_uuid, AppActionRun.app_id == app_id)
            .limit(1)
        )
        # A Project-bound run must always be addressed with that same Project;
        # accepting an omitted query parameter would turn an App id into a
        # cross-Project lookup oracle.
        requested_project = ctx.uuid(project_id, "project_id") if project_id else None
        # Output files are private to the starter until an explicit sharing
        # contract exists.  App runner permission alone must not turn a run
        # id into an output oracle for another user.  Also validate the
        # immutable execution owner recorded on the run against the App row.
        try:
            starter_id = UUID(str(run.started_by)) if run is not None else None
            owner_id = UUID(str(run.owner_user_id)) if run is not None else None
            app_owner_id = UUID(str(app.owner_user_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if (
            run is None
            or run.project_id != requested_project
            or starter_id != actor_id
            or owner_id != app_owner_id
        ):
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    async def _authorize_action_upload(
        session: Any,
        app_id: UUID,
        action_id: str,
        user: dict[str, Any],
        *,
        project_id: str | None,
    ) -> None:
        await _authorize(session, app_id, user, project_id=project_id, required="runner")
        # Resolve the release-qualified action through the same runtime owner
        # when available.  This rejects stale/foreign actions before bytes are
        # persisted and is also where Action input schema/expiry hooks attach.
        resolver = getattr(action_service, "_action_for_run", None) if action_service is not None else None
        if callable(resolver):
            user_id, user_role = _identity(user)
            await resolver(
                session,
                app_id,
                action_id,
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
            )

    async def _upload_input(
        app_id: str,
        action_id: str,
        request: Request,
        file: UploadFile,
        *,
        project_id: str | None,
        expected_sha256: str | None,
        expires_in_seconds: int | None,
    ) -> dict[str, Any]:
        user = await current_user(request)
        user_id, _role = _identity(user)
        session = await _session()
        try:
            app_uuid = _app_uuid(app_id)
            await _authorize_action_upload(session, app_uuid, action_id, user, project_id=project_id)
            metadata = await files.store_input_file(
                file,
                app_id=app_uuid,
                owner_user_id=user_id,
                action_id=action_id,
                project_id=project_id,
                expected_sha256=expected_sha256,
                ttl_seconds=expires_in_seconds,
            )
            return _metadata_response(metadata)
        except (AppActionFileError, AppActionRuntimeError) as exc:
            if isinstance(exc, AppActionFileError):
                raise _file_route_error(exc) from exc
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        finally:
            await session.close()

    @router.post(
        "/api/apps/{app_id}/actions/{action_id}/files",
        response_model=AppRunFileUploadResponse,
        status_code=201,
        operation_id="upload_app_action_input_file",
    )
    async def upload_app_action_input_file(
        app_id: str,
        action_id: str,
        request: Request,
        file: UploadFile = File(...),
        project_id: str | None = Query(None),
        expected_sha256: str | None = Header(None, alias="X-File-SHA256"),
        expires_in_seconds: int | None = Query(None, ge=1, le=7 * 24 * 60 * 60),
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        return await _upload_input(
            app_id,
            action_id,
            request,
            file,
            project_id=project_id,
            expected_sha256=expected_sha256,
            expires_in_seconds=expires_in_seconds,
        )

    @router.get(
        "/api/apps/{app_id}/runs/{run_id}/files",
        response_model=list[AppRunFileResponse],
        operation_id="list_app_action_run_files",
    )
    async def list_app_action_run_files(
        app_id: str,
        run_id: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ) -> list[dict[str, Any]]:
        user = await current_user(request)
        session = await _session()
        try:
            app_uuid = _app_uuid(app_id)
            run = await _load_run(session, app_uuid, run_id, user, project_id=project_id)
            actor, _role = _identity(user)
            rows = files.list_output_files(
                run.id,
                app_id=app_uuid,
                owner_user_id=run.owner_user_id or actor,
                project_id=run.project_id,
                action_id=run.action_ref,
            )
            return [_metadata_response(item, app_id=app_id, run_id=str(run.id)) for item in rows]
        except AppActionFileError as exc:
            raise _file_route_error(exc) from exc
        finally:
            await session.close()

    @router.get(
        "/api/apps/{app_id}/runs/{run_id}/files/{file_id}",
        response_model=AppRunFileResponse,
        operation_id="get_app_action_run_file",
    )
    async def get_app_action_run_file(
        app_id: str,
        run_id: str,
        file_id: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ) -> dict[str, Any]:
        user = await current_user(request)
        session = await _session()
        try:
            app_uuid = _app_uuid(app_id)
            run = await _load_run(session, app_uuid, run_id, user, project_id=project_id)
            actor, _role = _identity(user)
            metadata, _path = files.get_output_file(
                file_id,
                run_id=run.id,
                app_id=app_uuid,
                owner_user_id=run.owner_user_id or actor,
                project_id=run.project_id,
                action_id=run.action_ref,
            )
            return _metadata_response(metadata, app_id=app_id, run_id=str(run.id))
        except AppActionFileError as exc:
            raise _file_route_error(exc) from exc
        finally:
            await session.close()

    @router.get(
        "/api/apps/{app_id}/runs/{run_id}/files/{file_id}/download",
        operation_id="download_app_action_run_file",
    )
    async def download_app_action_run_file(
        app_id: str,
        run_id: str,
        file_id: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ) -> Response:
        user = await current_user(request)
        session = await _session()
        try:
            app_uuid = _app_uuid(app_id)
            run = await _load_run(session, app_uuid, run_id, user, project_id=project_id)
            actor, _role = _identity(user)
            if request.headers.get("x-aoitalk-download-preflight") == "1":
                metadata, path = files.get_output_file_metadata(
                    file_id,
                    run_id=run.id,
                    app_id=app_uuid,
                    owner_user_id=run.owner_user_id or actor,
                    project_id=run.project_id,
                    action_id=run.action_ref,
                )
                attachment = FileResponse(
                    path,
                    media_type=metadata.content_type,
                    filename=metadata.filename,
                )
                return Response(
                    content=b"",
                    media_type=metadata.content_type,
                    headers={
                        "Content-Disposition": attachment.headers["content-disposition"],
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            metadata, path = files.get_output_file(
                file_id,
                run_id=run.id,
                app_id=app_uuid,
                owner_user_id=run.owner_user_id or actor,
                project_id=run.project_id,
                action_id=run.action_ref,
            )
            return FileResponse(path, media_type=metadata.content_type, filename=metadata.filename)
        except AppActionFileError as exc:
            raise _file_route_error(exc) from exc
        finally:
            await session.close()

    return files


__all__ = [
    "register_app_action_file_routes",
]
