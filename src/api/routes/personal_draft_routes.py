"""本人の対話的 Web セッション専用の暗号化下書き API。

Agent はこの HTTP 境界を使わない。owner/actor は署名済みセッションから解決し、
受理は表示済み digest に束縛する。資料入力は本人の明示テキストのみで、パスや URL を
取得しない。下書きの作成自体は台帳残数・期限確認・通知を変更しない。
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...services.personal_draft_service import PersonalDraftService
from ...services.personal_ledger_service import LedgerError, user_action_actor
from .personal_ledger_routes import _http_error, require_interactive_owner

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class DraftProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["deadline", "inventory"]
    # 型付き内容・証拠との一致はサービスが再検証する。
    data: dict[str, Any]


class DraftEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal: DraftProposal
    source_text: str = Field(min_length=1, max_length=16_384, strict=True)
    idempotency_key: UUID


class PersonalDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_ref: UUID
    proposal: DraftProposal
    idempotency_key: UUID


class PersonalDraftDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_digest: str = Field(pattern=_DIGEST_PATTERN, strict=True)
    idempotency_key: UUID


class DraftEvidenceResponse(BaseModel):
    evidence_ref: UUID


class PersonalDraftProposalResponse(BaseModel):
    draft_ref: UUID
    confirmed_digest: str
    status: Literal["awaiting_user_review"]
    effective_ledger_change: Literal[False]


class PersonalDraftView(BaseModel):
    schema_version: int = Field(alias="schema")
    evidence_ref: UUID
    proposed_by: Literal["agent", "user_action"]
    draft_ref: UUID
    revision: int
    proposal: DraftProposal
    evidence: dict[str, Any]
    confirmed_digest: str
    status: str
    target_id: UUID | None = None


class PersonalDraftPage(BaseModel):
    items: list[PersonalDraftView]
    next_cursor: UUID | None


class PersonalDraftAcceptanceResponse(BaseModel):
    draft_id: UUID
    status: Literal["accepted", "materialized_requires_verification"]
    target_kind: str
    target_id: UUID
    replayed: bool


def register_personal_draft_routes(
    app: FastAPI, server, *, service: PersonalDraftService | None = None
) -> None:
    drafts = service or PersonalDraftService(lambda: server._db_manager.get_session())

    async def owner(request: Request, response: Response) -> str:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        owner_id = await require_interactive_owner(server, request)
        if request.method == "POST":
            # Next が同一 origin を検証する。直接到達する simple request も拒否し、
            # Cookie の SameSite にだけ依存しない。Origin/Referer は proxy で転送されない。
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json" or request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "本人の同一オリジン JSON 操作が必要です")
        return owner_id

    async def invoke(operation, owner_id: str, **fields: Any):
        try:
            return await operation(actor=user_action_actor(owner_id), **fields)
        except LedgerError as exc:
            raise _http_error(exc) from None

    @app.post("/api/personal-drafts/evidence", response_model=DraftEvidenceResponse, status_code=201)
    async def record_evidence(body: DraftEvidenceCreate, owner_id: str = Depends(owner)):
        return await invoke(
            drafts.record_evidence, owner_id, proposal=body.proposal.model_dump(),
            source_text=body.source_text, idempotency_key=body.idempotency_key,
        )

    @app.post("/api/personal-drafts", response_model=PersonalDraftProposalResponse, status_code=201)
    async def propose(body: PersonalDraftCreate, owner_id: str = Depends(owner)):
        return await invoke(
            drafts.propose, owner_id, evidence_ref=body.evidence_ref,
            proposal=body.proposal.model_dump(), idempotency_key=body.idempotency_key,
        )

    @app.get("/api/personal-drafts", response_model=PersonalDraftPage)
    async def list_drafts(limit: int = Query(50, ge=1, le=50), cursor: UUID | None = None,
                          owner_id: str = Depends(owner)):
        return await invoke(drafts.list_drafts, owner_id, limit=limit, cursor=cursor)

    @app.get("/api/personal-drafts/{draft_id}", response_model=PersonalDraftView)
    async def get_draft(draft_id: UUID, owner_id: str = Depends(owner)):
        return await invoke(drafts.get_draft, owner_id, draft_ref=draft_id)

    @app.post("/api/personal-drafts/{draft_id}/accept", response_model=PersonalDraftAcceptanceResponse)
    async def accept(draft_id: UUID, body: PersonalDraftDecision, owner_id: str = Depends(owner)):
        return await invoke(
            drafts.accept, owner_id, draft_ref=draft_id,
            confirmed_digest=body.confirmed_digest, idempotency_key=body.idempotency_key,
        )

    @app.post("/api/personal-drafts/{draft_id}/discard", response_model=dict[str, Any])
    async def discard(draft_id: UUID, body: PersonalDraftDecision, owner_id: str = Depends(owner)):
        return await invoke(
            drafts.discard, owner_id, draft_ref=draft_id,
            confirmed_digest=body.confirmed_digest, idempotency_key=body.idempotency_key,
        )
