"""本人専用の個人台帳（期限・在庫）API。

- owner は常にサーバー側で解決した本人のWebセッションの user id。リクエスト本文・クエリ・
  パスから owner や actor_kind を受け取らない（未知フィールドは 422）。
- 書込みの主体は ``user_action``（本人が画面で操作した事実）。Agent の観測は ``actor_kind=agent`` の
  行として区別して返す（``origin``）。このAPIからは Agent 主体の行を作れない。
- 履歴は削除も更新もしない。訂正は ``supersedes_entry_id`` つきの追記。PUT/PATCH/DELETE は無い。
- 共有 Project の Task / Records には一切投影しない。
- 日付は利用者が入力した値のみ。法定期限の算出や補完はしない。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...services.personal_ledger_service import (
    LedgerConflictError,
    LedgerEntryView,
    LedgerError,
    LedgerNotFoundError,
    LedgerPage,
    LedgerPermissionError,
    LedgerValidationError,
    MAX_PAGE_SIZE,
    PersonalLedgerService,
    user_action_actor,
)

_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_SUBJECT_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class LedgerEntryCreate(BaseModel):
    """本人が入力する行。確認・完了は専用エンドポイントでのみ作る。"""

    model_config = ConfigDict(extra="forbid")
    ledger: Literal["deadline", "inventory"]
    entry_type: Literal["deadline_candidate", "opening_balance", "purchase", "consumption", "void"]
    subject_key: str = Field(pattern=_SUBJECT_PATTERN)
    payload: dict[str, Any]
    idempotency_key: str = Field(pattern=_KEY_PATTERN)
    supersedes_entry_id: UUID | None = None


class DeadlineVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(pattern=_KEY_PATTERN)
    supersedes_entry_id: UUID | None = None


class DeadlineCompleteRequest(BaseModel):
    """完了日の記録。``supersedes_entry_id`` で既存の完了行を訂正する（取消しは void）。"""

    model_config = ConfigDict(extra="forbid")
    completed_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    idempotency_key: str = Field(pattern=_KEY_PATTERN)
    supersedes_entry_id: UUID | None = None


class PersonalLedgerEntryOut(BaseModel):
    id: str
    seq: int
    ledger: str
    entry_type: str
    subject_key: str
    # agent_observation = Agentの観測 / user_entered = 本人の入力 / user_confirmed = 本人の確認・完了
    origin: Literal["agent_observation", "user_entered", "user_confirmed"]
    supersedes_entry_id: str | None
    refers_to_entry_id: str | None
    payload: dict[str, Any]
    recorded_at: str | None
    superseded: bool


class PersonalLedgerEntriesResponse(BaseModel):
    """新しい順の1ページ。``next_before_seq`` があれば、それを ``before_seq`` に渡すと続きが読める。"""

    entries: list[PersonalLedgerEntryOut]
    next_before_seq: int | None


class PersonalLedgerAppendResponse(BaseModel):
    entry: PersonalLedgerEntryOut
    replayed: bool


class PersonalLedgerDeadlineOut(BaseModel):
    subject_key: str
    candidate_entry_id: str
    due_date: str
    label: str | None
    status: Literal["unverified", "verified"]
    candidate_origin: Literal["agent_observation", "user_entered"]
    verified_entry_id: str | None
    completed_on: str | None
    completion_entry_id: str | None
    needs_review: list[str]
    confirm_digest: str


class PersonalLedgerBalanceOut(BaseModel):
    unit: str
    balance: int


class PersonalLedgerInventoryOut(BaseModel):
    subject_key: str
    balances: list[PersonalLedgerBalanceOut]
    needs_review: list[str]


class PersonalLedgerStateResponse(BaseModel):
    deadlines: list[PersonalLedgerDeadlineOut]
    inventory: list[PersonalLedgerInventoryOut]


def _origin(view: LedgerEntryView) -> str:
    if view.entry_type in ("deadline_verification", "deadline_completion"):
        return "user_confirmed"
    return "agent_observation" if view.actor_kind == "agent" else "user_entered"


def _entry_out(view: LedgerEntryView, superseded_ids: set[UUID]) -> PersonalLedgerEntryOut:
    return PersonalLedgerEntryOut(
        id=str(view.id),
        seq=view.seq,
        ledger=view.ledger,
        entry_type=view.entry_type,
        subject_key=view.subject_key,
        origin=_origin(view),
        supersedes_entry_id=str(view.supersedes_entry_id) if view.supersedes_entry_id else None,
        refers_to_entry_id=str(view.refers_to_entry_id) if view.refers_to_entry_id else None,
        payload=dict(view.payload),
        recorded_at=view.recorded_at.isoformat() if isinstance(view.recorded_at, datetime) else None,
        superseded=view.id in superseded_ids,
    )


def _http_error(exc: LedgerError) -> HTTPException:
    status = {
        LedgerValidationError: 422,
        LedgerNotFoundError: 404,
        LedgerConflictError: 409,
        LedgerPermissionError: 403,
    }.get(type(exc), 400)
    # 内部メッセージは返さず、安定したコードだけ返す。
    return HTTPException(status, exc.code)


async def require_interactive_owner(server, request: Request) -> str:
    """本人の対話的Webセッションだけを受け入れ、その user id を返す。

    Bearer / 長期APIトークン / 内部転送マーカーのみの要求 / Agent・service 主体は受け付けない。
    ``require_confirmation_actor`` と同じく、署名済みNext Cookieを既存の認証実装で再検証する。
    管理者権限は要求しない（owner は本人のみ）。
    """

    if "authorization" in request.headers or not request.headers.get("cookie"):
        raise HTTPException(401, "本人のWebセッションが必要です")
    next_cookie = server._get_request_cookie(request, server.next_cookie_name)
    if not next_cookie:
        raise HTTPException(401, "本人のNext Webセッションが必要です")
    scope = dict(request.scope)
    scope["headers"] = [
        (key, value)
        for key, value in request.scope.get("headers", [])
        if key.lower() not in {b"x-internal-auth", b"cookie"} and not key.lower().startswith(b"x-forwarded-")
    ]
    scope["headers"].append((b"cookie", f"{server.next_cookie_name}={next_cookie}".encode("latin1")))
    authenticated_request = Request(scope)
    try:
        next_subject = server._get_next_user_id_from_request(authenticated_request, raise_on_db_error=True)
        if not next_subject:
            raise HTTPException(401, "本人のNext Webセッションが必要です")
        user = await server._get_user_info_from_request(authenticated_request, raise_on_db_error=True)
    except ValueError as exc:
        raise HTTPException(401, "WebセッションのユーザーIDが不正です") from exc
    if not user:
        raise HTTPException(401, "本人のWebセッションが必要です")
    if (
        getattr(server, "auth_enabled", True) is False
        or user.get("_authority_source") != "web_session"
        or user.get("is_agent")
        or any(user.get(key) not in {None, "", "human", "user"} for key in ("actor_type", "principal_kind"))
    ):
        raise HTTPException(403, "個人台帳は本人のWebセッションからのみ操作できます")
    try:
        owner_id = str(UUID(str(user["id"])))
        if owner_id != str(UUID(str(next_subject))):
            raise HTTPException(403, "Webセッションの主体が一致しません")
        return owner_id
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(403, "永続ユーザーIDが必要です") from exc


def register_personal_ledger_routes(
    app: FastAPI, server, *, service: PersonalLedgerService | None = None
) -> None:
    ledger_service = service or PersonalLedgerService(lambda: server._db_manager.get_session())

    async def owner(request: Request) -> str:
        return await require_interactive_owner(server, request)

    async def append(owner_id: str, response: Response, **fields: Any) -> PersonalLedgerAppendResponse:
        try:
            result = await ledger_service.append_entry(actor=user_action_actor(owner_id), **fields)
        except LedgerError as exc:
            raise _http_error(exc) from None
        response.status_code = 200 if result.replayed else 201
        return PersonalLedgerAppendResponse(entry=_entry_out(result.entry, set()), replayed=result.replayed)

    @app.get("/api/personal-ledger/state", response_model=PersonalLedgerStateResponse)
    async def read_state(owner_id: str = Depends(owner)):
        try:
            state = await ledger_service.current_state(actor=user_action_actor(owner_id))
        except LedgerError as exc:
            raise _http_error(exc) from None
        return PersonalLedgerStateResponse(
            deadlines=[
                PersonalLedgerDeadlineOut(
                    subject_key=item.subject_key,
                    candidate_entry_id=str(item.candidate_entry_id),
                    due_date=item.due_date,
                    label=item.label,
                    status=item.status,  # type: ignore[arg-type]
                    candidate_origin="agent_observation" if item.candidate_actor_kind == "agent" else "user_entered",
                    verified_entry_id=str(item.verified_entry_id) if item.verified_entry_id else None,
                    completed_on=item.completed_on,
                    completion_entry_id=str(item.completion_entry_id) if item.completion_entry_id else None,
                    needs_review=list(item.needs_review),
                    confirm_digest=item.confirm_digest,
                )
                for item in state.deadlines
            ],
            inventory=[
                PersonalLedgerInventoryOut(
                    subject_key=item.subject_key,
                    balances=[PersonalLedgerBalanceOut(unit=unit, balance=balance) for unit, balance in item.balances],
                    needs_review=list(item.needs_review),
                )
                for item in state.inventory
            ],
        )

    @app.get("/api/personal-ledger/entries", response_model=PersonalLedgerEntriesResponse)
    async def list_entries(
        ledger: Literal["deadline", "inventory"] | None = None,
        subject_key: str | None = Query(default=None, pattern=_SUBJECT_PATTERN),
        limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
        before_seq: int | None = Query(default=None, ge=1),
        owner_id: str = Depends(owner),
    ):
        try:
            page: LedgerPage = await ledger_service.list_entries_page(
                actor=user_action_actor(owner_id),
                ledger=ledger,
                subject_key=subject_key,
                limit=limit,
                before_seq=before_seq,
            )
        except LedgerError as exc:
            raise _http_error(exc) from None
        return PersonalLedgerEntriesResponse(
            entries=[_entry_out(v, set(page.superseded_ids)) for v in page.entries],
            next_before_seq=page.next_before_seq,
        )

    @app.post("/api/personal-ledger/entries", response_model=PersonalLedgerAppendResponse, status_code=201)
    async def add_entry(body: LedgerEntryCreate, response: Response, owner_id: str = Depends(owner)):
        return await append(
            owner_id,
            response,
            ledger=body.ledger,
            entry_type=body.entry_type,
            subject_key=body.subject_key,
            payload=body.payload,
            idempotency_key=body.idempotency_key,
            supersedes_entry_id=body.supersedes_entry_id,
        )

    async def candidate_of(owner_id: str, entry_id: UUID) -> LedgerEntryView:
        try:
            candidate = await ledger_service.get_entry(actor=user_action_actor(owner_id), entry_id=entry_id)
        except LedgerError as exc:
            raise _http_error(exc) from None
        if candidate.entry_type != "deadline_candidate":
            raise HTTPException(422, "ledger_validation")
        return candidate

    @app.post("/api/personal-ledger/entries/{entry_id}/verify", response_model=PersonalLedgerAppendResponse, status_code=201)
    async def verify_deadline(
        entry_id: UUID, body: DeadlineVerifyRequest, response: Response, owner_id: str = Depends(owner)
    ):
        candidate = await candidate_of(owner_id, entry_id)
        return await append(
            owner_id,
            response,
            ledger="deadline",
            entry_type="deadline_verification",
            subject_key=candidate.subject_key,
            payload={"confirmed_digest": body.confirmed_digest},
            idempotency_key=body.idempotency_key,
            supersedes_entry_id=body.supersedes_entry_id,
            refers_to_entry_id=candidate.id,
        )

    @app.post("/api/personal-ledger/entries/{entry_id}/complete", response_model=PersonalLedgerAppendResponse, status_code=201)
    async def complete_deadline(
        entry_id: UUID, body: DeadlineCompleteRequest, response: Response, owner_id: str = Depends(owner)
    ):
        candidate = await candidate_of(owner_id, entry_id)
        return await append(
            owner_id,
            response,
            ledger="deadline",
            entry_type="deadline_completion",
            subject_key=candidate.subject_key,
            payload={"completed_on": body.completed_on},
            idempotency_key=body.idempotency_key,
            supersedes_entry_id=body.supersedes_entry_id,
            refers_to_entry_id=candidate.id,
        )
