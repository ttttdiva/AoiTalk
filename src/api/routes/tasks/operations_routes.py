"""Idempotent, CAS-protected Task CRUD operations for remote clients."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from datetime import datetime
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import select, text

from ...datetime_utils import parse_utc_naive_datetime
from ....memory.models import Task, TaskApiOperationReceipt
from ....services.task_management._shared import TaskManagementError
from ....services.task_management.tasks import _assert_generation_mutation_allowed
from ...uuid_http import parse_uuid_or_400
from ._shared import (
    CreateTaskPayload,
    TaskRouterContext,
    UpdateTaskPayload,
    _build_update_task_updates,
    _parse_wall_clock_datetime,
)

logger = logging.getLogger(__name__)

TASK_OPERATION_LOCK_NAMESPACE = 1414481751


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _StrictCreateTaskPayload(CreateTaskPayload):
    model_config = ConfigDict(extra="forbid", strict=True)


class _StrictUpdateTaskPayload(UpdateTaskPayload):
    model_config = ConfigDict(extra="forbid", strict=True)


class _CreateOperation(_StrictPayload):
    action: Literal["create"]
    payload: _StrictCreateTaskPayload


class _UpdateOperation(_StrictPayload):
    action: Literal["update"]
    task_id: str
    expected_updated_at: str
    changes: _StrictUpdateTaskPayload


class _DeleteOperation(_StrictPayload):
    action: Literal["delete"]
    task_id: str
    expected_updated_at: str


class _RestoreOperation(_StrictPayload):
    action: Literal["restore"]
    task_id: str
    deletion_batch_id: str


_OperationRequest = Annotated[
    Union[_CreateOperation, _UpdateOperation, _DeleteOperation, _RestoreOperation],
    Field(discriminator="action"),
]
_OPERATION_ADAPTER = TypeAdapter(_OperationRequest)


def _operation_openapi_schema() -> dict[str, Any]:
    """Return the runtime validator schema with local definitions inlined."""

    schema = _OPERATION_ADAPTER.json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(value: Any) -> Any:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            return resolve(definitions[reference.removeprefix("#/$defs/")])
        return {key: resolve(item) for key, item in value.items()}

    resolved = resolve(schema)
    discriminator = resolved.get("discriminator")
    if isinstance(discriminator, dict):
        # The mapping points at the removed local definitions. Each inlined
        # branch still carries the exact action const used for discrimination.
        discriminator.pop("mapping", None)
    return resolved


_OPERATION_OPENAPI_SCHEMA = _operation_openapi_schema()


def _error(status_code: int, code: str, detail: Any | None = None) -> HTTPException:
    payload: dict[str, Any] = {"code": code}
    if detail is not None:
        if isinstance(detail, dict):
            payload.update(detail)
            payload["code"] = code
        else:
            payload["detail"] = str(detail)
    return HTTPException(status_code=status_code, detail=payload)


def _structured_http_error(exc: HTTPException) -> HTTPException:
    if isinstance(exc.detail, dict) and exc.detail.get("code"):
        return exc
    code = {
        400: "validation_error",
        401: "unauthorized",
        403: "forbidden",
        404: "task_not_found",
        409: "task_conflict",
        410: "task_restore_expired",
        422: "validation_error",
    }.get(exc.status_code, "task_operation_failed")
    return _error(exc.status_code, code, exc.detail)


def _task_error(exc: TaskManagementError) -> HTTPException:
    if isinstance(exc.detail, dict) and exc.detail.get("code"):
        code = str(exc.detail["code"])
        return _error(exc.status_code, code, exc.detail)
    code = {
        400: "validation_error",
        403: "forbidden",
        404: "task_not_found",
        409: "task_conflict",
        410: "task_restore_expired",
    }.get(exc.status_code, "task_operation_failed")
    return _error(exc.status_code, code, exc.message)


def _parse_operation(body: bytes) -> _OperationRequest:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(400, "validation_error", "Invalid JSON body") from exc
    try:
        return _OPERATION_ADAPTER.validate_python(value)
    except ValidationError as exc:
        # Pydantic's error details include caller values. Keep those out of API
        # errors and logs because payload metadata may contain private data.
        raise _error(422, "validation_error", "Invalid task operation payload") from exc


def _canonical_hash(payload: _OperationRequest) -> str:
    canonical = payload.model_dump(mode="json", exclude_unset=True)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _effect_response(
    *,
    operation_id: UUID,
    action: str,
    result: dict[str, Any] | None,
    effects: dict[str, Any],
    replayed: bool,
    details_redacted: bool = False,
) -> dict[str, Any]:
    response = {
        "operation_id": str(operation_id),
        "action": action,
        "committed": True,
        "replayed": replayed,
        "result": None if details_redacted else result,
        "effects": effects,
    }
    if details_redacted:
        response["details_redacted"] = True
    return response


def _render_effects(raw: Any, *, unconfirmed_pending: bool) -> dict[str, Any]:
    effects = raw if isinstance(raw, dict) else {}
    rendered: dict[str, Any] = {}
    for name in ("notification", "calendar"):
        state = effects.get(name)
        if not isinstance(state, dict):
            state = {"status": "pending"}
        state = dict(state)
        if unconfirmed_pending and state.get("status") == "pending":
            state["status"] = "unconfirmed"
        rendered[name] = state
    return rendered


def _is_postgresql(session: Any) -> bool:
    try:
        return session.get_bind().dialect.name == "postgresql"
    except Exception:
        return False


async def _lock_operation(session: Any, *, actor_id: UUID, operation_id: UUID) -> None:
    if not _is_postgresql(session):
        return
    digest = hashlib.sha256(f"{actor_id}:{operation_id}".encode("ascii")).digest()
    lock_key = int.from_bytes(digest[:4], "big", signed=False) & 0x7FFFFFFF
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
        {"namespace": TASK_OPERATION_LOCK_NAMESPACE, "key": lock_key},
    )


async def _check_read_access(
    session: Any, *, service: Any, actor_id: UUID, task_id: UUID | None
) -> bool:
    if task_id is None:
        return False
    result = await session.execute(
        select(Task.project_id).where(Task.id == task_id)
    )
    project_id = result.scalar_one_or_none()
    if project_id is None:
        return False
    try:
        await service.require_project_permission(
            session,
            project_id=project_id,
            user_id=actor_id,
            permission="read",
        )
    except TaskManagementError:
        return False
    return True


async def _lookup_own_receipt(
    session: Any, *, actor_id: UUID, operation_id: UUID
) -> TaskApiOperationReceipt | None:
    result = await session.execute(
        select(TaskApiOperationReceipt).where(
            TaskApiOperationReceipt.actor_id == actor_id,
            TaskApiOperationReceipt.operation_id == operation_id,
        )
    )
    return result.scalar_one_or_none()


def _task_operation_id(value: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise _error(400, "validation_error", "Invalid operation_id") from exc


def _expected_updated_at(value: str) -> datetime:
    try:
        return parse_utc_naive_datetime(value)
    except (TypeError, ValueError) as exc:
        raise _error(400, "validation_error", "Invalid expected_updated_at") from exc


async def _delete_calendar_snapshot(
    session: Any,
    *,
    service: Any,
    actor_id: UUID,
    task_id: UUID,
) -> dict[str, Any] | None:
    project_result = await session.execute(
        select(Task.project_id).where(Task.id == task_id)
    )
    project_id = project_result.scalar_one_or_none()
    if project_id is None:
        raise TaskManagementError("Task not found", status_code=404)
    await service._lock_project_acl_for_task_write(
        session, project_ids=(project_id,), user_id=actor_id
    )
    task_result = await session.execute(
        select(Task).where(Task.id == task_id).with_for_update()
    )
    task = task_result.scalar_one_or_none()
    if task is None:
        raise TaskManagementError("Task not found", status_code=404)
    if task.project_id != project_id:
        raise TaskManagementError(
            "Task project changed while the delete was being prepared",
            status_code=409,
            detail={"code": "task_cas_stale"},
        )
    await service.require_project_permission(
        session, project_id=task.project_id, user_id=actor_id, permission="write"
    )
    fresh_result = await session.execute(
        select(Task.updated_at, Task.deleted_at, Task.deletion_batch_id).where(
            Task.id == task_id
        )
    )
    _updated_at, deleted_at, _batch_id = fresh_result.one()
    if deleted_at is not None:
        return None
    try:
        await service.require_project_permission(
            session,
            project_id=task.project_id,
            user_id=actor_id,
            permission="read",
        )
    except TaskManagementError:
        return None
    metadata = task.task_metadata or {}
    calendar_metadata = metadata.get("google_calendar")
    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "metadata": (
            {"google_calendar": dict(calendar_metadata)}
            if isinstance(calendar_metadata, dict)
            else {}
        ),
    }


async def _invoke_callback(callback: Any) -> Any:
    result = callback()
    if inspect.isawaitable(result):
        return await result
    return result


async def _finish_operation_effects(
    *,
    ctx: TaskRouterContext,
    actor_id: UUID,
    operation_id: UUID,
    task_id: UUID | None,
    action: str,
    result: dict[str, Any],
    calendar_snapshot: dict[str, Any] | None,
    restore_callbacks: list[Any],
) -> None:
    effects: dict[str, Any] = {
        "notification": {"status": "pending"},
        "calendar": {"status": "pending"},
    }
    try:
        if not getattr(ctx.service, "_broadcaster", None) or result.get("idempotent"):
            effects["notification"] = {"status": "skipped"}
        else:
            broadcast_result: Any = True
            if restore_callbacks:
                for callback in restore_callbacks:
                    callback_result = await _invoke_callback(callback)
                    if callback_result is False:
                        broadcast_result = False
            else:
                event_by_action = {
                    "create": "task_created",
                    "update": "task_updated",
                    "delete": "task_deleted",
                    "restore": "task_restored",
                }
                if task_id is not None:
                    broadcast_result = await ctx.service._broadcast(
                        event_by_action[action],
                        result,
                    )
            effects["notification"] = {
                "status": "warning" if broadcast_result is False else "done"
            }
    except Exception:
        logger.warning(
            "Task operation notification effect failed for operation %s",
            operation_id,
        )
        effects["notification"] = {"status": "warning"}

    calendar_session = None
    try:
        if action == "delete":
            if calendar_snapshot is None:
                effects["calendar"] = {"status": "skipped"}
            else:
                calendar_session = await ctx.get_db_manager().get_session()
                calendar_result = await ctx.google_calendar.delete_auto_event_for_task(
                    calendar_session,
                    user_id=actor_id,
                    task=calendar_snapshot,
                )
                effects["calendar"] = {
                    "status": (
                        "warning"
                        if calendar_result.get("status") == "warning"
                        else "done"
                        if calendar_result.get("status") == "deleted"
                        else "skipped"
                    )
                }
        elif action in {"create", "update", "restore"} and task_id is not None:
            if result.get("idempotent"):
                effects["calendar"] = {"status": "skipped"}
            else:
                calendar_session = await ctx.get_db_manager().get_session()
                calendar_result = await ctx.sync_google_calendar_warning_only(
                    calendar_session,
                    user_id=actor_id,
                    task_id=str(task_id),
                )
                effects["calendar"] = {
                    "status": (
                        "warning"
                        if calendar_result.get("status") == "warning"
                        else "done"
                        if calendar_result.get("status") in {"created", "updated", "deleted"}
                        else "skipped"
                    )
                }
        else:
            effects["calendar"] = {"status": "skipped"}
    except Exception:
        logger.warning(
            "Task operation Calendar effect failed for operation %s", operation_id
        )
        effects["calendar"] = {"status": "warning"}
    finally:
        if calendar_session is not None:
            try:
                await calendar_session.close()
            except Exception:
                pass

    receipt_session = None
    try:
        receipt_session = await ctx.get_db_manager().get_session()
        receipt = await _lookup_own_receipt(
            receipt_session, actor_id=actor_id, operation_id=operation_id
        )
        if receipt is not None:
            stored = dict(receipt.result_json or {})
            stored["effects"] = effects
            receipt.result_json = stored
            await receipt_session.commit()
    except Exception:
        if receipt_session is not None:
            try:
                await receipt_session.rollback()
            except Exception:
                pass
        logger.warning(
            "Task operation effects receipt update failed for operation %s",
            operation_id,
        )
    finally:
        if receipt_session is not None:
            try:
                await receipt_session.close()
            except Exception:
                pass


def register_task_operation_routes(router: APIRouter, ctx: TaskRouterContext) -> None:
    async def require_operation_auth(request: Request) -> None:
        try:
            parameters = inspect.signature(ctx.require_auth_dependency).parameters
            result = (
                ctx.require_auth_dependency(request)
                if parameters
                else ctx.require_auth_dependency()
            )
            if inspect.isawaitable(result):
                await result
        except HTTPException as exc:
            raise _structured_http_error(exc) from exc

    @router.put(
        "/tasks/operations/{operation_id}",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": _OPERATION_OPENAPI_SCHEMA}
                },
            }
        },
    )
    async def submit_task_operation(
        operation_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        _auth=Depends(require_operation_auth),
    ):
        parsed_operation_id = _task_operation_id(operation_id)
        try:
            actor_id, _actor = await ctx.get_current_user(request)
        except HTTPException as exc:
            raise _structured_http_error(exc) from exc
        try:
            payload = _parse_operation(await request.body())
        except HTTPException:
            raise
        request_hash = _canonical_hash(payload)
        session = await ctx.get_db_manager().get_session()
        try:
            await _lock_operation(
                session, actor_id=actor_id, operation_id=parsed_operation_id
            )
            receipt = await _lookup_own_receipt(
                session,
                actor_id=actor_id,
                operation_id=parsed_operation_id,
            )
            if receipt is not None:
                if receipt.request_hash != request_hash:
                    raise _error(
                        409,
                        "operation_conflict",
                        "operation_id is already bound to a different request",
                    )
                stored = dict(receipt.result_json or {})
                can_read = await _check_read_access(
                    session,
                    service=ctx.service,
                    actor_id=actor_id,
                    task_id=receipt.task_id,
                )
                return _effect_response(
                    operation_id=parsed_operation_id,
                    action=receipt.operation,
                    result=stored.get("result") if isinstance(stored.get("result"), dict) else None,
                    effects=_render_effects(
                        stored.get("effects"), unconfirmed_pending=True
                    ),
                    replayed=True,
                    details_redacted=not can_read,
                )

            restore_callbacks: list[Any] = []
            calendar_snapshot: dict[str, Any] | None = None
            task_id: UUID | None = None
            action = payload.action
            if isinstance(payload, _CreateOperation):
                dto = payload.payload
                result = await ctx.service.create_task(
                    session,
                    user_id=actor_id,
                    project_id=parse_uuid_or_400(dto.project_id, "project_id"),
                    knowledge_node_id=parse_uuid_or_400(
                        dto.knowledge_node_id, "knowledge_node_id"
                    ),
                    title=dto.title,
                    description=dto.description,
                    status=dto.status,
                    priority=dto.priority,
                    start_at=_parse_wall_clock_datetime(dto.start_at, "start_at"),
                    end_at=_parse_wall_clock_datetime(dto.end_at, "end_at"),
                    all_day=dto.all_day,
                    auto_close_on_due=dto.auto_close_on_due,
                    reminder_offsets=dto.reminder_offsets,
                    notifications_enabled=dto.notifications_enabled,
                    estimated_hours=dto.estimated_hours,
                    parent_task_id=parse_uuid_or_400(
                        dto.parent_task_id, "parent_task_id"
                    ),
                    source=dto.source,
                    assignee_ids=[
                        parse_uuid_or_400(value, "assignee_id")
                        for value in dto.assignee_ids
                    ],
                    tag_ids=[
                        parse_uuid_or_400(value, "tag_id") for value in dto.tag_ids
                    ],
                    recurrence_rrule=dto.recurrence_rrule,
                    recurrence_timezone=dto.recurrence_timezone,
                    task_metadata=ctx.with_pending_agent_triage(dto.task_metadata),
                    commit=False,
                )
                task_id = UUID(str(result["id"]))
            elif isinstance(payload, _UpdateOperation):
                task_id = parse_uuid_or_400(payload.task_id, "task_id")
                result = await ctx.service.update_task(
                    session,
                    user_id=actor_id,
                    task_id=task_id,
                    updates=_build_update_task_updates(payload.changes),
                    close_incomplete_subtasks=(
                        payload.changes.close_incomplete_subtasks is True
                    ),
                    expected_updated_at=_expected_updated_at(
                        payload.expected_updated_at
                    ),
                    commit=False,
                )
            elif isinstance(payload, _DeleteOperation):
                task_id = parse_uuid_or_400(payload.task_id, "task_id")
                calendar_snapshot = await _delete_calendar_snapshot(
                    session,
                    service=ctx.service,
                    actor_id=actor_id,
                    task_id=task_id,
                )
                result = await ctx.service.delete_task(
                    session,
                    user_id=actor_id,
                    task_id=task_id,
                    expected_updated_at=_expected_updated_at(
                        payload.expected_updated_at
                    ),
                    commit=False,
                )
            else:
                task_id = parse_uuid_or_400(payload.task_id, "task_id")
                batch_id = parse_uuid_or_400(
                    payload.deletion_batch_id, "deletion_batch_id"
                )
                result = await ctx.service.restore_task(
                    session,
                    user_id=actor_id,
                    task_id=task_id,
                    deletion_batch_id=batch_id,
                    commit=False,
                    after_commit=restore_callbacks,
                )

            effects = {
                "notification": {"status": "pending"},
                "calendar": {"status": "pending"},
            }
            receipt = TaskApiOperationReceipt(
                actor_id=actor_id,
                operation_id=parsed_operation_id,
                operation=action,
                task_id=task_id,
                request_hash=request_hash,
                result_json={"result": result, "effects": effects},
            )
            session.add(receipt)
            _assert_generation_mutation_allowed()
            try:
                await session.commit()
            except Exception as exc:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise _error(
                    503,
                    "submission_uncertain",
                    "Commit outcome is uncertain; query or retry with the same operation_id",
                ) from exc

            background_tasks.add_task(
                _finish_operation_effects,
                ctx=ctx,
                actor_id=actor_id,
                operation_id=parsed_operation_id,
                task_id=task_id,
                action=action,
                result=result,
                calendar_snapshot=calendar_snapshot,
                restore_callbacks=restore_callbacks,
            )
            return _effect_response(
                operation_id=parsed_operation_id,
                action=action,
                result=result,
                effects=effects,
                replayed=False,
            )
        except TaskManagementError as exc:
            await session.rollback()
            raise _task_error(exc) from exc
        except HTTPException as exc:
            await session.rollback()
            raise _structured_http_error(exc) from exc
        except Exception as exc:
            await session.rollback()
            logger.error(
                "Task operation failed before commit for operation %s (%s)",
                parsed_operation_id,
                type(exc).__name__,
            )
            raise _error(500, "task_operation_failed", "Task operation failed") from exc
        finally:
            await session.close()

    @router.get("/tasks/operations/{operation_id}")
    async def get_task_operation(
        operation_id: str,
        request: Request,
        _auth=Depends(require_operation_auth),
    ):
        parsed_operation_id = _task_operation_id(operation_id)
        try:
            actor_id, _actor = await ctx.get_current_user(request)
        except HTTPException as exc:
            raise _structured_http_error(exc) from exc
        session = await ctx.get_db_manager().get_session()
        try:
            receipt = await _lookup_own_receipt(
                session,
                actor_id=actor_id,
                operation_id=parsed_operation_id,
            )
            if receipt is None:
                raise _error(
                    404,
                    "not_observed",
                    "No committed receipt is currently observable",
                )
            stored = dict(receipt.result_json or {})
            can_read = await _check_read_access(
                session,
                service=ctx.service,
                actor_id=actor_id,
                task_id=receipt.task_id,
            )
            return _effect_response(
                operation_id=parsed_operation_id,
                action=receipt.operation,
                result=stored.get("result") if isinstance(stored.get("result"), dict) else None,
                effects=_render_effects(
                    stored.get("effects"), unconfirmed_pending=True
                ),
                replayed=True,
                details_redacted=not can_read,
            )
        except HTTPException as exc:
            raise _structured_http_error(exc) from exc
        finally:
            await session.close()
