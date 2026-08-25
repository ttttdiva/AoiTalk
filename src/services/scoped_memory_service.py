"""Authoritative scope-safe memory pipeline.

All durable memory mutations, including Dreaming and correction handling, pass
through this service. Retrieval methods never commit or update usage fields.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

from sqlalchemy import and_, or_, select, text

from ..memory.database import get_db_session
from ..memory.models import (
    ContextMemory,
    ContextMemoryAudit,
    ConversationSession,
    Project,
    ProjectMember,
    ScopedMemoryPrincipal,
    ScopedMemoryJob,
    Task,
    User,
)
from .context_memory_service import _keywords

logger = logging.getLogger(__name__)


VALID_SCOPES = ("global", "user", "project", "task", "session")
SCOPE_PRIORITY = {"global": 1, "user": 2, "project": 3, "task": 4, "session": 5}
ACTIVE_STATUSES = {"active", "candidate"}
_DEDUPE_LOCKS: dict[str, tuple[asyncio.Lock, int]] = {}
_DEDUPE_LOCKS_GUARD = threading.Lock()
_SECRET_RE = re.compile(
    r"(?:password|passwd|secret|bearer\s+[a-z0-9._-]+|api[_ -]?key|"
    r"token\s*[:=]|秘密鍵|パスワード\s*[:：])",
    re.IGNORECASE,
)
_SENSITIVE_RE = re.compile(
    r"(?:\b\d{3}-\d{2}-\d{4}\b|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b|"
    r"住所|電話番号|生年月日)",
    re.IGNORECASE,
)
_GLOBAL_CORRECTION_RE = re.compile(
    r"(?:globally|across\s+all|for\s+every\s+project|全体で|いつでも|"
    r"どのプロジェクトでも|どの案件でも|すべての案件で)",
    re.IGNORECASE,
)
_TASK_CORRECTION_RE = re.compile(
    r"(?:このタスクだけ|この作業だけ|for\s+this\s+task)", re.IGNORECASE
)
_SESSION_CORRECTION_RE = re.compile(
    r"(?:今回だけ|この会話だけ|このやり取りだけ|this\s+time\s+only|"
    r"for\s+this\s+session)",
    re.IGNORECASE,
)
_CORRECTION_MARKER_RE = re.compile(
    r"(?:正しくは|ではなく|じゃなくて|違います?[。、, ]*|違う[。、, ]*|訂正[:：]?|"
    r"actually[, ]*|correction[:：]?)",
    re.IGNORECASE,
)
_EXTERNAL_PRINCIPAL_RE = re.compile(
    r"^(?P<provider>[a-z][a-z0-9._-]{0,31}):"
    r"(?P<tenant>[^:\s]{1,64}):(?P<subject>[^:\s]{1,64})$",
    re.IGNORECASE,
)


class ScopedMemoryError(RuntimeError):
    status_code = 400


class ScopedMemoryNotFound(ScopedMemoryError):
    status_code = 404


class ScopedMemoryPermissionDenied(ScopedMemoryError):
    status_code = 403


class ScopedMemoryConflict(ScopedMemoryError):
    status_code = 409


class ScopedMemoryValidationError(ScopedMemoryError):
    status_code = 422


@dataclass(frozen=True)
class MemoryScope:
    scope_type: str
    scope_id: str
    user_id: str
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "user_id": self.user_id,
            "project_id": str(self.project_id) if self.project_id else None,
            "task_id": str(self.task_id) if self.task_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
        }


def _uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _canonical_actor_id(value: Any) -> str:
    """Return the stable owner key used by every Scoped Memory query.

    UUID-backed AoiTalk users retain their normal textual UUID identity.  An
    external principal is an opaque, namespaced key and is deliberately not
    resolved to (or materialized as) a ``users`` row.  Discord keys are
    canonicalized to a lowercase provider prefix while preserving the
    guild/user components exactly as supplied by the integration.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ScopedMemoryValidationError("principal is required")
    parsed = _uuid(raw)
    if parsed is not None:
        return str(parsed)
    if len(raw) > 100 or "\x00" in raw:
        raise ScopedMemoryValidationError("invalid principal")
    match = _EXTERNAL_PRINCIPAL_RE.fullmatch(raw)
    if match is not None:
        provider = match.group("provider").casefold()
        tenant = match.group("tenant")
        subject = match.group("subject")
        # The canonical Discord form is intentionally three-part and does not
        # permit a caller to collapse guild and user into one shared bucket.
        if provider == "discord":
            return f"discord:{tenant}:{subject}"
        return f"{provider}:{tenant}:{subject}"
    # Keep legacy opaque owners working during rollout.  New integrations
    # should use the namespaced form above; the string owner column remains
    # bounded and all ACL checks are exact-key comparisons.
    return raw


def _is_discord_principal(value: Any) -> bool:
    """Return whether an owner key is a canonical Discord external principal."""
    try:
        return _canonical_actor_id(value).casefold().startswith("discord:")
    except ScopedMemoryError:
        return False


def _normalized_text(value: Any) -> str:
    text = " ".join(str(value or "").strip().casefold().split())
    return re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]+", "", text)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dedupe_key(content: str, memory_type: str, explicit: str | None = None) -> str:
    if explicit:
        return str(explicit).strip()[:128]
    return _digest(f"{memory_type.casefold()}:{_normalized_text(content)}")


@asynccontextmanager
async def _local_dedupe_lock(material: str):
    """Serialize non-PostgreSQL/test writers without leaking per-key locks."""
    with _DEDUPE_LOCKS_GUARD:
        lock, references = _DEDUPE_LOCKS.get(material, (asyncio.Lock(), 0))
        _DEDUPE_LOCKS[material] = (lock, references + 1)
    acquired = False
    try:
        await lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        with _DEDUPE_LOCKS_GUARD:
            current_lock, references = _DEDUPE_LOCKS.get(material, (lock, 1))
            if current_lock is lock and references <= 1:
                _DEDUPE_LOCKS.pop(material, None)
            elif current_lock is lock:
                _DEDUPE_LOCKS[material] = (lock, references - 1)


def classify_sensitivity(content: str) -> tuple[str, str | None]:
    if _SECRET_RE.search(content):
        return "secret", "secret-like material is not allowed in memory"
    if _SENSITIVE_RE.search(content):
        return "sensitive", None
    return "normal", None


def _scope(
    *,
    actor_id: str,
    scope_type: str,
    scope_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> MemoryScope:
    actor_id = _canonical_actor_id(actor_id)
    normalized_type = str(scope_type or "user").strip().casefold()
    if normalized_type not in VALID_SCOPES:
        raise ScopedMemoryValidationError(f"unsupported memory scope: {scope_type}")
    project_uuid = _uuid(project_id)
    task_uuid = _uuid(task_id)
    session_uuid = _uuid(session_id)
    resolved_scope_id = str(scope_id or "").strip()
    if normalized_type == "global":
        resolved_scope_id = resolved_scope_id or "global"
    elif normalized_type == "user":
        resolved_scope_id = resolved_scope_id or str(actor_id)
    elif normalized_type == "project":
        if not project_uuid:
            project_uuid = _uuid(resolved_scope_id)
        if not project_uuid:
            raise ScopedMemoryValidationError("project scope requires project_id")
        resolved_scope_id = str(project_uuid)
    elif normalized_type == "task":
        if not task_uuid:
            task_uuid = _uuid(resolved_scope_id)
        if not task_uuid:
            raise ScopedMemoryValidationError("task scope requires task_id")
        resolved_scope_id = str(task_uuid)
    elif normalized_type == "session":
        if not session_uuid:
            session_uuid = _uuid(resolved_scope_id)
        if not session_uuid:
            raise ScopedMemoryValidationError("session scope requires session_id")
        resolved_scope_id = str(session_uuid)
    return MemoryScope(
        scope_type=normalized_type,
        scope_id=resolved_scope_id,
        user_id=str(actor_id),
        project_id=project_uuid,
        task_id=task_uuid,
        session_id=session_uuid,
    )


def _turn_context(explicit: dict[str, Any] | None, tool_call_id: str | None) -> dict[str, Any]:
    if explicit is not None:
        context = dict(explicit)
    else:
        try:
            from .turn_context import get_turn_context

            current = get_turn_context()
            context = {
                "user_id": current.user_id,
                "project_id": current.project_id,
                "session_id": current.session_id,
                "message_id": current.message_id,
                "client_message_id": current.client_message_id,
                "tool_call_id": current.tool_call_id,
            }
        except Exception:
            context = {}
    if tool_call_id:
        context["tool_call_id"] = str(tool_call_id)
    return {key: value for key, value in context.items() if value not in (None, "")}


def _audit_snapshot(memory: ContextMemory | dict[str, Any] | None) -> dict[str, Any]:
    if memory is None:
        return {}
    if isinstance(memory, dict):
        return dict(memory)
    content = str(memory.content or "")
    return {
        "id": str(memory.id),
        "scope_type": memory.scope_type,
        "scope_id": memory.scope_id,
        "memory_type": memory.memory_type,
        "content_sha256": _digest(content),
        "status": memory.status,
        "version": memory.version,
        "dedupe_key": memory.dedupe_key,
        "supersedes_id": str(memory.supersedes_id) if memory.supersedes_id else None,
    }


class ScopedMemoryService:
    """Single mutation and retrieval boundary for all memory scopes."""

    def __init__(
        self,
        session_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_db_session

    async def _new_session(self):
        return await self._session_factory()

    @asynccontextmanager
    async def _serialized_dedupe_session(self, material: str):
        async with _local_dedupe_lock(material):
            async with await self._new_session() as session:
                bind = session.get_bind()
                if bind.dialect.name == "postgresql":
                    advisory_key = int.from_bytes(
                        hashlib.sha256(material.encode("utf-8")).digest()[:8],
                        byteorder="big",
                        signed=True,
                    )
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": advisory_key},
                    )
                yield session

    async def _require_project_permission(
        self,
        session,
        *,
        project_id: uuid.UUID,
        actor_id: str,
        write: bool,
    ) -> None:
        project = await session.get(Project, project_id)
        if project is None or project.deleted_at is not None:
            raise ScopedMemoryNotFound("project not found")
        actor_uuid = _uuid(actor_id)
        user = await session.get(User, actor_uuid) if actor_uuid else None
        if user is not None and user.role == "admin":
            return
        if actor_uuid and project.owner_id == actor_uuid:
            return
        if not actor_uuid:
            raise ScopedMemoryPermissionDenied("project access denied")
        member = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == actor_uuid,
                )
            )
        ).scalar_one_or_none()
        permission = "write" if write else "read"
        if member is None or not bool((member.permissions or {}).get(permission)):
            raise ScopedMemoryPermissionDenied("project access denied")

    async def _require_scope_permission(
        self,
        session,
        *,
        scope: MemoryScope,
        actor_id: str,
        write: bool,
    ) -> None:
        if scope.scope_type in {"global", "user"}:
            if scope.user_id != str(actor_id):
                raise ScopedMemoryPermissionDenied("cross-user memory access denied")
            return
        if scope.scope_type == "project":
            assert scope.project_id is not None
            await self._require_project_permission(
                session, project_id=scope.project_id, actor_id=actor_id, write=write
            )
            return
        if scope.scope_type == "task":
            task = await session.get(Task, scope.task_id)
            if task is None or task.deleted_at is not None:
                raise ScopedMemoryNotFound("task not found")
            await self._require_project_permission(
                session, project_id=task.project_id, actor_id=actor_id, write=write
            )
            return
        conversation = await session.get(ConversationSession, scope.session_id)
        if conversation is None or conversation.deleted_at is not None:
            raise ScopedMemoryNotFound("session not found")
        if str(conversation.user_id) != str(actor_id):
            raise ScopedMemoryPermissionDenied("session access denied")

    @staticmethod
    def _scope_from_memory(memory: ContextMemory) -> MemoryScope:
        return MemoryScope(
            scope_type=memory.scope_type,
            scope_id=str(memory.scope_id or ""),
            user_id=str(memory.user_id or ""),
            project_id=memory.project_id,
            task_id=memory.task_id,
            session_id=memory.session_id,
        )

    @staticmethod
    def _result(
        memory: ContextMemory,
        *,
        operation: str,
        reason: str,
        replaced_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        payload = memory.to_dict()
        return {
            "success": True,
            "memory_id": str(memory.id),
            "scope": memory.scope_type,
            "scope_id": memory.scope_id,
            "operation": operation,
            "replaced_id": str(replaced_id) if replaced_id else None,
            "reason": reason,
            "memory": payload,
        }

    @staticmethod
    def _add_audit(
        session,
        *,
        memory: ContextMemory | None,
        actor_id: str,
        operation: str,
        before: ContextMemory | dict[str, Any] | None,
        after: ContextMemory | dict[str, Any] | None,
        turn_context: dict[str, Any],
        reason: str,
    ) -> None:
        session.add(
            ContextMemoryAudit(
                id=uuid.uuid4(),
                memory_id=memory.id if memory else None,
                user_id=(memory.user_id if memory else None),
                operation=operation,
                actor=str(actor_id),
                turn_context=turn_context,
                before_snapshot=_audit_snapshot(before),
                after_snapshot=_audit_snapshot(after),
                reason=reason,
            )
        )

    async def upsert_memory(
        self,
        *,
        actor_id: str,
        content: str,
        scope_type: str = "user",
        scope_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        memory_type: str = "fact",
        title: str | None = None,
        structured_data: dict[str, Any] | None = None,
        source_type: str = "manual",
        source_ref: str | None = None,
        confidence: float = 1.0,
        importance: int = 5,
        trust_level: str | None = None,
        evidence_refs: Iterable[dict[str, Any]] | None = None,
        evidence_span: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        status: str | None = None,
        is_pinned: bool = False,
        expires_at: datetime | None = None,
        created_by_actor: str | None = None,
        projection_metadata: dict[str, Any] | None = None,
        migration_id: str | None = None,
        turn_context: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        clean_content = str(content or "").strip()
        if not clean_content:
            raise ScopedMemoryValidationError("memory content is required")
        scope = _scope(
            actor_id=str(actor_id),
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
        )
        memory_type = str(memory_type or "fact").strip().casefold()[:32]
        sensitivity, rejection_reason = classify_sensitivity(clean_content)
        effective_status = str(status or ("candidate" if source_type.endswith("_auto") else "active"))
        if rejection_reason:
            effective_status = "rejected"
        context = _turn_context(turn_context, tool_call_id)
        context.setdefault("user_id", str(actor_id))
        key = _dedupe_key(clean_content, memory_type, dedupe_key)
        effective_idempotency_key = str(idempotency_key or "").strip()
        if not effective_idempotency_key:
            effective_idempotency_key = (
                "implicit:"
                + _digest(
                    f"{actor_id}:{scope.scope_type}:{scope.scope_id}:"
                    f"{source_type}:{key}"
                )
            )
        effective_source_ref = str(source_ref or "").strip() or (
            f"{source_type}:{effective_idempotency_key}"
        )
        evidence = [dict(item) for item in evidence_refs or [] if isinstance(item, dict)]
        if not evidence:
            evidence = [
                {
                    "type": str(source_type or "manual"),
                    "source_ref": effective_source_ref,
                }
            ]
        metadata = dict(projection_metadata or {})
        metadata["idempotency_key"] = effective_idempotency_key
        metadata["turn_context"] = context

        lock_material = f"{actor_id}:{scope.scope_type}:{scope.scope_id}:{key}"
        async with self._serialized_dedupe_session(lock_material) as session:
            await self._require_scope_permission(
                session, scope=scope, actor_id=str(actor_id), write=True
            )
            existing_rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.user_id == str(actor_id),
                            ContextMemory.scope_type == scope.scope_type,
                            ContextMemory.scope_id == scope.scope_id,
                            ContextMemory.dedupe_key == key,
                            ContextMemory.status.in_(tuple(ACTIVE_STATUSES)),
                        )
                        .order_by(ContextMemory.version.desc())
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            idempotent_rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.user_id == str(actor_id),
                            ContextMemory.scope_type == scope.scope_type,
                            ContextMemory.scope_id == scope.scope_id,
                            ContextMemory.status.in_(tuple(ACTIVE_STATUSES)),
                        )
                        .order_by(ContextMemory.updated_at.desc())
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
            for existing in idempotent_rows:
                if (existing.projection_metadata or {}).get(
                    "idempotency_key"
                ) == effective_idempotency_key:
                    return self._result(
                        existing,
                        operation="unchanged",
                        reason="idempotency_key_replayed",
                    )
            identical = next(
                (
                    item
                    for item in existing_rows
                    if _normalized_text(item.content) == _normalized_text(clean_content)
                    and item.status == effective_status
                ),
                None,
            )
            if identical is not None:
                return self._result(
                    identical,
                    operation="unchanged",
                    reason="same_scope_dedupe_match",
                )

            replaced = next((item for item in existing_rows if item.status == "active"), None)
            if replaced is None:
                replaced = next((item for item in existing_rows if item.status == "candidate"), None)
            version = (int(replaced.version or 1) + 1) if replaced else 1
            now = datetime.utcnow()
            replaced_before = _audit_snapshot(replaced)
            if replaced is not None:
                replaced.status = "superseded"
                replaced.updated_at = now
                await session.flush()

            memory = ContextMemory(
                id=uuid.uuid4(),
                user_id=str(actor_id),
                project_id=scope.project_id,
                task_id=scope.task_id,
                session_id=scope.session_id,
                scope_type=scope.scope_type,
                scope_id=scope.scope_id,
                memory_type=memory_type,
                title=(str(title).strip()[:200] if title else None),
                content=clean_content,
                structured_data=dict(structured_data or {}),
                source_type=str(source_type or "manual")[:32],
                source_ref=effective_source_ref,
                confidence=max(0.0, min(float(confidence), 1.0)),
                importance=max(1, min(int(importance), 10)),
                trust_level=str(
                    trust_level
                    or ("verified" if source_type in {"manual", "correction"} else "inferred")
                )[:32],
                sensitivity=sensitivity,
                evidence_refs=evidence,
                evidence_span=dict(evidence_span or {}),
                dedupe_key=key,
                supersedes_id=replaced.id if replaced else None,
                version=version,
                created_by_actor=str(created_by_actor or actor_id)[:120],
                rejection_reason=rejection_reason,
                projection_metadata=metadata,
                migration_id=migration_id,
                status=effective_status,
                is_pinned=bool(is_pinned),
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(memory)
            await session.flush()
            reason = rejection_reason or (
                "superseded_same_scope_memory" if replaced else "new_scoped_memory"
            )
            self._add_audit(
                session,
                memory=memory,
                actor_id=str(actor_id),
                operation="rejected" if rejection_reason else ("superseded" if replaced else "created"),
                before=replaced_before,
                after=memory,
                turn_context=context,
                reason=reason,
            )
            await session.commit()
            await session.refresh(memory)
            return self._result(
                memory,
                operation="rejected" if rejection_reason else ("superseded" if replaced else "created"),
                reason=reason,
                replaced_id=replaced.id if replaced else None,
            )

    async def get_memory(self, memory_id: str, *, actor_id: str) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            memory = await session.get(ContextMemory, _uuid(memory_id))
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(memory),
                actor_id=str(actor_id),
                write=False,
            )
            return memory.to_dict()

    async def list_memories(
        self,
        *,
        actor_id: str,
        scope_type: str | None = None,
        scope_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        include_history: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            stmt = select(ContextMemory)
            if project_id:
                project_uuid = _uuid(project_id)
                if not project_uuid:
                    raise ScopedMemoryValidationError("invalid project id")
                await self._require_project_permission(
                    session, project_id=project_uuid, actor_id=str(actor_id), write=False
                )
                stmt = stmt.where(ContextMemory.project_id == project_uuid)
            else:
                stmt = stmt.where(ContextMemory.user_id == str(actor_id))
            if task_id:
                stmt = stmt.where(ContextMemory.task_id == _uuid(task_id))
            if session_id:
                stmt = stmt.where(ContextMemory.session_id == _uuid(session_id))
            if scope_type:
                stmt = stmt.where(ContextMemory.scope_type == scope_type)
            if scope_id:
                stmt = stmt.where(ContextMemory.scope_id == str(scope_id))
            if status:
                stmt = stmt.where(ContextMemory.status == status)
            elif not include_history:
                stmt = stmt.where(ContextMemory.status.in_(("active", "candidate")))
            rows = (
                await session.execute(
                    stmt.order_by(
                        ContextMemory.is_pinned.desc(),
                        ContextMemory.importance.desc(),
                        ContextMemory.updated_at.desc(),
                    ).limit(max(1, min(int(limit), 1000)))
                )
            ).scalars().all()
            return [row.to_dict() for row in rows]

    async def get_settings(
        self,
        *,
        actor_id: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            actor_uuid = _uuid(actor_id)
            user = await session.get(User, actor_uuid) if actor_uuid else None
            principal = None
            if actor_uuid:
                if user is None:
                    raise ScopedMemoryPermissionDenied("authenticated user not found")
                user_settings = user.user_settings if isinstance(user.user_settings, dict) else {}
            else:
                # External principals are first-class owners, not users.  A
                # missing row intentionally means the safe default (enabled)
                # and does not create a synthetic account on a read path.
                principal = await session.get(ScopedMemoryPrincipal, actor_id)
                user_settings = (
                    principal.settings
                    if principal is not None and isinstance(principal.settings, dict)
                    else {}
                )
            memory_settings = (
                dict(user_settings.get("scoped_memory") or {})
                if isinstance(user_settings.get("scoped_memory"), dict)
                else {}
            )
            project_enabled: bool | None = None
            if project_id:
                project_uuid = _uuid(project_id)
                if not project_uuid:
                    raise ScopedMemoryValidationError("invalid project id")
                await self._require_project_permission(
                    session,
                    project_id=project_uuid,
                    actor_id=str(actor_id),
                    write=False,
                )
                project = await session.get(Project, project_uuid)
                metadata = project.project_metadata if isinstance(project.project_metadata, dict) else {}
                project_enabled = bool(
                    metadata.get("scoped_memory_auto_enabled", memory_settings.get("auto_enabled", True))
                )
            return {
                "user_auto_enabled": bool(memory_settings.get("auto_enabled", True)),
                "project_auto_enabled": project_enabled,
                "project_id": str(project_id) if project_id else None,
            }

    async def update_settings(
        self,
        *,
        actor_id: str,
        user_auto_enabled: bool | None = None,
        project_id: str | None = None,
        project_auto_enabled: bool | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            actor_uuid = _uuid(actor_id)
            user = await session.get(User, actor_uuid) if actor_uuid else None
            principal = None
            if actor_uuid:
                if user is None:
                    raise ScopedMemoryPermissionDenied("authenticated user not found")
            else:
                principal = await session.get(ScopedMemoryPrincipal, actor_id)
                if principal is None:
                    principal = ScopedMemoryPrincipal(
                        principal_key=actor_id,
                        provider=actor_id.split(":", 1)[0].casefold()
                        if ":" in actor_id
                        else "external",
                        settings={},
                        metadata_json={},
                    )
                    session.add(principal)
            if user_auto_enabled is not None:
                settings = dict(
                    user.user_settings if actor_uuid and user is not None else principal.settings
                    if principal is not None
                    else {}
                )
                scoped = dict(settings.get("scoped_memory") or {})
                scoped["auto_enabled"] = bool(user_auto_enabled)
                settings["scoped_memory"] = scoped
                if actor_uuid and user is not None:
                    user.user_settings = settings
                elif principal is not None:
                    principal.settings = settings
            if project_auto_enabled is not None:
                project_uuid = _uuid(project_id)
                if not project_uuid:
                    raise ScopedMemoryValidationError(
                        "project_id is required for project auto-memory setting"
                    )
                await self._require_project_permission(
                    session,
                    project_id=project_uuid,
                    actor_id=str(actor_id),
                    write=True,
                )
                project = await session.get(Project, project_uuid)
                metadata = dict(project.project_metadata or {})
                metadata["scoped_memory_auto_enabled"] = bool(project_auto_enabled)
                project.project_metadata = metadata
            context = _turn_context(None, None)
            context.setdefault("user_id", str(actor_id))
            session.add(
                ContextMemoryAudit(
                    id=uuid.uuid4(),
                    memory_id=None,
                    user_id=str(actor_id),
                    operation="settings_updated",
                    actor=str(actor_id),
                    turn_context=context,
                    before_snapshot={},
                    after_snapshot={
                        "user_auto_enabled": user_auto_enabled,
                        "project_id": str(project_id) if project_id else None,
                        "project_auto_enabled": project_auto_enabled,
                    },
                    reason="explicit_settings_update",
                )
            )
            await session.commit()
        return await self.get_settings(actor_id=str(actor_id), project_id=project_id)

    async def list_jobs(
        self,
        *,
        actor_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            rows = list(
                (
                    await session.execute(
                        select(ScopedMemoryJob)
                        .where(ScopedMemoryJob.user_id == str(actor_id))
                        .order_by(ScopedMemoryJob.created_at.desc())
                        .limit(max(1, min(int(limit), 200)))
                    )
                ).scalars().all()
            )
            return [
                {
                    "id": str(row.id),
                    "session_id": str(row.session_id),
                    "project_id": str(row.project_id) if row.project_id else None,
                    "status": row.status,
                    "attempts": row.attempts,
                    "error": row.error,
                    "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ]

    async def update_memory(
        self,
        memory_id: str,
        *,
        actor_id: str,
        changes: dict[str, Any],
        expected_version: int,
        turn_context: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        allowed = {
            "content",
            "title",
            "memory_type",
            "structured_data",
            "confidence",
            "importance",
            "is_pinned",
            "expires_at",
            "trust_level",
            "evidence_refs",
            "evidence_span",
        }
        context = _turn_context(turn_context, tool_call_id)
        context.setdefault("user_id", str(actor_id))
        async with await self._new_session() as session:
            memory = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(memory),
                actor_id=str(actor_id),
                write=True,
            )
            if int(memory.version or 1) != int(expected_version):
                raise ScopedMemoryConflict(
                    f"memory version changed: expected {expected_version}, current {memory.version}"
                )
            if memory.status not in {"active", "candidate"}:
                raise ScopedMemoryConflict(
                    f"memory is not editable in status {memory.status}"
                )
            values = {key: getattr(memory, key) for key in allowed}
            values.update({key: value for key, value in changes.items() if key in allowed})
            content = str(values["content"] or "").strip()
            if not content:
                raise ScopedMemoryValidationError("memory content is required")
            sensitivity, rejection_reason = classify_sensitivity(content)
            before = _audit_snapshot(memory)
            now = datetime.utcnow()
            memory.status = "superseded"
            memory.updated_at = now
            await session.flush()
            replacement = ContextMemory(
                id=uuid.uuid4(),
                user_id=memory.user_id,
                project_id=memory.project_id,
                task_id=memory.task_id,
                session_id=memory.session_id,
                scope_type=memory.scope_type,
                scope_id=memory.scope_id,
                memory_type=str(values["memory_type"] or memory.memory_type)[:32],
                title=values["title"],
                content=content,
                structured_data=dict(values["structured_data"] or {}),
                source_type="manual_update",
                source_ref=memory.source_ref,
                confidence=max(0.0, min(float(values["confidence"]), 1.0)),
                importance=max(1, min(int(values["importance"]), 10)),
                trust_level=str(values["trust_level"] or memory.trust_level)[:32],
                sensitivity=sensitivity,
                evidence_refs=list(values["evidence_refs"] or []),
                evidence_span=dict(values["evidence_span"] or {}),
                dedupe_key=_dedupe_key(content, str(values["memory_type"] or memory.memory_type)),
                supersedes_id=memory.id,
                version=int(memory.version or 1) + 1,
                created_by_actor=str(actor_id),
                rejection_reason=rejection_reason,
                projection_metadata={"turn_context": context},
                status="rejected" if rejection_reason else "active",
                is_pinned=bool(values["is_pinned"]),
                expires_at=values["expires_at"],
                created_at=now,
                updated_at=now,
            )
            session.add(replacement)
            await session.flush()
            self._add_audit(
                session,
                memory=replacement,
                actor_id=str(actor_id),
                operation="updated",
                before=before,
                after=replacement,
                turn_context=context,
                reason="optimistic_lock_update",
            )
            await session.commit()
            await session.refresh(replacement)
            return self._result(
                replacement,
                operation="updated",
                reason="optimistic_lock_update",
                replaced_id=memory.id,
            )

    async def forget_memory(
        self,
        memory_id: str,
        *,
        actor_id: str,
        expected_version: int | None = None,
        full_deletion: bool = False,
        reason: str = "explicit_forget",
        turn_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            memory = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(memory),
                actor_id=str(actor_id),
                write=True,
            )
            if expected_version is not None and int(memory.version or 1) != int(expected_version):
                raise ScopedMemoryConflict("memory version changed")
            context = _turn_context(turn_context, None)
            context.setdefault("user_id", str(actor_id))
            before = _audit_snapshot(memory)
            if full_deletion:
                self._add_audit(
                    session,
                    memory=memory,
                    actor_id=str(actor_id),
                    operation="hard_deleted",
                    before=before,
                    after=None,
                    turn_context=context,
                    reason=reason,
                )
                await session.flush()
                await session.delete(memory)
                await session.commit()
                return {
                    "success": True,
                    "memory_id": memory_id,
                    "scope": before.get("scope_type"),
                    "operation": "hard_deleted",
                    "replaced_id": None,
                    "reason": reason,
                }
            memory.status = "forgotten"
            memory.version = int(memory.version or 1) + 1
            memory.updated_at = datetime.utcnow()
            self._add_audit(
                session,
                memory=memory,
                actor_id=str(actor_id),
                operation="forgotten",
                before=before,
                after=memory,
                turn_context=context,
                reason=reason,
            )
            await session.commit()
            return self._result(memory, operation="forgotten", reason=reason)

    async def decide_candidate(
        self,
        memory_id: str,
        *,
        actor_id: str,
        approve: bool,
        expected_version: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        context = _turn_context(None, None)
        context.setdefault("user_id", str(actor_id))
        async with await self._new_session() as session:
            candidate = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if candidate is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(candidate),
                actor_id=str(actor_id),
                write=True,
            )
            if candidate.status != "candidate":
                raise ScopedMemoryConflict("memory is not a candidate")
            if int(candidate.version or 1) != int(expected_version):
                raise ScopedMemoryConflict("memory version changed")
            if not approve:
                before = _audit_snapshot(candidate)
                candidate.status = "rejected"
                candidate.rejection_reason = reason or "rejected_by_user"
                candidate.version = int(candidate.version or 1) + 1
                candidate.updated_at = datetime.utcnow()
                self._add_audit(
                    session,
                    memory=candidate,
                    actor_id=str(actor_id),
                    operation="rejected",
                    before=before,
                    after=candidate,
                    turn_context=context,
                    reason=candidate.rejection_reason,
                )
                await session.commit()
                return self._result(
                    candidate, operation="rejected", reason=candidate.rejection_reason
                )

            before = _audit_snapshot(candidate)
            candidate.status = "superseded"
            candidate.updated_at = datetime.utcnow()
            await session.flush()
            approved = ContextMemory(
                id=uuid.uuid4(),
                user_id=candidate.user_id,
                project_id=candidate.project_id,
                task_id=candidate.task_id,
                session_id=candidate.session_id,
                scope_type=candidate.scope_type,
                scope_id=candidate.scope_id,
                memory_type=candidate.memory_type,
                title=candidate.title,
                content=candidate.content,
                structured_data=candidate.structured_data or {},
                source_type="candidate_approval",
                source_ref=candidate.source_ref,
                confidence=candidate.confidence,
                importance=candidate.importance,
                trust_level="verified",
                sensitivity=candidate.sensitivity,
                evidence_refs=candidate.evidence_refs or [],
                evidence_span=candidate.evidence_span or {},
                dedupe_key=candidate.dedupe_key,
                supersedes_id=candidate.id,
                version=int(candidate.version or 1) + 1,
                created_by_actor=str(actor_id),
                projection_metadata=candidate.projection_metadata or {},
                status="active",
                is_pinned=candidate.is_pinned,
                expires_at=candidate.expires_at,
            )
            session.add(approved)
            await session.flush()
            self._add_audit(
                session,
                memory=approved,
                actor_id=str(actor_id),
                operation="approved",
                before=before,
                after=approved,
                turn_context=context,
                reason=reason or "approved_by_user",
            )
            project_scope_id = (
                candidate.project_id
                if candidate.scope_type == "project" and candidate.project_id
                else None
            )
            if project_scope_id is not None:
                from .project_context_pack_service import invalidate_project_context_pack

                await invalidate_project_context_pack(
                    session=session,
                    project_id=project_scope_id,
                    reason="project_memory_candidate_approved",
                )
            await session.commit()
            await session.refresh(approved)
            result = self._result(
                approved,
                operation="approved",
                reason=reason or "approved_by_user",
                replaced_id=candidate.id,
            )
        if project_scope_id is not None:
            from .project_context_pack_job_service import enqueue_project_context_pack_rebuild

            try:
                await enqueue_project_context_pack_rebuild(
                    project_scope_id,
                    actor_id,
                    "project_memory_candidate_approved",
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue ProjectContextPack rebuild after memory approval"
                )
        return result

    async def record_correction(
        self,
        *,
        actor_id: str,
        subject: str,
        desired: str,
        evidence: str | None = None,
        utterance: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        clean_subject = str(subject or "").strip()
        clean_desired = str(desired or "").strip()
        if not clean_subject or not clean_desired:
            raise ScopedMemoryValidationError("correction subject and desired value are required")
        utterance_text = str(utterance or "")
        explicit_global = bool(_GLOBAL_CORRECTION_RE.search(utterance_text))
        explicit_task = bool(task_id and _TASK_CORRECTION_RE.search(utterance_text))
        explicit_session = bool(
            session_id and _SESSION_CORRECTION_RE.search(utterance_text)
        )
        if explicit_global:
            # "Every project" is still a preference owned by this user, not
            # a cross-tenant/global fact.
            scope_type, scope_id = "user", str(actor_id)
        elif explicit_session:
            scope_type, scope_id = "session", str(session_id)
        elif explicit_task:
            scope_type, scope_id = "task", str(task_id)
        elif project_id:
            scope_type, scope_id = "project", str(project_id)
        else:
            scope_type, scope_id = "user", str(actor_id)
        correction_key = _digest(_normalized_text(clean_subject))
        key = f"correction:{correction_key}"[:128]
        existing = await self.list_memories(
            actor_id=str(actor_id),
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id if scope_type == "project" else None,
            task_id=task_id if scope_type == "task" else None,
            session_id=session_id if scope_type == "session" else None,
            include_history=True,
        )
        matching = [
            item
            for item in existing
            if (item.get("structured_data") or {}).get("correction_key") == correction_key
            and _normalized_text((item.get("structured_data") or {}).get("desired"))
            == _normalized_text(clean_desired)
            and item.get("status") in {"candidate", "active"}
        ]
        active = next((item for item in matching if item.get("status") == "active"), None)
        if active:
            return {
                "success": True,
                "memory_id": active["id"],
                "scope": active["scope_type"],
                "scope_id": active["scope_id"],
                "operation": "unchanged",
                "replaced_id": None,
                "reason": "correction_already_active",
                "memory": active,
            }
        candidate = next((item for item in matching if item.get("status") == "candidate"), None)
        if candidate:
            return await self.decide_candidate(
                candidate["id"],
                actor_id=str(actor_id),
                approve=True,
                expected_version=int(candidate.get("version") or 1),
                reason="identical_correction_confirmed_twice",
            )
        return await self.upsert_memory(
            actor_id=str(actor_id),
            content=f"{clean_subject}: {clean_desired}",
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id if scope_type == "project" else None,
            task_id=task_id if scope_type == "task" else None,
            session_id=session_id if scope_type == "session" else None,
            memory_type="correction",
            structured_data={
                "correction_key": correction_key,
                "subject": clean_subject,
                "desired": clean_desired,
                "evidence": evidence,
                "explicit_global": explicit_global,
            },
            source_type="correction",
            evidence_refs=[
                {"type": "user_correction", "value": evidence or utterance or clean_desired}
            ],
            dedupe_key=key,
            status="candidate",
            trust_level="verified",
            importance=8,
        )

    async def move_scope(
        self,
        memory_id: str,
        *,
        actor_id: str,
        expected_version: int,
        scope_type: str,
        scope_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        reason: str = "scope_move",
        turn_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        target_scope = _scope(
            actor_id=str(actor_id),
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
        )
        context = _turn_context(turn_context, None)
        context.setdefault("user_id", str(actor_id))
        move_reason = reason or f"moved_to_{target_scope.scope_type}_scope"
        async with await self._new_session() as session:
            source = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if source is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(source),
                actor_id=str(actor_id),
                write=True,
            )
            await self._require_scope_permission(
                session,
                scope=target_scope,
                actor_id=str(actor_id),
                write=True,
            )
            if int(source.version or 1) != int(expected_version):
                raise ScopedMemoryConflict("memory version changed")
            if source.status not in ACTIVE_STATUSES:
                raise ScopedMemoryConflict(
                    f"memory cannot move in status {source.status}"
                )
            if (
                source.scope_type == target_scope.scope_type
                and str(source.scope_id or "") == target_scope.scope_id
            ):
                raise ScopedMemoryValidationError(
                    "target scope must differ from the current scope"
                )

            key = source.dedupe_key or _dedupe_key(
                source.content, source.memory_type
            )
            target_rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.user_id == str(actor_id),
                            ContextMemory.scope_type == target_scope.scope_type,
                            ContextMemory.scope_id == target_scope.scope_id,
                            ContextMemory.dedupe_key == key,
                            ContextMemory.status.in_(tuple(ACTIVE_STATUSES)),
                        )
                        .order_by(ContextMemory.version.desc())
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            source_before = _audit_snapshot(source)
            target_before = [(_audit_snapshot(row), row) for row in target_rows]
            source_status = source.status
            now = datetime.utcnow()
            source.status = "superseded"
            source.updated_at = now
            for row in target_rows:
                row.status = "superseded"
                row.updated_at = now
            await session.flush()

            predecessor = target_rows[0] if target_rows else source
            moved_status = (
                "active"
                if source_status == "active"
                or any(before.get("status") == "active" for before, _ in target_before)
                else "candidate"
            )
            metadata = dict(source.projection_metadata or {})
            metadata.update(
                {
                    "idempotency_key": (
                        f"scope-move:{source.id}:{expected_version}:"
                        f"{target_scope.scope_type}:{target_scope.scope_id}"
                    ),
                    "scope_move": {
                        "source_memory_id": str(source.id),
                        "source_scope": source.scope_type,
                        "target_scope": target_scope.scope_type,
                        "target_scope_id": target_scope.scope_id,
                        "reason": move_reason,
                    },
                    "turn_context": context,
                }
            )
            moved = ContextMemory(
                id=uuid.uuid4(),
                user_id=str(actor_id),
                project_id=target_scope.project_id,
                task_id=target_scope.task_id,
                session_id=target_scope.session_id,
                scope_type=target_scope.scope_type,
                scope_id=target_scope.scope_id,
                memory_type=source.memory_type,
                title=source.title,
                content=source.content,
                structured_data=dict(source.structured_data or {}),
                source_type="scope_move",
                source_ref=f"context_memory:{source.id}",
                confidence=source.confidence,
                importance=source.importance,
                trust_level=source.trust_level,
                sensitivity=source.sensitivity,
                evidence_refs=[
                    *(source.evidence_refs or []),
                    {
                        "type": "scope_move",
                        "memory_id": str(source.id),
                        "reason": move_reason,
                    },
                ],
                evidence_span=dict(source.evidence_span or {}),
                dedupe_key=key,
                supersedes_id=predecessor.id,
                version=max(
                    [int(source.version or 1)]
                    + [int(row.version or 1) for row in target_rows]
                )
                + 1,
                created_by_actor=str(actor_id),
                rejection_reason=source.rejection_reason,
                projection_metadata=metadata,
                migration_id=source.migration_id,
                status=moved_status,
                is_pinned=source.is_pinned,
                expires_at=source.expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(moved)
            await session.flush()
            self._add_audit(
                session,
                memory=moved,
                actor_id=str(actor_id),
                operation="moved",
                before=source_before,
                after=moved,
                turn_context=context,
                reason=move_reason,
            )
            for before, row in target_before:
                self._add_audit(
                    session,
                    memory=row,
                    actor_id=str(actor_id),
                    operation="superseded_by_scope_move",
                    before=before,
                    after=row,
                    turn_context=context,
                    reason=move_reason,
                )
            await session.commit()
            await session.refresh(moved)
            return self._result(
                moved,
                operation="moved",
                reason=move_reason,
                replaced_id=source.id,
            )

    async def retrieve_for_context(
        self,
        *,
        actor_id: str,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        query: str = "",
        limit: int = 20,
        max_chars: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        actor_id = _canonical_actor_id(actor_id)
        project_uuid = _uuid(project_id)
        task_uuid = _uuid(task_id)
        session_uuid = _uuid(session_id)
        conditions = [
            and_(ContextMemory.scope_type == "global", ContextMemory.user_id == str(actor_id)),
            and_(ContextMemory.scope_type == "user", ContextMemory.user_id == str(actor_id)),
        ]
        if project_uuid:
            conditions.append(
                and_(ContextMemory.scope_type == "project", ContextMemory.project_id == project_uuid)
            )
        if task_uuid:
            conditions.append(and_(ContextMemory.scope_type == "task", ContextMemory.task_id == task_uuid))
        if session_uuid:
            conditions.append(
                and_(ContextMemory.scope_type == "session", ContextMemory.session_id == session_uuid)
            )
        async with await self._new_session() as session:
            if project_uuid:
                await self._require_project_permission(
                    session,
                    project_id=project_uuid,
                    actor_id=str(actor_id),
                    write=False,
                )
            if task_uuid:
                await self._require_scope_permission(
                    session,
                    scope=_scope(
                        actor_id=str(actor_id),
                        scope_type="task",
                        task_id=str(task_uuid),
                    ),
                    actor_id=str(actor_id),
                    write=False,
                )
            if session_uuid:
                await self._require_scope_permission(
                    session,
                    scope=_scope(
                        actor_id=str(actor_id),
                        scope_type="session",
                        session_id=str(session_uuid),
                    ),
                    actor_id=str(actor_id),
                    write=False,
                )
            rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.status == "active",
                            or_(ContextMemory.expires_at.is_(None), ContextMemory.expires_at > datetime.utcnow()),
                            or_(*conditions),
                        )
                        .order_by(ContextMemory.updated_at.desc())
                        .limit(max(200, int(limit) * 20))
                    )
                )
                .scalars()
                .all()
            )

        terms = _keywords(query)
        now = datetime.utcnow()
        scored: list[tuple[float, ContextMemory, str]] = []
        trace: list[dict[str, Any]] = []
        for row in rows:
            row_terms = _keywords(f"{row.title or ''}\n{row.content or ''}")
            keyword_overlap = len(terms & row_terms) / max(1, len(terms)) if terms else 0.0
            scope_score = SCOPE_PRIORITY.get(row.scope_type, 0) / 5
            importance_score = max(0, min(int(row.importance or 0), 10)) / 10
            trust_score = {"verified": 1.0, "trusted": 0.9, "inferred": 0.6, "unverified": 0.3}.get(
                str(row.trust_level or "inferred"), 0.5
            )
            age_days = max(0.0, (now - (row.updated_at or row.created_at or now)).total_seconds() / 86400)
            recency_score = 1.0 / (1.0 + age_days / 30)
            score = (
                keyword_overlap * 0.28
                + scope_score * 0.25
                + importance_score * 0.18
                + trust_score * 0.12
                + recency_score * 0.07
                + (0.10 if row.is_pinned else 0.0)
            )
            reason = (
                "pinned"
                if row.is_pinned
                else "keyword_and_scope_match"
                if keyword_overlap
                else "scope_priority"
            )
            scored.append((score, row, reason))
        scored.sort(key=lambda item: (item[0], item[1].importance), reverse=True)
        selected: list[dict[str, Any]] = []
        used_chars = 0
        for score, row, reason in scored:
            data = row.to_dict()
            estimated_chars = len(str(data.get("content") or "")) + 64
            included = len(selected) < int(limit) and (
                max_chars is None or used_chars + estimated_chars <= max_chars
            )
            trace.append(
                {
                    "memory_id": str(row.id),
                    "scope": row.scope_type,
                    "reason": reason if included else "context_budget_exceeded",
                    "score": round(score, 4),
                    "source": row.source_type,
                    "excluded": not included,
                    "cost_chars": estimated_chars,
                }
            )
            if not included:
                continue
            data["retrieval_score"] = round(score, 4)
            data["selection_reason"] = reason
            selected.append(data)
            used_chars += estimated_chars
        return selected, trace

    async def search(
        self,
        *,
        actor_id: str,
        query: str,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        actor_id = _canonical_actor_id(actor_id)
        selected, _ = await self.retrieve_for_context(
            actor_id=str(actor_id),
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            query=query,
            limit=limit,
        )
        return selected

    async def explain(self, memory_id: str, *, actor_id: str) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            memory = await session.get(ContextMemory, _uuid(memory_id))
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(memory),
                actor_id=str(actor_id),
                write=False,
            )
            ancestors: list[dict[str, Any]] = []
            current = memory
            seen: set[uuid.UUID] = set()
            while current.supersedes_id and current.supersedes_id not in seen:
                seen.add(current.supersedes_id)
                current = await session.get(ContextMemory, current.supersedes_id)
                if current is None:
                    break
                ancestors.append(current.to_dict())
            descendants = list(
                (
                    await session.execute(
                        select(ContextMemory).where(ContextMemory.supersedes_id == memory.id)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "memory": memory.to_dict(),
                "lineage": {
                    "ancestors": ancestors,
                    "descendants": [item.to_dict() for item in descendants],
                },
                "explanation": {
                    "scope_priority": SCOPE_PRIORITY.get(memory.scope_type, 0),
                    "trust_level": memory.trust_level,
                    "source_type": memory.source_type,
                    "evidence_refs": memory.evidence_refs or [],
                    "dedupe_key": memory.dedupe_key,
                },
            }

    async def promote_to_project_information(
        self,
        memory_id: str,
        *,
        actor_id: str,
        expected_version: int,
        target_section: str | None = None,
        source_refs: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Explicitly materialize one memory as an editable Docs block.

        Scoped Memory provenance is retained in the child block's
        ``body_json.clip_ingest`` metadata.  The project information topic is
        only a container; it never receives a ``verbatim_blocks`` payload.
        """
        actor_id = _canonical_actor_id(actor_id)
        from ..memory.models import KnowledgeNode
        from .clip_ingest_service import ClipIngestService
        from .docs_graph_service import DocsGraphService

        context = _turn_context(None, None)
        context.setdefault("user_id", str(actor_id))
        async with await self._new_session() as session:
            memory = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            if memory.project_id is None:
                raise ScopedMemoryValidationError(
                    "promotion requires a project-scoped memory"
                )
            await self._require_project_permission(
                session,
                project_id=memory.project_id,
                actor_id=str(actor_id),
                write=True,
            )
            if int(memory.version or 1) != int(expected_version):
                raise ScopedMemoryConflict("memory version changed")
            project = await session.get(Project, memory.project_id)
            node = (
                await session.get(KnowledgeNode, project.knowledge_node_id)
                if project and project.knowledge_node_id
                else None
            )
            if node is None or node.archived_at is not None:
                raise ScopedMemoryNotFound("canonical Project Information page not found")

            content = str(memory.content or "").strip()
            block = ClipIngestService._make_verbatim_block(
                source_id=str(memory.id),
                source={
                    "source_type": "scoped_memory",
                    "url": f"memory://{memory.id}",
                },
                start_line=1,
                end_line=max(1, content.count("\n") + 1),
                kind="quote",
                label=target_section or memory.title or "Scoped Memory",
                content=content,
            )
            promotion_source_refs = [
                {
                    "type": "context_memory",
                    "memory_id": str(memory.id),
                    "version": memory.version,
                    "scope": memory.scope_type,
                },
                *[
                    dict(item)
                    for item in source_refs or []
                    if isinstance(item, dict)
                ],
            ]

            # A previous implementation stored the block on the topic.  Do
            # not use that legacy key as the user-visible source of truth, but
            # recognize an identical typed child for retry idempotence.  The
            # query is best-effort for lightweight adapters; the real
            # DocsGraphService write below remains authoritative.
            existing_children: list[KnowledgeNode] = []
            try:
                children_result = await session.execute(
                    select(KnowledgeNode).where(
                        KnowledgeNode.parent_id == node.id,
                        KnowledgeNode.docs_library_id == node.docs_library_id,
                        KnowledgeNode.archived_at.is_(None),
                    )
                )
                existing_children = [
                    child
                    for child in children_result.scalars().all()
                    if getattr(child, "parent_id", None) == node.id
                    and getattr(child, "archived_at", None) is None
                ]
            except Exception:
                existing_children = []
            for child in existing_children:
                child_body = getattr(child, "body_json", None)
                child_metadata = (
                    child_body.get("clip_ingest")
                    if isinstance(child_body, dict)
                    else None
                )
                if (
                    isinstance(child_body, dict)
                    and child_body.get("format") == "doc_block"
                    and isinstance(child_metadata, dict)
                    and child_metadata.get("source_id") == str(memory.id)
                    and child_metadata.get("sha256") == block["sha256"]
                    and child_body.get("content") == content
                ):
                    return {
                        "success": True,
                        "memory_id": str(memory.id),
                        "scope": memory.scope_type,
                        "operation": "unchanged",
                        "replaced_id": None,
                        "reason": "already_promoted",
                        "project_information_node_id": str(node.id),
                        "project_information_block_node_id": str(child.id),
                    }

            body_json = ClipIngestService._typed_block_body_json(block)
            block_label = body_json["label"]
            block_node = await DocsGraphService(session).create_node(
                docs_library_id=node.docs_library_id,
                user_id=_uuid(actor_id),
                title=block_label,
                parent=node,
                project_id=node.project_id,
                body_json=body_json,
                source_refs=promotion_source_refs,
            )
            before = _audit_snapshot(memory)
            metadata = dict(memory.projection_metadata or {})
            metadata["project_information_projection"] = {
                "node_id": str(node.id),
                "block_node_id": str(block_node.id),
                "content_sha256": block["sha256"],
                "promoted_at": datetime.utcnow().isoformat(),
                "promoted_by": str(actor_id),
                "target_section": target_section,
                "source_refs": promotion_source_refs,
            }
            memory.projection_metadata = metadata
            memory.version = int(memory.version or 1) + 1
            memory.updated_at = datetime.utcnow()
            self._add_audit(
                session,
                memory=memory,
                actor_id=str(actor_id),
                operation="promoted_to_project_information",
                before=before,
                after=memory,
                turn_context=context,
                reason="explicit_user_promotion",
            )
            from .project_context_pack_service import invalidate_project_context_pack

            await invalidate_project_context_pack(
                session=session,
                project_id=memory.project_id,
                reason="project_memory_promoted_to_docs",
            )
            await session.commit()
            result = {
                "success": True,
                "memory_id": str(memory.id),
                "scope": memory.scope_type,
                "operation": "promoted",
                "replaced_id": None,
                "reason": "explicit_user_promotion",
                "project_information_node_id": str(node.id),
                "project_information_block_node_id": str(block_node.id),
            }
        from .project_context_pack_job_service import enqueue_project_context_pack_rebuild

        try:
            await enqueue_project_context_pack_rebuild(
                memory.project_id,
                actor_id,
                "project_memory_promoted_to_docs",
            )
        except Exception:
            logger.exception(
                "Failed to enqueue ProjectContextPack rebuild after memory promotion"
            )
        return result

    @staticmethod
    def render_memories_for_prompt(memories: list[dict[str, Any]]) -> str:
        if not memories:
            return ""
        labels = {
            "global": "Global Memory",
            "user": "User Memory",
            "project": "Project Memory",
            "task": "Task Memory",
            "session": "Session Memory",
        }
        lines: list[str] = []
        for scope_type in ("user", "project", "task", "session", "global"):
            scoped = [item for item in memories if item.get("scope_type") == scope_type]
            if not scoped:
                continue
            lines.append(f"## {labels[scope_type]}")
            for item in scoped:
                title = f"{item.get('title')}: " if item.get("title") else ""
                lines.append(
                    f"- {title}{item.get('content')} "
                    f"[trust={item.get('trust_level')}, reason={item.get('selection_reason')}]"
                )
        return "\n".join(lines)


def parse_correction_utterance(text: str) -> dict[str, Any] | None:
    """Best-effort Japanese/English correction parsing for tool prefill."""
    raw = str(text or "").strip()
    marker = _CORRECTION_MARKER_RE.search(raw)
    if not marker:
        return None
    before = raw[: marker.start()].strip(" 、。,:：")
    after = raw[marker.end() :].strip(" 、。,:：")
    if not after:
        after = "直前の回答を採用せず、会話中の最新のユーザー指示を優先する"
    return {
        "subject": before or "ユーザー指定事項",
        "desired": after,
        "explicit_global": bool(_GLOBAL_CORRECTION_RE.search(raw)),
    }


__all__ = [
    "MemoryScope",
    "ScopedMemoryConflict",
    "ScopedMemoryError",
    "ScopedMemoryNotFound",
    "ScopedMemoryPermissionDenied",
    "ScopedMemoryService",
    "ScopedMemoryValidationError",
    "classify_sensitivity",
    "parse_correction_utterance",
]
