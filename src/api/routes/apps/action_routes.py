"""HTTP routes for immutable App Runtime, Actions and Action Runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select

from ....memory.models import App, AppRuntimeActivation as AppRuntimeActivationModel
from ....services.app_action_runtime_service import (
    AppActionConflict,
    AppActionForbidden,
    AppActionNotFound,
    AppActionRuntimeError,
    AppActionRuntimeService,
    AppActionUnavailable,
    AppActionValidationError,
    run_payload,
    runtime_action_payload,
    runtime_release_payload,
    runtime_target_payload,
)
from ...schemas.app_actions import (
    AppActionRunRequest,
    AppActionRunResponse,
    AppRunListResponse,
    AppRunLogsResponse,
    AppRuntimeResponse,
)
from ._shared import AppRouterContext


def _runtime_payload(
    catalog: Any,
    *,
    availability: dict[str, Any] | None = None,
    latest_runs: list[dict[str, Any]] | None = None,
    activation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the public runtime projection from a verified catalog."""
    raw_availability = availability or {
        "available": False,
        "reason": "実行Targetを確認できません",
    }
    # Per-Action availability belongs on each AppActionDescriptor.  The
    # top-level AppRuntimeAvailability schema intentionally contains only the
    # aggregate state, so do not leak the internal ``actions`` map here.
    public_availability = {
        "available": bool(raw_availability.get("available", False)),
        "reason": raw_availability.get("reason"),
    }

    def _action_availability(action: Any) -> dict[str, Any]:
        states = raw_availability.get("actions", {})
        state = states.get(action.action_id, {}) if isinstance(states, dict) else {}
        if not isinstance(state, dict):
            return {}
        normalized = dict(state)
        if "reason" in normalized and "unavailable_reason" not in normalized:
            normalized["unavailable_reason"] = normalized.pop("reason")
        return normalized

    return {
        "app_id": str(catalog.app.id),
        "release": runtime_release_payload(catalog.release)
        | {
            # The source bundle metadata is public integrity metadata, while
            # its contents remain server-owned.  Newer schema revisions expose
            # this object; old clients safely ignore the extra key.
            "source_bundle_sha256": str(catalog.source_artifact.sha256),
            "source_bundle_size_bytes": int(catalog.source_artifact.size_bytes or 0),
        },
        "target": runtime_target_payload(catalog.target, hide_entrypoint=True),
        "ui": catalog.ui,
        "actions": [
            runtime_action_payload(action)
            | _action_availability(action)
            for action in catalog.actions
        ],
        "availability": public_availability,
        "latest_runs": latest_runs or [],
        "activation": activation,
    }


def _route_error(exc: AppActionRuntimeError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


def _run_response(run: Any, *, target_id: str | None = None) -> dict[str, Any]:
    return {"run": run_payload(run, target_id=target_id)}


def _app_id(value: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="app_id が不正です") from exc


def register_app_action_routes(
    router: APIRouter,
    ctx: AppRouterContext,
    *,
    workspace_root: str | Path | None = None,
    execution_service: Any | None = None,
    execution_config: dict[str, Any] | None = None,
    action_service: AppActionRuntimeService | None = None,
) -> AppActionRuntimeService:
    """Register App Runtime routes onto the existing Apps router.

    ``AppRouterContext`` remains unchanged for the existing grant split.  The
    Action service receives the workspace and execution owner as independent
    arguments so its dependency boundary remains explicit.
    """

    service = action_service or AppActionRuntimeService(
        workspace_root=workspace_root,
        execution_service=execution_service,
        execution_config=execution_config,
    )
    get_db_manager = ctx.get_db_manager
    require_auth_dependency = ctx.require_auth_dependency
    current_user = ctx.current_user

    async def _session():
        return await get_db_manager().get_session()

    async def _activation(session: Any, app_id: UUID) -> dict[str, Any] | None:
        row = await session.scalar(
            select(AppRuntimeActivationModel)
            .where(AppRuntimeActivationModel.app_id == app_id)
            .limit(1)
        )
        return row.to_dict() if row is not None else None

    def _identity(user: dict[str, Any]) -> tuple[UUID, str | None]:
        try:
            return UUID(str(user.get("id"))), user.get("role")
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=401, detail="認証済みuser_idがありません") from exc

    async def _catalog(
        app_id: str,
        request: Request,
        *,
        project_id: str | None,
        required: str = "viewer",
    ) -> tuple[Any, Any, Any]:
        user = await current_user(request)
        user_id, user_role = _identity(user)
        session = await _session()
        try:
            if required == "viewer":
                catalog = await service.get_runtime(
                    session,
                    _app_id(app_id),
                    user_id=user_id,
                    user_role=user_role,
                    project_id=project_id,
                )
            else:
                app, _permission, _binding = await service.authorize(
                    session,
                    _app_id(app_id),
                    user_id=user_id,
                    user_role=user_role,
                    project_id=project_id,
                    required=required,
                )
                catalog = await service.get_runtime(
                    session,
                    app,
                    user_id=user_id,
                    user_role=user_role,
                    project_id=project_id,
                )
            return user, session, catalog
        except AppActionRuntimeError:
            await session.close()
            raise

    @router.get(
        "/api/apps/{app_id}/runtime",
        response_model=AppRuntimeResponse,
        operation_id="get_app_runtime",
    )
    async def get_app_runtime(
        app_id: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        user_id, user_role = _identity(user)
        session = await _session()
        try:
            catalog = await service.get_runtime(
                session,
                _app_id(app_id),
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
            )
            availability = await service.runtime_availability(catalog)
            latest_runs: list[dict[str, Any]] = []
            if catalog.permission in {"runner", "developer", "maintainer", "admin"}:
                latest_runs = [run_payload(run) for run in await service.list_runs(
                    session,
                    _app_id(app_id),
                    user_id=user_id,
                    user_role=user_role,
                    project_id=project_id,
                    limit=8,
                )]
            return _runtime_payload(
                catalog,
                availability=availability,
                latest_runs=latest_runs,
                activation=await _activation(session, _app_id(app_id)),
            )
        except AppActionRuntimeError as exc:
            raise _route_error(exc) from exc
        finally:
            await session.close()

    @router.post(
        "/api/apps/{app_id}/releases/{release_id}/activate",
        response_model=AppRuntimeResponse,
        operation_id="activate_app_release",
    )
    async def activate_app_release(
        app_id: str,
        release_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        user_id, user_role = _identity(user)
        session = await _session()
        try:
            catalog = await service.activate_release(
                session,
                _app_id(app_id),
                release_id,
                user_id=user_id,
                user_role=user_role,
            )
            await session.commit()
            return _runtime_payload(
                catalog,
                availability=await service.runtime_availability(catalog),
                activation=await _activation(session, _app_id(app_id)),
            )
        except AppActionRuntimeError as exc:
            await session.rollback()
            raise _route_error(exc) from exc
        finally:
            await session.close()

    @router.post(
        "/api/apps/{app_id}/actions/{action_id}/runs",
        response_model=AppActionRunResponse,
        status_code=202,
        operation_id="create_app_action_run",
    )
    async def create_app_action_run(
        app_id: str,
        action_id: str,
        payload: AppActionRunRequest,
        request: Request,
        project_id: str | None = Query(None),
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        _: None = Depends(require_auth_dependency),
    ):
        key = idempotency_key
        user = await current_user(request)
        user_id, user_role = _identity(user)
        session = await _session()
        try:
            run = await service.create_run(
                session,
                _app_id(app_id),
                action_id,
                payload.input,
                idempotency_key=key,
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
            )
            return _run_response(run)
        except AppActionRuntimeError as exc:
            raise _route_error(exc) from exc
        finally:
            await session.close()

    @router.get(
        "/api/apps/{app_id}/runs",
        response_model=AppRunListResponse,
        operation_id="list_app_action_runs",
    )
    async def list_app_action_runs(
        app_id: str,
        request: Request,
        project_id: str | None = Query(None),
        idempotency_key: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        user_id, user_role = _identity(user)
        session = await _session()
        try:
            runs = await service.list_runs(
                session,
                _app_id(app_id),
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
                idempotency_key=idempotency_key,
                limit=limit,
            )
            return {"runs": [run_payload(run) for run in runs]}
        except AppActionRuntimeError as exc:
            raise _route_error(exc) from exc
        finally:
            await session.close()

    @router.get(
        "/api/apps/{app_id}/runs/{run_id}",
        response_model=AppActionRunResponse,
        operation_id="get_app_action_run",
    )
    async def get_app_action_run(
        app_id: str,
        run_id: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        user_id, user_role = _identity(user)
        session = await _session()
        try:
            run = await service.get_run(
                session,
                _app_id(app_id),
                run_id,
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
            )
            return _run_response(run)
        except AppActionRuntimeError as exc:
            raise _route_error(exc) from exc
        finally:
            await session.close()

    @router.post(
        "/api/apps/{app_id}/runs/{run_id}/stop",
        response_model=AppActionRunResponse,
        operation_id="stop_app_action_run",
    )
    async def stop_app_action_run(
        app_id: str,
        run_id: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        user_id, user_role = _identity(user)
        session = await _session()
        try:
            run = await service.stop_run(
                session,
                _app_id(app_id),
                run_id,
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
            )
            return _run_response(run)
        except AppActionRuntimeError as exc:
            raise _route_error(exc) from exc
        finally:
            await session.close()

    @router.get(
        "/api/apps/{app_id}/runs/{run_id}/logs",
        response_model=AppRunLogsResponse,
        operation_id="get_app_action_run_logs",
    )
    async def get_app_action_run_logs(
        app_id: str,
        run_id: str,
        request: Request,
        project_id: str | None = Query(None),
        _: None = Depends(require_auth_dependency),
    ):
        user = await current_user(request)
        user_id, user_role = _identity(user)
        session = await _session()
        try:
            run = await service.get_run(
                session,
                _app_id(app_id),
                run_id,
                user_id=user_id,
                user_role=user_role,
                project_id=project_id,
            )
            app = await session.scalar(select(App).where(App.id == _app_id(app_id)).limit(1))
            if app is None:
                raise AppActionNotFound("App not found")
            logs = await service.read_logs(session, run, app=app)
            return {"run_id": str(run.id), "logs": logs}
        except AppActionRuntimeError as exc:
            raise _route_error(exc) from exc
        finally:
            await session.close()

    # Run file metadata/byte transport is registered by the dedicated
    # ``action_files_routes`` module.  Keeping it out of this router avoids
    # duplicate route matches and the old 501 upload placeholder.

    return service

__all__ = ["register_app_action_routes"]
