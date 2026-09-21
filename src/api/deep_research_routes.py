"""Deep Research API routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..services.deep_research_service import (
    DEFAULT_ENGINES,
    DeepResearchJob,
    DeepResearchManager,
    DeepResearchRequest,
    DeepResearchManagerClosedError,
    DeepResearchQueueFullError,
)


class StartDeepResearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=2, max_length=4000)
    mode: str = "detailed"
    max_iterations: int = Field(3, ge=1, le=8)
    questions_per_iteration: int = Field(3, ge=1, le=6)
    max_results_per_query: int = Field(5, ge=1, le=10)
    engines: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ENGINES),
        max_length=16,
    )
    include_local_knowledge: bool = False
    project_id: Optional[str] = None
    # Enterprise callers must provide the conversation id; Personal keeps a
    # short compatibility window for older clients that launched ad-hoc jobs.
    # The value is validated against durable session ownership below, never
    # interpreted as an already-authorized permission cache key.
    session_id: Optional[str] = None

    @field_validator("query")
    @classmethod
    def _query_must_contain_text(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("query must contain at least two non-whitespace characters")
        return normalized


def _job_payload(job: DeepResearchJob, *, include_report: bool = True) -> dict[str, Any]:
    payload = job.to_dict(include_report=include_report)
    # Privacy policy snapshots and copied session/project context are
    # server-owned authorization material.  They are needed by the worker for
    # drift checks but must not be exposed through job polling/markdown APIs
    # (even to a participant who can read the conversation).
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        for key in ("session_context", "project_metadata", "privacy_snapshot"):
            metadata.pop(key, None)
        payload["metadata"] = metadata
    payload.pop("privacy_snapshot", None)
    return payload


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build a stable, sanitized error envelope for API clients."""

    return HTTPException(
        status_code=status_code,
        detail={"code": str(code), "message": str(message)},
    )


async def _authorized_project_id(
    project_id: Any,
    *,
    user_info: dict[str, Any],
) -> Optional[str]:
    """Return a project scope only after checking the caller's read ACL.

    ``project_id`` is supplied by the request body and therefore cannot be
    copied directly into a usage context.  Keep malformed IDs and inaccessible
    projects fail-closed while letting ``ProjectRepository.has_permission``
    retain its global-admin semantics.
    """

    if project_id in (None, ""):
        return None

    try:
        project_uuid = UUID(str(project_id).strip())
    except (TypeError, ValueError, AttributeError) as exc:
        raise _api_error(400, "invalid_project", "Invalid project id") from exc

    try:
        user_uuid = UUID(str(user_info.get("id") or "").strip())
    except (TypeError, ValueError, AttributeError) as exc:
        # A project scope cannot be authorized for the legacy/default user,
        # even when the request reached this authenticated route.
        raise _api_error(403, "project_access_denied", "Project access denied") from exc

    try:
        from ..memory.database import get_database_manager
        from ..memory.project_repository import ProjectRepository

        database = get_database_manager()
        if database is None:
            raise _api_error(503, "database_unavailable", "Database not available")
        session = await database.get_session()
    except HTTPException:
        raise
    except Exception as exc:
        raise _api_error(503, "database_unavailable", "Database not available") from exc

    try:
        allowed = await ProjectRepository.has_permission(
            session,
            project_id=project_uuid,
            user_id=user_uuid,
            permission="read",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _api_error(503, "project_access_unavailable", "Project access unavailable") from exc
    finally:
        await session.close()

    if not allowed:
        raise _api_error(403, "project_access_denied", "Project access denied")
    return str(project_uuid)


def _is_enterprise_profile() -> bool:
    try:
        from ..features import Features

        return bool(Features.is_enterprise())
    except Exception:
        import os

        return any(
            str(os.getenv(name) or "").strip().lower() == "enterprise"
            for name in ("AOITALK_PROFILE", "AIVTUBER_ENV")
        )


async def _authorized_session_scope(
    session_id: Any,
    *,
    user_info: dict[str, Any],
    project_id: Any = None,
    include_metadata: bool = False,
) -> tuple[Optional[str], Optional[str]] | tuple[
    Optional[str], Optional[str], dict[str, Any], dict[str, Any]
]:
    """Validate a conversation session and return its server-owned scope.

    The browser may provide only the opaque conversation UUID.  Ownership,
    participant role, deletion state, and project ACL are checked by the
    ConversationRepository authority before a background job is enqueued.
    """

    raw = str(session_id or "").strip()
    if not raw:
        if _is_enterprise_profile():
            raise _api_error(400, "scope_missing", "Conversation session is required")
        if include_metadata:
            return None, None, {}, {}
        return None, None
    try:
        normalized = str(UUID(raw))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _api_error(400, "invalid_scope", "Invalid conversation session") from exc

    actor_id = str(user_info.get("id") or "").strip()
    if not actor_id:
        raise _api_error(403, "scope_denied", "Conversation access denied")
    try:
        from ..memory.conversation_repository import ConversationRepository

        repository = ConversationRepository()
        allowed = await repository.user_has_session_write_access(normalized, actor_id)
        if not allowed:
            raise _api_error(403, "scope_denied", "Conversation access denied")
        conversation = await repository.get_session_by_id(normalized, with_messages=False)
    except HTTPException:
        raise
    except Exception as exc:
        raise _api_error(503, "scope_unavailable", "Conversation access unavailable") from exc
    if conversation is None:
        raise _api_error(404, "scope_not_found", "Conversation session not found")

    bound_project = (
        conversation.get("project_id")
        if isinstance(conversation, dict)
        else getattr(conversation, "project_id", None)
    )
    bound_project_text = str(bound_project).strip().casefold() if bound_project else None
    supplied_project = str(project_id or "").strip().casefold() or None
    if supplied_project and supplied_project != bound_project_text:
        raise _api_error(403, "project_scope_denied", "Conversation project scope denied")
    if not include_metadata:
        return normalized, bound_project_text
    session_context = _privacy_context_from_session(conversation)
    relationship_project = (
        conversation.get("project")
        if isinstance(conversation, Mapping)
        else None
    )
    project_metadata = await _load_authoritative_project_metadata(
        bound_project_text,
        project=relationship_project,
    )
    if bound_project_text:
        project_metadata = {
            **project_metadata,
            "project_id": bound_project_text,
        }
    return normalized, bound_project_text, session_context, project_metadata


async def _authorize_job_scope(
    job: DeepResearchJob,
    *,
    user_id: str,
    write: bool = False,
) -> None:
    """Re-check a persisted job's conversation ACL before exposing/mutating it.

    A job may outlive a membership or project-policy change.  Ownership checks
    on the JSON job store alone are therefore insufficient for Enterprise;
    read/cancel/markdown endpoints must consult the same durable conversation
    authority used at launch time.
    """

    if not _is_enterprise_profile():
        return
    if not job.session_id:
        raise _api_error(403, "scope_denied", "Conversation access denied")
    actor_id = str(user_id or "").strip()
    if not actor_id:
        raise _api_error(403, "scope_denied", "Conversation access denied")
    try:
        from ..memory.conversation_repository import ConversationRepository

        repository = ConversationRepository()
        checker = (
            repository.user_has_session_write_access
            if write
            else repository.user_has_session_access
        )
        if not await checker(str(job.session_id), actor_id):
            raise _api_error(403, "scope_denied", "Conversation access denied")
        conversation = await repository.get_session_by_id(
            str(job.session_id), with_messages=False
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _api_error(503, "scope_unavailable", "Conversation access unavailable") from exc
    if conversation is None:
        raise _api_error(404, "scope_not_found", "Conversation session not found")
    current_project = (
        conversation.get("project_id")
        if isinstance(conversation, Mapping)
        else getattr(conversation, "project_id", None)
    )
    expected_project = str(job.project_id or "").strip().casefold() or None
    current_project_text = str(current_project).strip().casefold() if current_project else None
    if current_project_text != expected_project:
        raise _api_error(403, "project_scope_denied", "Conversation project scope denied")


_PRIVACY_POLICY_KEYS = frozenset(
    {
        "privacy_mode",
        "review_policy",
        "semantic_redaction_enabled",
        "raw_media_policy",
        "trusted_local_hosts",
        "local_provider",
        "local_model",
    }
)
_PRIVACY_MODES = frozenset({"direct", "protected", "local_only"})


def _safe_privacy_value(key: str, value: Any) -> Any:
    """Normalize one allowlisted policy value without copying secrets."""

    if key == "privacy_mode":
        normalized = str(value or "").strip().lower()
        return normalized if normalized in _PRIVACY_MODES else None
    if key in {"review_policy", "raw_media_policy", "local_provider", "local_model"}:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            return None
        return normalized
    if key == "semantic_redaction_enabled":
        if isinstance(value, bool):
            return value
        return None
    if key == "trusted_local_hosts":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set, frozenset)):
            return None
        hosts = []
        for item in value:
            item = str(item or "").strip().lower()
            if item and len(item) <= 253:
                hosts.append(item)
        return list(dict.fromkeys(hosts))[:32]
    return None


def _privacy_policy_subset(value: Any) -> dict[str, Any]:
    """Return only server-owned privacy settings from a session/project row."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in _PRIVACY_POLICY_KEYS:
        if key not in value:
            continue
        safe = _safe_privacy_value(key, value.get(key))
        if safe is not None:
            result[key] = safe
    return result


def _privacy_context_from_session(session: Any) -> dict[str, Any]:
    raw_context = (
        session.get("context")
        if isinstance(session, Mapping)
        else getattr(session, "context", None)
    )
    return _privacy_policy_subset(raw_context)


def _privacy_metadata_from_project(project: Any) -> dict[str, Any]:
    raw_metadata = (
        project.get("project_metadata")
        if isinstance(project, Mapping)
        else getattr(project, "project_metadata", None)
    )
    return _privacy_policy_subset(raw_metadata)


async def _load_authoritative_project_metadata(
    project_id: Optional[str],
    *,
    project: Any = None,
) -> dict[str, Any]:
    """Read project policy after session ACL validation, never client metadata."""

    if not project_id:
        return {}
    if project is not None:
        # ``project`` is accepted only when supplied by the authoritative
        # session query/relationship, never from request JSON.  An explicitly
        # loaded project with no policy keys is a valid empty snapshot.
        return _privacy_metadata_from_project(project)
    # A relationship may already be loaded by a repository test double or a
    # future query optimization.  The normal ConversationRepository object is
    # deliberately queried again through ProjectRepository so a queued job gets
    # a durable point-in-time policy snapshot, not a stale browser projection.
    try:
        from ..memory.database import get_database_manager
        from ..memory.project_repository import ProjectRepository

        database = get_database_manager()
        if database is None:
            raise RuntimeError("database unavailable")
        session = await database.get_session()
        try:
            project_uuid = UUID(str(project_id))
            getter = getattr(ProjectRepository, "get_by_id", None)
            if callable(getter):
                project = await getter(session, project_uuid)
            else:
                # The current ProjectRepository is intentionally a focused ACL
                # helper and does not expose a generic fetch method.  Use the
                # same database session for a direct authoritative row read.
                from sqlalchemy import select
                from ..memory.models import Project

                result = await session.execute(
                    select(Project).where(Project.id == project_uuid)
                )
                project = result.scalar_one_or_none()
        finally:
            await session.close()
    except Exception as exc:
        raise _api_error(
            503,
            "scope_unavailable",
            "Conversation privacy policy unavailable",
        ) from exc
    return _privacy_metadata_from_project(project)


def create_deep_research_router(
    *,
    require_auth_dependency,
    get_current_user,
    config: Any,
) -> APIRouter:
    router = APIRouter(prefix="/api/deep-research", tags=["deep-research"])
    manager = DeepResearchManager(config=config)
    # Keep the manager discoverable for composition roots that provide an
    # explicit lifecycle hook, while also registering the APIRouter shutdown
    # hook for ordinary FastAPI applications.
    setattr(router, "deep_research_manager", manager)

    @router.on_event("startup")
    async def _startup_deep_research_manager() -> None:
        # The same WebChatServer instance may be mounted across multiple
        # FastAPI lifespans in tests/dev reloads.  Re-arm the process-owned
        # manager after its previous shutdown has drained workers.
        manager.reopen()

    @router.on_event("shutdown")
    async def _shutdown_deep_research_manager() -> None:
        await manager.shutdown()

    async def _current_user_info(request: Request) -> dict[str, Any]:
        user = await get_current_user(request)
        if isinstance(user, dict):
            return user
        return {}

    async def _current_user_id(user: dict[str, Any] = Depends(_current_user_info)) -> str:
        if user:
            return str(user.get("id") or user.get("username") or "default_user")
        return "default_user"

    @router.get("/engines")
    async def list_engines(_: None = Depends(require_auth_dependency)):
        default_engines = list(DEFAULT_ENGINES)
        if _is_enterprise_profile():
            try:
                from ..services.search_egress_policy import approved_public_egress

                if not approved_public_egress(config):
                    default_engines = ["searxng"]
            except Exception:
                # A policy resolution failure must not advertise public
                # defaults for an Enterprise deployment.
                default_engines = ["searxng"]
        return {"engines": manager.available_engines(), "default": default_engines}

    @router.get("/jobs")
    async def list_jobs(
        _: None = Depends(require_auth_dependency),
        limit: int = Query(30, ge=1, le=100),
        user_id: str = Depends(_current_user_id),
    ):
        visible_jobs: list[DeepResearchJob] = []
        for job in manager.list_jobs(user_id=user_id, limit=limit):
            try:
                # A persisted job can outlive a membership/project ACL change.
                # Do not expose even its scope metadata in the Enterprise list
                # once the conversation is no longer readable.
                await _authorize_job_scope(job, user_id=user_id)
            except HTTPException:
                continue
            visible_jobs.append(job)
        return {
            "jobs": [
                _job_payload(job, include_report=False)
                for job in visible_jobs
            ]
        }

    @router.post("/jobs", status_code=202)
    async def start_job(
        body: StartDeepResearchPayload,
        _: None = Depends(require_auth_dependency),
        user_info: dict[str, Any] = Depends(_current_user_info),
    ):
        user_id = str(user_info.get("id") or user_info.get("username") or "default_user")
        (
            validated_session_id,
            bound_project_id,
            session_context,
            project_metadata,
        ) = await _authorized_session_scope(
            body.session_id,
            user_info=user_info,
            project_id=body.project_id,
            include_metadata=True,
        )
        authorized_project_id = (
            await _authorized_project_id(body.project_id, user_info=user_info)
            if body.project_id
            else bound_project_id
        )
        try:
            job = await manager.start_job(
                DeepResearchRequest(
                    query=body.query,
                    mode=body.mode,
                    max_iterations=body.max_iterations,
                    questions_per_iteration=body.questions_per_iteration,
                    max_results_per_query=body.max_results_per_query,
                    engines=body.engines,
                    include_local_knowledge=body.include_local_knowledge,
                    project_id=authorized_project_id,
                    actor_user_id=str(user_info.get("id")) if user_info.get("id") else None,
                    is_admin=user_info.get("role") == "admin",
                    session_id=validated_session_id,
                    session_context=session_context,
                    project_metadata=project_metadata,
                ),
                user_id=user_id,
            )
        except DeepResearchQueueFullError as exc:
            raise _api_error(429, "queue_full", "調査キューが混雑しています") from exc
        except DeepResearchManagerClosedError as exc:
            raise _api_error(503, "manager_shutdown", "調査サービスは現在停止中です") from exc
        return _job_payload(job)

    @router.get("/jobs/{job_id}")
    async def get_job(
        job_id: str,
        _: None = Depends(require_auth_dependency),
        user_id: str = Depends(_current_user_id),
    ):
        job = manager.get_job(job_id, user_id=user_id)
        if not job:
            raise HTTPException(status_code=404, detail="調査ジョブが見つかりません")
        await _authorize_job_scope(job, user_id=user_id)
        return _job_payload(job)

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        _: None = Depends(require_auth_dependency),
        user_id: str = Depends(_current_user_id),
    ):
        candidate = manager.get_job(job_id, user_id=user_id)
        if not candidate:
            raise HTTPException(status_code=404, detail="調査ジョブが見つかりません")
        await _authorize_job_scope(candidate, user_id=user_id, write=True)
        job = await manager.cancel_job(job_id, user_id=user_id)
        if not job:
            raise HTTPException(status_code=404, detail="調査ジョブが見つかりません")
        return _job_payload(job)

    @router.get("/jobs/{job_id}/markdown")
    async def export_markdown(
        job_id: str,
        _: None = Depends(require_auth_dependency),
        user_id: str = Depends(_current_user_id),
    ):
        job = manager.get_job(job_id, user_id=user_id)
        if not job:
            raise HTTPException(status_code=404, detail="調査ジョブが見つかりません")
        await _authorize_job_scope(job, user_id=user_id)
        return Response(
            content=job.report_markdown or "",
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="deep-research-{job.id}.md"'
            },
        )

    return router
