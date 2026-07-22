"""オカレンス・タイムエントリ・タイムレポートのエンドポイント。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ....services.task_management_service import TaskManagementError
from ._shared import (
    OccurrenceUpdatePayload,
    TaskRouterContext,
    TimeLogPayload,
    TimerStartPayload,
    TimerStopPayload,
    UpdateTimeEntryPayload,
    _parse_datetime,
    _parse_wall_clock_datetime,
)
from ...uuid_http import parse_uuid_or_400


def register_occurrence_time_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    require_auth_dependency = ctx.require_auth_dependency
    get_db_manager = ctx.get_db_manager
    service = ctx.service
    _get_current_user = ctx.get_current_user
    _translate_service_error = ctx.translate_service_error

    @router.get("/task-occurrences")
    async def list_occurrences(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.list_occurrences(
                session,
                user_id=user_id,
                project_id=parse_uuid_or_400(
                    request.query_params.get("project_id"), "project_id"
                ),
                space_id=parse_uuid_or_400(
                    request.query_params.get("space_id"), "space_id"
                ),
                start_from=_parse_datetime(
                    request.query_params.get("start_from"), "start_from"
                ),
                end_to=_parse_datetime(request.query_params.get("end_to"), "end_to"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/task-occurrences/{occurrence_id}")
    async def update_occurrence(
        occurrence_id: str,
        payload: OccurrenceUpdatePayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.update_occurrence(
                session,
                user_id=user_id,
                occurrence_id=parse_uuid_or_400(occurrence_id, "occurrence_id"),
                updates={
                    "status": payload.status,
                    "start_at": (
                        _parse_wall_clock_datetime(payload.start_at, "start_at")
                        if payload.start_at is not None
                        else None
                    ),
                    "end_at": (
                        _parse_wall_clock_datetime(payload.end_at, "end_at")
                        if payload.end_at is not None
                        else None
                    ),
                    "reminder_offsets": payload.reminder_offsets,
                },
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/time-entries")
    async def list_time_entries(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.list_time_entries(
                session,
                user_id=user_id,
                project_id=parse_uuid_or_400(
                    request.query_params.get("project_id"), "project_id"
                ),
                space_id=parse_uuid_or_400(
                    request.query_params.get("space_id"), "space_id"
                ),
                task_id=parse_uuid_or_400(request.query_params.get("task_id"), "task_id"),
                active_only=request.query_params.get("active_only", "false").lower()
                in {"1", "true", "yes"},
                # DB はローカル壁時計時刻保存のため、Web BFF と同じ壁時計解釈で受ける
                date_from=_parse_wall_clock_datetime(
                    request.query_params.get("date_from"), "date_from"
                ),
                date_to=_parse_wall_clock_datetime(
                    request.query_params.get("date_to"), "date_to"
                ),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/time-entries/active")
    async def get_active_time_entry(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.get_active_time_entry(session, user_id=user_id)
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/time-entries/start")
    async def start_timer(
        payload: TimerStartPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.start_timer(
                session,
                user_id=user_id,
                task_id=parse_uuid_or_400(payload.task_id, "task_id"),
                occurrence_id=parse_uuid_or_400(payload.occurrence_id, "occurrence_id"),
                source=payload.source,
                note=payload.note,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/time-entries/stop")
    async def stop_timer(
        payload: TimerStopPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.stop_timer(
                session,
                user_id=user_id,
                time_entry_id=parse_uuid_or_400(payload.time_entry_id, "time_entry_id"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.patch("/time-entries/{entry_id}")
    async def update_time_entry(
        entry_id: str,
        payload: UpdateTimeEntryPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.update_time_entry(
                session,
                user_id=user_id,
                entry_id=parse_uuid_or_400(entry_id, "entry_id"),
                started_at=(
                    _parse_wall_clock_datetime(payload.started_at, "started_at")
                    if payload.started_at is not None
                    else None
                ),
                ended_at=(
                    _parse_wall_clock_datetime(payload.ended_at, "ended_at")
                    if payload.ended_at is not None
                    else None
                ),
                note=payload.note,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.delete("/time-entries/{entry_id}", status_code=204)
    async def delete_time_entry(
        entry_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            await service.delete_time_entry(
                session,
                user_id=user_id,
                entry_id=parse_uuid_or_400(entry_id, "entry_id"),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.post("/time-entries/log")
    async def log_time(
        payload: TimeLogPayload,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.log_time(
                session,
                user_id=user_id,
                task_id=parse_uuid_or_400(payload.task_id, "task_id"),
                occurrence_id=parse_uuid_or_400(payload.occurrence_id, "occurrence_id"),
                started_at=_parse_wall_clock_datetime(payload.started_at, "started_at"),
                ended_at=_parse_wall_clock_datetime(payload.ended_at, "ended_at"),
                note=payload.note,
                source=payload.source,
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()

    @router.get("/reports/time")
    async def get_time_report(
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        user_id, _ = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            return await service.get_time_report(
                session,
                user_id=user_id,
                project_id=parse_uuid_or_400(
                    request.query_params.get("project_id"), "project_id"
                ),
                space_id=parse_uuid_or_400(
                    request.query_params.get("space_id"), "space_id"
                ),
                date_from=_parse_wall_clock_datetime(
                    request.query_params.get("date_from"), "date_from"
                ),
                date_to=_parse_wall_clock_datetime(
                    request.query_params.get("date_to"), "date_to"
                ),
            )
        except TaskManagementError as exc:
            raise _translate_service_error(exc)
        finally:
            await session.close()
