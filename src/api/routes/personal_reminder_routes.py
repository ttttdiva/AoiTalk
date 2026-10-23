"""本人の対話的 Web セッション専用の期限リマインダー API。"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...services.personal_ledger_service import LedgerError, user_action_actor
from ...services.personal_reminder_service import PersonalReminderService, PersonalReminderUnavailable
from .personal_ledger_routes import _http_error, require_interactive_owner

_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ReminderSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_entry_id: UUID
    confirmed_digest: str = Field(pattern=_DIGEST_PATTERN, strict=True)
    remind_at: str = Field(min_length=20, max_length=40, strict=True)
    time_zone: str = Field(min_length=1, max_length=80, strict=True)
    idempotency_key: str = Field(pattern=_KEY_PATTERN, max_length=128, strict=True)


class ReminderProposeRequest(ReminderSelectionRequest):
    """本人Web proposal; Agent tool proposals still use the service adapter."""


class ReminderScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_digest: str = Field(pattern=_DIGEST_PATTERN, strict=True)
    idempotency_key: str = Field(pattern=_KEY_PATTERN, max_length=128, strict=True)


class ReminderActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(pattern=_KEY_PATTERN, max_length=128, strict=True)


class ReminderSelectionResponse(BaseModel):
    selection_ref: UUID
    delivery_available: bool


class ReminderActionResponse(BaseModel):
    draft_ref: UUID
    status: Literal["awaiting_user_review", "scheduled", "cancelled", "delivered", "acknowledged"]
    delivery: Literal["owner_private_in_app"]
    scheduled: bool
    was_scheduled: bool
    delivery_available: bool


class ReminderItem(BaseModel):
    draft_ref: UUID
    candidate_entry_id: UUID
    status: Literal["awaiting_user_review", "scheduled", "cancelled", "delivered", "invalidated", "acknowledged"]
    delivery: Literal["owner_private_in_app"]
    scheduled: bool
    was_scheduled: bool
    remind_at: str | None
    time_zone: str | None
    confirmation_digest: str | None
    delivery_available: bool
    delivery_ref: UUID | None = None
    title: str | None = None
    due_date: str | None = None
    label: str | None = None
    subject_key: str | None = None
    scheduled_for: str | None = None
    acknowledged_at: str | None = None


class ReminderListResponse(BaseModel):
    items: list[ReminderItem]
    delivery_available: bool


def register_personal_reminder_routes(
    app: FastAPI, server, *, service: PersonalReminderService | None = None,
    schema_ready: Callable[[], Any] | None = None,
    worker_ready: Callable[[], bool] | None = None,
) -> None:
    reminders = service or PersonalReminderService(lambda: server._db_manager.get_session())
    is_schema_ready = schema_ready or (lambda: False)
    is_worker_ready = worker_ready or (lambda: False)

    async def schema_available() -> bool:
        try:
            result = is_schema_ready()
            if inspect.isawaitable(result):
                result = await result
            return result is True
        except Exception:
            return False

    def delivery_available() -> bool:
        try:
            return is_worker_ready() is True
        except Exception:
            return False

    async def require_schema_ready() -> None:
        if not await schema_available():
            raise HTTPException(503, "personal_reminder_unavailable",
                                headers={"Cache-Control": "no-store"})

    async def owner(request: Request, response: Response) -> str:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        owner_id = await require_interactive_owner(server, request)
        if request.method == "POST":
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json" or request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "本人の同一オリジン JSON 操作が必要です")
        return owner_id

    async def invoke(operation, owner_id: str, **fields: Any):
        try:
            return await operation(actor=user_action_actor(owner_id), **fields)
        except PersonalReminderUnavailable:
            raise HTTPException(503, "personal_reminder_unavailable",
                                headers={"Cache-Control": "no-store"}) from None
        except LedgerError as exc:
            raise _http_error(exc) from None

    @app.get("/api/personal-reminders", response_model=ReminderListResponse)
    async def list_reminders(limit: int = Query(50, ge=1, le=200), owner_id: str = Depends(owner)):
        await require_schema_ready()
        ready = delivery_available()
        result = await invoke(reminders.list_owner, owner_id, limit=limit)
        return {"items": [{**item, "delivery_available": ready} for item in result["items"]],
                "delivery_available": ready}

    @app.get("/api/personal-reminders/{draft_id}", response_model=ReminderItem)
    async def get_reminder(draft_id: UUID, owner_id: str = Depends(owner)):
        await require_schema_ready()
        result = await invoke(reminders.get_owner, owner_id, draft_ref=draft_id)
        return {**result, "delivery_available": delivery_available()}

    @app.post("/api/personal-reminders/selections", response_model=ReminderSelectionResponse, status_code=201)
    async def select_time(body: ReminderSelectionRequest, owner_id: str = Depends(owner)):
        await require_schema_ready()
        ready = delivery_available()
        result = await invoke(reminders.record_selection, owner_id,
            candidate_entry_id=body.candidate_entry_id, confirmed_digest=body.confirmed_digest,
            remind_at=body.remind_at, time_zone=body.time_zone, idempotency_key=body.idempotency_key)
        return {**result, "delivery_available": ready}

    @app.post("/api/personal-reminders/propose", response_model=ReminderActionResponse, status_code=201)
    async def propose(body: ReminderProposeRequest, owner_id: str = Depends(owner)):
        await require_schema_ready()
        ready = delivery_available()
        result = await invoke(reminders.propose, owner_id,
            candidate_entry_id=body.candidate_entry_id, confirmed_digest=body.confirmed_digest,
            remind_at=body.remind_at, time_zone=body.time_zone, idempotency_key=body.idempotency_key)
        return {**result, "was_scheduled": False, "delivery_available": ready}

    @app.post("/api/personal-reminders/{draft_id}/schedule", response_model=ReminderActionResponse)
    async def schedule(draft_id: UUID, body: ReminderScheduleRequest, owner_id: str = Depends(owner)):
        await require_schema_ready()
        result = await invoke(reminders.schedule, owner_id, draft_ref=draft_id,
            confirmed_digest=body.confirmed_digest, idempotency_key=body.idempotency_key,
            worker_ready=is_worker_ready)
        # Readiness is reported after the atomic service operation. The service
        # gate above decides whether a new row may be appended; a replay remains
        # valid when the worker went down after the original commit.
        ready = delivery_available()
        return {**result, "delivery_available": ready}

    @app.post("/api/personal-reminders/{draft_id}/cancel", response_model=ReminderActionResponse)
    async def cancel(draft_id: UUID, body: ReminderActionRequest, owner_id: str = Depends(owner)):
        await require_schema_ready()
        ready = delivery_available()
        result = await invoke(reminders.cancel, owner_id, draft_ref=draft_id,
            idempotency_key=body.idempotency_key)
        return {**result, "delivery_available": ready}

    @app.post("/api/personal-reminders/{draft_id}/ack", response_model=ReminderActionResponse)
    async def acknowledge(draft_id: UUID, body: ReminderActionRequest, owner_id: str = Depends(owner)):
        await require_schema_ready()
        ready = delivery_available()
        result = await invoke(reminders.acknowledge, owner_id, draft_ref=draft_id,
            idempotency_key=body.idempotency_key)
        return {**result, "delivery_available": ready}
