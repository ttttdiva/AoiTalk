"""
Conversation History API Routes

Provides REST API endpoints for managing conversation sessions and messages.
"""

import logging
from typing import Literal, Optional
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config_errors import (
    CharacterLookupError,
    CharacterNotFoundError,
    add_character_lookup_context,
    build_character_lookup_error,
    character_lookup_http_detail,
    character_lookup_http_status,
)
from ..llm.context_snapshot import enrich_persisted_context_snapshot
from ..services.conversation_title_service import ensure_conversation_title
from .http_cache import etag_json_response, make_weak_etag_from_payload

logger = logging.getLogger(__name__)


def _request_correlation_ids(request: Request | None) -> tuple[str | None, str | None]:
    """Read optional request/trace IDs for character lookup diagnostics."""

    if request is None:
        return None, None
    headers = request.headers
    return (
        headers.get("x-request-id") or headers.get("x-correlation-id"),
        headers.get("x-trace-id"),
    )


def _character_lookup_http_exception(
    exc: BaseException,
    *,
    request: Request | None = None,
    not_found_status: int = 400,
) -> HTTPException:
    """Map character lookup failures to a safe conversation API response."""

    if isinstance(exc, CharacterNotFoundError):
        return HTTPException(
            status_code=not_found_status,
            detail="Character not found",
        )
    request_id, trace_id = _request_correlation_ids(request)
    typed = add_character_lookup_context(
        exc,
        request_id=request_id,
        trace_id=trace_id,
    )
    return HTTPException(
        status_code=character_lookup_http_status(typed),
        detail=character_lookup_http_detail(typed),
    )


async def _load_character_for_conversation(
    character_name: str,
    request: Request | None,
):
    """Load a character while preserving not-found vs. DB failure semantics."""

    from ..services.character_service import (
        CharacterNotFoundError as ServiceCharacterNotFoundError,
        get_character_for_prompt,
    )

    request_id, trace_id = _request_correlation_ids(request)
    try:
        return await get_character_for_prompt(character_name)
    except (ServiceCharacterNotFoundError, CharacterNotFoundError):
        return None
    except CharacterLookupError:
        raise
    except Exception as exc:
        raise build_character_lookup_error(
            exc,
            request_id=request_id,
            trace_id=trace_id,
        ) from None


def _parse_since_param(value: Optional[str]) -> Optional[datetime]:
    """ISO8601 UTC 文字列を naive UTC datetime に正規化する。

    DB の created_at / updated_at は naive UTC で格納されているため、
    タイムゾーン付き入力は UTC に変換したうえで tzinfo を落として比較する。
    不正な値は 400 を返す。
    """
    if value in (None, ""):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid since") from exc


class CreateSessionMainRoute(BaseModel):
    """Optional displayed route to pin on a newly created session."""

    provider: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None


class CreateSessionRequest(BaseModel):
    """Request model for creating a new session"""

    character_name: str
    project_id: Optional[str] = None
    app_id: Optional[str] = None
    app_target_id: Optional[str] = None
    main_route: Optional[CreateSessionMainRoute] = None


class UpdateSessionRequest(BaseModel):
    """Request model for updating a session"""

    title: Optional[str] = None
    is_active: Optional[bool] = None
    project_id: Optional[str] = None
    character_name: Optional[str] = None
    app_id: Optional[str] = None
    app_target_id: Optional[str] = None
    development_status: Optional[str] = None


class AddMessageRequest(BaseModel):
    """Request model for adding a message"""

    # Assistant/system/tool messages are server-owned outputs.  Accepting a
    # client-supplied role would let a project writer inject prompt-control
    # messages into the durable transcript.
    role: Literal["user"] = "user"
    content: str
    client_message_id: Optional[str] = Field(default=None, max_length=512)


class EditMessageRequest(BaseModel):
    """Request model for editing a message (creates a new branch)"""

    content: str


class SwitchBranchRequest(BaseModel):
    """Request model for switching to a different branch"""

    target_message_id: Optional[str] = None
    branch_index: Optional[int] = None


class ForkConversationRequest(BaseModel):
    """独立した会話へ履歴を複製する。"""

    from_message_id: str
    title: Optional[str] = None


class UpdateRpSettingsRequest(BaseModel):
    """Request model for updating RP steering settings"""

    creativity: Optional[float] = None
    detail: Optional[float] = None
    tempo: Optional[float] = None
    emotion: Optional[float] = None


class ConversationSearchResult(BaseModel):
    """Stable result projection shared by Web and direct Mobile clients."""

    id: str
    match_type: Literal["message", "session"]
    session_id: str
    message_id: Optional[str] = None
    title: str
    character_name: str
    role: Optional[str] = None
    snippet: str
    created_at: Optional[str] = None
    last_activity: Optional[str] = None
    project_id: Optional[str] = None


class ConversationSearchResponse(BaseModel):
    """Canonical conversation search response for all native clients."""

    results: list[ConversationSearchResult]
    total: int


def _conversation_search_snippet(content: str, query: str, max_length: int = 160) -> str:
    """Normalize a matched message and keep result payloads bounded."""

    normalized_content = " ".join(str(content or "").split())
    if len(normalized_content) <= max_length:
        return normalized_content
    lower_content = normalized_content.casefold()
    lower_query = query.casefold()
    match_index = lower_content.find(lower_query)
    center = match_index if match_index >= 0 else 0
    start = max(0, center - max_length // 3)
    end = min(len(normalized_content), start + max_length)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(normalized_content) else ""
    return f"{prefix}{normalized_content[start:end]}{suffix}"


def _is_story_workflow_session(session) -> bool:
    """Keep writing-workflow sessions out of the generic Chat search."""

    character_name = str(getattr(session, "character_name", "") or "")
    title = str(getattr(session, "title", "") or "")
    return character_name.startswith("story_") or title.startswith("[執筆]")


def _conversation_search_session_result(session, query: str) -> dict[str, object]:
    title = str(getattr(session, "title", "") or "")
    character_name = str(getattr(session, "character_name", "") or "")
    snippet = " / ".join(
        value
        for value in (
            f"タイトル: {title}" if title else None,
            f"相手: {character_name}" if character_name else None,
        )
        if value
    )
    created_at = getattr(session, "last_activity", None) or getattr(
        session, "session_start", None
    )
    last_activity = getattr(session, "last_activity", None) or getattr(
        session, "session_start", None
    )
    return {
        "id": f"session:{session.id}",
        "match_type": "session",
        "session_id": str(session.id),
        "message_id": None,
        "title": title or "無題の会話",
        "character_name": character_name,
        "role": None,
        "snippet": _conversation_search_snippet(snippet, query),
        "created_at": created_at.isoformat() if created_at else None,
        "last_activity": last_activity.isoformat() if last_activity else None,
        "project_id": str(session.project_id) if session.project_id else None,
    }


def _conversation_search_message_result(session, message, content: str, query: str) -> dict[str, object]:
    created_at = getattr(message, "created_at", None) or getattr(
        session, "last_activity", None
    ) or getattr(session, "session_start", None)
    last_activity = getattr(session, "last_activity", None) or getattr(
        session, "session_start", None
    )
    return {
        "id": f"message:{message.id}",
        "match_type": "message",
        "session_id": str(session.id),
        "message_id": str(message.id),
        "title": str(getattr(session, "title", "") or "無題の会話"),
        "character_name": str(getattr(session, "character_name", "") or ""),
        "role": getattr(message, "role", None),
        "snippet": _conversation_search_snippet(content, query),
        "created_at": created_at.isoformat() if created_at else None,
        "last_activity": last_activity.isoformat() if last_activity else None,
        "project_id": str(session.project_id) if session.project_id else None,
    }


def create_conversation_router(
    require_auth, get_current_user, get_llm_for_title_generation=None
) -> APIRouter:
    """Create conversation history router

    Args:
        require_auth: Auth dependency function
        get_current_user: Function to get current user info from request
        get_llm_for_title_generation: Optional async function to generate title via LLM

    Returns:
        APIRouter with conversation endpoints
    """
    router = APIRouter(prefix="/api/conversations", tags=["conversations"])

    # Import repository
    try:
        from ..memory.conversation_repository import ConversationRepository

        REPO_AVAILABLE = True
    except ImportError:
        REPO_AVAILABLE = False
        logger.warning("ConversationRepository not available")


    async def _get_current_conversation_user(request: Request) -> dict:
        user_info = get_current_user(request)
        if hasattr(user_info, "__await__"):
            user_info = await user_info
        return user_info or {"id": "default_user", "username": "default_user"}

    async def _get_visible_conversation_user_ids(request: Request) -> list[str]:
        user_info = await _get_current_conversation_user(request)
        return [str(user_info.get("id") or "default_user")]

    def _validate_development_status(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip()
        if normalized not in {"working", "waiting_for_user", "completed"}:
            raise HTTPException(status_code=400, detail="開発チャットの状態が不正です")
        return normalized

    def _session_to_list_dict(session) -> dict:
        """Serialize a session with the sidebar-specific unread projection."""
        payload = session.to_dict()
        last_activity = getattr(session, "last_activity", None)
        last_read_at = getattr(session, "last_read_at", None)
        payload["is_unread"] = bool(
            getattr(session, "app_id", None) is not None
            and getattr(session, "development_status", None) == "waiting_for_user"
            and last_activity is not None
            and (last_read_at is None or last_activity > last_read_at)
        )
        return payload

    def _messages_with_branch_info(messages, branch_info: dict) -> list[dict]:
        """Serialize messages with the branch projection shared by chat clients."""
        payload = []
        for message in messages:
            message_dict = message.to_dict()
            projection = branch_info.get(str(message.id), {})
            message_dict["branch_count"] = projection.get("branch_count", 1)
            message_dict["branch_index"] = projection.get("branch_index", 0)
            payload.append(message_dict)
        return payload

    async def _validate_app_scope(
        request: Request,
        *,
        app_id: Optional[str],
        app_target_id: Optional[str],
        project_id: Optional[str],
        db_session=None,
    ) -> tuple[Optional[str], Optional[str]]:
        """Validate App/Target UUIDs and viewer access before persisting context."""
        if not app_id:
            if app_target_id:
                raise HTTPException(status_code=400, detail="app_target_id には app_id が必要です")
            return None, None
        from uuid import UUID

        from sqlalchemy import and_, select

        from ..memory.database import get_database_manager
        from ..memory.models import App, AppTarget, ProjectApp
        from ..services.app_service import AppAccessError, AppService

        try:
            app_uuid = UUID(str(app_id))
            target_uuid = UUID(str(app_target_id)) if app_target_id else None
            user_info = await _get_current_conversation_user(request)
            user_uuid = UUID(str(user_info.get("id")))
            project_uuid = UUID(str(project_id)) if project_id else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="App context UUIDが不正です") from exc

        owns_session = db_session is None
        if owns_session:
            db_session = await get_database_manager().get_session()
        try:
            app = await db_session.scalar(select(App).where(App.id == app_uuid).limit(1))
            if not app:
                raise HTTPException(status_code=404, detail="App not found")
            try:
                await AppService().require_permission(
                    db_session,
                    app,
                    user_id=user_uuid,
                    required="viewer",
                    user_role=user_info.get("role"),
                    project_id=project_uuid,
                )
            except AppAccessError as exc:
                raise HTTPException(status_code=403, detail="Appを閲覧できません") from exc
            if project_uuid is not None:
                binding = await db_session.scalar(select(ProjectApp).where(
                    ProjectApp.project_id == project_uuid,
                    ProjectApp.app_id == app_uuid,
                ).limit(1))
                if binding is None or not binding.enabled:
                    raise HTTPException(status_code=403, detail="このProjectではAppが有効化されていません")
            if target_uuid:
                target = await db_session.scalar(select(AppTarget).where(
                    and_(AppTarget.id == target_uuid, AppTarget.app_id == app_uuid)
                ).limit(1))
                if not target:
                    raise HTTPException(status_code=404, detail="App Target not found")
            return str(app_uuid), str(target_uuid) if target_uuid else None
        finally:
            if owns_session:
                await db_session.close()

    def _session_is_visible(session, visible_user_ids: list[str]) -> bool:
        if str(session.user_id) in visible_user_ids:
            return True
        for participant in getattr(session, "participants", []) or []:
            if (
                participant.participant_type == "user"
                and str(participant.participant_id) in visible_user_ids
                and participant.status == "joined"
            ):
                return True
        return False

    def _session_can_manage(session, user_info: dict) -> bool:
        """Only the owner or an explicit conversation admin may mutate/delete."""
        if str(user_info.get("role") or "") == "admin":
            return True
        user_id = str(user_info.get("id") or "")
        if not user_id:
            return False
        if str(session.user_id) == user_id:
            return True
        for participant in getattr(session, "participants", []) or []:
            if (
                participant.participant_type == "user"
                and str(participant.participant_id) == user_id
                and participant.status == "joined"
                and participant.role in {"owner", "admin"}
            ):
                return True
        return False

    def _session_can_write(session, user_info: dict) -> bool:
        """Allow generation/message writes for joined members, not viewers."""
        if str(user_info.get("role") or "") == "admin":
            return True
        user_id = str(user_info.get("id") or "")
        if not user_id:
            return False
        if str(session.user_id) == user_id:
            return True
        for participant in getattr(session, "participants", []) or []:
            if (
                participant.participant_type == "user"
                and str(participant.participant_id) == user_id
                and participant.status == "joined"
                and participant.role in {"owner", "admin", "member"}
            ):
                return True
        return False

    async def _require_project_permission(
        project_id,
        user_info: dict,
        *,
        permission: str,
        db_session=None,
    ) -> None:
        """Enforce the Project ACL for direct FastAPI conversation writes.

        The Next.js BFF has its own authorization helpers, but Enterprise also
        exposes selected FastAPI routes to explicit bearer credentials. Keep
        this backend path fail-closed and use the database as the source of
        truth rather than the conversation participant role.
        """
        if project_id is None:
            return
        try:
            project_uuid = UUID(str(project_id))
            user_uuid = UUID(str(user_info.get("id") or ""))
        except (TypeError, ValueError):
            # The legacy no-auth development server uses default_user. It has
            # no durable Project ACL, so project-scoped authenticated requests
            # must not be accepted through this direct route.
            raise HTTPException(status_code=403, detail="Project access denied")

        owns_session = db_session is None
        if owns_session:
            from ..memory.database import get_database_manager

            db_session = await get_database_manager().get_session()
        try:
            from ..memory.project_repository import ProjectRepository

            allowed = await ProjectRepository.has_permission(
                db_session,
                project_uuid,
                user_uuid,
                permission,
            )
            if not allowed:
                raise HTTPException(
                    status_code=403,
                    detail="Project permission denied",
                )
        finally:
            if owns_session:
                await db_session.close()

    async def _require_session_project_permission(
        session,
        user_info: dict,
        *,
        permission: str = "read",
    ) -> None:
        """Apply the durable Project ACL to a loaded conversation session."""
        await _require_project_permission(
            getattr(session, "project_id", None),
            user_info,
            permission=permission,
        )

    @router.get(
        "/search",
        response_model=ConversationSearchResponse,
        operation_id="search_conversations",
    )
    async def search_conversations(
        q: str = "",
        project_id: Optional[str] = None,
        limit: int = Query(default=50, ge=1, le=50),
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Search readable conversation messages and session metadata.

        Message content is encrypted at rest, so the bounded message scan is
        decrypted inside the API process after the same visibility and Project
        ACL checks used by the rest of the conversation router.  This is the
        FastAPI canonical operation; the Web BFF remains an adapter.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        normalized = str(q or "").strip()
        if not normalized:
            return ConversationSearchResponse(results=[], total=0)

        user_info = await _get_current_conversation_user(request)
        visible_user_ids = await _get_visible_conversation_user_ids(request)
        from sqlalchemy import and_, desc, func, or_, select

        from ..memory.database import get_database_manager
        from ..memory.models import ConversationMessage, ConversationParticipant, ConversationSession

        db_session = await get_database_manager().get_session()
        try:
            participant_session_ids = (
                select(ConversationParticipant.session_id)
                .where(
                    and_(
                        ConversationParticipant.participant_type == "user",
                        ConversationParticipant.participant_id.in_(visible_user_ids),
                        ConversationParticipant.status == "joined",
                    )
                )
                .scalar_subquery()
            )
            session_conditions = [
                or_(
                    ConversationSession.user_id.in_(visible_user_ids),
                    ConversationSession.id.in_(participant_session_ids),
                ),
                ConversationSession.deleted_at.is_(None),
            ]

            if project_id:
                try:
                    project_uuid = UUID(project_id)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=403, detail="Project access denied") from exc
                await _require_project_permission(
                    project_uuid,
                    user_info,
                    permission="read",
                    db_session=db_session,
                )
                session_conditions.append(ConversationSession.project_id == project_uuid)

            project_access: dict[str, bool] = {}

            async def readable(session) -> bool:
                """Apply ACL once per project while preserving null-project chat."""

                session_project_id = getattr(session, "project_id", None)
                if session_project_id is None:
                    return True
                key = str(session_project_id)
                if key in project_access:
                    return project_access[key]
                try:
                    await _require_project_permission(
                        session_project_id,
                        user_info,
                        permission="read",
                        db_session=db_session,
                    )
                except HTTPException as exc:
                    if exc.status_code == 403:
                        project_access[key] = False
                        return False
                    raise
                project_access[key] = True
                return True

            # Content is encrypted at rest; scan only the most recent bounded
            # active-branch messages, then decrypt/filter in-process.
            message_rows = (
                await db_session.execute(
                    select(ConversationSession, ConversationMessage)
                    .join(
                        ConversationMessage,
                        ConversationMessage.session_id == ConversationSession.id,
                    )
                    .where(
                        and_(
                            *session_conditions,
                            ConversationMessage.deleted_at.is_(None),
                            or_(
                                ConversationMessage.is_active_branch.is_(True),
                                ConversationMessage.is_active_branch.is_(None),
                            ),
                        )
                    )
                    .order_by(
                        desc(ConversationMessage.created_at),
                        desc(ConversationMessage.id),
                    )
                    .limit(2000)
                )
            ).all()

            results: list[dict[str, object]] = []
            lower_query = normalized.casefold()
            for session, message in message_rows:
                if len(results) >= limit:
                    break
                if _is_story_workflow_session(session) or not await readable(session):
                    continue
                content = str(getattr(message, "content", "") or "")
                if lower_query not in content.casefold():
                    continue
                results.append(
                    _conversation_search_message_result(
                        session, message, content, normalized
                    )
                )

            remaining_limit = max(0, limit - len(results))
            if remaining_limit:
                matched_session_ids = {result["session_id"] for result in results}
                # Titles and character names are not encrypted, so the DB can
                # narrow this second query before the stable Python projection.
                escaped = (
                    normalized.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                    .casefold()
                )
                pattern = f"%{escaped}%"
                session_rows = (
                    await db_session.execute(
                        select(ConversationSession)
                        .where(
                            and_(
                                *session_conditions,
                                or_(
                                    func.lower(
                                        func.coalesce(ConversationSession.title, "")
                                    ).like(pattern, escape="\\"),
                                    func.lower(
                                        func.coalesce(
                                            ConversationSession.character_name, ""
                                        )
                                    ).like(pattern, escape="\\"),
                                ),
                            )
                        )
                        .order_by(
                            desc(
                                func.coalesce(
                                    ConversationSession.last_activity,
                                    ConversationSession.session_start,
                                )
                            ),
                            desc(ConversationSession.id),
                        )
                        .limit(remaining_limit),
                    )
                ).scalars().all()
                for session in session_rows:
                    if len(results) >= limit:
                        break
                    if (
                        str(session.id) in matched_session_ids
                        or _is_story_workflow_session(session)
                        or not await readable(session)
                    ):
                        continue
                    results.append(_conversation_search_session_result(session, normalized))

            return ConversationSearchResponse(results=results, total=len(results))
        finally:
            await db_session.close()

    @router.get("")
    async def list_sessions(
        limit: int = 50,
        offset: int = 0,
        project_id: Optional[str] = None,
        app_id: Optional[str] = None,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get list of conversation sessions for current user"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = await _get_current_conversation_user(request)
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            normalized_app_id = None
            if app_id:
                normalized_app_id, _ = await _validate_app_scope(
                    request,
                    app_id=app_id,
                    app_target_id=None,
                    project_id=project_id if project_id not in {"none", "all"} else None,
                )
            # memory_managerと同じuser_id（default_user）を使用
            repo = ConversationRepository()

            # Handle special 'none' value to filter for conversations without project_id
            filter_project_id = project_id
            if project_id == "none":
                filter_project_id = (
                    ""  # Empty string signals repository to filter for NULL project_id
                )

            sessions = await repo.get_user_sessions(
                visible_user_ids,
                limit=limit,
                offset=offset,
                project_id=filter_project_id,
                app_id=normalized_app_id,
            )
            readable_sessions = []
            for session in sessions:
                try:
                    await _require_session_project_permission(session, user_info)
                except HTTPException as exc:
                    if exc.status_code == 403:
                        continue
                    raise
                readable_sessions.append(session)
            sessions = readable_sessions
            total = len(sessions)

            payload = {
                "success": True,
                "conversations": [_session_to_list_dict(s) for s in sessions],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
            # ETag/304: 本文から弱い ETag を算出し、If-None-Match 一致なら 304。
            # ユーザー固有データのため private, no-cache を付与して分離する。
            return etag_json_response(request, payload)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/by-project/{project_id}")
    async def get_project_conversations(
        project_id: str,
        limit: int = 50,
        offset: int = 0,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get conversations for a specific project"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = await _get_current_conversation_user(request)
            await _require_project_permission(
                project_id,
                user_info,
                permission="read",
            )
            visible_user_ids = await _get_visible_conversation_user_ids(request)

            repo = ConversationRepository()
            sessions = [
                session
                for session in await repo.get_sessions_by_project(
                    project_id=project_id, limit=limit, offset=offset
                )
                if _session_is_visible(session, visible_user_ids)
            ]
            total = len(sessions)

            return JSONResponse(
                {
                    "success": True,
                    "conversations": [_session_to_list_dict(s) for s in sessions],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "project_id": project_id,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get project conversations: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("")
    async def create_session(
        payload: CreateSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Create a new conversation session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = await _get_current_conversation_user(request)
            user_id = str(user_info.get("id") or "default_user")

            requested_character_name = str(payload.character_name or "").strip()
            char_data = await _load_character_for_conversation(
                requested_character_name,
                request,
            )
            if not char_data:
                raise HTTPException(
                    status_code=400,
                    detail=f"存在しないキャラクターです: {requested_character_name}",
                )
            character_name = str(
                char_data.get("slug") or requested_character_name
            ).strip()

            # Normalize project_id: convert invalid values to None
            normalized_project_id = payload.project_id
            if payload.project_id:
                # Convert string values like "none", "all", "" to None
                if payload.project_id.lower() in ["none", "all", ""]:
                    normalized_project_id = None

            # Validate the requested scope before changing the user's current
            # session. A denied create request must not have a side effect on
            # an unrelated active conversation.
            await _require_project_permission(
                normalized_project_id,
                user_info,
                permission="write",
            )

            normalized_app_id, normalized_app_target_id = await _validate_app_scope(
                request,
                app_id=payload.app_id,
                app_target_id=payload.app_target_id,
                project_id=normalized_project_id,
            )

            repo = ConversationRepository()

            # Deactivate current active session for this user/character only
            # after all authorization and scope validation has succeeded.
            active = await repo.get_active_session(user_id, character_name)
            if active:
                # Closing the previous session is lifecycle metadata, not a
                # new chat activity. Keep its last_activity timestamp stable
                # so creating a new session does not reorder the old one.
                await repo.deactivate_session(str(active.id), touch_activity=False)

            # Create new session
            session = await repo.create_session(
                user_id=user_id,
                character_name=character_name,
                title="",  # Will be generated on first message
                project_id=normalized_project_id,
                app_id=normalized_app_id,
                app_target_id=normalized_app_target_id,
                development_status="working" if normalized_app_id else None,
            )
            await repo.ensure_participant(
                str(session.id),
                "user",
                user_id,
                display_name=str(
                    user_info.get("display_name")
                    or user_info.get("username")
                    or "default_user"
                ),
                role="owner",
                status="joined",
            )

            try:
                from ..services.conversation_session_selection import (
                    SessionRouteStampError,
                    stamp_new_session_main_route,
                )

                client_main_route = (
                    payload.main_route.model_dump()
                    if payload.main_route is not None
                    else None
                )
                session = await stamp_new_session_main_route(
                    repo,
                    session,
                    user_id,
                    client_main_route,
                )
            except SessionRouteStampError as stamp_exc:
                raise HTTPException(
                    status_code=500,
                    detail="Failed to persist displayed LLM route on new session",
                ) from stamp_exc

            # first_message 挿入
            first_msg_content = None
            try:
                if char_data:
                    first_msg_content = char_data.get("first_message", "")
                    if not first_msg_content and char_data.get("alternate_greetings"):
                        import random

                        greetings = char_data["alternate_greetings"]
                        if greetings:
                            first_msg_content = random.choice(greetings)
            except Exception as e:
                logger.warning(f"Failed to get first_message: {e}")

            if first_msg_content:
                await repo.add_message(
                    session_id=str(session.id),
                    role="assistant",
                    content=first_msg_content,
                    sender_type="character",
                    sender_id=character_name,
                    sender_display_name=str(
                        char_data.get("name") or requested_character_name
                    ).strip(),
                )

            response = {"success": True, "session": session.to_dict()}
            if first_msg_content:
                response["first_message"] = first_msg_content
            return JSONResponse(response)
        except HTTPException:
            raise
        except CharacterLookupError as exc:
            logger.error(
                "Character lookup failed while creating conversation: "
                "category=%s trace_id=%s request_id=%s",
                exc.category,
                exc.trace_id,
                exc.request_id or _request_correlation_ids(request)[0] or "-",
            )
            raise _character_lookup_http_exception(exc, request=request) from None
        except Exception as e:
            logger.error(
                "Failed to create session: exception_type=%s",
                type(e).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to create conversation",
            ) from None

    # Note: This route must come before /{session_id} to avoid path matching conflicts
    @router.get("/active/current")
    async def get_active_session(
        character_name: Optional[str] = None,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get the current active session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = await _get_current_conversation_user(request)
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            repo = ConversationRepository()
            sessions = await repo.get_user_sessions(
                visible_user_ids, include_inactive=False
            )
            if character_name:
                sessions = [
                    session
                    for session in sessions
                    if session.character_name == character_name
                ]
            session = None
            for candidate in sessions:
                try:
                    await _require_session_project_permission(candidate, user_info)
                except HTTPException as exc:
                    if exc.status_code == 403:
                        continue
                    raise
                session = candidate
                break

            if session:
                messages = await repo.get_session_messages(str(session.id))
                return JSONResponse(
                    {
                        "success": True,
                        "session": session.to_dict(),
                        "messages": [m.to_dict() for m in messages],
                    }
                )
            else:
                return JSONResponse({"success": True, "session": None, "messages": []})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get active session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{session_id}")
    async def get_session(
        session_id: str, _: None = Depends(require_auth), request: Request = None
    ):
        """Get a specific session with its messages"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id, with_messages=False)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            # Verify ownership
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            user_info = await _get_current_conversation_user(request)
            await _require_session_project_permission(session, user_info)

            return JSONResponse({"success": True, "session": session.to_dict()})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{session_id}/context-snapshot")
    async def get_session_context_snapshot(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Return the latest active-branch model request snapshot for a session."""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        try:
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id, with_messages=False)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            user_info = await _get_current_conversation_user(request)
            await _require_session_project_permission(session, user_info)
            messages = await repo.get_recent_messages(session_id, count=100)
            latest = None
            for message in reversed(messages):
                if message.role != "assistant" or not getattr(message, "is_active_branch", True):
                    continue
                metadata = (
                    message.message_metadata
                    if isinstance(message.message_metadata, dict)
                    else {}
                )
                candidate = metadata.get("context_snapshot")
                if isinstance(candidate, dict):
                    latest = enrich_persisted_context_snapshot(candidate)
                    if latest is None:
                        continue
                    latest.setdefault("session_id", session_id)
                    latest.setdefault("message_id", str(message.id))
                    break
            return JSONResponse({
                "success": True,
                "status": "available" if latest else "unavailable",
                "snapshot": latest,
            })
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to get context snapshot: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    @router.get("/{session_id}/messages")
    async def get_session_messages(
        session_id: str,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get messages for a session

        低帯域環境向け:
        - ``since`` (ISO8601 UTC) 指定時は updated_at がそれより後のメッセージ
          だけを差分取得する。レスポンス形状（messages 配列）は不変。
        - レスポンスに ``server_time`` (ISO8601 UTC) を追加し、クライアントが
          次回の ``since`` に使えるようにする。
        - ETag/304 で未変更時の転送を抑制する。
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            since_dt = _parse_since_param(since)
            query_started_at = datetime.now(timezone.utc)

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            user_info = await _get_current_conversation_user(request)
            await _require_session_project_permission(session, user_info)

            try:
                messages, branch_info = (
                    await repo.get_session_messages_with_branch_info(
                        session_id, limit=limit, since=since_dt
                    )
                )
            except Exception as branch_error:
                logger.warning(
                    "Failed to get branch projection for session messages: %s",
                    branch_error,
                )
                messages = await repo.get_session_messages(
                    session_id, limit=limit, since=since_dt
                )
                branch_info = {}

            timestamps = [
                stamp
                for message in messages
                for stamp in (
                    getattr(message, "updated_at", None),
                    getattr(message, "created_at", None),
                )
                if stamp is not None
            ]
            if timestamps:
                latest = max(timestamps)
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=timezone.utc)
                # overlap で since より古い行だけが返っても cursor を後退させない。
                if since_dt is not None:
                    normalized_since = since_dt
                    if normalized_since.tzinfo is None:
                        normalized_since = normalized_since.replace(
                            tzinfo=timezone.utc
                        )
                    latest = max(latest, normalized_since)
                server_time = latest.astimezone(timezone.utc).isoformat()
            elif since is not None:
                # 差分なしなら cursor を固定し、同一 URL の ETag/304 を成立させる。
                server_time = since
            else:
                server_time = query_started_at.isoformat()

            payload = {
                "success": True,
                "messages": _messages_with_branch_info(messages, branch_info),
                "server_time": server_time,
            }
            # ETag は messages 本文のみから算出する（server_time は毎回変わるため
            # 署名に含めない。含めると 304 が成立しなくなる）。
            etag = make_weak_etag_from_payload(
                {"messages": payload["messages"], "since": since}
            )
            return etag_json_response(request, payload, etag=etag)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get messages: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{session_id}/messages")
    async def add_message(
        session_id: str,
        payload: AddMessageRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Add a message to a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = await _get_current_conversation_user(request)
            user_id = str(user_info.get("id") or "default_user")

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _session_can_write(session, user_info):
                raise HTTPException(status_code=403, detail="Conversation write denied")
            await _require_project_permission(
                session.project_id,
                user_info,
                permission="write",
            )

            # Add message
            message = await repo.add_message(
                session_id=session_id,
                role=payload.role,
                content=payload.content,
                metadata=(
                    {"client_message_id": payload.client_message_id}
                    if payload.client_message_id
                    else None
                ),
                sender_type="user" if payload.role == "user" else None,
                sender_id=user_id if payload.role == "user" else None,
                sender_display_name=(
                    str(user_info.get("display_name") or user_info.get("username") or "")
                    if payload.role == "user"
                    else None
                ),
            )

            generated_title = await ensure_conversation_title(
                repo=repo,
                session_id=session_id,
                llm_generator=get_llm_for_title_generation,
            )

            response = {"success": True, "message": message.to_dict()}
            if generated_title:
                response["title"] = generated_title.title
                response["title_source"] = generated_title.source
            return JSONResponse(response)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to add message: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{session_id}/generate-title")
    async def generate_session_title(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Generate a title once early conversation context is available."""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            user_info = await _get_current_conversation_user(request)
            if not _session_can_manage(session, user_info):
                raise HTTPException(status_code=403, detail="Conversation title denied")
            await _require_project_permission(
                session.project_id,
                user_info,
                permission="write",
            )

            generated = await ensure_conversation_title(
                repo=repo,
                session_id=session_id,
                llm_generator=get_llm_for_title_generation,
            )

            updated = await repo.get_session_by_id(session_id)
            return JSONResponse(
                {
                    "success": True,
                    "title": updated.title if updated else session.title,
                    "generated": generated is not None,
                    "source": generated.source if generated else None,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to generate session title: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{session_id}/read")
    async def mark_session_read(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Mark a conversation as read without changing its activity order."""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            user_info = await _get_current_conversation_user(request)
            await _require_session_project_permission(session, user_info)
            if not _session_can_manage(session, user_info):
                raise HTTPException(status_code=403, detail="Conversation read-state denied")
            read_at = datetime.utcnow()
            updated = await repo.update_session(
                session_id,
                touch_activity=False,
                last_read_at=read_at,
            )
            if not updated:
                raise HTTPException(status_code=404, detail="Session not found")
            return JSONResponse(
                {
                    "success": True,
                    "session_id": session_id,
                    "last_read_at": read_at.isoformat(),
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to mark session read: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.put("/{session_id}")
    async def update_session(
        session_id: str,
        payload: UpdateSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Update session details"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        db_session = None
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"

            from ..memory.database import get_database_manager

            db_session = await get_database_manager().get_session()
            repo = ConversationRepository(session=db_session)
            session = await repo.get_session_by_id(session_id, for_update=True)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _session_can_manage(session, user_info):
                raise HTTPException(status_code=403, detail="Conversation management denied")
            # Rebinding a session must be authorized by the current Project
            # before it can be moved to another scope or to App/global scope.
            await _require_session_project_permission(
                session,
                user_info,
                permission="write",
            )

            # Build update dict
            updates = {}
            project_scope_provided = "project_id" in payload.model_fields_set
            normalized_project_id: str | None = None
            if project_scope_provided:
                raw_project_id = (payload.project_id or "").strip()
                if raw_project_id:
                    try:
                        normalized_project_id = str(UUID(raw_project_id))
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail="project_idが不正です") from exc
            if payload.title is not None:
                updates["title"] = payload.title
            if payload.is_active is not None:
                updates["is_active"] = payload.is_active
            if project_scope_provided:
                updates["project_id"] = UUID(normalized_project_id) if normalized_project_id else None
            if payload.character_name is not None:
                requested_character_name = str(payload.character_name).strip()
                char_data = await _load_character_for_conversation(
                    requested_character_name,
                    request,
                )
                if not char_data:
                    raise HTTPException(
                        status_code=400,
                        detail=f"存在しないキャラクターです: {requested_character_name}",
                    )
                updates["character_name"] = str(
                    char_data.get("slug") or requested_character_name
                ).strip()
            app_scope_provided = (
                "app_id" in payload.model_fields_set
                or "app_target_id" in payload.model_fields_set
            )
            # Changing a session's Project also changes the authorization scope
            # of its existing App context.  Validate that combination even
            # when the request does not repeat app_id/app_target_id; otherwise
            # an App could be silently moved into an unrelated Project.
            effective_scope_project_id = (
                normalized_project_id
                if project_scope_provided
                else str(session.project_id) if session.project_id else None
            )
            await _require_project_permission(
                effective_scope_project_id,
                user_info,
                permission="write",
                db_session=db_session,
            )
            scope_app_id = (
                payload.app_id
                if app_scope_provided
                else str(session.app_id) if session.app_id else None
            )
            scope_app_target_id = (
                payload.app_target_id
                if app_scope_provided
                else str(session.app_target_id) if session.app_target_id else None
            )
            normalized_app_id: str | None = None
            if app_scope_provided or (project_scope_provided and scope_app_id):
                normalized_app_id, normalized_app_target_id = await _validate_app_scope(
                    request,
                    app_id=scope_app_id,
                    app_target_id=scope_app_target_id,
                    project_id=effective_scope_project_id,
                    db_session=db_session,
                )
            else:
                normalized_app_target_id = None
            if app_scope_provided:
                updates["app_id"] = UUID(normalized_app_id) if normalized_app_id else None
                updates["app_target_id"] = (
                    UUID(normalized_app_target_id) if normalized_app_target_id else None
                )

            effective_app_id = (
                normalized_app_id
                if app_scope_provided
                else (str(session.app_id) if session.app_id else None)
            )
            if "development_status" in payload.model_fields_set:
                status = _validate_development_status(payload.development_status)
                if status is not None and effective_app_id is None:
                    raise HTTPException(
                        status_code=400,
                        detail="App開発状態はApp context付きChatでのみ設定できます",
                    )
                updates["development_status"] = status
            elif app_scope_provided and effective_app_id is None:
                # App context解除時に古い状態を残すと、通常Chatが
                # working/waiting_for_userとしてサイドバーへ投影される。
                updates["development_status"] = None

            if updates:
                # Session metadata (title, character/scope, active flag, or
                # development state) is not chat activity. Only creating a
                # session/message or receiving an agent response may move it
                # in the history list.
                await repo.update_session(
                    session_id,
                    touch_activity=False,
                    **updates,
                )

            # Get updated session
            updated = await repo.get_session_by_id(session_id)

            return JSONResponse(
                {"success": True, "session": updated.to_dict() if updated else None}
            )
        except HTTPException:
            raise
        except CharacterLookupError as exc:
            logger.error(
                "Character lookup failed while updating conversation: "
                "category=%s trace_id=%s request_id=%s",
                exc.category,
                exc.trace_id,
                exc.request_id or _request_correlation_ids(request)[0] or "-",
            )
            raise _character_lookup_http_exception(exc, request=request) from None
        except Exception as e:
            logger.error(
                "Failed to update session: exception_type=%s",
                type(e).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to update conversation",
            ) from None
        finally:
            if db_session is not None:
                await db_session.close()

    @router.delete("/{session_id}")
    async def delete_session(
        session_id: str, _: None = Depends(require_auth), request: Request = None
    ):
        """Delete a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _session_can_manage(session, user_info):
                raise HTTPException(status_code=403, detail="Conversation deletion denied")
            await _require_project_permission(
                session.project_id,
                user_info,
                permission="write",
            )

            deleted = await repo.delete_session(session_id)

            return JSONResponse({"success": deleted})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{session_id}/resume")
    async def resume_session(
        session_id: str,
        include_messages: bool = True,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Resume (reactivate) a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _session_can_manage(session, user_info):
                raise HTTPException(status_code=403, detail="Conversation resume denied")
            await _require_project_permission(
                session.project_id,
                user_info,
                permission="write",
            )

            # Deactivate all other sessions for this user/character
            all_sessions = await repo.get_user_sessions(visible_user_ids)
            for s in all_sessions:
                if (
                    str(s.id) != session_id
                    and s.character_name == session.character_name
                ):
                    await repo.deactivate_session(str(s.id), touch_activity=False)

            # Activate this session
            await repo.update_session(
                session_id,
                is_active=True,
                touch_activity=False,
            )

            # キャッシュ再訪時はセッション再開だけ行い、messages は差分 GET に任せる。
            messages = (
                await repo.get_active_branch_messages(session_id)
                if include_messages
                else []
            )

            updated = await repo.get_session_by_id(session_id)

            return JSONResponse(
                {
                    "success": True,
                    "session": updated.to_dict() if updated else None,
                    "messages": [m.to_dict() for m in messages],
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to resume session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ─── Branching Endpoints ─────────────────────────────────────────────

    @router.post("/{session_id}/fork")
    async def fork_conversation(
        session_id: str,
        payload: ForkConversationRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """指定メッセージまでを、元会話を変更せず独立セッションへ複製する。"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            repo = ConversationRepository()
            source = await repo.get_session_by_id(session_id)
            if not source:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(source, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            user_info = await _get_current_conversation_user(request)
            user_id = str(user_info.get("id") or "").strip()
            if not user_id:
                raise HTTPException(status_code=401, detail="Not authenticated")
            if not _session_can_manage(source, user_info):
                raise HTTPException(status_code=403, detail="Conversation fork denied")
            await _require_project_permission(
                source.project_id,
                user_info,
                permission="write",
            )

            forked = await repo.fork_session(
                session_id,
                payload.from_message_id,
                user_id=user_id,
                title=payload.title,
            )

            # シナリオ執筆チャットの場合だけ、対象Docs nodeと選択範囲も引き継ぐ。
            from ..services.story_studio import clone_story_writing_session_for_conversation

            try:
                await clone_story_writing_session_for_conversation(
                    session_id, str(forked.id)
                )
            except Exception:
                logger.exception(
                    "Conversation fork succeeded but writing context clone failed: %s",
                    forked.id,
                )
            return JSONResponse({"success": True, "session": forked.to_dict()})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to fork conversation: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    @router.put("/{session_id}/messages/{message_id}")
    async def edit_message(
        session_id: str,
        message_id: str,
        payload: EditMessageRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Edit a message by creating a new branch

        This creates a new branch with the edited content,
        deactivating the original message and its descendants.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()

            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session_obj, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _session_can_manage(session_obj, user_info):
                raise HTTPException(status_code=403, detail="Message edit denied")
            await _require_project_permission(
                session_obj.project_id,
                user_info,
                permission="write",
            )

            # Verify message belongs to session
            message = await repo.get_message_by_id(message_id)
            if not message:
                raise HTTPException(status_code=404, detail="Message not found")
            if str(message.session_id) != session_id:
                raise HTTPException(
                    status_code=400, detail="Message does not belong to this session"
                )

            # Edit message (creates new branch)
            new_message = await repo.edit_message_and_branch(
                message_id=message_id, new_content=payload.content
            )

            logger.info(
                f"Message {message_id} edited, new branch created: {new_message.id}"
            )

            return JSONResponse(
                {
                    "success": True,
                    "message": new_message.to_dict(),
                    "branch_created": True,
                }
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{session_id}/messages/{message_id}/branches")
    async def get_message_branches(
        session_id: str,
        message_id: str,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get all sibling branches for a message

        Returns all messages that share the same parent message,
        which represents different branches at that point.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()

            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session_obj, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _session_can_manage(session_obj, user_info):
                raise HTTPException(status_code=403, detail="Branch management denied")
            await _require_session_project_permission(
                session_obj,
                user_info,
                permission="write",
            )

            # Get sibling branches
            siblings = await repo.get_branch_siblings(message_id, session_id)

            # Find current message's index
            current_index = next(
                (i for i, s in enumerate(siblings) if s["id"] == message_id), 0
            )

            return JSONResponse(
                {
                    "success": True,
                    "branches": siblings,
                    "total": len(siblings),
                    "current_index": current_index,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get message branches: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{session_id}/messages/{message_id}/switch-branch")
    async def switch_branch(
        session_id: str,
        message_id: str,
        payload: SwitchBranchRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Switch to a different branch

        Activates the specified message and its descendants,
        deactivating the current branch.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()

            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session_obj, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _session_can_manage(session_obj, user_info):
                raise HTTPException(status_code=403, detail="Branch management denied")
            await _require_session_project_permission(
                session_obj,
                user_info,
                permission="write",
            )
            await _require_project_permission(
                session_obj.project_id,
                user_info,
                permission="write",
            )

            target_message_id = payload.target_message_id
            if not target_message_id and payload.branch_index is not None:
                siblings = await repo.get_branch_siblings(message_id, session_id)
                if payload.branch_index < 0 or payload.branch_index >= len(siblings):
                    raise HTTPException(status_code=400, detail="Invalid branch index")
                target_message_id = siblings[payload.branch_index]["id"]
            if not target_message_id:
                raise HTTPException(
                    status_code=400, detail="target_message_id or branch_index is required"
                )

            # Switch branch
            success = await repo.switch_active_branch(
                session_id=session_id, target_message_id=target_message_id
            )

            if not success:
                raise HTTPException(status_code=400, detail="Failed to switch branch")

            # Get updated messages
            messages = await repo.get_active_branch_messages(session_id)

            logger.info(f"Switched to branch with message {target_message_id}")

            return JSONResponse(
                {"success": True, "messages": [m.to_dict() for m in messages]}
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to switch branch: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{session_id}/active-messages")
    async def get_active_branch_messages(
        session_id: str, _: None = Depends(require_auth), request: Request = None
    ):
        """Get messages in the active branch only

        This filters out inactive branches and returns only
        the messages that should be displayed, with branch info included.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()

            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session_obj, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            await _require_session_project_permission(session_obj, user_info)

            try:
                messages, branch_info = (
                    await repo.get_active_branch_messages_with_branch_info(session_id)
                )
            except Exception as branch_error:
                logger.warning(
                    "Failed to get branch projection for active messages: %s",
                    branch_error,
                )
                messages = await repo.get_active_branch_messages(session_id)
                branch_info = {}

            return JSONResponse(
                {
                    "success": True,
                    "messages": _messages_with_branch_info(messages, branch_info),
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get active branch messages: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ─── RP Steering Settings Endpoints ─────────────────────────────────

    @router.get("/{session_id}/rp-settings")
    async def get_rp_settings(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get RP steering slider settings for a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            user_info = await _get_current_conversation_user(request)
            await _require_session_project_permission(session, user_info)

            rp_settings = session.rp_settings or {
                "creativity": 0.5,
                "detail": 0.5,
                "tempo": 0.5,
                "emotion": 0.5,
            }

            return JSONResponse({"success": True, "rp_settings": rp_settings})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get RP settings: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.put("/{session_id}/rp-settings")
    async def update_rp_settings(
        session_id: str,
        payload: UpdateRpSettingsRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Update RP steering slider settings for a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _session_can_manage(session, user_info):
                raise HTTPException(status_code=403, detail="RP settings management denied")
            await _require_project_permission(
                session.project_id,
                user_info,
                permission="write",
            )

            # 既存設定にマージ
            current_settings = session.rp_settings or {
                "creativity": 0.5,
                "detail": 0.5,
                "tempo": 0.5,
                "emotion": 0.5,
            }
            updates = {k: v for k, v in payload.model_dump().items() if v is not None}
            # 値のバリデーション (0.0 ~ 1.0)
            for key, val in updates.items():
                if not (0.0 <= val <= 1.0):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} は 0.0 ~ 1.0 の範囲で指定してください",
                    )
            current_settings.update(updates)

            # DB更新
            from sqlalchemy import update as sa_update
            from ..memory.models import ConversationSession as SessionModel
            from ..memory.database import get_database_manager
            import uuid as uuid_mod

            db_manager = get_database_manager()
            db_session = await db_manager.get_session()
            try:
                stmt = (
                    sa_update(SessionModel)
                    .where(SessionModel.id == uuid_mod.UUID(session_id))
                    .values(rp_settings=current_settings)
                )
                await db_session.execute(stmt)
                await db_session.commit()
            finally:
                await db_session.close()

            return JSONResponse({"success": True, "rp_settings": current_settings})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update RP settings: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
