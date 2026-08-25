"""Docs REST API routes (/api/docs/*)。

完了条件1（Bearer トークンで動作）を満たす正準 REST 面。書き込み系は
``apply_docs_operation`` に委譲し、``/api/sync/push`` の Docs ハンドラと同一実装を共有する。
モバイルが直接使うのは online 限定の search / today のみ。
"""

from __future__ import annotations

import logging
import inspect
import re
import hashlib
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from os import PathLike
from typing import Any, AsyncContextManager, Awaitable, Callable, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from ..llm.generation_error import GenerationErrorKind, user_message_for_generation_kind
from ..memory.models import (
    ClipIngestReceipt,
    DocsClipIngestJob,
    DocsLibrary,
    KnowledgeNode,
)
from ..services.clip_ingest_service import ClipIngestError
from ..services.clip_ingest_storage import ClipIngestStorage, ClipUploadError
from ..services.docs_ingest_service import (
    DocsIngestBusyError,
    DocsIngestService,
    DocsIngestUnavailableError,
    canonicalize_clip_source,
)
from ..services.docs_graph_service import DocsGraphService
from ..services.docs_acl import (
    docs_readable_node_predicate,
    can_read_node,
    can_write_node,
    library_can_read,
)
from .http_cache import etag_json_response
from .docs_sync import (
    DocsOperationError,
    apply_docs_operation,
    normalize_docs_body_json,
    serialize_docs_edge,
    serialize_docs_field,
    serialize_docs_field_value,
    serialize_docs_node,
    serialize_docs_node_supertag,
    serialize_docs_placement,
    serialize_docs_supertag,
    serialize_docs_supertag_field,
)

logger = logging.getLogger(__name__)


async def _load_node_subtree(session, root: KnowledgeNode) -> list[KnowledgeNode]:
    # Keep recursive traversal bounded even if a malformed import introduced a
    # cycle.  ``UNION`` de-duplicates IDs, while the explicit depth cap avoids
    # unbounded work from a long (but acyclic) chain.
    from sqlalchemy import literal

    subtree = (
        select(KnowledgeNode.id, literal(0).label("depth"))
        .where(KnowledgeNode.id == root.id)
        .cte("docs_ingest_subtree", recursive=True)
    )
    subtree = subtree.union(
        select(
            KnowledgeNode.id,
            (subtree.c.depth + 1).label("depth"),
        ).where(
            KnowledgeNode.parent_id == subtree.c.id,
            KnowledgeNode.docs_library_id == root.docs_library_id,
            KnowledgeNode.archived_at.is_(None),
            subtree.c.depth < 512,
        )
    )
    nodes = list(
        (
            await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.id.in_(select(subtree.c.id)))
                .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [root, *(node for node in nodes if node.id != root.id)]


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid since") from exc


def _normalize_scope_uuid(value: Any) -> UUID | None:
    """Return a canonical UUID string boundary for request scope values."""

    if value is None:
        return None
    try:
        return UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


async def _validate_ingest_scope(
    session,
    *,
    user_id: UUID,
    session_id: Any = None,
    project_id: Any = None,
) -> dict[str, Any]:
    """Keep only UUID scopes visible to the authenticated user.

    Headers/query params are client-controlled, so a syntactically valid ID is
    not enough: conversation access and project read permission are checked in
    the same DB session used by the ingest transaction.  A session's bound
    project wins over an unrelated project hint.
    """

    normalized_user_id = UUID(str(user_id))
    candidate_session = _normalize_scope_uuid(session_id)
    candidate_project = _normalize_scope_uuid(project_id)
    valid_session: UUID | None = None
    session_project: UUID | None = None
    session_context: dict[str, Any] = {}

    if candidate_session is not None:
        from ..memory.conversation_repository import ConversationRepository

        try:
            repo = ConversationRepository(session)
            accessible = await repo.user_has_session_access(
                str(candidate_session),
                str(normalized_user_id),
            )
            conversation = (
                await repo.get_session_by_id(str(candidate_session), with_messages=False)
                if accessible
                else None
            )
        except Exception:
            # An untrusted header/query must not turn a successful Docs
            # request into a server error when the optional conversation
            # tables are unavailable in a legacy deployment.
            conversation = None
        if conversation is not None:
            raw_context = getattr(conversation, "context", None)
            if isinstance(raw_context, Mapping):
                privacy_mode = raw_context.get("privacy_mode")
                if privacy_mode:
                    session_context["privacy_mode"] = str(privacy_mode)
            bound_project = getattr(conversation, "project_id", None)
            if bound_project is None:
                valid_session = candidate_session
            else:
                # ``user_has_session_access`` includes the bound project's
                # read ACL check; reuse that authorized project scope.
                valid_session = candidate_session
                session_project = UUID(str(bound_project))

    valid_project: UUID | None = None
    if candidate_project is not None:
        # If a validated session is project-bound, do not let an unrelated
        # project hint redirect the usage row to a different project.
        if session_project is not None and candidate_project != session_project:
            candidate_project = None
        if candidate_project is not None:
            from ..memory.project_repository import ProjectRepository

            try:
                readable = await ProjectRepository.has_permission(
                    session,
                    candidate_project,
                    normalized_user_id,
                    "read",
                )
            except Exception:
                readable = False
            if readable:
                valid_project = candidate_project

    effective_project = session_project or valid_project
    project_metadata: dict[str, Any] = {}
    if effective_project is not None:
        from ..memory.project_repository import ProjectRepository

        try:
            project = await ProjectRepository.get_by_id(session, effective_project)
        except Exception:
            project = None
        raw_metadata = getattr(project, "project_metadata", None)
        if isinstance(raw_metadata, Mapping):
            privacy_mode = raw_metadata.get("privacy_mode")
            if privacy_mode:
                project_metadata["privacy_mode"] = str(privacy_mode)
        if "privacy_mode" in project_metadata:
            project_metadata["project_id"] = str(effective_project)
    result = {
        "user_id": str(normalized_user_id),
        "session_id": str(valid_session) if valid_session is not None else None,
        "project_id": str(effective_project) if effective_project is not None else None,
    }
    if session_context:
        result["session_context"] = session_context
    if project_metadata:
        result["project_metadata"] = project_metadata
    return result


class NodeCreateBody(BaseModel):
    id: Optional[str] = None
    parent_id: Optional[str] = None
    project_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    body_json: Optional[dict[str, Any]] = None
    node_type: Optional[str] = None
    sort_order: Optional[float] = None
    day_date: Optional[str] = None


class NodePatchBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    body_json: Optional[dict[str, Any]] = None
    parent_id: Optional[str] = None
    sort_order: Optional[float] = None
    project_id: Optional[str] = None
    day_date: Optional[str] = None


class NodeMoveBody(BaseModel):
    new_parent_id: str
    sort_order: Optional[float] = None
    leave_reference: bool = False


class SupertagAddBody(BaseModel):
    supertag_id: Optional[str] = None
    name: Optional[str] = None


class FieldValueBody(BaseModel):
    value: Any = None


class SupertagCreateBody(BaseModel):
    name: str
    base_type: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None


class DocsIngestBody(BaseModel):
    source: str = Field(default="", max_length=100_000)
    upload_ids: list[str] = Field(default_factory=list, max_length=32)
    skip_image_recognition: bool = False
    # Trusted, request-scoped switch for URL/Web research.  Omitting the
    # field keeps legacy clients (including mobile) on the historical ON
    # behavior.
    enable_external_research: bool = True
    target_node_id: UUID | None = None


class DocsClipIngestJobResponse(BaseModel):
    """Source-free durable ClipIngest job DTO."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    id: str | None = None
    status: str
    created_at: str | None = None
    actor_user_id: str | None = None
    docs_library_id: str | None = None
    session_id: str | None = None
    project_id: str | None = None
    target_node_id: str | None = None
    retry_of_job_id: str | None = None
    receipt_id: str | None = None
    source_sha256: str | None = None
    upload_ids: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    attempt: int = 0
    attempt_count: int = 0
    retryable: bool = True
    result: dict[str, Any] = Field(default_factory=dict)
    result_json: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    error_json: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None


class DocsClipIngestJobListResponse(BaseModel):
    jobs: list[DocsClipIngestJobResponse] = Field(default_factory=list)
    items: list[DocsClipIngestJobResponse] = Field(default_factory=list)


class ClipIngestResultResponse(BaseModel):
    """Canonical response shape for one Docs ClipIngest operation."""

    model_config = ConfigDict(extra="allow")

    target_id: str
    target_label: str
    action: Literal["create", "append", "duplicate_skip"]
    changed_node_id: str | None = None
    changed_node_title: str | None = None
    open_node_id: str
    open_node_title: str
    direct_urls: list[str] = Field(default_factory=list)
    supplemental_urls: list[str] = Field(default_factory=list)
    failed_urls: list[dict[str, Any]] = Field(default_factory=list)
    used_urls: list[str] = Field(default_factory=list)
    unconfirmed: list[str] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    clip_ingest_receipt_id: str | None = None
    receipt_id: str | None = None
    receipt_ids: list[str] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None
    receipts: list[dict[str, Any]] = Field(default_factory=list)


class ClipIngestResponse(BaseModel):
    """Response envelope for POST /api/docs/ingest."""

    model_config = ConfigDict(extra="allow")

    result: ClipIngestResultResponse
    node: dict[str, Any] | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    receipt_id: str | None = None
    receipt_ids: list[str] = Field(default_factory=list)
    clip_ingest_receipt_id: str | None = None


class ClipIngestReceiptSummaryResponse(BaseModel):
    """Source-free list DTO for one durable ClipIngest receipt."""

    model_config = ConfigDict(extra="allow")

    id: UUID
    topic_node_id: UUID
    target_node_id: UUID | None = None
    action: Literal["create", "append", "duplicate_skip"]
    created_at: datetime | None = None
    open_node_title: str | None = None
    open_node_id: UUID | None = None
    target_label: str | None = None
    source_sha256: str | None = None


class ClipIngestReceiptListResponse(BaseModel):
    receipts: list[ClipIngestReceiptSummaryResponse] = Field(default_factory=list)
    items: list[ClipIngestReceiptSummaryResponse] = Field(default_factory=list)


class ClipIngestReceiptDetailResponse(BaseModel):
    """Authorized durable receipt DTO; source text is detail-only."""

    model_config = ConfigDict(extra="allow")

    id: UUID
    topic_node_id: UUID
    target_node_id: UUID | None = None
    actor_user_id: UUID | None = None
    action: Literal["create", "append", "duplicate_skip"]
    source_text: str
    source_sha256: str
    result: dict[str, Any] = Field(default_factory=dict)
    result_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class ClipIngestReceiptDetailEnvelope(BaseModel):
    receipt: ClipIngestReceiptDetailResponse


def create_docs_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    *,
    docs_ingest_plan_llm: Callable[[str], Awaitable[str]] | None = None,
    docs_ingest_plan_llm_factory: (
        Callable[..., AsyncContextManager[Callable[[str], Awaitable[str]]]] | None
    ) = None,
    docs_ingest_config: Any = None,
    workspace_root: "str | PathLike[str] | None" = None,
) -> APIRouter:
    """Docs REST ルーターを生成する。

    Args:
        workspace_root: App/Project library root。``None`` なら
            ``AOITALK_WORKSPACES_DIR`` 由来の既定 root を使う。Docs から App の
            README を書き戻す経路（``docs_sync._update_app_readme_from_docs``）は
            この root でロックを取り実ファイルを触るため、ロック側と実 I/O 側で
            root が食い違わないよう ``DocsGraphService`` へ必ず透過する。
    """
    router = APIRouter(prefix="/api/docs", tags=["docs"])
    clip_storage = ClipIngestStorage(workspace_root)

    def _docs_service(session) -> DocsGraphService:
        """Docs 操作用 service を実効 root 付きで生成する。

        直接 ``DocsGraphService(session)`` を書くと既定 root へ落ちるため、
        本ルーター内の生成はすべてこのヘルパー経由にする。
        """
        return DocsGraphService(session, workspace_root=workspace_root)

    async def _resolve_read_workspace(
        session,
        service: DocsGraphService,
        *,
        user_id: UUID,
        project_id: UUID | None = None,
    ):
        """Resolve personal/project Docs library and enforce read ACL."""

        try:
            library = (
                await service.get_project_information_library(project_id, user_id)
                if project_id is not None
                else await service.ensure_library(user_id)
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if project_id is not None and library is None:
            raise HTTPException(status_code=404, detail="Project Docs library not found")
        # A project scope is authorized by the Project ACL, not by ownership
        # of the owner's Personal Docs Library.  ``get_project_information_library``
        # already performs the Project ``read`` check and returns the canonical
        # owner library; requiring ``library_can_read`` here would incorrectly
        # reject every non-owner project member.  Node-level ACL filtering is
        # applied by ``accessible_node_ids`` and the individual serializers.
        if project_id is None and not await library_can_read(session, library, user_id):
            raise HTTPException(status_code=403, detail="Docs workspaceへの読み取り権限がありません")
        return library

    async def _resolve_project_ref(
        service: DocsGraphService,
        project_ref: Any,
    ):
        """Resolve a caller-supplied project reference without broad queries."""

        if project_ref in (None, ""):
            return None
        try:
            project = await service.resolve_project(str(project_ref))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if project is None:
            # Do not disclose whether an arbitrary project exists to a caller
            # lacking membership; the shared library resolver maps this to a
            # uniform not-found response.
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    async def _resolve_write_workspace(
        session,
        service: DocsGraphService,
        *,
        user_id: UUID,
        table: str,
        entity_id: str,
        payload: dict[str, Any],
    ):
        """Resolve personal or canonical Project Docs library for a write.

        Existing node mutations carry no ``project_id`` in the REST body, so
        infer the target library from the node itself.  New project nodes
        use the explicit project hint; all other operations stay personal.
        """

        project_ref = payload.get("project_id")
        target_ref = payload.get("node_id") or payload.get("parent_id")
        if target_ref is None and entity_id:
            target_ref = str(entity_id).split(":", 1)[0]
        target_node = None
        target_uuid = _normalize_scope_uuid(target_ref)
        if target_uuid is not None and table != "knowledge_supertags":
            target_node = await session.get(KnowledgeNode, target_uuid)
        if target_node is not None:
            # Existing node library is authoritative; do not let a stale
            # client payload redirect an update into a different library.
            from ..memory.models import DocsLibrary

            target_workspace = await session.get(DocsLibrary, target_node.docs_library_id)
            if target_workspace is not None:
                if not await can_read_node(
                    session,
                    target_node,
                    user_id,
                    library=target_workspace,
                    include_archived=True,
                ):
                    raise HTTPException(status_code=404, detail="Docs node not found")
                return target_workspace

        project = await _resolve_project_ref(service, project_ref)
        if project is not None:
            try:
                # Writes may create/repair the canonical library, but only
                # after the Project write ACL has been checked by the ensure
                # resolver.  Read routes intentionally use the pure getter.
                return await service.ensure_project_information_library(project.id, user_id)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _resolve_read_workspace(
            session,
            service,
            user_id=user_id,
            project_id=project.id if project is not None else None,
        )

    async def _get_current_user(request: Request) -> UUID:
        user_info = await get_user_from_request(request)
        if not user_info or "id" not in user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return UUID(user_info["id"])

    def _request_scope(request: Request, user_id: UUID) -> dict[str, str | None]:
        """Extract request-scoped conversation/project identity without mutating globals.

        Normal browser requests carry this state on the request (when a chat
        context middleware is present).  The header/query fallbacks keep the
        REST route usable from mobile/API clients while the authenticated user
        always comes from ``get_user_from_request`` above.
        """

        state = getattr(request, "state", None)
        state_user = getattr(state, "user", None) if state is not None else None

        authenticated = str(user_id)

        def principal_value(source: Any) -> str | None:
            if isinstance(source, dict):
                for name in ("id", "user_id"):
                    value = source.get(name)
                    if value:
                        return str(value).strip() or None
            for name in ("id", "user_id"):
                value = getattr(source, name, None) if source is not None else None
                if value:
                    return str(value).strip() or None
            return None

        # ``request.state.user`` is trusted only when it describes the same
        # principal resolved by the auth dependency; otherwise its scope is
        # ignored as stale or cross-user middleware state.
        state_user_is_trusted = state_user is None or (
            principal_value(state_user) in {None, authenticated}
        )

        def state_value(*names: str) -> str | None:
            for source in (state, state_user):
                if isinstance(source, dict):
                    values = source
                else:
                    values = getattr(source, "__dict__", None)
                if isinstance(values, dict):
                    for name in names:
                        value = values.get(name)
                        if value:
                            return str(value).strip() or None
                for name in names:
                    value = getattr(source, name, None) if source is not None else None
                    if value:
                        return str(value).strip() or None
            return None

        def header_value(*names: str) -> str | None:
            for name in names:
                value = request.headers.get(name)
                if value:
                    return str(value).strip() or None
            return None

        state_session_id = (
            state_value("session_id", "conversation_session_id")
            if state_user_is_trusted
            else None
        )
        state_project_id = state_value("project_id") if state_user_is_trusted else None

        # Request middleware/turn context is authoritative over caller-owned
        # headers and query parameters.  The latter are retained only as
        # candidates and are checked against DB ACLs below.
        try:
            from ..services.turn_context import get_turn_context

            turn = get_turn_context()
        except Exception:  # pragma: no cover - import/runtime compatibility
            turn = None
        turn_is_trusted = (
            turn is not None
            and str(getattr(turn, "user_id", "") or "") == authenticated
        )
        turn_session_id = getattr(turn, "session_id", None) if turn_is_trusted else None
        turn_project_id = getattr(turn, "project_id", None) if turn_is_trusted else None

        session_id = state_session_id or turn_session_id
        untrusted_session_id = header_value(
            "X-Session-ID",
            "X-Conversation-Session-ID",
        )
        untrusted_session_id = untrusted_session_id or request.query_params.get(
            "session_id"
        ) or request.query_params.get("conversation_session_id")

        project_id = state_project_id or turn_project_id
        untrusted_project_id = header_value("X-Project-ID") or request.query_params.get(
            "project_id"
        )
        return {
            "user_id": authenticated,
            "session_id": str(session_id or untrusted_session_id).strip()
            if session_id or untrusted_session_id
            else None,
            "project_id": str(project_id or untrusted_project_id).strip()
            if project_id or untrusted_project_id
            else None,
        }

    @staticmethod
    def _factory_scope_kwargs(
        factory: Callable[..., Any],
        scope: dict[str, str | None],
    ) -> dict[str, str | None] | None:
        """Return supported scope kwargs, or ``None`` for a legacy factory."""

        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return dict(scope)
        parameters = signature.parameters
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return dict(scope)
        accepted = {
            name: scope[name]
            for name, parameter in parameters.items()
            if name in scope
            and parameter.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        }
        return accepted if accepted else None

    def _map_error(exc: Exception) -> HTTPException:
        if isinstance(exc, DocsOperationError):
            return HTTPException(status_code=exc.status_code, detail=exc.message)
        if isinstance(exc, ValueError):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    async def _write(request: Request, table: str, action: str, entity_id: str, payload: dict[str, Any]):
        user_id = await _get_current_user(request)
        if table == "knowledge_nodes" and "body_json" in payload and payload.get("body_json") is not None:
            # Validate before resolving/creating a workspace so rejected
            # legacy immutable content has no side effects on the request.
            try:
                payload["body_json"] = normalize_docs_body_json(payload["body_json"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        session = await get_db_manager().get_session()
        try:
            service = _docs_service(session)
            library = await _resolve_write_workspace(
                session,
                service,
                user_id=user_id,
                table=table,
                entity_id=entity_id,
                payload=payload,
            )
            await session.commit()
            try:
                return await apply_docs_operation(
                    session,
                    service,
                    user_id=user_id,
                    docs_library_id=library.id,
                    table=table,
                    action=action,
                    entity_id=entity_id,
                    payload=payload,
                )
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                raise _map_error(exc) from exc
        finally:
            await session.close()

    def _serialize_clip_ingest_job(job: DocsClipIngestJob) -> dict[str, Any]:
        """Return the source-free wire DTO for a durable ingest job."""

        raw = dict(job.to_dict() or {})
        # ``to_dict`` is already source-safe.  Keep this boundary defensive in
        # case a compatibility model/double adds a private field later.
        for private_name in ("source_text", "request_json", "_source_text", "_request_json"):
            raw.pop(private_name, None)
        raw["result"] = raw.get("result") if isinstance(raw.get("result"), dict) else {}
        raw["result_json"] = (
            raw.get("result_json") if isinstance(raw.get("result_json"), dict) else {}
        )
        raw["error"] = raw.get("error") if isinstance(raw.get("error"), dict) else {}
        raw["error_json"] = (
            raw.get("error_json") if isinstance(raw.get("error_json"), dict) else {}
        )
        return DocsClipIngestJobResponse.model_validate(raw).model_dump(mode="json")

    def _job_id_or_404(value: str) -> UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=404, detail="ClipIngest job not found") from exc

    def _idempotency_key(request: Request) -> str | None:
        value = request.headers.get("idempotency-key")
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if len(value) > 128:
            raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
        return value

    def _job_request_snapshot(
        scope: dict[str, Any],
        *,
        skip_image_recognition: bool,
        enable_external_research: bool,
        target_node_id: UUID | None,
        upload_ids: list[str],
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build the allowlisted encrypted request snapshot consumed by worker."""

        session_context = scope.get("session_context")
        project_metadata = scope.get("project_metadata")
        session_privacy = None
        project_privacy = None
        if isinstance(session_context, Mapping):
            session_privacy = _safe_job_privacy(session_context.get("privacy_mode"))
        if isinstance(project_metadata, Mapping):
            project_privacy = _safe_job_privacy(project_metadata.get("privacy_mode"))
        safe_scope = {
            "user_id": str(scope.get("user_id") or ""),
            "session_id": str(scope.get("session_id")) if scope.get("session_id") else None,
            "project_id": str(scope.get("project_id")) if scope.get("project_id") else None,
        }
        safe_session = {
            "id": safe_scope["session_id"],
            "privacy_mode": session_privacy,
        }
        safe_project = {
            "id": safe_scope["project_id"],
            "privacy_mode": project_privacy,
        }
        safe_privacy = {
            "session": session_privacy,
            "project": project_privacy,
        }
        safe_flags = {
            "skip_image_recognition": bool(skip_image_recognition),
            "enable_external_research": bool(enable_external_research),
        }
        safe_target = {"node_id": str(target_node_id) if target_node_id else None}
        safe_attachments = []
        for item in attachments or []:
            safe_item = _safe_job_attachment_metadata(item)
            if safe_item:
                safe_attachments.append(safe_item)
        # Keep both the descriptive sections and the worker's stable legacy
        # keys.  No source body, headers, credentials, or arbitrary metadata
        # is copied into this encrypted snapshot.
        return {
            "scope": safe_scope,
            "session": safe_session,
            "project": safe_project,
            "privacy": safe_privacy,
            "flags": safe_flags,
            "target": safe_target,
            "upload_ids": list(upload_ids),
            "attachments": safe_attachments,
            "session_context": {"privacy_mode": session_privacy}
            if session_privacy
            else {},
            "project_metadata": {
                "project_id": safe_scope["project_id"],
                "privacy_mode": project_privacy,
            }
            if project_privacy
            else {},
            "skip_image_recognition": bool(skip_image_recognition),
            "enable_external_research": bool(enable_external_research),
        }

    def _safe_job_privacy(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value[:64] if value else None

    def _safe_job_attachment_metadata(value: Any) -> dict[str, Any]:
        """Keep only metadata needed to recover a promoted payload on retry."""

        if not isinstance(value, Mapping):
            return {}
        try:
            upload_id = str(UUID(str(value.get("upload_id"))))
        except (TypeError, ValueError, AttributeError):
            return {}
        raw_name = str(value.get("file_name") or "").replace("\\", "/")
        raw_name = "".join(char for char in raw_name if char >= " " or char == "\t")
        file_name = raw_name.rsplit("/", 1)[-1].strip().strip(".")[:255]
        if not file_name:
            return {}
        mime_type = str(value.get("mime_type") or "application/octet-stream").split(";", 1)[0].strip().lower()
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*",
            mime_type,
            re.IGNORECASE,
        ):
            mime_type = "application/octet-stream"
        size_bytes = value.get("size_bytes")
        if isinstance(size_bytes, bool):
            return {}
        try:
            size_bytes = int(size_bytes)
        except (TypeError, ValueError):
            return {}
        sha256 = str(value.get("sha256") or "").strip().lower()
        if size_bytes < 0 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            return {}
        is_image = value.get("is_image")
        if not isinstance(is_image, bool):
            is_image = mime_type.startswith("image/")
        created_at = value.get("created_at", 0)
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            created_at = 0.0
        return {
            "upload_id": upload_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "is_image": is_image,
            "created_at": created_at,
        }

    async def _resolve_job_scope(
        session,
        *,
        user_id: UUID,
        request_scope: dict[str, Any],
        target_node_id: UUID | None,
    ) -> tuple[UUID | None, UUID | None]:
        """Resolve the authorized library and optional target for a queued job."""

        if target_node_id is not None:
            target = await session.get(KnowledgeNode, target_node_id)
            library = (
                await session.get(DocsLibrary, getattr(target, "docs_library_id", None))
                if target is not None
                else None
            )
            if (
                target is None
                or library is None
                or getattr(target, "archived_at", None) is not None
                or not await can_write_node(session, target, user_id, library=library)
            ):
                raise HTTPException(status_code=404, detail="Docs node not found")
            return library.id, target.id

        project_id = _normalize_scope_uuid(request_scope.get("project_id"))
        service = _docs_service(session)
        try:
            library = (
                await service.get_project_information_library(project_id, user_id)
                if project_id is not None
                else await service.ensure_library(user_id)
            )
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Docs workspace not found") from exc
        return getattr(library, "id", None), None

    async def _find_job_by_idempotency(session, user_id: UUID, key: str | None):
        if not key:
            return None
        return await session.scalar(
            select(DocsClipIngestJob)
            .where(
                DocsClipIngestJob.actor_user_id == user_id,
                DocsClipIngestJob.idempotency_key == key,
            )
            .limit(1)
        )

    async def _active_clip_ingest_upload_ids() -> list[str] | None:
        """Collect upload IDs referenced by queued/running durable jobs.

        Staging GC is request-triggered and may perform a global sweep.  The
        sweep therefore needs a database-backed protection set, not merely the
        current request's IDs, so one user's upload request cannot delete an
        older active job belonging to another user.
        """

        session = None
        try:
            manager = get_db_manager()
            if manager is None:
                return None
            session = await manager.get_session()
            result = await session.execute(
                select(DocsClipIngestJob).where(
                    DocsClipIngestJob.status.in_(("queued", "running"))
                )
            )
            protected: set[str] = set()
            for job in result.scalars().all():
                raw_ids = job.upload_ids_json
                if not isinstance(raw_ids, (list, tuple, set)):
                    continue
                for raw_id in raw_ids:
                    parsed = _normalize_scope_uuid(raw_id)
                    if parsed is not None:
                        protected.add(str(parsed))
            return sorted(protected)
        except Exception:
            if session is not None:
                try:
                    await session.rollback()
                except Exception:
                    pass
            logger.debug("Unable to load active Docs ClipIngest upload IDs", exc_info=True)
            return None
        finally:
            if session is not None:
                await session.close()

    async def _run_clip_storage_gc(user_id: UUID) -> None:
        """Run fail-soft GC while protecting every active durable upload."""

        cleanup = getattr(clip_storage, "opportunistic_gc", None)
        if not callable(cleanup):
            return
        protected = await _active_clip_ingest_upload_ids()
        # If the protection query cannot be completed, do not risk a global
        # sweep that might delete an active durable job.  GC is maintenance,
        # never an availability dependency.
        if protected is None:
            return
        try:
            try:
                parameters = inspect.signature(cleanup).parameters
            except (TypeError, ValueError):
                parameters = {}
            kwargs: dict[str, Any] = {}
            if "protected_upload_ids" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                kwargs["protected_upload_ids"] = protected
            cleanup(user_id, **kwargs)
        except Exception:  # noqa: BLE001
            logger.debug("ClipIngest staging GC skipped", exc_info=True)

    def _job_snapshot_uuid(
        snapshot: Mapping[str, Any],
        key: str,
    ) -> tuple[UUID | None, bool]:
        """Parse an immutable job-scope UUID without widening on bad input."""

        value = snapshot.get(key)
        if value in (None, ""):
            return None, True
        parsed = _normalize_scope_uuid(value)
        return parsed, parsed is not None

    async def _job_read_allowed(
        session,
        *,
        job: DocsClipIngestJob,
        user_id: UUID,
    ) -> bool:
        """Re-check current Docs ACLs before returning a durable job DTO.

        ``actor_user_id`` is necessary but not sufficient: a project/session
        or shared target can be revoked after the worker succeeds.  The
        encrypted request snapshot is an immutable scope assertion; nullable
        foreign keys are never treated as a fallback to personal scope.
        """

        if _normalize_scope_uuid(getattr(job, "actor_user_id", None)) != user_id:
            return False
        raw_request = job.request_json if isinstance(job.request_json, Mapping) else {}
        raw_scope = raw_request.get("scope")
        scope = raw_scope if isinstance(raw_scope, Mapping) else {}
        snapshot_session, session_valid = _job_snapshot_uuid(scope, "session_id")
        snapshot_project, project_valid = _job_snapshot_uuid(scope, "project_id")
        if not session_valid or not project_valid:
            return False
        persisted_session = _normalize_scope_uuid(getattr(job, "session_id", None))
        persisted_project = _normalize_scope_uuid(getattr(job, "project_id", None))
        if persisted_session != snapshot_session or persisted_project != snapshot_project:
            return False

        library_id = _normalize_scope_uuid(getattr(job, "docs_library_id", None))
        if library_id is None:
            return False
        try:
            library = await session.get(DocsLibrary, library_id)
        except Exception:
            return False
        if library is None:
            return False

        if persisted_session is not None:
            from ..memory.conversation_repository import ConversationRepository

            try:
                repo = ConversationRepository(session)
                if not await repo.user_has_session_access(str(persisted_session), str(user_id)):
                    return False
                conversation = await repo.get_session_by_id(
                    str(persisted_session), with_messages=False
                )
            except Exception:
                return False
            if conversation is None:
                return False
            if _normalize_scope_uuid(getattr(conversation, "project_id", None)) != persisted_project:
                return False

        if persisted_project is not None:
            from ..memory.project_repository import ProjectRepository

            try:
                if not await ProjectRepository.has_permission(
                    session, persisted_project, user_id, "read"
                ):
                    return False
            except Exception:
                return False
        elif not await library_can_read(session, library, user_id):
            return False

        raw_target = raw_request.get("target")
        target_value = raw_target.get("node_id") if isinstance(raw_target, Mapping) else None
        snapshot_target, target_valid = _job_snapshot_uuid({"node_id": target_value}, "node_id")
        if not target_valid:
            return False
        persisted_target = _normalize_scope_uuid(getattr(job, "target_node_id", None))
        if persisted_target != snapshot_target:
            return False
        if persisted_target is not None:
            target = await session.get(KnowledgeNode, persisted_target)
            if target is None or _normalize_scope_uuid(getattr(target, "docs_library_id", None)) != library_id:
                return False
            try:
                if not await can_read_node(session, target, user_id, library=library):
                    return False
            except Exception:
                return False
        return True

    async def _serialize_existing_job_if_allowed(
        session,
        *,
        job: DocsClipIngestJob | None,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        """Return an idempotent duplicate only after current ACL recheck."""

        if job is None:
            return None
        if not await _job_read_allowed(session, job=job, user_id=user_id):
            raise HTTPException(
                status_code=409,
                detail="ClipIngest jobのscopeが利用できないため再利用できません",
            )
        return _serialize_clip_ingest_job(job)

    @router.post(
        "/ingest/jobs",
        response_model=DocsClipIngestJobResponse,
        status_code=202,
    )
    async def enqueue_clip_ingest_job(
        body: DocsIngestBody,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        """Persist a durable ClipIngest request; execution belongs to worker."""

        user_id = await _get_current_user(request)
        upload_ids = [str(item).strip() for item in body.upload_ids if str(item).strip()]
        source = canonicalize_clip_source(body.source or "")
        if not source.strip() and not upload_ids:
            raise HTTPException(status_code=400, detail="取り込む情報または添付ファイルが必要です")
        idempotency_key = _idempotency_key(request)
        session = await get_db_manager().get_session()
        try:
            existing = await _find_job_by_idempotency(session, user_id, idempotency_key)
            if existing is not None:
                return await _serialize_existing_job_if_allowed(
                    session, job=existing, user_id=user_id
                )
            try:
                resolved_uploads = clip_storage.resolve_uploads(user_id, upload_ids)
            except ClipUploadError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            upload_ids = [item.upload_id for item in resolved_uploads]
            attachments = [item.to_public_dict() for item in resolved_uploads]
            request_scope = _request_scope(request, user_id)
            request_scope = await _validate_ingest_scope(
                session,
                user_id=user_id,
                session_id=request_scope.get("session_id"),
                project_id=request_scope.get("project_id"),
            )
            docs_library_id, resolved_target_id = await _resolve_job_scope(
                session,
                user_id=user_id,
                request_scope=request_scope,
                target_node_id=body.target_node_id,
            )
            now = datetime.utcnow()
            job = DocsClipIngestJob(
                id=uuid4(),
                actor_user_id=user_id,
                docs_library_id=docs_library_id,
                session_id=_normalize_scope_uuid(request_scope.get("session_id")),
                project_id=_normalize_scope_uuid(request_scope.get("project_id")),
                target_node_id=resolved_target_id,
                retry_of_job_id=None,
                receipt_id=None,
                source_text=source,
                source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
                upload_ids_json=upload_ids,
                request_json=_job_request_snapshot(
                    request_scope,
                    skip_image_recognition=body.skip_image_recognition,
                    enable_external_research=body.enable_external_research,
                    target_node_id=resolved_target_id,
                    upload_ids=upload_ids,
                    attachments=attachments,
                ),
                status="queued",
                idempotency_key=idempotency_key,
                attempt_count=0,
                retryable=True,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.commit()
            return _serialize_clip_ingest_job(job)
        except IntegrityError:
            await session.rollback()
            existing = await _find_job_by_idempotency(session, user_id, idempotency_key)
            if existing is not None:
                return await _serialize_existing_job_if_allowed(
                    session, job=existing, user_id=user_id
                )
            raise HTTPException(status_code=409, detail="ClipIngest job could not be created")
        except HTTPException:
            await session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("Failed to enqueue Docs ClipIngest job")
            raise HTTPException(status_code=500, detail="ClipIngest job could not be created") from exc
        finally:
            await session.close()

    @router.get(
        "/ingest/jobs",
        response_model=DocsClipIngestJobListResponse,
    )
    async def list_clip_ingest_jobs(
        request: Request,
        limit: int = Query(50, ge=1, le=50),
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            rows = list(
                (
                    await session.execute(
                        select(DocsClipIngestJob)
                        .where(DocsClipIngestJob.actor_user_id == user_id)
                        .order_by(DocsClipIngestJob.created_at.desc(), DocsClipIngestJob.id.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            jobs = []
            for row in rows:
                try:
                    if await _job_read_allowed(session, job=row, user_id=user_id):
                        jobs.append(_serialize_clip_ingest_job(row))
                except Exception:
                    # A revoked/deleted scope is intentionally indistinguishable
                    # from an absent job at this boundary.
                    logger.debug("Skipping inaccessible Docs ClipIngest job", exc_info=True)
            return {"jobs": jobs, "items": list(jobs)}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("Failed to list Docs ClipIngest jobs")
            raise HTTPException(status_code=500, detail="ClipIngest jobs could not be loaded") from exc
        finally:
            await session.close()

    @router.get(
        "/ingest/jobs/{job_id}",
        response_model=DocsClipIngestJobResponse,
    )
    async def get_clip_ingest_job(
        request: Request,
        job_id: str,
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _get_current_user(request)
        parsed_job_id = _job_id_or_404(job_id)
        session = await get_db_manager().get_session()
        try:
            row = await session.scalar(
                select(DocsClipIngestJob).where(
                    DocsClipIngestJob.id == parsed_job_id,
                    DocsClipIngestJob.actor_user_id == user_id,
                )
            )
            if row is None or not await _job_read_allowed(
                session, job=row, user_id=user_id
            ):
                raise HTTPException(status_code=404, detail="ClipIngest job not found")
            return _serialize_clip_ingest_job(row)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("Failed to load Docs ClipIngest job")
            raise HTTPException(status_code=500, detail="ClipIngest job could not be loaded") from exc
        finally:
            await session.close()

    @router.post(
        "/ingest/jobs/{job_id}/retry",
        response_model=DocsClipIngestJobResponse,
        status_code=202,
    )
    async def retry_clip_ingest_job(
        request: Request,
        job_id: str,
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _get_current_user(request)
        parsed_job_id = _job_id_or_404(job_id)
        idempotency_key = _idempotency_key(request)
        session = await get_db_manager().get_session()
        try:
            existing = await _find_job_by_idempotency(session, user_id, idempotency_key)
            if existing is not None:
                return await _serialize_existing_job_if_allowed(
                    session, job=existing, user_id=user_id
                )
            source_job = await session.scalar(
                select(DocsClipIngestJob).where(
                    DocsClipIngestJob.id == parsed_job_id,
                    DocsClipIngestJob.actor_user_id == user_id,
                )
            )
            if source_job is None:
                raise HTTPException(status_code=404, detail="ClipIngest job not found")
            if source_job.status != "failed" or not bool(source_job.retryable):
                raise HTTPException(status_code=409, detail="ClipIngest job is not retryable")
            source_request = source_job.request_json if isinstance(source_job.request_json, Mapping) else {}
            source_scope = source_request.get("scope")
            source_scope = source_scope if isinstance(source_scope, Mapping) else {}
            snapshot_session, session_valid = _job_snapshot_uuid(source_scope, "session_id")
            snapshot_project, project_valid = _job_snapshot_uuid(source_scope, "project_id")
            persisted_session = _normalize_scope_uuid(source_job.session_id)
            persisted_project = _normalize_scope_uuid(source_job.project_id)
            if (
                not session_valid
                or not project_valid
                or snapshot_session != persisted_session
                or snapshot_project != persisted_project
            ):
                source_job.retryable = False
                source_job.updated_at = datetime.utcnow()
                await session.commit()
                raise HTTPException(
                    status_code=409,
                    detail="ClipIngest jobのscopeが利用できないため再試行できません",
                )
            snapshot_target = source_request.get("target")
            snapshot_target_id = (
                snapshot_target.get("node_id")
                if isinstance(snapshot_target, Mapping)
                else None
            )
            if snapshot_target_id:
                try:
                    immutable_target_id = UUID(str(snapshot_target_id))
                except (TypeError, ValueError, AttributeError) as exc:
                    source_job.retryable = False
                    source_job.updated_at = datetime.utcnow()
                    await session.commit()
                    raise HTTPException(
                        status_code=409,
                        detail="ClipIngest jobの保存先指定を確認できません",
                    ) from exc
                if source_job.target_node_id is None or UUID(str(source_job.target_node_id)) != immutable_target_id:
                    # The FK may be SET NULL when the explicit target was
                    # deleted.  Never turn that immutable request into an
                    # automatic classification on retry.
                    source_job.retryable = False
                    source_job.updated_at = datetime.utcnow()
                    await session.commit()
                    raise HTTPException(
                        status_code=409,
                        detail="ClipIngest jobの保存先が利用できないため再試行できません",
                    )
            # Re-check the persisted scope/target against current ACLs before
            # creating a second queued row.  A retry must not resurrect access
            # that was revoked after the original request.
            retry_scope = await _validate_ingest_scope(
                session,
                user_id=user_id,
                session_id=source_job.session_id,
                project_id=source_job.project_id,
            )
            if source_job.session_id and retry_scope.get("session_id") is None:
                raise HTTPException(status_code=404, detail="ClipIngest job not found")
            if source_job.project_id and retry_scope.get("project_id") is None:
                raise HTTPException(status_code=404, detail="ClipIngest job not found")
            resolved_library_id, resolved_target_id = await _resolve_job_scope(
                session,
                user_id=user_id,
                request_scope=retry_scope,
                target_node_id=source_job.target_node_id,
            )
            source = canonicalize_clip_source(source_job.source_text or "")
            upload_ids = [
                str(item).strip()
                for item in (source_job.upload_ids_json or [])
                if str(item).strip()
            ]
            source_attachments = source_request.get("attachments")
            attachments = [
                safe_item
                for item in source_attachments
                if (safe_item := _safe_job_attachment_metadata(item))
            ] if isinstance(source_attachments, (list, tuple)) else []
            flags = source_request.get("flags") if isinstance(source_request.get("flags"), Mapping) else {}
            target_node_id = resolved_target_id
            now = datetime.utcnow()
            retry_job = DocsClipIngestJob(
                id=uuid4(),
                actor_user_id=user_id,
                docs_library_id=resolved_library_id or source_job.docs_library_id,
                session_id=_normalize_scope_uuid(retry_scope.get("session_id")),
                project_id=_normalize_scope_uuid(retry_scope.get("project_id")),
                target_node_id=target_node_id,
                retry_of_job_id=source_job.id,
                receipt_id=None,
                source_text=source,
                source_sha256=source_job.source_sha256
                or hashlib.sha256(source.encode("utf-8")).hexdigest(),
                upload_ids_json=upload_ids,
                request_json=_job_request_snapshot(
                    retry_scope,
                    skip_image_recognition=bool(
                        flags.get("skip_image_recognition", source_request.get("skip_image_recognition", False))
                    ),
                    enable_external_research=bool(
                        flags.get("enable_external_research", source_request.get("enable_external_research", True))
                    ),
                    target_node_id=target_node_id,
                    upload_ids=upload_ids,
                    attachments=attachments,
                ),
                status="queued",
                idempotency_key=idempotency_key,
                attempt_count=0,
                retryable=True,
                created_at=now,
                updated_at=now,
            )
            session.add(retry_job)
            await session.commit()
            return _serialize_clip_ingest_job(retry_job)
        except IntegrityError:
            await session.rollback()
            existing = await _find_job_by_idempotency(session, user_id, idempotency_key)
            if existing is not None:
                return await _serialize_existing_job_if_allowed(
                    session, job=existing, user_id=user_id
                )
            raise HTTPException(status_code=409, detail="ClipIngest retry could not be created")
        except HTTPException:
            await session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.exception("Failed to retry Docs ClipIngest job")
            raise HTTPException(status_code=500, detail="ClipIngest retry could not be created") from exc
        finally:
            await session.close()

    @router.post("/ingest/uploads")
    async def upload_ingest_files(
        request: Request,
        files: list[UploadFile] | None = File(default=None),
        _auth=Depends(require_auth_dependency),
    ):
        """Stage ClipIngest files in the authenticated user's namespace."""

        user_id = await _get_current_user(request)
        if not files:
            raise HTTPException(status_code=400, detail="アップロードファイルがありません")
        # Opportunistically reclaim abandoned uploads before accepting more
        # data, but protect every queued/running durable job in the database.
        await _run_clip_storage_gc(user_id)
        try:
            staged = await clip_storage.stage_uploads(user_id, files)
        except ClipUploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"uploads": [item.to_public_dict() for item in staged]}

    @router.post("/ingest", response_model=ClipIngestResponse)
    async def ingest_clip(
        body: DocsIngestBody,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        """URL・文章・staged添付を設定済み取り込み先へ保存する。"""
        user_id = await _get_current_user(request)
        request_scope = _request_scope(request, user_id)
        if not str(body.source or "").strip() and not body.upload_ids:
            raise HTTPException(status_code=400, detail="取り込む情報または添付ファイルが必要です")
        # Cleanup also runs when a user only submits existing staged IDs (or
        # cancels an upload without making another multipart request).  It is
        # deliberately fail-soft: a filesystem maintenance hiccup must not
        # turn an otherwise valid ingest into a 500.
        await _run_clip_storage_gc(user_id)
        if docs_ingest_plan_llm is None and docs_ingest_plan_llm_factory is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "category": "llm_unavailable",
                    "code": GenerationErrorKind.LLM_NOT_CONFIGURED,
                    "message": user_message_for_generation_kind(
                        GenerationErrorKind.LLM_NOT_CONFIGURED
                    ),
                    "retryable": False,
                },
            )

        result = None

        async def _cleanup_failed_ingest() -> None:
            """Remove promoted files when DB/response preparation rolls back."""

            if result is None:
                return
            storage = getattr(result, "_clip_ingest_storage", None)
            paths = list(getattr(result, "_clip_ingest_promoted_paths", []) or [])
            upload_ids = list(getattr(result, "_clip_ingest_upload_ids", []) or [])
            if storage is None:
                return
            try:
                if paths:
                    storage.cleanup_promoted(paths)
                if upload_ids:
                    await storage.cleanup_uploads(user_id, upload_ids)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to cleanup ClipIngest side effects after rollback")

        session = await get_db_manager().get_session()
        try:
            # 複数workerや複数端末からの同一ユーザー並行実行もtransaction単位で拒否する。
            lock_result = await session.execute(
                text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtext('docs-ingest'), hashtext(:user_id))"
                ),
                {"user_id": str(user_id)},
            )
            if lock_result.scalar() is not True:
                raise DocsIngestBusyError("クリップ取り込みは既に実行中です")

            request_scope = await _validate_ingest_scope(
                session,
                user_id=user_id,
                session_id=request_scope.get("session_id"),
                project_id=request_scope.get("project_id"),
            )

            # Keep the authenticated scope task-local for supplemental hosted
            # search usage.  Unlike client attributes, ContextVar state does
            # not race with another HTTP request using the shared main client.
            from ..services.turn_context import reset_turn_context, set_turn_context

            turn_token = set_turn_context(
                user_id=request_scope["user_id"],
                session_id=request_scope["session_id"],
                project_id=request_scope["project_id"],
            )
            try:
                if docs_ingest_plan_llm_factory is not None:
                    factory_kwargs = _factory_scope_kwargs(
                        docs_ingest_plan_llm_factory,
                        request_scope,
                    )
                    plan_context = (
                        docs_ingest_plan_llm_factory(**factory_kwargs)
                        if factory_kwargs is not None
                        else docs_ingest_plan_llm_factory()
                    )
                    async with plan_context as request_plan_llm:
                        result = await DocsIngestService(
                            session,
                            config=docs_ingest_config,
                            session_context=request_scope.get("session_context"),
                            project_metadata=request_scope.get("project_metadata"),
                            storage=clip_storage,
                        ).run(
                            user_id=user_id,
                            source=body.source,
                            plan_llm=request_plan_llm,
                            upload_ids=body.upload_ids,
                            skip_image_recognition=body.skip_image_recognition,
                            enable_external_research=body.enable_external_research,
                            target_node_id=body.target_node_id,
                            clip_ingest_route=getattr(request_plan_llm, "clip_ingest_route", None),
                            clip_ingest_client=getattr(request_plan_llm, "clip_ingest_client", None),
                        )
                else:
                    result = await DocsIngestService(
                        session,
                        config=docs_ingest_config,
                        session_context=request_scope.get("session_context"),
                        project_metadata=request_scope.get("project_metadata"),
                        storage=clip_storage,
                    ).run(
                        user_id=user_id,
                        source=body.source,
                        plan_llm=docs_ingest_plan_llm,
                        upload_ids=body.upload_ids,
                        skip_image_recognition=body.skip_image_recognition,
                        enable_external_research=body.enable_external_research,
                        target_node_id=body.target_node_id,
                        clip_ingest_route=getattr(docs_ingest_plan_llm, "clip_ingest_route", None),
                        clip_ingest_client=getattr(docs_ingest_plan_llm, "clip_ingest_client", None),
                    )
            finally:
                reset_turn_context(turn_token)
            node = await session.get(KnowledgeNode, UUID(result.open_node_id))
            if node is None:
                raise ClipIngestError("保存したDocsノードを確認できません")
            nodes = await _load_node_subtree(session, node)
            try:
                result_payload = asdict(result)
            except TypeError:
                # Keep the route boundary compatible with lightweight result
                # doubles used by integrations while real ClipIngestResult
                # remains the normal path.
                result_payload = dict(getattr(result, "__dict__", {}) or {})
            # The receipt field is owned by the semantic worker's optional
            # ClipIngestResult contract.  Copy it defensively when present so
            # this server-side persistence workstream does not edit that
            # shared dataclass.
            for field_name in (
                "receipt",
                "receipt_id",
                "receipt_ids",
                "clip_ingest_receipt_id",
            ):
                value = getattr(result, field_name, None)
                if value is not None:
                    result_payload[field_name] = value
            payload = {
                "result": result_payload,
                "node": serialize_docs_node(node),
                "nodes": [serialize_docs_node(item) for item in nodes],
            }
            receipt_value = getattr(result, "receipt", None)
            if isinstance(receipt_value, dict):
                payload["receipt"] = receipt_value
                payload["receipts"] = [receipt_value]
                if receipt_value.get("id"):
                    payload["receipt_id"] = receipt_value["id"]
            await session.commit()
            return payload
        except DocsIngestBusyError as exc:
            await session.rollback()
            await _cleanup_failed_ingest()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DocsIngestUnavailableError as exc:
            await session.rollback()
            await _cleanup_failed_ingest()
            # The exception retains provider diagnostics for server-side
            # logging, but only its typed allowlisted detail may cross the
            # global HTTP boundary.  Never interpolate the raw exception here
            # (OpenAI errors can contain keys, URLs, and response bodies).
            logger.error(
                "Docs clip ingest unavailable code=%s technical_detail=%s request_id=%s",
                getattr(exc, "safe_code", "unknown"),
                (
                    exc.safe_technical_detail()
                    if callable(getattr(exc, "safe_technical_detail", None))
                    else ""
                ),
                (
                    request.headers.get("x-request-id")
                    or request.headers.get("x-correlation-id")
                    or request.headers.get("x-trace-id")
                    or ""
                )
                if re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,128}",
                    request.headers.get("x-request-id")
                    or request.headers.get("x-correlation-id")
                    or request.headers.get("x-trace-id")
                    or "",
                )
                else "",
            )
            detail = (
                exc.safe_detail(
                    trace_id=request.headers.get("x-trace-id"),
                    request_id=request.headers.get("x-request-id")
                    or request.headers.get("x-correlation-id"),
                )
                if callable(getattr(exc, "safe_detail", None))
                else {"category": "llm_unavailable", "code": "unknown"}
            )
            raise HTTPException(status_code=503, detail=detail) from exc
        except ClipIngestError as exc:
            await session.rollback()
            await _cleanup_failed_ingest()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            await session.rollback()
            await _cleanup_failed_ingest()
            raise
        except Exception as exc:
            await session.rollback()
            await _cleanup_failed_ingest()
            logger.exception("Docs clip ingest failed")
            raise HTTPException(
                status_code=500,
                detail="クリップ取り込みを完了できませんでした。Docsは変更していません",
            ) from exc
        finally:
            await session.close()

    _RECEIPT_RESULT_FIELDS = (
        "target_id",
        "target_label",
        "action",
        "open_node_id",
        "open_node_title",
        "direct_urls",
        "supplemental_urls",
        "failed_urls",
        "used_urls",
        "unconfirmed",
    )

    def _receipt_not_found() -> HTTPException:
        """Use one indistinguishable response for all receipt ACL failures."""

        return HTTPException(status_code=404, detail="ClipIngest receipt not found")

    async def _readable_clip_topic(
        session,
        *,
        topic_id: UUID,
        user_id: UUID,
    ) -> tuple[KnowledgeNode, DocsLibrary]:
        """Resolve a topic through its library and node ACL boundary."""

        topic = await session.get(KnowledgeNode, topic_id)
        if topic is None or getattr(topic, "archived_at", None) is not None:
            raise _receipt_not_found()
        library = await session.get(DocsLibrary, getattr(topic, "docs_library_id", None))
        if library is None:
            raise _receipt_not_found()
        if _normalize_scope_uuid(getattr(topic, "docs_library_id", None)) != _normalize_scope_uuid(
            getattr(library, "id", None)
        ):
            raise _receipt_not_found()
        try:
            readable = await can_read_node(
                session,
                topic,
                user_id,
                library=library,
                include_archived=False,
            )
        except Exception:
            readable = False
        if not readable:
            raise _receipt_not_found()
        return topic, library

    async def _readable_clip_receipt(
        session,
        *,
        receipt_id: UUID,
        user_id: UUID,
    ) -> tuple[ClipIngestReceipt, KnowledgeNode, DocsLibrary]:
        """Resolve one receipt through its topic/library ACL boundary.

        A receipt's creator is not an ACL shortcut.  The topic is authoritative
        for visibility, while the duplicated library ID is checked first as a
        cross-library consistency fence.  Archived topics intentionally make
        their receipts unavailable from this history surface.
        """

        receipt = await session.get(ClipIngestReceipt, receipt_id)
        if receipt is None:
            raise _receipt_not_found()
        topic = await session.get(KnowledgeNode, receipt.topic_node_id)
        library = await session.get(DocsLibrary, receipt.docs_library_id)
        if topic is None or library is None:
            raise _receipt_not_found()
        receipt_library_id = _normalize_scope_uuid(receipt.docs_library_id)
        if (
            receipt_library_id is None
            or _normalize_scope_uuid(getattr(topic, "docs_library_id", None))
            != receipt_library_id
            or _normalize_scope_uuid(getattr(library, "id", None)) != receipt_library_id
            or getattr(topic, "archived_at", None) is not None
        ):
            raise _receipt_not_found()
        target_id = getattr(receipt, "target_node_id", None)
        if target_id is not None:
            target = await session.get(KnowledgeNode, target_id)
            if target is None or _normalize_scope_uuid(getattr(target, "docs_library_id", None)) != receipt_library_id:
                raise _receipt_not_found()
        try:
            readable = await can_read_node(
                session,
                topic,
                user_id,
                library=library,
                include_archived=False,
            )
        except Exception:
            readable = False
        if not readable:
            # Keep cross-user, cross-library, and ACL-denied rows
            # indistinguishable from a missing receipt.
            raise _receipt_not_found()
        return receipt, topic, library

    def _receipt_result_fields(result_json: Any) -> dict[str, Any]:
        """Project the allowlisted result snapshot into the public DTO."""

        raw = result_json if isinstance(result_json, dict) else {}
        projected: dict[str, Any] = {}
        for field_name in _RECEIPT_RESULT_FIELDS:
            if field_name not in raw:
                continue
            field_value = raw[field_name]
            if field_name in {"direct_urls", "supplemental_urls", "used_urls", "unconfirmed"}:
                projected[field_name] = list(field_value) if isinstance(field_value, list) else []
            elif field_name == "failed_urls":
                projected[field_name] = list(field_value) if isinstance(field_value, list) else []
            else:
                projected[field_name] = field_value
        return projected

    def _receipt_response(
        receipt: ClipIngestReceipt,
        topic: KnowledgeNode,
        library: DocsLibrary,
        *,
        include_source_text: bool,
    ) -> dict[str, Any]:
        """Serialize a receipt without exposing encrypted request metadata."""

        value = receipt.to_dict(include_source_text=include_source_text)
        # request_json is an encrypted audit snapshot, not a public receipt
        # field.  In particular, do not let future upload metadata become a
        # list/detail credential or filesystem-path leak.
        value.pop("request_json", None)
        result_json = value.get("result_json")
        result_fields = _receipt_result_fields(result_json)
        value["result"] = result_fields
        value["target_id"] = result_fields.get("target_id") or value.get("target_node_id")
        value["target_label"] = result_fields.get("target_label") or None
        value["action"] = value.get("action") or result_fields.get("action")
        value["open_node_id"] = result_fields.get("open_node_id") or value.get("topic_node_id")
        value["open_node_title"] = result_fields.get("open_node_title") or getattr(topic, "title", None)
        value["node_id"] = value.get("topic_node_id")
        value["node_title"] = getattr(topic, "title", None)
        value["title"] = getattr(topic, "title", None)
        value["source_type"] = "clip_ingest"
        value["topic_title"] = getattr(topic, "title", None)
        value["library"] = {
            "id": str(library.id),
            "name": library.name,
        }
        # Keep the selected provenance fields available to clients without
        # making them parse the encrypted snapshot.  This is metadata, never
        # the original source body.
        for field_name in _RECEIPT_RESULT_FIELDS:
            if field_name in result_fields:
                value[field_name] = result_fields[field_name]
        if not include_source_text:
            # The model already omits this key, but make the list contract
            # explicit in case a compatibility model double adds it.
            value.pop("source_text", None)
            value.pop("result_json", None)
            value.pop("actor_user_id", None)
        else:
            value["status"] = "completed"
            value["error"] = None
        return value

    async def _list_clip_ingest_receipts(
        session,
        *,
        user_id: UUID,
        limit: int,
        selected_library_id: UUID | None = None,
        selected_topic_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """List only ACL-readable, non-archived receipt summaries."""

        # Query bounded metadata candidates, then apply point ACL authority on
        # every topic.  Source/request ciphertext is not selected into the
        # response; the model serializer is called only for canonical fields.
        stmt = (
            select(ClipIngestReceipt, KnowledgeNode, DocsLibrary)
            .join(KnowledgeNode, KnowledgeNode.id == ClipIngestReceipt.topic_node_id)
            .join(DocsLibrary, DocsLibrary.id == ClipIngestReceipt.docs_library_id)
            .where(
                KnowledgeNode.docs_library_id == ClipIngestReceipt.docs_library_id,
                KnowledgeNode.archived_at.is_(None),
            )
            .order_by(ClipIngestReceipt.created_at.desc(), ClipIngestReceipt.id.desc())
            .limit(limit * 4)
        )
        if selected_library_id is not None:
            stmt = stmt.where(ClipIngestReceipt.docs_library_id == selected_library_id)
        if selected_topic_id is not None:
            stmt = stmt.where(ClipIngestReceipt.topic_node_id == selected_topic_id)

        rows = list((await session.execute(stmt)).all())
        receipts: list[dict[str, Any]] = []
        for receipt, topic, library in rows:
            if len(receipts) >= limit:
                break
            receipt_library_id = _normalize_scope_uuid(receipt.docs_library_id)
            if (
                receipt_library_id is None
                or receipt_library_id != _normalize_scope_uuid(topic.docs_library_id)
                or receipt_library_id != _normalize_scope_uuid(library.id)
                or topic.archived_at is not None
            ):
                continue
            try:
                readable = await can_read_node(
                    session,
                    topic,
                    user_id,
                    library=library,
                    include_archived=False,
                )
            except Exception:
                readable = False
            if readable:
                receipts.append(
                    _receipt_response(
                        receipt,
                        topic,
                        library,
                        include_source_text=False,
                    )
                )
        return receipts

    @router.get(
        "/nodes/{node_id}/clip-ingest-receipts",
        response_model=ClipIngestReceiptListResponse,
        operation_id="list_clip_ingest_receipts_for_node",
    )
    async def list_clip_ingest_receipts_for_node(
        request: Request,
        node_id: str,
        limit: int = Query(50, ge=1, le=100),
        _auth=Depends(require_auth_dependency),
    ):
        """List receipts for one ACL-readable topic node."""

        user_id = await _get_current_user(request)
        try:
            normalized_node_id = UUID(node_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid node id") from exc
        session = await get_db_manager().get_session()
        try:
            topic, library = await _readable_clip_topic(
                session,
                topic_id=normalized_node_id,
                user_id=user_id,
            )
            receipts = await _list_clip_ingest_receipts(
                session,
                user_id=user_id,
                limit=limit,
                selected_library_id=library.id,
                selected_topic_id=topic.id,
            )
            return {"receipts": receipts, "items": receipts}
        finally:
            await session.close()

    @router.get(
        "/ingest/receipts",
        response_model=ClipIngestReceiptListResponse,
        operation_id="list_clip_ingest_receipts_legacy_ingest",
    )
    @router.get(
        "/receipts",
        response_model=ClipIngestReceiptListResponse,
        operation_id="list_clip_ingest_receipts_legacy",
    )
    @router.get(
        "/clip-ingest/receipts",
        response_model=ClipIngestReceiptListResponse,
        operation_id="list_clip_ingest_receipts_legacy_clip_ingest",
    )
    async def list_clip_ingest_receipts(
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        library_id: UUID | None = Query(None),
        docs_library_id: UUID | None = Query(None),
        topic_id: UUID | None = Query(None),
        topic_node_id: UUID | None = Query(None),
        node_id: UUID | None = Query(None),
        _auth=Depends(require_auth_dependency),
    ):
        """List compact ClipIngest receipt metadata visible to the actor."""

        user_id = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            receipts = await _list_clip_ingest_receipts(
                session,
                user_id=user_id,
                limit=limit,
                selected_library_id=library_id or docs_library_id,
                selected_topic_id=topic_id or topic_node_id or node_id,
            )
            return {"receipts": receipts, "items": receipts}
        finally:
            await session.close()

    @router.get(
        "/clip-ingest-receipts/{receipt_id}",
        response_model=ClipIngestReceiptDetailEnvelope,
        operation_id="get_clip_ingest_receipt",
    )
    @router.get(
        "/ingest/receipts/{receipt_id}",
        response_model=ClipIngestReceiptDetailEnvelope,
        operation_id="get_clip_ingest_receipt_legacy_ingest",
    )
    @router.get(
        "/receipts/{receipt_id}",
        response_model=ClipIngestReceiptDetailEnvelope,
        operation_id="get_clip_ingest_receipt_legacy",
    )
    @router.get(
        "/clip-ingest/receipts/{receipt_id}",
        response_model=ClipIngestReceiptDetailEnvelope,
        operation_id="get_clip_ingest_receipt_legacy_clip_ingest",
    )
    async def get_clip_ingest_receipt(
        receipt_id: str,
        request: Request,
        _auth=Depends(require_auth_dependency),
    ):
        """Return one canonical receipt after topic ACL verification."""

        user_id = await _get_current_user(request)
        try:
            normalized_receipt_id = UUID(receipt_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid receipt id") from exc
        session = await get_db_manager().get_session()
        try:
            receipt, topic, library = await _readable_clip_receipt(
                session,
                receipt_id=normalized_receipt_id,
                user_id=user_id,
            )
            return {
                "receipt": _receipt_response(
                    receipt,
                    topic,
                    library,
                    include_source_text=True,
                )
            }
        finally:
            await session.close()

    # ---------------- 読み取り ----------------

    @router.get("/tree")
    async def get_tree(
        request: Request,
        since: Optional[str] = None,
        include_archived: Optional[str] = None,
        project: Optional[str] = None,
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _get_current_user(request)
        since_dt = _parse_since(since)
        session = await get_db_manager().get_session()
        try:
            service = _docs_service(session)
            project_obj = await _resolve_project_ref(service, project)
            library = await _resolve_read_workspace(
                session,
                service,
                user_id=user_id,
                project_id=project_obj.id if project_obj is not None else None,
            )
            await session.commit()
            from sqlalchemy import or_, select
            from sqlalchemy.orm import aliased
            from ..memory.models import (
                KnowledgeEdge,
                KnowledgeField,
                KnowledgeFieldValue,
                KnowledgeNodePlacement,
                KnowledgeNodeSupertag,
                KnowledgeSupertag,
                KnowledgeSupertagField,
            )

            visibility = docs_readable_node_predicate(
                KnowledgeNode,
                docs_library_id=library.id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            node_stmt = select(KnowledgeNode).where(
                KnowledgeNode.docs_library_id == library.id,
                visibility,
            )
            if not (include_archived == "1"):
                node_stmt = node_stmt.where(KnowledgeNode.archived_at.is_(None))
            if since_dt:
                node_stmt = node_stmt.where(
                    or_(KnowledgeNode.updated_at > since_dt, KnowledgeNode.archived_at > since_dt)
                )
            nodes = list((await session.execute(node_stmt)).scalars().all())

            node_supertags = list(
                (await session.execute(
                    select(KnowledgeNodeSupertag)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
                    .where(
                        KnowledgeNode.docs_library_id == library.id,
                        visibility,
                    )
                )).scalars().all()
            )
            field_target = aliased(KnowledgeNode)
            field_target_visibility = docs_readable_node_predicate(
                field_target,
                docs_library_id=library.id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            placement_parent = aliased(KnowledgeNode)
            placement_parent_visibility = docs_readable_node_predicate(
                placement_parent,
                docs_library_id=library.id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            edge_target = aliased(KnowledgeNode)
            edge_target_visibility = docs_readable_node_predicate(
                edge_target,
                docs_library_id=library.id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            full_workspace_access = await library_can_read(session, library, user_id)
            tag_filter = KnowledgeSupertag.docs_library_id == library.id
            if not full_workspace_access:
                tag_filter = tag_filter & KnowledgeSupertag.id.in_(
                    select(KnowledgeNodeSupertag.supertag_id)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
                    .where(
                        KnowledgeNode.docs_library_id == library.id,
                        visibility,
                    )
                )
            supertags = list(
                (await session.execute(select(KnowledgeSupertag).where(tag_filter))).scalars().all()
            )
            supertag_ids = {item.id for item in supertags}
            node_supertags = [
                item for item in node_supertags if item.supertag_id in supertag_ids
            ]
            supertag_fields = list(
                (await session.execute(
                    select(KnowledgeSupertagField)
                    .join(
                        KnowledgeField,
                        KnowledgeField.id == KnowledgeSupertagField.field_id,
                    )
                    .where(
                        KnowledgeSupertagField.supertag_id.in_(supertag_ids)
                        if supertag_ids else text("false"),
                        KnowledgeField.docs_library_id == library.id,
                    )
                )).scalars().all()
            )
            field_ids = {item.field_id for item in supertag_fields}
            fields = list(
                (await session.execute(
                    select(KnowledgeField).where(
                        KnowledgeField.docs_library_id == library.id,
                        KnowledgeField.id.in_(field_ids) if field_ids else text("false"),
                    )
                )).scalars().all()
            )
            field_values = list(
                (await session.execute(
                    select(KnowledgeFieldValue)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id)
                    .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
                    .where(
                        KnowledgeNode.docs_library_id == library.id,
                        KnowledgeField.docs_library_id == library.id,
                        visibility,
                        (
                            KnowledgeFieldValue.target_node_id.is_(None)
                            | select(field_target.id)
                            .where(
                                field_target.id == KnowledgeFieldValue.target_node_id,
                                field_target_visibility,
                            )
                            .exists()
                        ),
                    )
                )).scalars().all()
            )
            placements = list(
                (await session.execute(
                    select(KnowledgeNodePlacement)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodePlacement.node_id)
                    .where(
                        KnowledgeNode.docs_library_id == library.id,
                        visibility,
                        select(placement_parent.id)
                        .where(
                            placement_parent.id == KnowledgeNodePlacement.parent_node_id,
                            placement_parent_visibility,
                        )
                        .exists(),
                    )
                )).scalars().all()
            )
            edges = list(
                (await session.execute(
                    select(KnowledgeEdge)
                    .join(KnowledgeNode, KnowledgeNode.id == KnowledgeEdge.source_node_id)
                    .where(
                        KnowledgeNode.docs_library_id == library.id,
                        visibility,
                        select(edge_target.id)
                        .where(
                            edge_target.id == KnowledgeEdge.target_node_id,
                            edge_target_visibility,
                        )
                        .exists(),
                    )
                )).scalars().all()
            )
            library_payload = {
                    "id": str(library.id),
                    "name": library.name,
                    "owner_user_id": str(library.owner_user_id) if library.owner_user_id else None,
            }
            payload = {
                # Canonical REST response.  Keep the scope ID explicit so
                # clients do not need to infer it from nested metadata.
                "library": library_payload,
                "docs_library_id": str(library.id),
                # Legacy mobile clients used ``workspace``; this is a
                # deliberate compatibility key (not a duplicate ``library``
                # mapping).  The canonical key above remains authoritative.
                "workspace": library_payload,
                "nodes": [serialize_docs_node(n) for n in nodes],
                "supertags": [serialize_docs_supertag(s) for s in supertags],
                "node_supertags": [serialize_docs_node_supertag(n) for n in node_supertags],
                "supertag_fields": [serialize_docs_supertag_field(s) for s in supertag_fields],
                "fields": [serialize_docs_field(f) for f in fields],
                "field_values": [serialize_docs_field_value(v) for v in field_values],
                "placements": [serialize_docs_placement(p) for p in placements],
                "edges": [serialize_docs_edge(e) for e in edges],
            }
            # 低帯域環境向け ETag/304。library 単位のデータのため private。
            # since 差分クエリと併用しても本文ハッシュで自然に分離される。
            return etag_json_response(request, payload)
        finally:
            await session.close()

    @router.get("/search")
    async def search(
        request: Request,
        q: Optional[str] = None,
        tag: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 20,
        _auth=Depends(require_auth_dependency),
    ):
        user_id = await _get_current_user(request)
        session = await get_db_manager().get_session()
        try:
            service = _docs_service(session)
            project_obj = await _resolve_project_ref(service, project)
            library = await _resolve_read_workspace(
                session,
                service,
                user_id=user_id,
                project_id=project_obj.id if project_obj is not None else None,
            )
            await session.commit()
            project_id = project_obj.id if project_obj is not None else None
            nodes = await service.search(
                docs_library_id=library.id,
                query=q or "",
                project_id=project_id,
                tag=tag or "",
                limit=limit,
                user_id=user_id,
            )
            parents = await service._parent_titles(nodes, user_id=user_id)
            node_ids = [n.id for n in nodes]
            from sqlalchemy import select
            from ..memory.models import KnowledgeNodeSupertag, KnowledgeSupertag

            tags_by_node: dict[Any, list[str]] = {}
            if node_ids:
                tag_rows = await session.execute(
                    select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
                    .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
                    .where(
                        KnowledgeNodeSupertag.node_id.in_(node_ids),
                        KnowledgeSupertag.docs_library_id == library.id,
                    )
                )
                for nid, name in tag_rows.all():
                    tags_by_node.setdefault(nid, []).append(name)
            results = [
                {
                    "id": str(n.id),
                    "title": n.title,
                    "tags": tags_by_node.get(n.id, []),
                    "project_id": str(n.project_id) if n.project_id else None,
                    "parent_title": parents.get(n.id),
                }
                for n in nodes
            ]
            return {"results": results}
        finally:
            await session.close()

    @router.get("/nodes/{node_id}")
    async def get_node(
        request: Request,
        node_id: str,
        _auth=Depends(require_auth_dependency),
    ):
        """認証ユーザーのDocs workspace内にあるNodeを読み取る。"""
        user_id = await _get_current_user(request)
        try:
            node_uuid = UUID(node_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid node id") from exc
        session = await get_db_manager().get_session()
        try:
            service = _docs_service(session)
            from sqlalchemy import select
            from sqlalchemy.orm import aliased
            from ..memory.models import (
                KnowledgeEdge,
                KnowledgeField,
                KnowledgeFieldValue,
                KnowledgeNodePlacement,
                KnowledgeNodeSupertag,
                KnowledgeSupertag,
                KnowledgeSupertagField,
                DocsLibrary,
            )

            node = await session.scalar(
                select(KnowledgeNode).where(KnowledgeNode.id == node_uuid)
            )
            library = await session.get(DocsLibrary, node.docs_library_id) if node else None
            if node is None or library is None or not await can_read_node(
                session, node, user_id, library=library
            ):
                raise HTTPException(status_code=404, detail="Docs node not found")
            field_target = aliased(KnowledgeNode)
            field_target_visibility = docs_readable_node_predicate(
                field_target,
                docs_library_id=library.id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            placement_parent = aliased(KnowledgeNode)
            placement_parent_visibility = docs_readable_node_predicate(
                placement_parent,
                docs_library_id=library.id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            edge_source = aliased(KnowledgeNode)
            edge_target = aliased(KnowledgeNode)
            edge_source_visibility = docs_readable_node_predicate(
                edge_source,
                docs_library_id=library.id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            edge_target_visibility = docs_readable_node_predicate(
                edge_target,
                docs_library_id=library.id,
                user_id=user_id,
                library_owner_id=getattr(library, "owner_user_id", None),
            )
            node_supertags = list(
                (await session.execute(
                    select(KnowledgeNodeSupertag).where(
                        KnowledgeNodeSupertag.node_id == node.id
                    )
                )).scalars().all()
            )
            field_values = list(
                (await session.execute(
                    select(KnowledgeFieldValue)
                    .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
                    .where(
                        KnowledgeFieldValue.node_id == node.id,
                        KnowledgeField.docs_library_id == library.id,
                        (
                            KnowledgeFieldValue.target_node_id.is_(None)
                            | select(field_target.id)
                            .where(
                                field_target.id == KnowledgeFieldValue.target_node_id,
                                field_target_visibility,
                            )
                            .exists()
                        ),
                    )
                )).scalars().all()
            )
            placements = list(
                (await session.execute(
                    select(KnowledgeNodePlacement).where(
                        KnowledgeNodePlacement.node_id == node.id,
                        select(placement_parent.id)
                        .where(
                            placement_parent.id == KnowledgeNodePlacement.parent_node_id,
                            placement_parent_visibility,
                        )
                        .exists(),
                    )
                )).scalars().all()
            )
            edges = list(
                (await session.execute(
                    select(KnowledgeEdge).where(
                        (KnowledgeEdge.source_node_id == node.id)
                        | (KnowledgeEdge.target_node_id == node.id)
                    ).where(
                        select(edge_source.id)
                        .where(
                            edge_source.id == KnowledgeEdge.source_node_id,
                            edge_source_visibility,
                        )
                        .exists(),
                        select(edge_target.id)
                        .where(
                            edge_target.id == KnowledgeEdge.target_node_id,
                            edge_target_visibility,
                        )
                        .exists(),
                    )
                )).scalars().all()
            )
            supertag_ids = [item.supertag_id for item in node_supertags]
            supertags = []
            if supertag_ids:
                supertags = list(
                    (await session.execute(
                        select(KnowledgeSupertag).where(
                            KnowledgeSupertag.id.in_(supertag_ids),
                            KnowledgeSupertag.docs_library_id == library.id,
                        )
                    )).scalars().all()
                )
            supertag_ids = {item.id for item in supertags}
            node_supertags = [
                item for item in node_supertags if item.supertag_id in supertag_ids
            ]
            fields = list(
                (await session.execute(
                    select(KnowledgeField).where(
                        KnowledgeField.docs_library_id == library.id,
                        KnowledgeField.id.in_(
                            select(KnowledgeSupertagField.field_id).where(
                                KnowledgeSupertagField.supertag_id.in_(supertag_ids)
                            )
                        ) if supertag_ids else text("false"),
                    )
                )).scalars().all()
            )
            supertag_fields = list(
                (await session.execute(
                    select(KnowledgeSupertagField)
                    .join(
                        KnowledgeField,
                        KnowledgeField.id == KnowledgeSupertagField.field_id,
                    )
                    .where(
                        KnowledgeSupertagField.supertag_id.in_(supertag_ids),
                        KnowledgeField.docs_library_id == library.id,
                    )
                )).scalars().all()
            ) if supertag_ids else []
            return {
                "node": serialize_docs_node(node),
                "nodes": [serialize_docs_node(node)],
                "supertags": [serialize_docs_supertag(item) for item in supertags],
                "node_supertags": [serialize_docs_node_supertag(item) for item in node_supertags],
                "supertag_fields": [serialize_docs_supertag_field(item) for item in supertag_fields],
                "fields": [serialize_docs_field(item) for item in fields],
                "field_values": [serialize_docs_field_value(item) for item in field_values],
                "placements": [serialize_docs_placement(item) for item in placements],
                "edges": [serialize_docs_edge(item) for item in edges],
            }
        finally:
            await session.close()

    @router.get("/today")
    async def today(
        request: Request,
        date: Optional[str] = None,
        _auth=Depends(require_auth_dependency),
    ):
        from datetime import date as date_cls
        import zoneinfo

        user_id = await _get_current_user(request)
        if date:
            try:
                target = date_cls.fromisoformat(date)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid date") from exc
        else:
            target = datetime.now(zoneinfo.ZoneInfo("Asia/Tokyo")).date()
        session = await get_db_manager().get_session()
        try:
            service = _docs_service(session)
            library = await _resolve_read_workspace(
                session,
                service,
                user_id=user_id,
            )
            node, supertag, node_supertags = await service.ensure_daily_page(
                docs_library_id=library.id, user_id=user_id, day=target
            )
            await session.commit()
            return {
                "node": serialize_docs_node(node),
                "supertag": serialize_docs_supertag(supertag),
                "node_supertags": [serialize_docs_node_supertag(ns) for ns in node_supertags],
            }
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            raise _map_error(exc) from exc
        finally:
            await session.close()

    # ---------------- 書き込み（apply_docs_operation 委譲） ----------------

    @router.post("/nodes")
    async def create_node(request: Request, body: NodeCreateBody, _auth=Depends(require_auth_dependency)):
        payload = body.model_dump(exclude_none=False)
        entity_id = payload.get("id") or ""
        node = await _write(request, "knowledge_nodes", "create", entity_id, payload)
        return {"node": node}

    @router.patch("/nodes/{node_id}")
    async def patch_node(request: Request, node_id: str, body: NodePatchBody, _auth=Depends(require_auth_dependency)):
        payload = body.model_dump(exclude_unset=True)
        node = await _write(request, "knowledge_nodes", "update", node_id, payload)
        return {"node": node}

    @router.post("/nodes/{node_id}/move")
    async def move_node(request: Request, node_id: str, body: NodeMoveBody, _auth=Depends(require_auth_dependency)):
        payload = body.model_dump()
        node = await _write(request, "knowledge_nodes", "move", node_id, payload)
        return {"node": node}

    @router.post("/nodes/{node_id}/archive")
    async def archive_node(request: Request, node_id: str, _auth=Depends(require_auth_dependency)):
        node = await _write(request, "knowledge_nodes", "archive", node_id, {})
        return {"node": node}

    @router.post("/nodes/{node_id}/supertags")
    async def add_supertag(request: Request, node_id: str, body: SupertagAddBody, _auth=Depends(require_auth_dependency)):
        payload = {"node_id": node_id, **body.model_dump(exclude_none=True)}
        entity = await _write(request, "knowledge_node_supertags", "create", node_id, payload)
        return {"ok": True, "entity": entity}

    @router.delete("/nodes/{node_id}/supertags/{supertag_id}")
    async def remove_supertag(request: Request, node_id: str, supertag_id: str, _auth=Depends(require_auth_dependency)):
        payload = {"node_id": node_id, "supertag_id": supertag_id}
        entity = await _write(request, "knowledge_node_supertags", "delete", f"{node_id}:{supertag_id}", payload)
        return {"ok": True, "entity": entity}

    @router.put("/nodes/{node_id}/fields/{field_id}")
    async def set_field(request: Request, node_id: str, field_id: str, body: FieldValueBody, _auth=Depends(require_auth_dependency)):
        payload = {"node_id": node_id, "field_id": field_id, "value": body.value}
        entity = await _write(request, "knowledge_field_values", "update", f"{node_id}:{field_id}", payload)
        return {"ok": True, "entity": entity}

    @router.post("/supertags")
    async def create_supertag(request: Request, body: SupertagCreateBody, _auth=Depends(require_auth_dependency)):
        payload = body.model_dump(exclude_none=True)
        entity = await _write(request, "knowledge_supertags", "create", "", payload)
        return {"ok": True, "entity": entity}

    return router
