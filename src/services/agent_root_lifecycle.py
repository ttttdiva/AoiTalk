"""Lifecycle guards for the owner-scoped AoiDots Agent root.

The AoiDots root is an ordinary ``Agent``/``ConversationSession`` pair, but
the pair is identity-bearing.  This module keeps that distinction at the
conversation boundary: callers can use the normal chat and message APIs,
while operations that would detach or repurpose the canonical root fail
closed from the durable ``AgentPrivateRoot`` row.

The client projection is deliberately advisory.  A request may omit or forge
it; every mutation guard resolves the row again from the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError

from ..memory.models import AgentPrivateRoot, ConversationSession


AOIDOTS_PURPOSE = "aoidots"
ROOT_METADATA_ATTR = "_private_agent_root_metadata"

# These fields change the identity/scope of the ordinary session that the
# AgentPrivateRoot points at.  Title and history presentation fields remain
# normal conversation mutations.
ROOT_PROTECTED_SESSION_FIELDS = frozenset(
    {
        "user_id",
        "project_id",
        "agent_id",
        "character_name",
        "is_active",
        "deleted_at",
        "is_group_chat",
        "group_character_names",
        "app_id",
        "app_target_id",
        "development_status",
    }
)


class AgentRootLifecycleError(RuntimeError):
    """Base error with a stable API-facing status and code."""

    status_code = 409
    code = "private_agent_root_protected"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.code = str(code or self.code)
        super().__init__(message or self.code)


class AgentRootOwnerError(AgentRootLifecycleError):
    """The actor is not the human owner of the canonical root."""

    status_code = 403
    code = "private_agent_root_owner_only"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        super().__init__(
            message or "AoiDotsの基幹セッションは所有者本人だけが変更できます",
            code=code,
        )


class AgentRootBindingError(AgentRootLifecycleError):
    """The durable root row and ordinary session no longer agree."""

    code = "private_agent_root_binding_invalid"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        super().__init__(
            message or "AoiDotsの基幹セッションの永続紐付けが不正です",
            code=code,
        )


class AgentRootProtectionError(AgentRootLifecycleError):
    """A mutation would detach or repurpose the canonical root."""

    code = "private_agent_root_protected"


@dataclass(frozen=True)
class AgentPrivateRootMetadata:
    """Safe read projection for a canonical private Agent root."""

    root_id: str
    owner_user_id: str
    purpose: str
    agent_id: str
    session_id: str
    state: str
    readonly_binding: bool = True

    def to_dict(self) -> dict[str, Any]:
        # The three short keys are part of the Web/Mobile read contract.  The
        # longer identifiers are useful for diagnostics and do not authorize
        # a client-side mutation.
        return {
            "private_agent_root": True,
            "purpose": self.purpose,
            "readonly_binding": bool(self.readonly_binding),
            "root_id": self.root_id,
            "owner_user_id": self.owner_user_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "state": self.state,
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _same_uuidish(left: Any, right: Any) -> bool:
    return _text(left).casefold() == _text(right).casefold()


def _missing_schema_table(exc: BaseException, *table_names: str) -> bool:
    """Recognize old test/installation schemas without hiding other failures."""

    detail = str(exc).casefold()
    return any(str(table).casefold() in detail for table in table_names) and (
        "no such table" in detail
        or "does not exist" in detail
        or "undefined table" in detail
    )


def _missing_root_table(exc: BaseException) -> bool:
    return _missing_schema_table(exc, "agent_private_roots")


async def _clear_missing_root_table_error(db_session: Any) -> None:
    """Clear an aborted PostgreSQL transaction without expiring SQLite rows."""

    # SQLite's aiosqlite adapter leaves the loaded ORM objects usable after an
    # undefined-table read.  Calling rollback there expires those objects and
    # causes an unexpected async lazy load in callers.  PostgreSQL does need a
    # rollback after an undefined relation error.
    bind = getattr(db_session, "bind", None)
    dialect_name = str(getattr(getattr(bind, "dialect", None), "name", ""))
    if dialect_name.casefold() == "sqlite":
        return
    rollback = getattr(db_session, "rollback", None)
    if callable(rollback):
        result = rollback()
        if hasattr(result, "__await__"):
            await result


def _session_row(value: Any) -> Any | None:
    if isinstance(value, ConversationSession):
        return value
    # Keep the helper usable by repository fakes and lightweight unit tests.
    return value if hasattr(value, "id") and hasattr(value, "user_id") else None


def build_private_root_metadata(
    root: AgentPrivateRoot,
    conversation: Any | None = None,
) -> AgentPrivateRootMetadata:
    """Validate and project one root row against its ordinary session."""

    row = _session_row(conversation)
    if row is not None:
        if not _same_uuidish(root.session_id, row.id):
            raise AgentRootBindingError("private Agent root session binding is invalid")
        if not _same_uuidish(root.owner_user_id, row.user_id):
            raise AgentRootBindingError("private Agent root owner binding is invalid")
        if row.project_id is not None:
            raise AgentRootBindingError("private Agent root must remain projectless")
        if row.agent_id is None or not _same_uuidish(root.agent_id, row.agent_id):
            raise AgentRootBindingError("private Agent root Agent binding is invalid")
    purpose = _text(getattr(root, "purpose", ""))
    if purpose != AOIDOTS_PURPOSE:
        raise AgentRootBindingError("private Agent root purpose is invalid")
    return AgentPrivateRootMetadata(
        root_id=str(root.id),
        owner_user_id=str(root.owner_user_id),
        purpose=purpose,
        agent_id=str(root.agent_id),
        session_id=str(root.session_id),
        state=_text(getattr(root, "state", "active")) or "active",
    )


async def find_private_agent_root(
    db_session: Any,
    conversation_or_session_id: Any,
    *,
    owner_user_id: Any | None = None,
    lock: bool = False,
) -> AgentPrivateRootMetadata | None:
    """Resolve root metadata from the durable row, if one exists.

    A missing table is only tolerated for legacy/SQLite fixtures where the
    feature has not been migrated yet.  Other database errors propagate so a
    transient failure cannot turn a protected root into an ordinary session.
    """

    row = _session_row(conversation_or_session_id)
    session_id = getattr(row, "id", conversation_or_session_id)
    try:
        session_uuid = UUID(str(session_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AgentRootBindingError("conversation session id is invalid") from exc
    statement = select(AgentPrivateRoot).where(
        AgentPrivateRoot.session_id == session_uuid,
        AgentPrivateRoot.purpose == AOIDOTS_PURPOSE,
    )
    if lock:
        statement = statement.with_for_update()
    try:
        root = await db_session.scalar(statement)
    except (OperationalError, ProgrammingError) as exc:
        if _missing_root_table(exc):
            # PostgreSQL transactions are aborted after an undefined-table
            # error; clear it before the caller issues its normal query.
            await _clear_missing_root_table_error(db_session)
            return None
        raise
    # Lightweight repository fakes may return their only session row for an
    # unrecognised SELECT; never interpret that ordinary row as a root.
    if root is None or not all(
        hasattr(root, field)
        for field in ("session_id", "owner_user_id", "purpose", "agent_id")
    ):
        return None
    metadata = build_private_root_metadata(root, row)
    if owner_user_id is not None and not _same_uuidish(metadata.owner_user_id, owner_user_id):
        raise AgentRootOwnerError("private Agent root owner access is required")
    if row is not None:
        setattr(row, ROOT_METADATA_ATTR, metadata.to_dict())
    return metadata


async def guard_private_root_operation(
    db_session: Any,
    conversation: Any,
    *,
    operation: str,
    actor_user_id: Any | None = None,
    requested_agent_id: Any | None = None,
    lock: bool = False,
) -> AgentPrivateRootMetadata | None:
    """Guard one mutation and return root metadata for a root session.

    ``agent_binding`` is idempotent for the already-canonical Agent.  This
    keeps ordinary human-to-Agent chat selection stable when a client repeats
    the current binding after opening a session; detach and replacement are
    still rejected.
    """

    metadata = await find_private_agent_root(
        db_session,
        conversation,
        owner_user_id=actor_user_id,
        lock=lock,
    )
    if metadata is None:
        return None
    if metadata.state != "active":
        raise AgentRootProtectionError(
            "AoiDotsの基幹セッションは削除済みのため変更できません",
            code="private_agent_root_tombstoned",
        )
    normalized_operation = _text(operation).casefold()
    if normalized_operation == "agent_binding":
        if requested_agent_id is not None and _same_uuidish(
            requested_agent_id, metadata.agent_id
        ):
            return metadata
    raise AgentRootProtectionError(
        f"AoiDotsの基幹セッションでは{normalized_operation or 'この'}変更はできません"
    )


async def guard_private_root_update(
    db_session: Any,
    conversation: Any,
    *,
    fields: Iterable[str] | Mapping[str, Any],
    actor_user_id: Any | None = None,
    lock: bool = False,
) -> AgentPrivateRootMetadata | None:
    """Reject structural session updates while allowing normal title edits.

    Callers that have the proposed values may pass a mapping instead of only
    field names.  Re-sending the canonical ``user_id`` is an idempotent
    no-op; a different owner remains a protected structural mutation.
    """

    proposed = fields if isinstance(fields, Mapping) else None
    changed = {str(field) for field in (proposed or fields)}
    if (
        proposed is not None
        and "user_id" in changed
        and _same_uuidish(
            proposed.get("user_id"),
            getattr(conversation, "user_id", None),
        )
    ):
        changed.discard("user_id")
    protected = changed.intersection(ROOT_PROTECTED_SESSION_FIELDS)
    if not protected:
        return await find_private_agent_root(
            db_session,
            conversation,
            owner_user_id=actor_user_id,
            lock=lock,
        )
    metadata = await find_private_agent_root(
        db_session,
        conversation,
        owner_user_id=actor_user_id,
        lock=lock,
    )
    if metadata is None:
        return None
    if metadata.state != "active":
        raise AgentRootProtectionError(
            "AoiDotsの基幹セッションは削除済みのため変更できません",
            code="private_agent_root_tombstoned",
        )
    raise AgentRootProtectionError(
        "AoiDotsの基幹セッションのProject・Agent・Character紐付けは変更できません"
    )


async def attach_private_root_metadata(
    db_session: Any,
    rows: Iterable[Any],
) -> None:
    """Attach read-only root metadata to loaded session objects in-place."""

    materialized = [row for row in rows if _session_row(row) is not None]
    if not materialized:
        return
    session_ids = []
    for row in materialized:
        try:
            session_ids.append(UUID(str(row.id)))
        except (TypeError, ValueError, AttributeError):
            continue
    if not session_ids:
        return
    statement = select(AgentPrivateRoot).where(
        AgentPrivateRoot.session_id.in_(session_ids),
        AgentPrivateRoot.purpose == AOIDOTS_PURPOSE,
    )
    try:
        roots = (await db_session.execute(statement)).scalars().all()
    except (OperationalError, ProgrammingError) as exc:
        if _missing_root_table(exc):
            await _clear_missing_root_table_error(db_session)
            return
        raise
    by_session: dict[str, AgentPrivateRoot] = {
        str(root.session_id): root for root in roots
    }
    for row in materialized:
        root = by_session.get(str(row.id))
        if root is None:
            continue
        metadata = build_private_root_metadata(root, row)
        setattr(row, ROOT_METADATA_ATTR, metadata.to_dict())


async def fence_child_dialogues_for_binding_change(
    db_session: Any,
    worker_session: Any,
    *,
    actor_user_id: Any,
    current_binding: Mapping[str, Any] | None = None,
    project_id: Any | None = None,
    character_name: Any | None = None,
) -> list[dict[str, Any]]:
    """Fence immutable child-dialogue identity after an explicit human edit.

    A worker session may be opened and read without changing its dialogue.  If
    the human explicitly changes its Agent, Project, or Character binding,
    the durable dialogue identity remains historical while its control fence
    advances, pending inputs/deliveries are invalidated, and the dialogue is
    blocked with a safe root notice.  The caller owns the surrounding
    transaction and commits this work together with the session mutation.
    """

    row = _session_row(worker_session)
    if row is None:
        return []
    # Agent/service callers are the only path allowed to invoke this helper;
    # keep the actor required so a forged client marker cannot fence another
    # owner's work.
    actor = _text(actor_user_id)
    if not actor:
        raise AgentRootOwnerError("明示的な人間ユーザーが必要です")
    from ..memory.models import (
        AgentDialogue,
        AgentDialogueDelivery,
        AgentDialogueInput,
        AgentExecutionBinding,
        ConversationMessage,
    )

    try:
        dialogues = list(
            (
                await db_session.execute(
                    select(AgentDialogue)
                    .where(AgentDialogue.worker_session_id == row.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
    except (OperationalError, ProgrammingError) as exc:
        if _missing_schema_table(
            exc,
            "agent_dialogues",
            "agent_dialogue_deliveries",
            "agent_dialogue_inputs",
        ):
            await _clear_missing_root_table_error(db_session)
            return []
        raise
    if not dialogues:
        return []

    validated_root_sessions: dict[str, Any] = {}
    for dialogue in dialogues:
        if not _same_uuidish(dialogue.owner_user_id, actor):
            raise AgentRootOwnerError(
                "child dialogue owner does not match the acting user"
            )
        root_session = await db_session.get(ConversationSession, dialogue.root_session_id)
        worker_row = await db_session.get(ConversationSession, dialogue.worker_session_id)
        if root_session is None or worker_row is None:
            raise AgentRootBindingError("child dialogue session binding is unavailable")
        if not _same_uuidish(root_session.user_id, actor) or not _same_uuidish(
            worker_row.user_id, actor
        ):
            raise AgentRootOwnerError(
                "child dialogue session owner does not match the acting user"
            )
        root_metadata = await find_private_agent_root(
            db_session,
            root_session,
            owner_user_id=actor,
        )
        if root_metadata is None:
            raise AgentRootBindingError(
                "child dialogue canonical Root mapping is unavailable"
            )
        if not _same_uuidish(dialogue.root_agent_id, root_metadata.agent_id):
            raise AgentRootBindingError(
                "child dialogue root Agent binding does not match canonical Root"
            )
        validated_root_sessions[str(dialogue.id)] = root_session

    binding = dict(current_binding or {})
    if "agent_id" not in binding:
        binding["agent_id"] = str(getattr(row, "agent_id", "")) or None
    if "agent_revision_id" not in binding and binding.get("agent_id") is not None and getattr(row, "id", None) is not None:
        latest_binding = (
            await db_session.execute(
                select(AgentExecutionBinding)
                .where(AgentExecutionBinding.session_id == row.id)
                .order_by(AgentExecutionBinding.created_at.desc(), AgentExecutionBinding.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        binding["agent_revision_id"] = (
            str(latest_binding.agent_revision_id) if latest_binding is not None else None
        )
    if "project_id" not in binding:
        binding["project_id"] = str(project_id) if project_id else None
    if "character_name" not in binding:
        binding["character_name"] = str(character_name or getattr(row, "character_name", ""))

    now = datetime.utcnow()
    notices: list[dict[str, Any]] = []
    for dialogue in dialogues:
        # A dialogue row is historical identity.  Never assign the new Agent,
        # revision, Project, or worker session into it.
        old_revision = int(dialogue.control_revision or 0)
        new_revision = old_revision + 1
        pending_inputs = list(
            (
                await db_session.execute(
                    select(AgentDialogueInput)
                    .where(
                        AgentDialogueInput.dialogue_id == dialogue.id,
                        AgentDialogueInput.state == "pending",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        pending_deliveries = list(
            (
                await db_session.execute(
                    select(AgentDialogueDelivery)
                    .where(
                        AgentDialogueDelivery.dialogue_id == dialogue.id,
                        AgentDialogueDelivery.state.in_(("pending", "available", "leased")),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        invalidated_inputs: list[str] = []
        for input_row in pending_inputs:
            input_row.state = "invalidated"
            input_row.updated_at = now
            invalidated_inputs.append(str(input_row.id))
        cancelled_deliveries: list[str] = []
        for delivery in pending_deliveries:
            delivery.state = "cancelled"
            delivery.updated_at = now
            cancelled_deliveries.append(str(delivery.id))
        dialogue.control_revision = new_revision
        dialogue.state = "blocked"
        dialogue.updated_at = now

        root_notice_id: str | None = None
        root_session = validated_root_sessions[str(dialogue.id)]
        if root_session is not None and root_session.deleted_at is None:
            notice_metadata = {
                "agent_dialogue_id": str(dialogue.id),
                "notice_code": "worker_binding_changed",
                "blocked_reason": "worker_binding_changed",
                "control_revision": new_revision,
                "current_binding": dict(binding),
                "successor_handoff_required": True,
                "source_worker_session_id": str(row.id),
                "actor_user_id": actor,
            }
            notice = ConversationMessage(
                id=uuid4(),
                session_id=root_session.id,
                role="assistant",
                content=(
                    "担当セッションの紐付けが変更されたため、既存の指示を停止しました。"
                    "現在のAgent設定を確認してから、新しい担当を明示してください。"
                ),
                sender_type="agent",
                sender_id=str(dialogue.root_agent_id),
                sender_display_name="AoiDots",
                message_metadata=notice_metadata,
                created_at=now,
                updated_at=now,
            )
            db_session.add(notice)
            root_session.message_count = int(root_session.message_count or 0) + 1
            root_notice_id = str(notice.id)
        notices.append(
            {
                "dialogue_id": str(dialogue.id),
                "root_session_id": str(dialogue.root_session_id),
                "worker_session_id": str(dialogue.worker_session_id),
                "control_revision": new_revision,
                "state": "blocked",
                "blocked_reason": "worker_binding_changed",
                "invalidated_input_ids": invalidated_inputs,
                "cancelled_delivery_ids": cancelled_deliveries,
                "current_binding": dict(binding),
                "successor_handoff_required": True,
                "root_notice_message_id": root_notice_id,
            }
        )
    await db_session.flush()
    return notices


def project_private_root_metadata(payload: Mapping[str, Any], metadata: Any | None) -> dict[str, Any]:
    """Add the stable root marker to a normal session payload."""

    result = dict(payload)
    if metadata is None:
        return result
    if isinstance(metadata, AgentPrivateRootMetadata):
        metadata = metadata.to_dict()
    if not isinstance(metadata, Mapping):
        return result
    root_projection = dict(metadata)
    root_projection.setdefault("private_agent_root", True)
    root_projection.setdefault("purpose", AOIDOTS_PURPOSE)
    root_projection.setdefault("readonly_binding", True)
    # Keep the marker flat for Web/Mobile clients that consume ordinary
    # ConversationSession DTOs, while preserving a grouped copy for clients
    # that treat this as session metadata.
    result["private_agent_root"] = True
    result["purpose"] = root_projection.get("purpose")
    result["readonly_binding"] = bool(root_projection.get("readonly_binding", True))
    result["private_agent_root_metadata"] = root_projection
    return result


class AgentRootLifecycleService:
    """Small session-bound facade for callers that prefer a service object."""

    def __init__(self, db_session: Any) -> None:
        self.db_session = db_session

    async def read_metadata(
        self,
        conversation_or_session_id: Any,
        *,
        owner_user_id: Any | None = None,
        lock: bool = False,
    ) -> AgentPrivateRootMetadata | None:
        return await find_private_agent_root(
            self.db_session,
            conversation_or_session_id,
            owner_user_id=owner_user_id,
            lock=lock,
        )

    async def guard_operation(
        self,
        conversation: Any,
        *,
        operation: str,
        actor_user_id: Any | None = None,
        requested_agent_id: Any | None = None,
        lock: bool = False,
    ) -> AgentPrivateRootMetadata | None:
        return await guard_private_root_operation(
            self.db_session,
            conversation,
            operation=operation,
            actor_user_id=actor_user_id,
            requested_agent_id=requested_agent_id,
            lock=lock,
        )

    async def guard_update(
        self,
        conversation: Any,
        *,
        fields: Iterable[str],
        actor_user_id: Any | None = None,
        lock: bool = False,
    ) -> AgentPrivateRootMetadata | None:
        return await guard_private_root_update(
            self.db_session,
            conversation,
            fields=fields,
            actor_user_id=actor_user_id,
            lock=lock,
        )


__all__ = [
    "AOIDOTS_PURPOSE",
    "AgentRootLifecycleService",
    "AgentPrivateRootMetadata",
    "AgentRootBindingError",
    "AgentRootLifecycleError",
    "AgentRootOwnerError",
    "AgentRootProtectionError",
    "ROOT_METADATA_ATTR",
    "ROOT_PROTECTED_SESSION_FIELDS",
    "attach_private_root_metadata",
    "build_private_root_metadata",
    "find_private_agent_root",
    "fence_child_dialogues_for_binding_change",
    "guard_private_root_operation",
    "guard_private_root_update",
    "project_private_root_metadata",
]
